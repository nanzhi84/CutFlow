"""XiaoVmaoPublishAdapter (CDP 驱动小V猫) unit tests.

These exercise adapter selection, the 4-platform constant guard, and — with a
mocked CDP driver (no live 小V猫 / no real accounts) — the honest-result contract:
the adapter only reports success after the 小V猫 bridge accepts a PublishLog task
and a matching task record reaches a success/scheduled status.
"""

from __future__ import annotations

import asyncio
import re

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
        def __init__(self, *, host, port):
            self.host, self.port = host, port
            self.local_id = "l_fake"

        async def connect(self, timeout_seconds: int = 30):
            if connect_error is not None:
                raise connect_error
            recorded["connected"] = True

        async def close(self):
            recorded["closed"] = True

        async def set_files_by_index(self, selector, index, files):
            raise AssertionError("publish should use CatBridge tasks, not DOM file inputs")

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
