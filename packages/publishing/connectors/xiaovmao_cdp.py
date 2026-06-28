"""小V猫 CDP connector (M6c) — UNVERIFIED real-platform driver.

Drives the 小V猫 Electron desktop app over its CDP (remote-debugging) endpoint to
drive multi-platform publishing (抖音 / 快手 / 视频号 / 小红书).

UNVERIFIED: this code has NOT been validated against the live 小V猫 app or real
platform accounts in this repo. It requires the desktop app running with
``--remote-debugging-port`` and logged-in accounts, plus the optional
``websockets`` dependency. ``publish`` currently reads accounts + fills the publish
form but does NOT click submit or verify on-page success (deferred to PR5
real-machine work), so it never fabricates a published result. All automation is
best-effort and raises ``XiaoVmaoUnavailableError`` when the app/accounts/deps/inputs
are missing so callers degrade to an explicit failure instead of fabricating a publish.

The pure account-matching / scheduling logic lives in
``packages.publishing.account_matching`` and is unit-tested independently of this
driver.
"""

from __future__ import annotations

import asyncio
import json
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from packages.core import contracts as c
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
CDP_RESPONSE_TIMEOUT_SECONDS = 30.0


class XiaoVmaoUnavailableError(RuntimeError):
    """Raised when the 小V猫 app / CDP endpoint / accounts are not reachable."""


@dataclass
class Target:
    title: str
    target_type: str
    url: str
    ws_url: str


class XiaoVmaoDriver:
    """Low-level CDP driver for the 小V猫 Electron app."""

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

    async def connect_to_target(self, target: Target) -> None:
        try:
            import websockets  # noqa: PLC0415 (optional dependency)
        except Exception as exc:  # pragma: no cover - optional dep
            raise XiaoVmaoUnavailableError(f"websockets is required for the 小V猫 connector: {exc}") from exc
        if not target.ws_url:
            raise XiaoVmaoUnavailableError(f"CDP target has no websocket URL: {target.url}")
        self.websocket = await websockets.connect(target.ws_url, max_size=10_000_000)

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
        deadline = time.monotonic() + CDP_RESPONSE_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise XiaoVmaoUnavailableError(f"CDP {method} 超时，未收到响应")
            try:
                raw = await asyncio.wait_for(self.websocket.recv(), timeout=remaining)
            except TimeoutError as exc:
                raise XiaoVmaoUnavailableError(f"CDP {method} 超时，未收到响应") from exc
            data = json.loads(raw)
            if data.get("id") == request_id:
                if "error" in data:
                    # CDP 协议层错误（如目标/节点不存在）必须显式失败，不可静默成功。
                    raise XiaoVmaoUnavailableError(f"CDP {method} 失败: {data['error']}")
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
# CDP-driven login flow (dashboard QR stream via 小V猫)
# ---------------------------------------------------------------------------

_LOGIN_QR_JS = r"""
(() => {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return !!(
      el.offsetParent !== null &&
      rect.width >= 110 &&
      rect.width <= 300 &&
      rect.height >= 110 &&
      rect.height <= 300 &&
      Math.abs(rect.width - rect.height) <= 24 &&
      style.visibility !== 'hidden' &&
      style.display !== 'none'
    );
  };
  const images = Array.from(document.querySelectorAll('img'))
    .map((img) => ({ img, rect: img.getBoundingClientRect() }))
    .filter(({ img }) => visible(img) && String(img.src || '').startsWith('data:image'))
    .sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
  const bodyText = document.body ? document.body.innerText || '' : '';
  const expired = /失效|过期/.test(bodyText);
  const first = images[0];
  return {
    qr_image: first ? first.img.src : null,
    expired,
    qr_rect: first ? {
      x: first.rect.x,
      y: first.rect.y,
      width: first.rect.width,
      height: first.rect.height,
    } : null,
  };
})()
"""

_VERIFICATION_JS = r"""
(() => {
  const text = document.body ? document.body.innerText || '' : '';
  if (/身份验证|短信验证码|接收短信|安全验证|二次验证/.test(text)) {
    return { detail: '平台要求身份验证，请按页面提示完成短信验证码或安全验证' };
  }
  return { detail: null };
})()
"""

_PLATFORM_LOGIN_URL_HINTS = {
    "douyin": ("douyin.com", "creator.douyin.com"),
    "kuaishou": ("kuaishou.com",),
    "shipinhao": ("channels.weixin.qq.com", "weixin.qq.com"),
    "xiaohongshu": ("xiaohongshu.com",),
}


@dataclass
class LoginSessionSnapshot:
    login_id: str
    account_id: str
    platform: str
    status: str
    detail: str | None = None
    login_state: c.PublishLoginState = "unknown"


class XiaoVmaoLoginDriver:
    """CDP driver for 小V猫 account-login UI.

    UNVERIFIED: 真机登录完整链路（过平台风控）尚未端到端验证；PR0 Spike 证明
    CDP 机制、二维码抓取和抖音二次验证可驱动，但抖音风控未跑通最后一步。
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        *,
        driver_factory: Callable[[], XiaoVmaoDriver] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._driver_factory = driver_factory

    def _new_driver(self) -> XiaoVmaoDriver:
        if self._driver_factory is not None:
            return self._driver_factory()
        return XiaoVmaoDriver(host=self.host, port=self.port)

    async def run_login(
        self,
        *,
        platform: str,
        account_name: str | None = None,
        xiaovmao_uid: str | None = None,
        emit: Callable[[c.LoginStreamEvent], None],
        stop: Callable[[], bool] | None = None,
        timeout_seconds: int = 180,
    ) -> PlatformAccount:
        stop = stop or (lambda: False)
        main_driver = self._new_driver()
        login_page: XiaoVmaoDriver | None = None
        await main_driver.connect()
        try:
            before_targets = {target.ws_url for target in main_driver.fetch_targets()}
            existing_accounts = await _read_accounts(main_driver)
            known_uids = {
                account.uid for account in existing_accounts if account.platform == platform and account.uid
            }
            if xiaovmao_uid:
                existing = await self.find_completed_account(
                    main_driver,
                    platform=platform,
                    known_uids=set(),
                    target_uid=xiaovmao_uid,
                )
                if existing is not None:
                    return existing

            await self.open_platform_login(main_driver, platform)
            login_page = await self.wait_for_login_target(main_driver, platform, before_targets)
            deadline = time.time() + timeout_seconds
            last_qr: str | None = None
            while time.time() < deadline:
                if stop():
                    raise XiaoVmaoUnavailableError("登录已取消")
                await self.emit_verification_if_needed(login_page, emit)
                qr_image = await self.capture_qr_image(login_page)
                if qr_image and qr_image != last_qr:
                    last_qr = qr_image
                    emit(c.LoginStreamEvent(type="qr", qr_image=qr_image))
                completed = await self.find_completed_account(
                    main_driver,
                    platform=platform,
                    known_uids=known_uids,
                    target_uid=xiaovmao_uid,
                )
                if completed is not None:
                    return completed
                await asyncio.sleep(2)
        finally:
            if login_page is not None:
                await login_page.close()
            await main_driver.close()
        raise XiaoVmaoUnavailableError(f"等待 {account_name or platform} 登录超时")

    async def open_platform_login(self, driver: XiaoVmaoDriver, platform: str) -> None:
        await self.click_visible_text(driver, "账号管理")
        await asyncio.sleep(0.2)
        await self.click_visible_text(driver, "添加账号")
        await asyncio.sleep(0.2)
        platform_label = XIAOVMAO_PLATFORM_NAME_MAP.get(platform, platform)
        await self.click_visible_text(driver, platform_label)
        await asyncio.sleep(0.2)
        await self.click_visible_text(driver, "打开登录页面添加")

    async def wait_for_login_target(
        self,
        driver: XiaoVmaoDriver,
        platform: str,
        before_targets: set[str],
        *,
        timeout_seconds: int = 30,
    ) -> XiaoVmaoDriver:
        deadline = time.time() + timeout_seconds
        hints = _PLATFORM_LOGIN_URL_HINTS.get(platform, ())
        while time.time() < deadline:
            for target in driver.fetch_targets():
                if not target.ws_url or target.ws_url in before_targets:
                    continue
                if hints and not any(hint in target.url for hint in hints):
                    continue
                page = self._new_driver()
                await page.connect_to_target(target)
                return page
            await asyncio.sleep(0.5)
        raise XiaoVmaoUnavailableError(f"未找到 {platform} 平台登录 webview target")

    async def click_visible_text(self, driver: XiaoVmaoDriver, text: str) -> None:
        rect = await driver.evaluate(_visible_text_rect_js(text))
        if not isinstance(rect, dict) or not rect.get("ok"):
            raise XiaoVmaoUnavailableError(f"未找到可点击文本: {text}")
        await self._click_rect_center(driver, rect)

    async def capture_qr_image(self, page_driver: XiaoVmaoDriver) -> str | None:
        payload = await page_driver.evaluate(_LOGIN_QR_JS)
        if not isinstance(payload, dict):
            return None
        # 先处理失效：过期的页面仍会渲染一张旧二维码 img，必须先刷新再取，
        # 否则会把已失效的码推给运营、导致扫码超时而非刷新。
        if payload.get("expired"):
            await self._refresh_expired_qr(page_driver, payload)
            payload = await page_driver.evaluate(_LOGIN_QR_JS)
            if not isinstance(payload, dict):
                return None
        qr_image = payload.get("qr_image")
        if isinstance(qr_image, str) and qr_image.startswith("data:image"):
            return qr_image
        return None

    async def emit_verification_if_needed(
        self,
        page_driver: XiaoVmaoDriver,
        emit: Callable[[c.LoginStreamEvent], None],
    ) -> None:
        payload = await page_driver.evaluate(_VERIFICATION_JS)
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if detail:
            emit(c.LoginStreamEvent(type="status", status="verifying", detail=detail))

    async def find_completed_account(
        self,
        driver: XiaoVmaoDriver,
        *,
        platform: str,
        known_uids: set[str],
        target_uid: str | None = None,
    ) -> PlatformAccount | None:
        accounts = await _read_accounts(driver)
        for account in accounts:
            if account.platform != platform or not account.is_login:
                continue
            if target_uid and account.uid == target_uid:
                return account
            if not target_uid and account.uid not in known_uids:
                return account
        return None

    async def _refresh_expired_qr(self, page_driver: XiaoVmaoDriver, payload: dict[str, Any]) -> None:
        rect = payload.get("qr_rect")
        if isinstance(rect, dict):
            await self._click_rect_center(page_driver, rect)
            return
        await page_driver.send("Page.reload", {"ignoreCache": True})

    async def _click_rect_center(self, driver: XiaoVmaoDriver, rect: dict[str, Any]) -> None:
        x = float(rect["x"]) + float(rect["width"]) / 2
        y = float(rect["y"]) + float(rect["height"]) / 2
        await driver.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        await driver.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )


class XiaoVmaoLoginManager:
    """Thread-backed login session manager used by FastAPI sync routes + WS streams."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        driver_factory: Callable[[], XiaoVmaoLoginDriver] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self._driver_factory = driver_factory
        self._sessions: dict[str, LoginSessionSnapshot] = {}
        self._events: dict[str, queue.Queue[c.LoginStreamEvent]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._started: dict[str, float] = {}
        self._lock = threading.Lock()

    # Orphan login sessions (begin without a stream consumer) are swept after this
    # long; the WS stream cancels its own session on close, so this only catches
    # the no-subscriber edge case.
    _SESSION_TTL_SECONDS = 600

    def probe_accounts(self) -> tuple[list[PlatformAccount], bool, str | None]:
        return probe_xiaovmao_accounts(host=self.host, port=self.port)

    def begin(
        self,
        login_id: str,
        account: c.PublishAccount,
        *,
        on_account: Callable[[PlatformAccount], c.PublishAccount | None],
    ) -> LoginSessionSnapshot:
        snapshot = LoginSessionSnapshot(
            login_id=login_id,
            account_id=account.id,
            platform=account.platform,
            status="pending",
        )
        cancel_event = threading.Event()
        self._sweep()
        with self._lock:
            self._sessions[login_id] = snapshot
            self._events[login_id] = queue.Queue()
            self._cancel[login_id] = cancel_event
            self._started[login_id] = time.time()
        thread = threading.Thread(
            target=self._run_login,
            args=(login_id, account, cancel_event, on_account),
            daemon=True,
        )
        thread.start()
        return snapshot

    def poll(self, login_id: str) -> LoginSessionSnapshot | None:
        with self._lock:
            return self._sessions.get(login_id)

    def cancel(self, login_id: str) -> bool:
        with self._lock:
            cancel_event = self._cancel.pop(login_id, None)
            existed = self._sessions.pop(login_id, None) is not None
            self._events.pop(login_id, None)
            self._started.pop(login_id, None)
        if cancel_event is not None:
            cancel_event.set()
        return existed

    def _sweep(self) -> None:
        now = time.time()
        with self._lock:
            stale = [
                login_id
                for login_id, started in self._started.items()
                if now - started > self._SESSION_TTL_SECONDS
            ]
        for login_id in stale:
            self.cancel(login_id)

    def next_event(self, login_id: str, timeout: float = 30) -> c.LoginStreamEvent | None:
        with self._lock:
            event_queue = self._events.get(login_id)
        if event_queue is None:
            return None
        try:
            return event_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _new_driver(self) -> XiaoVmaoLoginDriver:
        if self._driver_factory is not None:
            return self._driver_factory()
        return XiaoVmaoLoginDriver(host=self.host, port=self.port)

    def _run_login(
        self,
        login_id: str,
        account: c.PublishAccount,
        cancel_event: threading.Event,
        on_account: Callable[[PlatformAccount], c.PublishAccount | None],
    ) -> None:
        try:
            platform_account = asyncio.run(
                self._new_driver().run_login(
                    platform=account.platform,
                    account_name=account.account_name,
                    xiaovmao_uid=account.xiaovmao_uid,
                    emit=lambda event: self._emit(login_id, event),
                    stop=cancel_event.is_set,
                )
            )
            if cancel_event.is_set():
                return
            updated = on_account(platform_account)
            if updated is None:
                # 平台登录成功但本地账号绑定未持久化（账号可能并发归档/删除）——
                # 诚实失败，绝不在未落库 xiaovmao_uid 绑定时报告"已登录"。
                detail = "登录成功但账号绑定未持久化（账号可能已归档或被删除）"
                self._set_status(login_id, "failed", detail=detail, login_state="unknown")
                self._emit(login_id, c.LoginStreamEvent(type="error", status="failed", detail=detail))
                return
            self._set_status(login_id, "active", login_state="logged_in")
            self._emit(login_id, c.LoginStreamEvent(type="status", status="active"))
            self._emit(login_id, c.LoginStreamEvent(type="account", account=updated))
        except Exception as exc:
            if cancel_event.is_set():
                return
            detail = str(exc)
            self._set_status(login_id, "failed", detail=detail, login_state="unknown")
            self._emit(login_id, c.LoginStreamEvent(type="error", status="failed", detail=detail))

    def _set_status(
        self,
        login_id: str,
        status: str,
        *,
        detail: str | None = None,
        login_state: c.PublishLoginState | None = None,
    ) -> None:
        with self._lock:
            session = self._sessions.get(login_id)
            if session is None:
                return
            session.status = status
            session.detail = detail
            if login_state is not None:
                session.login_state = login_state

    def _emit(self, login_id: str, event: c.LoginStreamEvent) -> None:
        if event.type == "status" and event.status == "verifying":
            self._set_status(login_id, "verifying", detail=event.detail)
        with self._lock:
            event_queue = self._events.get(login_id)
        if event_queue is not None:
            event_queue.put(event)


def _visible_text_rect_js(text: str) -> str:
    return f"""
    (() => {{
      const wanted = {json.dumps(text, ensure_ascii=False)};
      const visible = (el) => {{
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return !!(
          el.offsetParent !== null &&
          rect.width > 0 &&
          rect.height > 0 &&
          style.visibility !== 'hidden' &&
          style.display !== 'none'
        );
      }};
      const elements = Array.from(document.querySelectorAll('button, [role="button"], div, span, a'))
        .filter((el) => visible(el) && (el.innerText || el.textContent || '').trim().includes(wanted))
        .map((el) => {{
          const rect = el.getBoundingClientRect();
          return {{ x: rect.x, y: rect.y, width: rect.width, height: rect.height }};
        }})
        .sort((a, b) => a.x - b.x || a.y - b.y);
      const rect = elements[0];
      return rect ? {{ ok: true, ...rect }} : {{ ok: false }};
    }})()
    """


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
    if payload.account_id and not payload.account_uid:
        raise XiaoVmaoUnavailableError(
            f"发布账号 {payload.account_name or payload.account_id} 未绑定 xiaovmao_uid，无法精确路由小V猫账号"
        )

    # Resolve a logged-in 小V猫 account per requested platform.
    selected: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for platform in payload.platforms:
        account = match_account(
            accounts,
            platform=platform,
            account_group=payload.account_group,
            case_name=payload.case_name,
            account_uid=payload.account_uid,
        )
        if account is None or not account.is_login:
            reason = XIAOVMAO_PLATFORM_NAME_MAP.get(platform, platform) + "未匹配到已登录账号"
            failures.append({"platform": platform, "success": False, "error": reason})
            continue
        selected.append({"platform": platform, "account": account.uid})
    if not selected:
        raise XiaoVmaoUnavailableError("未选择到任何可发布的小V猫账号")

    # A real publish requires the finished video on the 小V猫 machine's filesystem.
    if not payload.video_uri:
        raise XiaoVmaoUnavailableError("缺少成片文件路径（video_uri），无法发布")

    # Best-effort drive 小V猫's publish form. Every DOM step is verified so a lost
    # selector / CDP error raises instead of being silently treated as a publish.
    await driver.set_files_by_index("input[type=file]", 0, [payload.video_uri])
    text_result = await driver.evaluate(_fill_text_js(payload.title, payload.description))
    if not (isinstance(text_result, dict) and text_result.get("ok")):
        raise XiaoVmaoUnavailableError("填写小V猫标题/正文失败（DOM 选择器可能已变更）")
    if payload.tags:
        tags_result = await driver.evaluate(_fill_tags_js(list(payload.tags)))
        if not (isinstance(tags_result, dict) and tags_result.get("ok")):
            raise XiaoVmaoUnavailableError("填写小V猫标签失败（DOM 选择器可能已变更）")

    # 诚实失败铁律：驱动提交按钮 + 平台任务创建成功检测尚未对真小V猫实现/验证（待 PR5
    # 真机联调）。表单已填充，但在未真正提交并验证成功前，绝不伪造发布成功。
    pending = [
        {
            "platform": item["platform"],
            "account": item["account"],
            "success": False,
            "error": "已填充小V猫发布表单，但提交与成功检测未实现（UNVERIFIED，待 PR5）；未发布",
        }
        for item in selected
    ]
    return PublishOutcome(
        success=False,
        adapter_id=XIAOVMAO_ADAPTER_ID,
        results=pending + failures,
        error_message=(
            "已填充小V猫发布表单，但提交与发布成功检测尚未实现"
            "（UNVERIFIED，待 PR5 真机联调）；未真正发布，不伪造成功。"
        ),
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
        accounts = _run_async(_run)
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

    return _run_async(_run)  # pragma: no cover - real-platform path


def _run_async(factory: Callable[[], Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    result: dict[str, Any] = {}

    def target() -> None:
        try:
            result["value"] = asyncio.run(factory())
        except BaseException as exc:  # pragma: no cover - bridge thread error path
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")
