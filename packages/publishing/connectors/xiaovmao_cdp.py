"""小V猫 CDP connector (M6c) — CDP driver for the desktop app.

Drives the 小V猫 Electron desktop app over its CDP (remote-debugging) endpoint to
drive multi-platform publishing (抖音 / 快手 / 视频号 / 小红书).

This requires the desktop app running with ``--remote-debugging-port`` and
logged-in accounts, plus the optional ``websockets`` dependency. ``publish`` uses
the app's own ``CatBridge`` APIs to create ``PublishLog`` tasks, calls
``PublishController.startPublishTask``, and polls the real 小V猫 task status. It
still never fabricates a published result: app/CDP/bridge failures, platform
verification prompts, and failed task records are returned as explicit failures.

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
import uuid
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
            try:
                await self.websocket.close()
            except Exception:
                # The platform webview may navigate/close itself after a successful
                # scan, which makes the CDP websocket close without a normal close
                # frame. Cleanup must not mask the completed login/publish result.
                pass
            finally:
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
  const intersect = (a, b) => ({
    left: Math.max(a.left, b.left),
    top: Math.max(a.top, b.top),
    right: Math.min(a.right, b.right),
    bottom: Math.min(a.bottom, b.bottom),
  });
  const hasArea = (rect) => rect.right > rect.left && rect.bottom > rect.top;
  const rectPayload = (rect) => ({
    x: rect.left,
    y: rect.top,
    width: rect.right - rect.left,
    height: rect.bottom - rect.top,
  });
  const absoluteRect = (el, offset) => {
    const rect = el.getBoundingClientRect();
    return {
      left: rect.left + offset.x,
      top: rect.top + offset.y,
      right: rect.right + offset.x,
      bottom: rect.bottom + offset.y,
    };
  };
  const styleVisible = (el) => {
    const view = el.ownerDocument.defaultView || window;
    const style = view.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
  };
  const visibleInBounds = (el, offset, bounds) => {
    if (!styleVisible(el)) return null;
    const rect = absoluteRect(el, offset);
    if (!hasArea(rect)) return null;
    const clipped = intersect(rect, bounds);
    return hasArea(clipped) ? rect : null;
  };
  const imageDataUrl = (img) => {
    const src = String(img.currentSrc || img.src || '');
    if (src.startsWith('data:image')) return src;
    try {
      const canvas = img.ownerDocument.createElement('canvas');
      canvas.width = img.naturalWidth || img.width;
      canvas.height = img.naturalHeight || img.height;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0);
      const dataUrl = canvas.toDataURL('image/png');
      return dataUrl.startsWith('data:image') ? dataUrl : null;
    } catch (_error) {
      return null;
    }
  };
  const canvasDataUrl = (canvas) => {
    try {
      const dataUrl = canvas.toDataURL('image/png');
      return dataUrl.startsWith('data:image') ? dataUrl : null;
    } catch (_error) {
      return null;
    }
  };
  const candidates = [];
  const visibleTexts = [];
  const refreshRects = [];
  const scanDocument = (doc, offset, bounds, depth) => {
    for (const img of Array.from(doc.querySelectorAll('img'))) {
      const rect = visibleInBounds(img, offset, bounds);
      const width = rect ? rect.right - rect.left : 0;
      const height = rect ? rect.bottom - rect.top : 0;
      const dataUrl = rect ? imageDataUrl(img) : null;
      if (
        dataUrl &&
        width >= 110 &&
        width <= 320 &&
        height >= 110 &&
        height <= 320 &&
        Math.abs(width - height) <= 28
      ) {
        const className = String(img.className || '').toLowerCase();
        const score = width * height + (className.includes('qr') ? 100000 : 0);
        candidates.push({ qr_image: dataUrl, rect, score });
      }
    }
    for (const canvas of Array.from(doc.querySelectorAll('canvas'))) {
      const rect = visibleInBounds(canvas, offset, bounds);
      const width = rect ? rect.right - rect.left : 0;
      const height = rect ? rect.bottom - rect.top : 0;
      const dataUrl = rect ? canvasDataUrl(canvas) : null;
      if (
        dataUrl &&
        width >= 110 &&
        width <= 320 &&
        height >= 110 &&
        height <= 320 &&
        Math.abs(width - height) <= 28
      ) {
        candidates.push({ qr_image: dataUrl, rect, score: width * height + 50000 });
      }
    }
    for (const el of Array.from(doc.querySelectorAll('body *'))) {
      const directText = Array.from(el.childNodes || [])
        .filter((node) => node.nodeType === Node.TEXT_NODE)
        .map((node) => node.textContent || '')
        .join('')
        .trim();
      const text = directText || (el.children.length === 0 ? String(el.innerText || el.textContent || '').trim() : '');
      if (!text) continue;
      const rect = visibleInBounds(el, offset, bounds);
      if (!rect) continue;
      visibleTexts.push(text);
      if (/刷新|失效|过期|网络不可用/.test(text)) {
        const target = el.closest('.refresh-wrap, .mask, .qrcode-wrap, button, a, [role="button"]') || el;
        const targetRect = visibleInBounds(target, offset, bounds) || rect;
        refreshRects.push(targetRect);
      }
    }
    if (depth >= 2) return;
    for (const frame of Array.from(doc.querySelectorAll('iframe'))) {
      try {
        const frameRect = visibleInBounds(frame, offset, bounds);
        if (!frameRect) continue;
        const childBounds = intersect(frameRect, bounds);
        if (!hasArea(childBounds) || !frame.contentDocument) continue;
        scanDocument(
          frame.contentDocument,
          { x: frameRect.left, y: frameRect.top },
          childBounds,
          depth + 1,
        );
      } catch (_error) {
        // Cross-origin frames cannot be inspected from this context.
      }
    }
  };
  scanDocument(document, { x: 0, y: 0 }, { left: 0, top: 0, right: innerWidth, bottom: innerHeight }, 0);
  const expired = visibleTexts.some((text) => /失效|过期/.test(text));
  const first = candidates.sort((a, b) => b.score - a.score || a.rect.top - b.rect.top || a.rect.left - b.rect.left)[0];
  const refresh = refreshRects.sort((a, b) => (b.right - b.left) * (b.bottom - b.top) - (a.right - a.left) * (a.bottom - a.top))[0];
  return {
    qr_image: first ? first.qr_image : null,
    expired,
    qr_rect: first ? rectPayload(first.rect) : null,
    refresh_rect: refresh ? rectPayload(refresh) : null,
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

_DISMISS_PLATFORM_LOGIN_PROMPT_JS = r"""
(() => {
  const normalize = (value) => String(value || '').replace(/\s+/g, '').trim();
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return !!(
      el.offsetParent !== null &&
      rect.width > 0 &&
      rect.height > 0 &&
      style.visibility !== 'hidden' &&
      style.display !== 'none' &&
      style.opacity !== '0'
    );
  };
  const bodyText = document.body ? document.body.innerText || '' : '';
  const promptMarkers = [
    '切换到管理员身份登录带货助手',
    '带货助手已升级',
    '切换到前台页面登录',
    '前台页面可能触发限制',
    '切换到快手小店登录',
    '快手小店登录',
    '创作中心登录困难',
    '小店登录',
  ];
  const promptMatch = (text) => promptMarkers.some(
    (marker) => String(text || '').includes(marker)
  );
  if (!promptMatch(bodyText)) {
    return { ok: false };
  }
  const buttons = Array.from(document.querySelectorAll('button, [role="button"]'))
    .filter((el) => visible(el) && normalize(el.innerText || el.textContent) === '关闭')
    .map((el) => {
      const container = el.closest('.ant-alert, .ant-modal, .ant-modal-root') || el;
      const containerText = container.innerText || container.textContent || '';
      const rect = el.getBoundingClientRect();
      return {
        ok: true,
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
        score: (promptMatch(containerText) ? 0 : 1000) + rect.y,
      };
    })
    .sort((a, b) => a.score - b.score || a.x - b.x);
  const button = buttons[0];
  return button || { ok: false };
})()
"""

_SWITCH_XIAOHONGSHU_QR_LOGIN_JS = r"""
(() => {
  const text = document.body ? document.body.innerText || '' : '';
  if (/APP扫一扫登录|扫码即同意/.test(text)) {
    return { ok: false };
  }
  if (!/短信登录|手机号|发送验证码/.test(text)) {
    return { ok: false };
  }
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return !!(
      el.offsetParent !== null &&
      rect.width >= 40 &&
      rect.width <= 90 &&
      rect.height >= 40 &&
      rect.height <= 90 &&
      style.visibility !== 'hidden' &&
      style.display !== 'none' &&
      style.opacity !== '0'
    );
  };
  const icons = Array.from(document.querySelectorAll('img'))
    .filter((img) => visible(img) && String(img.src || '').startsWith('data:image'))
    .map((img) => {
      const rect = img.getBoundingClientRect();
      return {
        ok: true,
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
        score: -rect.x + rect.y,
      };
    })
    .sort((a, b) => a.score - b.score);
  return icons[0] || { ok: false };
})()
"""

_SWITCH_KUAISHOU_QR_LOGIN_JS = r"""
(() => {
  const text = document.body ? document.body.innerText || '' : '';
  if (/扫码后|请使用快手|打开快手|快手APP/.test(text)) {
    return { ok: false };
  }
  if (!/扫码登录/.test(text)) {
    return { ok: false };
  }
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return !!(
      el.offsetParent !== null &&
      rect.width > 0 &&
      rect.height > 0 &&
      style.visibility !== 'hidden' &&
      style.display !== 'none' &&
      style.opacity !== '0'
    );
  };
  const switchSelector = [
    '.platform-switch',
    '[class*="platform-switch"]',
    'button',
    '[role="button"]',
    'a',
  ].join(', ');
  const candidates = Array.from(
    document.querySelectorAll(`${switchSelector}, div, span`)
  )
    .filter((el) => visible(el) && /扫码登录/.test(el.innerText || el.textContent || ''))
    .map((el) => {
      const clickable = el.closest(switchSelector) || el;
      const rect = clickable.getBoundingClientRect();
      const className = String(clickable.className || '');
      const compact = (
        rect.width >= 30 &&
        rect.width <= 120 &&
        rect.height >= 30 &&
        rect.height <= 80
      );
      const switchScore = className.includes('platform-switch') ? 0 : 100;
      return {
        ok: true,
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
        score: (compact ? 0 : 1000) + switchScore - rect.x + rect.y,
      };
    })
    .sort((a, b) => a.score - b.score);
  return candidates[0] || { ok: false };
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

            login_page = await self.connect_existing_login_target(main_driver, platform)
            if login_page is None:
                try:
                    await self.open_platform_login(main_driver, platform)
                except XiaoVmaoUnavailableError:
                    login_page = await self.connect_existing_login_target(main_driver, platform)
                    if login_page is None:
                        raise
            if login_page is None:
                login_page = await self.wait_for_login_target(main_driver, platform, before_targets)
            deadline = time.time() + timeout_seconds
            last_qr: str | None = None
            while time.time() < deadline:
                if stop():
                    raise XiaoVmaoUnavailableError("登录已取消")
                await self.dismiss_login_obstructions(main_driver, platform)
                try:
                    await self.prepare_platform_login_page(login_page, platform)
                    await self.emit_verification_if_needed(login_page, emit)
                    qr_image = await self.capture_qr_image(login_page)
                except Exception as exc:
                    completed = await self.find_completed_account(
                        main_driver,
                        platform=platform,
                        known_uids=known_uids,
                        target_uid=xiaovmao_uid,
                    )
                    if completed is not None:
                        return completed
                    reconnected = await self.connect_existing_login_target(main_driver, platform)
                    if reconnected is None:
                        raise XiaoVmaoUnavailableError(f"登录页面连接中断: {exc}") from exc
                    await login_page.close()
                    login_page = reconnected
                    await asyncio.sleep(1)
                    continue
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
        fallback_target: Target | None = None
        while time.time() < deadline:
            for target in driver.fetch_targets():
                if not target.ws_url or target.ws_url in before_targets:
                    if (
                        target.ws_url
                        and hints
                        and any(hint in target.url for hint in hints)
                        and target.target_type in {"page", "webview"}
                    ):
                        fallback_target = target
                    continue
                if hints and not any(hint in target.url for hint in hints):
                    continue
                page = self._new_driver()
                await page.connect_to_target(target)
                return page
            if fallback_target is not None and time.time() > deadline - timeout_seconds + 1:
                page = self._new_driver()
                await page.connect_to_target(fallback_target)
                return page
            await asyncio.sleep(0.5)
        raise XiaoVmaoUnavailableError(f"未找到 {platform} 平台登录 webview target")

    async def connect_existing_login_target(
        self,
        driver: XiaoVmaoDriver,
        platform: str,
    ) -> XiaoVmaoDriver | None:
        target = self._matching_login_target(driver.fetch_targets(), platform)
        if target is None:
            return None
        page = self._new_driver()
        await page.connect_to_target(target)
        return page

    def _matching_login_target(self, targets: list[Target], platform: str) -> Target | None:
        hints = _PLATFORM_LOGIN_URL_HINTS.get(platform, ())
        if not hints:
            return None
        for target in targets:
            if (
                target.ws_url
                and target.target_type in {"page", "webview"}
                and any(hint in target.url for hint in hints)
            ):
                return target
        return None

    async def click_visible_text(self, driver: XiaoVmaoDriver, text: str) -> None:
        rect = await driver.evaluate(_visible_text_rect_js(text))
        if not isinstance(rect, dict) or not rect.get("ok"):
            raise XiaoVmaoUnavailableError(f"未找到可点击文本: {text}")
        await self._click_rect_center(driver, rect)

    async def dismiss_login_obstructions(self, driver: XiaoVmaoDriver, platform: str) -> None:
        if platform not in {"shipinhao", "xiaohongshu", "kuaishou"}:
            return
        payload = await driver.evaluate(_DISMISS_PLATFORM_LOGIN_PROMPT_JS)
        if not isinstance(payload, dict) or not payload.get("ok"):
            return
        await self._click_rect_center(driver, payload)
        await asyncio.sleep(0.2)

    async def prepare_platform_login_page(self, page_driver: XiaoVmaoDriver, platform: str) -> None:
        script = {
            "kuaishou": _SWITCH_KUAISHOU_QR_LOGIN_JS,
            "xiaohongshu": _SWITCH_XIAOHONGSHU_QR_LOGIN_JS,
        }.get(platform)
        if script is None:
            return
        payload = await page_driver.evaluate(script)
        if not isinstance(payload, dict) or not payload.get("ok"):
            return
        await self._click_rect_center(page_driver, payload)
        await asyncio.sleep(0.5)

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
        rect = payload.get("refresh_rect") or payload.get("qr_rect")
        if isinstance(rect, dict):
            await self._click_rect_center(page_driver, rect)
            await asyncio.sleep(0.5)
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
      const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
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
          const text = normalize(el.innerText || el.textContent || '');
          const tag = String(el.tagName || '').toLowerCase();
          const role = el.getAttribute('role') || '';
          const className = String(el.className || '');
          const exact = text === wanted;
          const interactive = tag === 'button' || tag === 'a' || role === 'button';
          const tabLike = className.includes('ant-tabs-tab') || className.includes('ant-select-item');
          const area = Math.max(1, rect.width * rect.height);
          const score =
            (exact ? 0 : 1000) +
            (interactive || tabLike ? 0 : 100) +
            Math.min(text.length, 500) +
            Math.log(area);
          return {{ x: rect.x, y: rect.y, width: rect.width, height: rect.height, score }};
        }})
        .sort((a, b) => a.score - b.score || a.y - b.y || a.x - b.x);
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
# Publish flow via 小V猫 CatBridge task APIs
# ---------------------------------------------------------------------------

PUBLISH_TASK_TIMEOUT_SECONDS = 15 * 60
PUBLISH_TASK_POLL_INTERVAL_SECONDS = 3.0
PUBLISH_LOG_SUCCESS_STATUS = 2
PUBLISH_LOG_FAILED_STATUSES = {3, 4, 5}
PUBLISH_LOG_STATUS_LABELS = {
    0: "待发布",
    1: "正在发布",
    2: "已发布",
    3: "已暂停",
    4: "失败",
    5: "已取消",
    6: "本机定时等待",
}


def _catbridge_call_js(name: str, args: list[Any]) -> str:
    return f"""
    Promise.resolve().then(async () => {{
      try {{
        if (!(window.CatBridge && window.CatBridge.getCall)) {{
          return {{ ok: false, error: 'CatBridge unavailable' }};
        }}
        const callName = {json.dumps(name)};
        const args = {json.dumps(args, ensure_ascii=False, default=str)};
        const value = await window.CatBridge.getCall.call(window.CatBridge, callName, ...args);
        return {{ ok: true, value }};
      }} catch (error) {{
        return {{
          ok: false,
          error: String(error && (error.stack || error.message || error)),
        }};
      }}
    }})
    """


async def _catbridge_call(driver: XiaoVmaoDriver, name: str, *args: Any) -> Any:
    payload = await driver.evaluate(_catbridge_call_js(name, list(args)), await_promise=True)
    if not isinstance(payload, dict):
        raise XiaoVmaoUnavailableError(f"小V猫 CatBridge {name} 返回格式异常")
    if not payload.get("ok"):
        raise XiaoVmaoUnavailableError(payload.get("error") or f"小V猫 CatBridge {name} 调用失败")
    return payload.get("value")


def _new_local_id() -> str:
    return f"l_cutagent_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def _publish_batch_source(payload: PublishPayload) -> str:
    raw_name = (payload.case_name or payload.title or "任务分发").strip() or "任务分发"
    name = raw_name.replace("\n", " ")[:32]
    return f"Cutagent_{name}_{time.strftime('%m-%d %H:%M:%S')}"


def _file_url(uri: str | None) -> str | None:
    if not uri:
        return None
    if uri.startswith(("file://", "http://", "https://")):
        return uri
    if uri.startswith("/"):
        return f"file://{uri}"
    return uri


def _publish_timing(payload: PublishPayload) -> dict[str, Any]:
    if payload.scheduled_at is None:
        return {"type": 1}
    return {
        "type": 2,
        "time": payload.scheduled_at.isoformat(),
        "autoReset": False,
    }


def _normalised_tags(payload: PublishPayload, limit: int = 10) -> list[str]:
    return [tag.strip().lstrip("#") for tag in payload.tags if tag.strip()][:limit]


def _platform_form_payload(
    *,
    platform_key: str,
    title: str,
    description: str,
    tags: list[str],
    cover: str | None,
    timing: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    base: dict[str, Any] = {"CAT_timing": timing}
    if cover:
        base["CAT_cover"] = cover

    if platform_key == "Douyin":
        title_value = title[:30]
        desc_value = description[:1000]
        form_data = {
            **base,
            "title": title_value,
            "item_title": title_value,
            "text": desc_value,
            "tags": [{"cid": tag, "cha_name": tag} for tag in tags[:5]],
        }
        return form_data, {
            "title": title_value,
            "desc": desc_value,
            "timing": timing.get("time"),
            "commentStatus": False,
        }

    if platform_key == "KuaiShou":
        desc_value = description or title
        form_data = {
            **base,
            "CAT_desc": desc_value,
            "CAT_tags": tags[:10],
        }
        return form_data, {
            "title": title,
            "desc": desc_value,
            "timing": timing.get("time"),
        }

    if platform_key == "Channels":
        title_value = title[:64]
        desc_value = description or title
        form_data = {
            **base,
            "title": title_value,
            "CAT_shortTitle": title[:16],
            "CAT_desc": desc_value,
            "topics": tags[:10],
        }
        return form_data, {
            "title": title_value,
            "shortTitle": title[:16],
            "desc": desc_value,
            "timing": timing.get("time"),
        }

    # 小红书：小V猫 UI 仍以 CAT_* common fields + title/desc 组织发布表单。
    title_value = title[:20]
    desc_value = description
    form_data = {
        **base,
        "title": title_value,
        "desc": desc_value,
        "CAT_desc": desc_value,
        "CAT_tags": tags[:10],
    }
    return form_data, {
        "title": title_value,
        "desc": desc_value,
        "timing": timing.get("time"),
    }


def _build_publish_task(
    *,
    payload: PublishPayload,
    platform: str,
    account_uid: str,
    batch_source: str,
) -> dict[str, Any]:
    platform_key = XIAOVMAO_PLATFORM_KEY_MAP[platform]
    title = (payload.title or "未命名发布").strip() or "未命名发布"
    description = payload.description or ""
    tags = _normalised_tags(payload)
    cover = _file_url(payload.cover_uri)
    timing = _publish_timing(payload)
    form_data, task_fields = _platform_form_payload(
        platform_key=platform_key,
        title=title,
        description=description,
        tags=tags,
        cover=cover,
        timing=timing,
    )
    local_id = _new_local_id()
    return {
        "uid": account_uid,
        "videoPath": payload.video_uri,
        "videoDuration": None,
        "platform": platform_key,
        "localId": local_id,
        "status": 0,
        "postType": 1,
        "publishType": timing.get("type") or 1,
        "resetTimeout": False,
        "originCover": cover,
        "isVertical": False,
        "isCustomCover": bool(cover),
        "images": [],
        "batchSource": batch_source,
        "formData": form_data,
        **task_fields,
    }


async def _create_publish_task(driver: XiaoVmaoDriver, task: dict[str, Any]) -> None:
    await _catbridge_call(driver, "Models.PublishLog.bulkCreateWithStat", [task])
    await _catbridge_call(driver, "PublishController.startPublishTask")


def _extract_publish_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("list", "records", "rows", "data"):
        items = value.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
        if isinstance(items, dict):
            nested = _extract_publish_records(items)
            if nested:
                return nested
    return []


def _record_local_id(record: dict[str, Any]) -> str | None:
    value = record.get("localId") or record.get("local_id")
    return str(value) if value else None


def _record_status(record: dict[str, Any]) -> int | None:
    value = record.get("status")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_error(record: dict[str, Any]) -> str | None:
    for key in ("error", "errorMessage", "message", "reason", "failReason"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def _record_url(record: dict[str, Any]) -> str | None:
    for key in ("url", "postUrl", "shareUrl", "workUrl"):
        value = record.get(key)
        if value:
            return str(value)
    return None


async def _query_publish_record(driver: XiaoVmaoDriver, task: dict[str, Any]) -> dict[str, Any] | None:
    queries = [
        (
            {"current": 1, "pageSize": 50},
            {"kw": task["localId"]},
        ),
        (
            {"current": 1, "pageSize": 50},
            {
                "uids": [task["uid"]],
                "platform": task["platform"],
                "batchSource": task["batchSource"],
            },
        ),
    ]
    for pager, filters in queries:
        value = await _catbridge_call(driver, "Models.PublishLog.queryAll", pager, filters)
        for record in _extract_publish_records(value):
            if _record_local_id(record) == task["localId"]:
                return record
    return None


def _status_label(status: int | None) -> str:
    if status is None:
        return "未知"
    return PUBLISH_LOG_STATUS_LABELS.get(status, str(status))


async def _wait_for_publish_record(
    driver: XiaoVmaoDriver,
    task: dict[str, Any],
    *,
    scheduled: bool,
) -> dict[str, Any]:
    deadline = time.monotonic() + PUBLISH_TASK_TIMEOUT_SECONDS
    last_record: dict[str, Any] | None = None
    while True:
        record = await _query_publish_record(driver, task)
        if record:
            last_record = record
            status = _record_status(record)
            if status == PUBLISH_LOG_SUCCESS_STATUS:
                return record
            if scheduled and status in {0, 6}:
                return record
            if status in PUBLISH_LOG_FAILED_STATUSES:
                label = _status_label(status)
                detail = _record_error(record)
                raise XiaoVmaoUnavailableError(f"小V猫发布任务{label}: {detail or '未返回失败原因'}")
        if time.monotonic() >= deadline:
            status = _record_status(last_record or {}) if last_record else None
            detail = f"，最后状态：{_status_label(status)}" if last_record else "，未查到 PublishLog 记录"
            raise XiaoVmaoUnavailableError(f"小V猫发布任务超时{detail}")
        await asyncio.sleep(PUBLISH_TASK_POLL_INTERVAL_SECONDS)


def _success_result(
    *,
    platform: str,
    account_uid: str,
    task: dict[str, Any],
    record: dict[str, Any],
    scheduled: bool,
) -> dict[str, Any]:
    status = _record_status(record)
    result: dict[str, Any] = {
        "platform": platform,
        "account": account_uid,
        "success": True,
        "scheduled": scheduled,
        "external_task_id": task["localId"],
        "xiaovmao_status": status,
        "xiaovmao_status_label": _status_label(status),
    }
    url = _record_url(record)
    if url:
        result["url"] = url
    return result


async def _drive_publish(
    driver: XiaoVmaoDriver, payload: PublishPayload, accounts: list[PlatformAccount]
) -> PublishOutcome:
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

    batch_source = _publish_batch_source(payload)
    task_results: list[dict[str, Any]] = []
    error_messages: list[str] = []
    external_task_ids: list[str] = []
    scheduled = payload.scheduled_at is not None
    for item in selected:
        platform = item["platform"]
        account_uid = item["account"]
        task = _build_publish_task(
            payload=payload,
            platform=platform,
            account_uid=account_uid,
            batch_source=batch_source,
        )
        try:
            await _create_publish_task(driver, task)
            record = await _wait_for_publish_record(driver, task, scheduled=scheduled)
            task_results.append(
                _success_result(
                    platform=platform,
                    account_uid=account_uid,
                    task=task,
                    record=record,
                    scheduled=scheduled,
                )
            )
            external_task_ids.append(task["localId"])
        except XiaoVmaoUnavailableError as exc:
            message = str(exc)
            error_messages.append(message)
            task_results.append(
                {
                    "platform": platform,
                    "account": account_uid,
                    "success": False,
                    "external_task_id": task["localId"],
                    "error": message,
                }
            )

    all_results = task_results + failures
    for failure in failures:
        if failure.get("error"):
            error_messages.append(str(failure["error"]))
    success = bool(task_results) and all(result.get("success") for result in all_results)
    return PublishOutcome(
        success=success,
        adapter_id=XIAOVMAO_ADAPTER_ID,
        external_task_id=",".join(external_task_ids) if external_task_ids else None,
        results=all_results,
        error_message="; ".join(error_messages) if error_messages else None,
        scheduled=scheduled,
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
