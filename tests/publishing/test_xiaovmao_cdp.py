"""XiaoVmaoPublishAdapter (CDP 驱动小V猫) unit tests.

These exercise adapter selection, the 4-platform constant guard, and — with a
mocked CDP driver (no live 小V猫 / no real accounts) — the honest-result contract:
the adapter only reports success after the 小V猫 bridge accepts a PublishLog task
and a matching task record reaches a success/scheduled status.
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.core import contracts as c
from packages.core.contracts import PlatformAccount
from packages.publishing.connectors import xiaovmao_cdp as cdp
from packages.publishing.platform_adapter import (
    XIAOVMAO_ADAPTER_ID,
    XIAOVMAO_PLATFORM_KEY_MAP,
    XIAOVMAO_PLATFORM_NAME_MAP,
    PublishPayload,
    XiaoVmaoPublishAdapter,
    select_adapter,
)

_LOGGED_IN = PlatformAccount(uid="acct-douyin", platform="douyin", is_login=True)


def _install_fake_cdp(
    monkeypatch,
    *,
    accounts,
    create_ok=True,
    publish_statuses=(2,),
    publish_error=None,
    connect_error=None,
):
    """Patch the CDP connector with a fake driver + account reader. Returns a
    ``recorded`` dict capturing the bridge calls _drive_publish makes."""
    recorded: dict = {
        "created_tasks": [],
        "evals": 0,
        "started": 0,
        "queries": 0,
        "connected": False,
        "closed": False,
    }
    statuses = list(publish_statuses)
    monkeypatch.setattr(cdp, "PUBLISH_TASK_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(cdp, "PUBLISH_TASK_TIMEOUT_SECONDS", 1.0)

    class FakeDriver:
        def __init__(self, *, host, port, auto_launch=False):
            self.host, self.port = host, port
            self.auto_launch = auto_launch
            self.local_id = "l_fake"

        async def connect(self, timeout_seconds: int = 30):
            if connect_error is not None:
                raise connect_error
            recorded["connected"] = True

        async def close(self):
            recorded["closed"] = True

        async def evaluate(self, expression, await_promise: bool = False):
            recorded["evals"] += 1
            if "Models.PublishLog.bulkCreateWithStat" in expression:
                if not create_ok:
                    return {"ok": False, "error": "bulk create failed"}
                match = re.search(r'"localId":\s*"([^"]+)"', expression)
                self.local_id = match.group(1) if match else self.local_id
                recorded["created_tasks"].append(expression)
                return {"ok": True, "value": [{"localId": self.local_id}]}
            if "PublishController.startPublishTask" in expression:
                recorded["started"] += 1
                return {"ok": True, "value": True}
            if "Models.PublishLog.queryAll" in expression:
                recorded["queries"] += 1
                index = min(recorded["queries"] - 1, len(statuses) - 1)
                status = statuses[index]
                return {
                    "ok": True,
                    "value": {
                        "list": [
                            {
                                "localId": self.local_id,
                                "status": status,
                                "error": publish_error if status == 4 else None,
                                "postUrl": "https://example.invalid/post/1" if status == 2 else None,
                            }
                        ]
                    },
                }
            return {"ok": True}

    async def fake_read(driver):
        return list(accounts)

    monkeypatch.setattr(cdp, "XiaoVmaoDriver", FakeDriver)
    monkeypatch.setattr(cdp, "_read_accounts", fake_read)
    return recorded


def test_select_xiaovmao_adapter(monkeypatch):
    monkeypatch.setenv("CUTAGENT_PUBLISH_ADAPTER", "xiaovmao.cdp")
    adapter = select_adapter()
    assert isinstance(adapter, XiaoVmaoPublishAdapter)
    assert adapter.adapter_id == XIAOVMAO_ADAPTER_ID == "xiaovmao.cdp"


def test_xiaovmao_platform_maps_cover_four_platforms_no_bilibili():
    four = {"douyin", "kuaishou", "shipinhao", "xiaohongshu"}
    assert set(XIAOVMAO_PLATFORM_KEY_MAP) == four
    assert set(XIAOVMAO_PLATFORM_NAME_MAP) == four
    assert "bilibili" not in XIAOVMAO_PLATFORM_KEY_MAP


def test_publish_unavailable_returns_honest_failure(monkeypatch):
    # 小V猫不可达（connect 抛错）→ 显式失败，绝不伪造成功。
    _install_fake_cdp(
        monkeypatch,
        accounts=[_LOGGED_IN],
        connect_error=cdp.XiaoVmaoUnavailableError("小V猫未运行"),
    )
    outcome = XiaoVmaoPublishAdapter().publish(
        PublishPayload(title="t", platforms=("douyin",), video_uri="/v.mp4")
    )
    assert outcome.success is False
    assert outcome.adapter_id == "xiaovmao.cdp"
    assert outcome.error_message


def test_probe_accounts_unavailable_returns_reason(monkeypatch):
    _install_fake_cdp(
        monkeypatch,
        accounts=[],
        connect_error=cdp.XiaoVmaoUnavailableError("小V猫未运行"),
    )
    accounts, available, reason = XiaoVmaoPublishAdapter().probe_accounts()
    assert accounts == []
    assert available is False
    assert reason


def test_publish_creates_xiaovmao_task_and_waits_for_success(monkeypatch):
    # 账号匹配 + CatBridge 任务创建 + PublishLog 成功状态 → 报告真实成功。
    recorded = _install_fake_cdp(monkeypatch, accounts=[_LOGGED_IN], publish_statuses=(1, 2))
    outcome = XiaoVmaoPublishAdapter().publish(
        PublishPayload(
            title="标题",
            description="正文",
            platforms=("douyin",),
            tags=("话题a",),
            video_uri="/local/finished.mp4",
        )
    )
    assert recorded["created_tasks"]
    assert recorded["started"] == 1
    assert recorded["queries"] >= 2
    created_js = recorded["created_tasks"][0]
    assert "/local/finished.mp4" in created_js
    assert '"platform": "Douyin"' in created_js
    assert '"uid": "acct-douyin"' in created_js
    assert outcome.success is True
    assert outcome.external_task_id
    assert outcome.results[0]["success"] is True
    assert outcome.results[0]["xiaovmao_status_label"] == "已发布"


def test_publish_fails_when_video_missing(monkeypatch):
    # 账号已登录但缺成片 → 诚实失败，且不会创建小V猫任务。
    recorded = _install_fake_cdp(monkeypatch, accounts=[_LOGGED_IN])
    outcome = XiaoVmaoPublishAdapter().publish(PublishPayload(title="t", platforms=("douyin",)))
    assert outcome.success is False
    assert outcome.error_message
    assert recorded["created_tasks"] == []


def test_publish_fails_when_task_creation_failed(monkeypatch):
    # 小V猫桥接拒绝创建 PublishLog → 诚实失败，不伪造成功。
    recorded = _install_fake_cdp(monkeypatch, accounts=[_LOGGED_IN], create_ok=False)
    outcome = XiaoVmaoPublishAdapter().publish(
        PublishPayload(title="t", platforms=("douyin",), video_uri="/v.mp4")
    )
    assert outcome.success is False
    assert outcome.error_message
    assert recorded["started"] == 0


def test_publish_surfaces_verification_failure_from_task_record(monkeypatch):
    # 平台风控 / 验证码会在小V猫任务里体现为失败原因，必须透出给前端/操作员。
    _install_fake_cdp(
        monkeypatch,
        accounts=[_LOGGED_IN],
        publish_statuses=(1, 4),
        publish_error="请输入验证码信息",
    )
    outcome = XiaoVmaoPublishAdapter().publish(
        PublishPayload(title="t", platforms=("douyin",), video_uri="/v.mp4")
    )
    assert outcome.success is False
    assert "请输入验证码信息" in (outcome.error_message or "")
    assert "请输入验证码信息" in outcome.results[0]["error"]


def test_publish_fails_when_no_logged_in_account(monkeypatch):
    recorded = _install_fake_cdp(
        monkeypatch,
        accounts=[PlatformAccount(uid="a", platform="douyin", is_login=False)],
    )
    outcome = XiaoVmaoPublishAdapter().publish(
        PublishPayload(title="t", platforms=("douyin",), video_uri="/v.mp4")
    )
    assert outcome.success is False
    assert outcome.error_message
    assert recorded["created_tasks"] == []


def test_publish_fails_targeted_account_without_xiaovmao_uid(monkeypatch):
    recorded = _install_fake_cdp(monkeypatch, accounts=[_LOGGED_IN])
    outcome = XiaoVmaoPublishAdapter().publish(
        PublishPayload(
            title="t",
            platforms=("douyin",),
            video_uri="/v.mp4",
            account_id="acct_1",
            account_name="dy",
        )
    )

    assert outcome.success is False
    assert "xiaovmao_uid" in (outcome.error_message or "")
    assert recorded["created_tasks"] == []


def test_cdp_send_timeout_fails_loudly():
    class TimeoutWebSocket:
        async def send(self, _payload):
            return None

        async def recv(self):
            raise asyncio.TimeoutError

    driver = cdp.XiaoVmaoDriver()
    driver.websocket = TimeoutWebSocket()

    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="Runtime.evaluate"):
        asyncio.run(driver.send("Runtime.evaluate"))


def test_cdp_close_does_not_mask_disconnected_webview():
    class BrokenCloseWebSocket:
        async def close(self):
            raise RuntimeError("no close frame received or sent")

    driver = cdp.XiaoVmaoDriver()
    driver.websocket = BrokenCloseWebSocket()

    asyncio.run(driver.close())

    assert driver.websocket is None


def test_driver_local_probe_launch_hint_and_no_websocket_edges(monkeypatch):
    def broken_run(*_args, **_kwargs):
        raise RuntimeError("process table unavailable")

    monkeypatch.setattr(cdp.subprocess, "run", broken_run)
    assert cdp.XiaoVmaoDriver().is_app_running() is False

    monkeypatch.setattr(cdp.sys, "platform", "darwin")
    auto_driver = cdp.XiaoVmaoDriver(auto_launch=True)
    assert auto_driver.should_auto_launch() is True
    assert auto_driver.try_launch_app() is False
    assert "自动启动" in auto_driver.connect_hint(app_running=False)
    assert "不会反复聚焦" in auto_driver.connect_hint(app_running=True)

    fallback = cdp.Target("fallback", "page", "https://example.invalid", "")
    assert cdp.XiaoVmaoDriver.choose_main_target([fallback]) == fallback

    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="no websocket URL"):
        asyncio.run(cdp.XiaoVmaoDriver().connect_to_target(fallback))

    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="WebSocket 未连接"):
        asyncio.run(cdp.XiaoVmaoDriver().send("Runtime.evaluate"))


def test_cdp_send_protocol_error_and_empty_evaluate_result():
    class ErrorWebSocket:
        async def send(self, _payload):
            return None

        async def recv(self):
            return '{"id": 1, "error": {"message": "node detached"}}'

    driver = cdp.XiaoVmaoDriver()
    driver.websocket = ErrorWebSocket()
    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="node detached"):
        asyncio.run(driver.send("DOM.querySelector"))

    async def empty_result_send(_method, _params=None):
        return {"result": {"result": {}}}

    driver.send = empty_result_send  # type: ignore[method-assign]
    assert asyncio.run(driver.evaluate("void 0")) is None


@pytest.mark.parametrize(
    ("platform", "platform_key", "expected"),
    [
        ("douyin", "Douyin", {"tag_count": 5}),
        ("kuaishou", "KuaiShou", {"desc_key": "CAT_desc", "tag_key": "CAT_tags"}),
        ("shipinhao", "Channels", {}),
        ("xiaohongshu", "XiaoHongShu", {}),
    ],
)
def test_build_publish_task_maps_platform_specific_form_payloads(
    monkeypatch, platform, platform_key, expected
):
    monkeypatch.setattr(cdp, "_new_local_id", lambda: "local-fixed")
    payload = PublishPayload(
        title="超长标题需要被截断到三十个字符以内但仍然可读",
        description="正文",
        platforms=(platform,),
        tags=("#话题一", " 话题二 ", "", "#话题三", "#话题四", "#话题五", "#话题六"),
        video_uri="/video.mp4",
        cover_uri="/cover.jpg",
        scheduled_at=datetime(2026, 7, 3, 10, 30, tzinfo=timezone.utc),
    )

    task = cdp._build_publish_task(
        payload=payload,
        platform=platform,
        account_uid="uid-1",
        batch_source="Cutagent_case",
    )

    assert task["localId"] == "local-fixed"
    assert task["uid"] == "uid-1"
    assert task["platform"] == platform_key
    assert task["videoPath"] == "/video.mp4"
    assert task["originCover"] == "file:///cover.jpg"
    assert task["publishType"] == 2
    assert task["formData"]["CAT_timing"]["time"] == payload.scheduled_at.isoformat()
    assert task["isCustomCover"] is True
    if platform == "douyin":
        assert task["title"] == payload.title[:30]
        assert len(task["title"]) <= 30
        assert len(task["formData"]["tags"]) == expected["tag_count"]
    elif platform == "kuaishou":
        assert task["formData"][expected["desc_key"]] == "正文"
        assert task["formData"][expected["tag_key"]] == [
            "话题一",
            "话题二",
            "话题三",
            "话题四",
            "话题五",
            "话题六",
        ]
    elif platform == "shipinhao":
        assert task["shortTitle"] == payload.title[:16]
        assert len(task["shortTitle"]) <= 16
        assert task["formData"]["topics"][0] == "话题一"
    else:
        assert task["title"] == payload.title[:20]
        assert len(task["title"]) <= 20
        assert task["formData"]["CAT_tags"][0] == "话题一"


def test_publish_record_helpers_accept_nested_shapes_and_alt_keys():
    nested = {"data": {"rows": [{"local_id": "local-a", "status": "2", "shareUrl": "https://x"}]}}
    records = cdp._extract_publish_records(nested)

    assert cdp._extract_publish_records([{"localId": "l"}, "bad"]) == [{"localId": "l"}]
    assert cdp._extract_publish_records("bad") == []
    assert records == [{"local_id": "local-a", "status": "2", "shareUrl": "https://x"}]
    assert cdp._record_local_id(records[0]) == "local-a"
    assert cdp._record_status(records[0]) == 2
    assert cdp._record_status({"status": "bad"}) is None
    assert cdp._record_error({"failReason": "验证码"}) == "验证码"
    assert cdp._record_url(records[0]) == "https://x"
    assert cdp._status_label(None) == "未知"
    assert cdp._status_label(99) == "99"


def test_catbridge_call_rejects_bad_payloads():
    class BadShapeDriver:
        async def evaluate(self, *_args, **_kwargs):
            return ["bad"]

    class ErrorDriver:
        async def evaluate(self, *_args, **_kwargs):
            return {"ok": False, "error": "bridge down"}

    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="返回格式异常"):
        asyncio.run(cdp._catbridge_call(BadShapeDriver(), "noop"))
    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="bridge down"):
        asyncio.run(cdp._catbridge_call(ErrorDriver(), "noop"))


def test_wait_for_publish_record_accepts_scheduled_pending(monkeypatch):
    async def fake_query(_driver, task):
        return {"localId": task["localId"], "status": 6}

    monkeypatch.setattr(cdp, "_query_publish_record", fake_query)
    record = asyncio.run(
        cdp._wait_for_publish_record(
            object(), {"localId": "local-scheduled"}, scheduled=True
        )
    )

    assert record["status"] == 6


def test_wait_for_publish_record_surfaces_failed_record(monkeypatch):
    async def fake_query(_driver, task):
        return {"localId": task["localId"], "status": 4, "errorMessage": "平台验证失败"}

    monkeypatch.setattr(cdp, "_query_publish_record", fake_query)

    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="平台验证失败"):
        asyncio.run(
            cdp._wait_for_publish_record(
                object(), {"localId": "local-failed"}, scheduled=False
            )
        )


def test_wait_for_publish_record_times_out_without_record(monkeypatch):
    async def fake_query(_driver, _task):
        return None

    monkeypatch.setattr(cdp, "_query_publish_record", fake_query)
    monkeypatch.setattr(cdp, "PUBLISH_TASK_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(cdp, "PUBLISH_TASK_POLL_INTERVAL_SECONDS", 0)

    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="未查到 PublishLog"):
        asyncio.run(
            cdp._wait_for_publish_record(
                object(), {"localId": "local-timeout"}, scheduled=False
            )
        )


def test_driver_auto_launches_local_macos_app(monkeypatch):
    calls: list[list[str]] = []

    class FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return FakeCompletedProcess()

    monkeypatch.setattr(cdp.sys, "platform", "darwin")
    monkeypatch.setattr(cdp.subprocess, "run", fake_run)

    driver = cdp.XiaoVmaoDriver(auto_launch=True)

    assert driver.try_launch_app() is True
    assert calls == [
        [
            "open",
            "-a",
            "小V猫",
            "--args",
            "--remote-debugging-port=9222",
        ]
    ]


def test_driver_does_not_auto_launch_for_remote_cdp_host(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        raise AssertionError("remote CDP hosts must not launch a local app")

    monkeypatch.setattr(cdp.sys, "platform", "darwin")
    monkeypatch.setattr(cdp.subprocess, "run", fake_run)

    driver = cdp.XiaoVmaoDriver(host="10.0.0.5", auto_launch=True)

    assert driver.try_launch_app() is False
    assert calls == []


def test_connect_does_not_focus_already_running_app_without_cdp(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "websockets",
        SimpleNamespace(connect=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr(cdp.sys, "platform", "darwin")
    driver = cdp.XiaoVmaoDriver(auto_launch=True)
    monkeypatch.setattr(driver, "fetch_targets", lambda: [])
    monkeypatch.setattr(driver, "is_app_running", lambda: True)

    def fail_launch() -> bool:
        raise AssertionError("running 小V猫 must not be focused via open -a")

    monkeypatch.setattr(driver, "try_launch_app", fail_launch)

    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="不会反复聚焦"):
        asyncio.run(driver.connect(timeout_seconds=0))


class _FakeLoginPage:
    def __init__(self, *evaluate_results):
        self.evaluate_results = list(evaluate_results)
        self.sent: list[tuple[str, dict]] = []

    async def evaluate(self, expression, await_promise: bool = False):
        return self.evaluate_results.pop(0)

    async def send(self, method, params=None):
        self.sent.append((method, params or {}))
        return {"result": {}}


def test_login_driver_extracts_data_url_qr_image():
    page = _FakeLoginPage({"qr_image": "data:image/png;base64,qr", "expired": False})
    driver = cdp.XiaoVmaoLoginDriver()

    qr = asyncio.run(driver.capture_qr_image(page))

    assert qr == "data:image/png;base64,qr"
    assert page.sent == []


def test_login_driver_refreshes_expired_qr_by_clicking_center():
    page = _FakeLoginPage(
        {
            "qr_image": None,
            "expired": True,
            "qr_rect": {"x": 10, "y": 20, "width": 120, "height": 120},
        },
        {"qr_image": "data:image/png;base64,fresh", "expired": False},
    )
    driver = cdp.XiaoVmaoLoginDriver()

    qr = asyncio.run(driver.capture_qr_image(page))

    assert qr == "data:image/png;base64,fresh"
    assert [call[0] for call in page.sent] == [
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert page.sent[0][1]["x"] == 70
    assert page.sent[0][1]["y"] == 80


def test_login_driver_refreshes_expired_qr_with_refresh_rect_first():
    page = _FakeLoginPage(
        {
            "qr_image": None,
            "expired": True,
            "qr_rect": {"x": 10, "y": 20, "width": 120, "height": 120},
            "refresh_rect": {"x": 30, "y": 40, "width": 80, "height": 60},
        },
        {"qr_image": "data:image/png;base64,fresh", "expired": False},
    )
    driver = cdp.XiaoVmaoLoginDriver()

    qr = asyncio.run(driver.capture_qr_image(page))

    assert qr == "data:image/png;base64,fresh"
    assert page.sent[0][1]["x"] == 70
    assert page.sent[0][1]["y"] == 70


@pytest.mark.parametrize("platform", ["shipinhao", "xiaohongshu", "kuaishou"])
def test_login_driver_dismisses_platform_prompt(platform):
    page = _FakeLoginPage({"ok": True, "x": 30, "y": 40, "width": 80, "height": 60})
    driver = cdp.XiaoVmaoLoginDriver()

    asyncio.run(driver.dismiss_login_obstructions(page, platform))

    assert [call[0] for call in page.sent] == [
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert page.sent[0][1]["x"] == 70
    assert page.sent[0][1]["y"] == 70


def test_login_driver_does_not_dismiss_other_platforms():
    page = _FakeLoginPage({"ok": True, "x": 30, "y": 40, "width": 80, "height": 60})
    driver = cdp.XiaoVmaoLoginDriver()

    asyncio.run(driver.dismiss_login_obstructions(page, "douyin"))

    assert page.evaluate_results
    assert page.sent == []


def test_login_driver_switches_xiaohongshu_to_qr_login():
    page = _FakeLoginPage({"ok": True, "x": 30, "y": 40, "width": 64, "height": 64})
    driver = cdp.XiaoVmaoLoginDriver()

    asyncio.run(driver.prepare_platform_login_page(page, "xiaohongshu"))

    assert [call[0] for call in page.sent] == [
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert page.sent[0][1]["x"] == 62
    assert page.sent[0][1]["y"] == 72


def test_login_driver_switches_kuaishou_to_qr_login():
    page = _FakeLoginPage({"ok": True, "x": 1210, "y": 155.5, "width": 40, "height": 40})
    driver = cdp.XiaoVmaoLoginDriver()

    asyncio.run(driver.prepare_platform_login_page(page, "kuaishou"))

    assert [call[0] for call in page.sent] == [
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert page.sent[0][1]["x"] == 1230
    assert page.sent[0][1]["y"] == 175.5


def test_login_driver_does_not_prepare_qr_login_for_other_platforms():
    page = _FakeLoginPage({"ok": True, "x": 30, "y": 40, "width": 64, "height": 64})
    driver = cdp.XiaoVmaoLoginDriver()

    asyncio.run(driver.prepare_platform_login_page(page, "shipinhao"))

    assert page.evaluate_results
    assert page.sent == []


def test_login_driver_emits_verifying_status_for_second_factor():
    page = _FakeLoginPage({"detail": "抖音身份验证，请完成短信验证码"})
    driver = cdp.XiaoVmaoLoginDriver()
    events: list[c.LoginStreamEvent] = []

    asyncio.run(driver.emit_verification_if_needed(page, events.append))

    assert events == [
        c.LoginStreamEvent(
            type="status",
            status="verifying",
            detail="抖音身份验证，请完成短信验证码",
        )
    ]


def test_login_driver_detects_completed_logged_in_account(monkeypatch):
    async def fake_read_accounts(_driver):
        return [
            PlatformAccount(uid="old", platform="douyin", nickname="old", is_login=True),
            PlatformAccount(uid="new", platform="douyin", nickname="new", is_login=True),
        ]

    monkeypatch.setattr(cdp, "_read_accounts", fake_read_accounts)
    driver = cdp.XiaoVmaoLoginDriver()

    account = asyncio.run(
        driver.find_completed_account(object(), platform="douyin", known_uids={"old"})
    )

    assert account is not None
    assert account.uid == "new"


def test_login_manager_emits_error_event_on_driver_exception():
    class FailingLoginDriver:
        async def run_login(self, **kwargs):
            raise cdp.XiaoVmaoUnavailableError("小V猫不可达")

    manager = cdp.XiaoVmaoLoginManager(driver_factory=lambda: FailingLoginDriver())
    account = c.PublishAccount(
        id="acct_1",
        client_id="client_1",
        platform="douyin",
        account_name="dy",
    )

    manager.begin("login_1", account, on_account=lambda _platform_account: account)
    events: list[c.LoginStreamEvent] = []
    for _ in range(4):
        event = manager.next_event("login_1", timeout=1)
        if event is not None:
            events.append(event)
        if manager.poll("login_1").status == "failed":
            break

    assert manager.poll("login_1").status == "failed"
    assert any(event.type == "error" and "小V猫不可达" in (event.detail or "") for event in events)


def test_driver_fetch_targets_parses_cdp_json_and_fallbacks(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"""[
                {"title":"main","type":"page","url":"file:///Resources/app/index.html","webSocketDebuggerUrl":"ws://localhost/main"},
                {"title":"devtools","type":"other","url":"about:blank","webSocketDebuggerUrl":"ws://localhost/dev"}
            ]"""

    monkeypatch.setattr(cdp.urllib.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())
    driver = cdp.XiaoVmaoDriver(host="127.0.0.1", port=9999)

    targets = driver.fetch_targets()

    assert targets[0].ws_url == "ws://127.0.0.1/main"
    assert cdp.XiaoVmaoDriver.choose_main_target(targets) == targets[0]
    assert cdp.XiaoVmaoDriver.choose_main_target([targets[1]]) is None

    def fail_urlopen(*_args, **_kwargs):
        raise cdp.urllib.error.URLError("down")

    monkeypatch.setattr(cdp.urllib.request, "urlopen", fail_urlopen)
    assert driver.fetch_targets() == []


def test_driver_evaluate_surfaces_protocol_errors(monkeypatch):
    driver = cdp.XiaoVmaoDriver()

    async def error_send(_method, _params=None):
        return {"result": {"exceptionDetails": {"text": "js exploded"}}}

    driver.send = error_send  # type: ignore[method-assign]
    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="js exploded"):
        asyncio.run(driver.evaluate("throw new Error()"))


def test_driver_connect_success_uses_main_target(monkeypatch):
    connected: list[str] = []

    async def fake_connect(url, **_kwargs):
        connected.append(url)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=fake_connect))
    driver = cdp.XiaoVmaoDriver()
    monkeypatch.setattr(
        driver,
        "fetch_targets",
        lambda: [
            cdp.Target(
                title="main",
                target_type="page",
                url="file:///Resources/app/index.html",
                ws_url="ws://127.0.0.1/main",
            )
        ],
    )
    monkeypatch.setattr(driver, "is_app_running", lambda: True)

    asyncio.run(driver.connect(timeout_seconds=1))

    assert connected == ["ws://127.0.0.1/main"]
    assert driver.websocket is not None


def test_read_accounts_maps_platform_keys_and_errors():
    class GoodDriver:
        async def evaluate(self, *_args, **_kwargs):
            return {
                "accounts": [
                    {
                        "uid": "uid-1",
                        "platform": "Douyin",
                        "nickname": "dy",
                        "remark": "main",
                        "subName": "sub",
                        "isLogin": 1,
                    },
                    {"uid": "uid-2", "platform": "Custom", "isLogin": False},
                ]
            }

    accounts = asyncio.run(cdp._read_accounts(GoodDriver()))
    assert accounts[0].platform == "douyin"
    assert accounts[0].sub_name == "sub"
    assert accounts[0].is_login is True
    assert accounts[1].platform == "Custom"

    class ErrorDriver:
        async def evaluate(self, *_args, **_kwargs):
            return {"error": "CatBridge unavailable"}

    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="CatBridge unavailable"):
        asyncio.run(cdp._read_accounts(ErrorDriver()))


def test_login_driver_target_selection_and_click_failures(monkeypatch):
    class MainDriver:
        def fetch_targets(self):
            return [
                cdp.Target("old", "page", "https://creator.douyin.com/login", "ws://old"),
                cdp.Target("new", "webview", "https://creator.douyin.com/login", "ws://new"),
            ]

    class PageDriver:
        def __init__(self):
            self.target: cdp.Target | None = None

        async def connect_to_target(self, target):
            self.target = target

    pages: list[PageDriver] = []

    def factory():
        page = PageDriver()
        pages.append(page)
        return page

    driver = cdp.XiaoVmaoLoginDriver(driver_factory=factory)
    page = asyncio.run(
        driver.wait_for_login_target(
            MainDriver(),
            "douyin",
            before_targets={"ws://old"},
            timeout_seconds=1,
        )
    )
    assert page.target.ws_url == "ws://new"

    existing = asyncio.run(driver.connect_existing_login_target(MainDriver(), "douyin"))
    assert existing is not None
    assert existing.target.ws_url == "ws://old"
    assert driver._matching_login_target([], "unknown") is None

    with pytest.raises(cdp.XiaoVmaoUnavailableError, match="未找到可点击文本"):
        asyncio.run(cdp.XiaoVmaoLoginDriver().click_visible_text(_FakeLoginPage({"ok": False}), "添加账号"))


def test_login_driver_capture_qr_reload_and_existing_uid(monkeypatch):
    page = _FakeLoginPage({"expired": True}, {"qr_image": "data:image/png;base64,new"})
    driver = cdp.XiaoVmaoLoginDriver()

    assert asyncio.run(driver.capture_qr_image(page)) == "data:image/png;base64,new"
    assert page.sent == [("Page.reload", {"ignoreCache": True})]

    class MainDriver:
        def __init__(self):
            self.connected = False
            self.closed = False

        async def connect(self):
            self.connected = True

        def fetch_targets(self):
            return []

        async def close(self):
            self.closed = True

    main = MainDriver()

    async def fake_read_accounts(_driver):
        return [PlatformAccount(uid="target", platform="douyin", nickname="dy", is_login=True)]

    monkeypatch.setattr(cdp, "_read_accounts", fake_read_accounts)
    account = asyncio.run(
        cdp.XiaoVmaoLoginDriver(driver_factory=lambda: main).run_login(
            platform="douyin",
            xiaovmao_uid="target",
            emit=lambda _event: None,
        )
    )

    assert account.uid == "target"
    assert main.connected is True
    assert main.closed is True


def test_drive_publish_partial_failure_and_success(monkeypatch):
    monkeypatch.setattr(cdp, "_new_local_id", lambda: "local-fixed")

    async def fake_create(_driver, task):
        if task["platform"] == "Douyin":
            return None
        raise cdp.XiaoVmaoUnavailableError("桥接失败")

    async def fake_wait(_driver, task, *, scheduled):
        assert scheduled is True
        return {"localId": task["localId"], "status": 6, "shareUrl": "https://share"}

    monkeypatch.setattr(cdp, "_create_publish_task", fake_create)
    monkeypatch.setattr(cdp, "_wait_for_publish_record", fake_wait)

    outcome = asyncio.run(
        cdp._drive_publish(
            object(),
            PublishPayload(
                title="t",
                platforms=("douyin", "kuaishou", "xiaohongshu"),
                video_uri="/v.mp4",
                scheduled_at=datetime(2026, 7, 3, 10, tzinfo=timezone.utc),
            ),
            [
                PlatformAccount(uid="dy", platform="douyin", is_login=True),
                PlatformAccount(uid="ks", platform="kuaishou", is_login=True),
            ],
        )
    )

    assert outcome.success is False
    assert outcome.scheduled is True
    assert outcome.external_task_id == "local-fixed"
    assert outcome.results[0]["success"] is True
    assert outcome.results[0]["url"] == "https://share"
    assert outcome.results[1]["error"] == "桥接失败"
    assert "小红书未匹配到已登录账号" in (outcome.error_message or "")


def test_login_manager_success_binding_failure_cancel_and_sweep(monkeypatch):
    class SuccessfulLoginDriver:
        async def run_login(self, **kwargs):
            kwargs["emit"](c.LoginStreamEvent(type="status", status="verifying", detail="扫码"))
            return PlatformAccount(uid="uid", platform="douyin", nickname="dy", is_login=True)

    account = c.PublishAccount(
        id="acct_1",
        client_id="client_1",
        platform="douyin",
        account_name="dy",
    )
    updated = account.model_copy(update={"xiaovmao_uid": "uid", "login_state": "logged_in"})
    manager = cdp.XiaoVmaoLoginManager(driver_factory=lambda: SuccessfulLoginDriver())

    manager.begin("login_success", account, on_account=lambda _platform_account: updated)
    events: list[c.LoginStreamEvent] = []
    for _ in range(5):
        event = manager.next_event("login_success", timeout=1)
        if event is not None:
            events.append(event)
        if any(seen.type == "account" for seen in events):
            break

    assert manager.poll("login_success").status == "active"
    assert [event.type for event in events][-2:] == ["status", "account"]

    manager.begin("login_missing", account, on_account=lambda _platform_account: None)
    for _ in range(5):
        if manager.poll("login_missing").status == "failed":
            break
        manager.next_event("login_missing", timeout=1)
    assert "未持久化" in (manager.poll("login_missing").detail or "")

    assert manager.cancel("login_success") is True
    assert manager.cancel("missing") is False
    assert manager.next_event("login_success", timeout=0) is None

    manager._sessions["stale"] = cdp.LoginSessionSnapshot(
        login_id="stale",
        account_id="acct_1",
        platform="douyin",
        status="pending",
    )
    manager._events["stale"] = cdp.queue.Queue()
    manager._cancel["stale"] = cdp.threading.Event()
    manager._started["stale"] = cdp.time.time() - manager._SESSION_TTL_SECONDS - 1
    manager._sweep()
    assert manager.poll("stale") is None


def test_run_async_bridges_from_existing_event_loop():
    async def inner():
        return cdp._run_async(lambda: _async_value("ok"))

    async def failing_inner():
        return cdp._run_async(lambda: _async_failure())

    assert asyncio.run(inner()) == "ok"
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(failing_inner())


async def _async_value(value):
    return value


async def _async_failure():
    raise RuntimeError("boom")
