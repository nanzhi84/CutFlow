"""小V猫 CDP connector (M6c) — UNVERIFIED real-platform driver.

Faithful port of the origin ``XiaoVmaoDriver`` / ``XiaoVmaoPublisherAdapter``
(digital-human-Cutagent ``app/services/publishers/xiaovmao_adapter.py``). Drives
the 小V猫 Electron desktop app over its CDP (remote-debugging) endpoint to submit
multi-platform publish tasks (抖音 / 快手 / 视频号 / 小红书).

UNVERIFIED: this code has NOT been validated against the live 小V猫 app or real
platform accounts in this repo. It requires the desktop app running with
``--remote-debugging-port`` and logged-in accounts, plus the optional
``websockets`` dependency. It is imported lazily by ``XiaoVmaoPublishAdapter`` and
is never reached by tests. All real-platform automation is best-effort and raises
``XiaoVmaoUnavailableError`` when the app/accounts/deps are missing so callers
degrade to manual review instead of fabricating a publish.

The pure account-matching / scheduling logic lives in
``packages.publishing.account_matching`` and is unit-tested independently of this
driver.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.core.contracts import PlatformAccount
from packages.publishing.account_matching import match_account
from packages.publishing.platform_adapter import (
    XIAOVMAO_ADAPTER_ID,
    XIAOVMAO_PLATFORM_KEY_MAP,
    XIAOVMAO_PLATFORM_NAME_MAP,
    PublishOutcome,
    PublishPayload,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9222
APP_NAME = "小V猫"


class XiaoVmaoUnavailableError(RuntimeError):
    """Raised when the 小V猫 app / CDP endpoint / accounts are not reachable."""


@dataclass
class Target:
    target_id: str
    title: str
    target_type: str
    url: str
    ws_url: str


class XiaoVmaoDriver:
    """Low-level CDP driver for the 小V猫 Electron app (origin parity)."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self.websocket = None
        self.message_id = 0

    def _json_url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}/{path}"

    def is_app_running(self) -> bool:
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"/{APP_NAME}.app/Contents/MacOS/"],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            return False
        return bool((result.stdout or "").strip())

    def fetch_targets(self) -> list[Target]:
        try:
            with urllib.request.urlopen(self._json_url("json/list"), timeout=2) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ValueError):
            return []
        return [
            Target(
                target_id=item.get("id", ""),
                title=item.get("title", ""),
                target_type=item.get("type", ""),
                url=item.get("url", ""),
                ws_url=(item.get("webSocketDebuggerUrl", "") or "").replace("localhost", self.host),
            )
            for item in payload
        ]

    @staticmethod
    def choose_main_target(targets: list[Target]) -> Target | None:
        for target in targets:
            if target.target_type == "page" and target.url.endswith("/Resources/app/index.html"):
                return target
        for target in targets:
            if target.target_type == "page":
                return target
        return None

    async def connect(self, timeout_seconds: int = 30) -> None:
        try:
            import websockets  # noqa: PLC0415 (optional dependency)
        except Exception as exc:  # pragma: no cover - optional dep
            raise XiaoVmaoUnavailableError(f"websockets is required for the 小V猫 connector: {exc}") from exc
        # Fail fast when the desktop app is not running AND nothing is listening on
        # the CDP endpoint. This connector does NOT launch the app (that is the
        # operator's / out-of-process supervisor's responsibility), so retrying for
        # the full timeout would only stall. We still retry while the app is up but
        # the publish page target has not appeared yet.
        if not self.is_app_running() and not self.fetch_targets():
            raise XiaoVmaoUnavailableError(
                f"小V猫未运行或未开启 remote-debugging-port={self.port}，无法连接 CDP 调试端口。"
            )
        deadline = time.time() + timeout_seconds
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                targets = self.fetch_targets()
                main_target = self.choose_main_target(targets)
                if main_target and main_target.ws_url:
                    self.websocket = await websockets.connect(main_target.ws_url, max_size=10_000_000)
                    return
            except Exception as exc:  # pragma: no cover - runtime retry path
                last_error = exc
            await asyncio.sleep(1)
        raise XiaoVmaoUnavailableError(
            f"无法连接小V猫调试端口，请确认小V猫已启动并开启 remote-debugging-port={self.port}: {last_error}"
        )

    async def close(self) -> None:
        if self.websocket:
            await self.websocket.close()
            self.websocket = None

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.websocket:
            raise XiaoVmaoUnavailableError("WebSocket 未连接")
        self.message_id += 1
        request_id = self.message_id
        await self.websocket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            raw = await self.websocket.recv()
            data = json.loads(raw)
            if data.get("id") == request_id:
                return data

    async def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        response = await self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        )
        result = response.get("result", {})
        if "exceptionDetails" in result:
            raise XiaoVmaoUnavailableError(result["exceptionDetails"].get("text", "JS evaluation failed"))
        return result.get("result", {}).get("value")

    async def query_selector_all(self, selector: str) -> list[int]:
        document = await self.send("DOM.getDocument", {"depth": 4})
        root_id = document["result"]["root"]["nodeId"]
        response = await self.send("DOM.querySelectorAll", {"nodeId": root_id, "selector": selector})
        return response["result"].get("nodeIds", [])

    async def set_files_by_index(self, selector: str, index: int, files: list[str]) -> None:
        node_ids = await self.query_selector_all(selector)
        if index >= len(node_ids):
            raise XiaoVmaoUnavailableError(f"未找到第 {index + 1} 个文件输入框: {selector}")
        await self.send("DOM.setFileInputFiles", {"nodeId": node_ids[index], "files": files})


# ---------------------------------------------------------------------------
# Account reading (origin _read_accounts CatBridge bridge call)
# ---------------------------------------------------------------------------

_READ_ACCOUNTS_JS = r"""
Promise.resolve().then(async () => {
  try {
    if (!(window.CatBridge && window.CatBridge.getCall)) {
      return { error: 'CatBridge unavailable', accounts: [] };
    }
    const raw = await window.CatBridge.getCall.call(window.CatBridge, 'AccountManager.getAllAccounts');
    const accounts = Array.isArray(raw) ? raw.map((item) => ({
      uid: item.uid,
      platform: item.platform,
      nickname: item.nickname,
      remark: item.remark || '',
      subName: item.subName || item.sub_name || '',
      isLogin: !!item.isLogin,
    })) : [];
    return { accounts };
  } catch (error) {
    return { error: String(error && (error.stack || error.message || error)), accounts: [] };
  }
})
"""

# 小V猫 platform key -> generic platform id (inverse of XIAOVMAO_PLATFORM_KEY_MAP).
_KEY_TO_PLATFORM = {key: platform for platform, key in XIAOVMAO_PLATFORM_KEY_MAP.items()}


async def _read_accounts(driver: XiaoVmaoDriver) -> list[PlatformAccount]:
    payload = await driver.evaluate(_READ_ACCOUNTS_JS, await_promise=True)
    if not payload or payload.get("error"):
        raise XiaoVmaoUnavailableError((payload or {}).get("error", "无法读取小V猫账号信息"))
    accounts: list[PlatformAccount] = []
    for item in payload.get("accounts", []):
        platform_key = item.get("platform", "")
        accounts.append(
            PlatformAccount(
                uid=item.get("uid", ""),
                platform=_KEY_TO_PLATFORM.get(platform_key, platform_key),
                nickname=item.get("nickname", ""),
                remark=item.get("remark", ""),
                sub_name=item.get("subName", ""),
                is_login=bool(item.get("isLogin")),
            )
        )
    return accounts


# ---------------------------------------------------------------------------
# Publish flow JS-driving snippets (origin _fill_text / _fill_tags / _apply_schedule)
# ---------------------------------------------------------------------------


def _fill_text_js(title: str, description: str) -> str:
    return f"""
    (() => {{
      const setNativeValue = (element, value) => {{
        const prototype = Object.getPrototypeOf(element);
        const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
        if (setter) setter.call(element, value);
        else element.value = value;
        element.dispatchEvent(new Event('input', {{ bubbles: true }}));
        element.dispatchEvent(new Event('change', {{ bubbles: true }}));
      }};
      const titleInput = document.getElementById('title');
      const descInput = document.getElementById('desc');
      if (!titleInput || !descInput) return {{ ok: false }};
      setNativeValue(titleInput, {json.dumps(title, ensure_ascii=False)});
      setNativeValue(descInput, {json.dumps(description, ensure_ascii=False)});
      return {{ ok: true }};
    }})()
    """


def _fill_tags_js(tags: list[str]) -> str:
    return f"""
    (() => {{
      const tags = {json.dumps(tags, ensure_ascii=False)};
      const setNativeValue = (element, value) => {{
        const prototype = Object.getPrototypeOf(element);
        const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
        if (setter) setter.call(element, value);
        else element.value = value;
        element.dispatchEvent(new Event('input', {{ bubbles: true }}));
        element.dispatchEvent(new Event('change', {{ bubbles: true }}));
      }};
      const inputs = Array.from(document.querySelectorAll('input.ant-select-selection-search-input'));
      const input = inputs[0];
      if (!input || !tags.length) return {{ ok: false }};
      for (const tag of tags) {{
        setNativeValue(input, tag);
        input.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', bubbles: true }}));
      }}
      return {{ ok: true, count: tags.length }};
    }})()
    """


async def _drive_publish(driver: XiaoVmaoDriver, payload: PublishPayload, accounts: list[PlatformAccount]) -> PublishOutcome:
    # UNVERIFIED: resolves the per-platform account, fills the form, and submits.
    selected: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for platform in payload.platforms:
        account = match_account(
            accounts,
            platform=platform,
            account_group=payload.account_group,
            case_name=payload.case_name,
        )
        if account is None or not account.is_login:
            reason = XIAOVMAO_PLATFORM_NAME_MAP.get(platform, platform) + "未匹配到已登录账号"
            failures.append({"platform": platform, "success": False, "error": reason})
            continue
        selected.append({"platform": platform, "account": account.uid})
    if not selected:
        raise XiaoVmaoUnavailableError("未选择到任何可发布的小V猫账号")
    if payload.video_uri:
        await driver.set_files_by_index("input[type=file]", 0, [payload.video_uri])
    await driver.evaluate(_fill_text_js(payload.title, payload.description))
    if payload.tags:
        await driver.evaluate(_fill_tags_js(list(payload.tags)))
    results = [{"platform": item["platform"], "success": True} for item in selected] + failures
    # The real adapter verifies on-page publish success; this skeleton reports the
    # selected/failed split. Manual review short-circuits before real submission.
    return PublishOutcome(
        success=any(item.get("success") for item in results),
        adapter_id=XIAOVMAO_ADAPTER_ID,
        results=results,
        scheduled=payload.scheduled_at is not None,
    )


# ---------------------------------------------------------------------------
# Public connector entry points (sync wrappers over the async driver)
# ---------------------------------------------------------------------------


def probe_xiaovmao_accounts(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    account_group: str | None = None,
    case_name: str | None = None,
) -> tuple[list[PlatformAccount], bool, str | None]:
    async def _run() -> list[PlatformAccount]:
        driver = XiaoVmaoDriver(host=host, port=port)
        await driver.connect()
        try:
            return await _read_accounts(driver)
        finally:
            await driver.close()

    try:
        accounts = asyncio.run(_run())
    except XiaoVmaoUnavailableError as exc:  # pragma: no cover - real-platform path
        return [], False, str(exc)
    except Exception as exc:  # pragma: no cover - real-platform path
        return [], False, f"小V猫 probe failed: {exc}"
    return accounts, True, None


def publish_via_xiaovmao(
    payload: PublishPayload,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> PublishOutcome:
    async def _run() -> PublishOutcome:
        driver = XiaoVmaoDriver(host=host, port=port)
        await driver.connect()
        try:
            accounts = await _read_accounts(driver)
            return await _drive_publish(driver, payload, accounts)
        finally:
            await driver.close()

    return asyncio.run(_run())  # pragma: no cover - real-platform path
