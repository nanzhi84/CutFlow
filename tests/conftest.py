from __future__ import annotations

import os
import atexit
import shutil
import tempfile

_TEST_OBJECTSTORE_PATH = tempfile.mkdtemp(prefix="cutagent-test-objstore-")
os.environ.setdefault("CUTAGENT_LOCAL_OBJECTSTORE_PATH", _TEST_OBJECTSTORE_PATH)
atexit.register(shutil.rmtree, _TEST_OBJECTSTORE_PATH, ignore_errors=True)

import anyio
import httpx
import asyncio
import json
import queue
import threading
from functools import partial
from urllib.parse import urlsplit
import warnings

import pytest

from tests.fixtures.media import MediaFixtureFactory


os.environ.setdefault("CUTAGENT_STORAGE_BACKEND", "memory")
os.environ.setdefault("CUTAGENT_DISABLE_BACKGROUND_DISPATCHER", "1")
# The golden / fallback fixtures deliberately run without armed real provider
# secrets and rely on the seeded sandbox providers. Production defaults to no
# sandbox fallback (fail loudly when no real provider is armed); the suite opts
# in so those fixtures keep exercising the sandbox path.
os.environ.setdefault("CUTAGENT_ALLOW_SANDBOX_FALLBACK", "1")


class _ASGISyncTestClient:
    __test__ = False

    def __init__(
        self,
        app,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
        **_: object,
    ) -> None:
        self.app = app
        self.base_url = base_url
        self.raise_server_exceptions = raise_server_exceptions
        self.cookies = httpx.Cookies()
        self._lifespan_cm = None

    def __enter__(self):
        self._lifespan_cm = self.app.router.lifespan_context(self.app)
        anyio.run(self._lifespan_cm.__aenter__)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._lifespan_cm is not None:
            anyio.run(self._lifespan_cm.__aexit__, None, None, None)
            self._lifespan_cm = None

    async def _request_async(self, method: str, url: str, **kwargs) -> httpx.Response:
        full_url = url if url.startswith(("http://", "https://")) else f"{self.base_url.rstrip('/')}/{url.lstrip('/')}"
        request = httpx.Request(method, full_url, **kwargs)
        self.cookies.set_cookie_header(request)
        body = request.read()
        parsed = urlsplit(str(request.url))
        raw_headers = [
            (key.lower(), value)
            for key, value in request.headers.raw
            if key.lower() != b"host"
        ]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": parsed.scheme,
            "path": parsed.path or "/",
            "raw_path": (parsed.path or "/").encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "headers": raw_headers,
            "client": ("testclient", 50000),
            "server": (parsed.hostname or "testserver", parsed.port or 80),
            "root_path": "",
        }
        response_started: dict[str, object] = {}
        response_body = bytearray()
        request_sent = False
        response_complete = anyio.Event()

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await response_complete.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                response_started["status"] = message["status"]
                response_started["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    response_complete.set()

        try:
            await self.app(scope, receive, send)
        finally:
            if not response_complete.is_set():
                response_complete.set()
        response = httpx.Response(
            int(response_started.get("status", 500)),
            headers=[(key.decode("latin-1"), value.decode("latin-1")) for key, value in response_started.get("headers", [])],
            content=bytes(response_body),
            request=request,
        )
        self.cookies.update(response.cookies)
        return response

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return anyio.run(partial(self._request_async, method, url, **kwargs))

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def websocket_connect(self, url: str):
        return _ASGIWebSocketSession(self, url)


class _ASGIWebSocketSession:
    def __init__(self, client: _ASGISyncTestClient, url: str) -> None:
        self.client = client
        self.url = url
        self._app_to_client: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client_to_app: asyncio.Queue | None = None
        self._ready = threading.Event()

    def __enter__(self):
        self._thread = threading.Thread(target=lambda: anyio.run(self._run), daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        message = self._app_to_client.get(timeout=5)
        if message["type"] != "websocket.accept":
            raise RuntimeError(f"WebSocket was not accepted: {message}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._client_to_app = asyncio.Queue()
        self._ready.set()
        full_url = (
            self.url
            if self.url.startswith(("ws://", "wss://"))
            else f"{self.client.base_url.rstrip('/')}/{self.url.lstrip('/')}"
        )
        full_url = full_url.replace("http://", "ws://").replace("https://", "wss://", 1)
        parsed = urlsplit(full_url)
        headers = [(b"host", (parsed.netloc or "testserver").encode("latin-1"))]
        cookie_header = self.client.cookies.jar._cookies
        if cookie_header:
            cookie = "; ".join(
                f"{name}={morsel.value}"
                for domain in cookie_header.values()
                for path in domain.values()
                for name, morsel in path.items()
            )
            headers.append((b"cookie", cookie.encode("latin-1")))
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "scheme": parsed.scheme,
            "path": parsed.path or "/",
            "raw_path": (parsed.path or "/").encode("ascii"),
            "query_string": parsed.query.encode("ascii"),
            "headers": headers,
            "client": ("testclient", 50000),
            "server": (parsed.hostname or "testserver", parsed.port or 80),
            "root_path": "",
            "subprotocols": [],
        }
        connected = False

        async def receive():
            nonlocal connected
            if not connected:
                connected = True
                return {"type": "websocket.connect"}
            return await self._client_to_app.get()

        async def send(message):
            self._app_to_client.put(message)

        await self.client.app(scope, receive, send)

    def receive_json(self):
        while True:
            message = self._app_to_client.get(timeout=5)
            if message["type"] == "websocket.send":
                text = message.get("text")
                if text is None:
                    text = message.get("bytes", b"").decode("utf-8")
                return json.loads(text)
            if message["type"] == "websocket.close":
                raise RuntimeError(f"WebSocket closed: {message}")

    def send_json(self, payload) -> None:
        self._send_to_app({"type": "websocket.receive", "text": json.dumps(payload)})

    def close(self) -> None:
        self._send_to_app({"type": "websocket.disconnect", "code": 1000})

    def _send_to_app(self, message) -> None:
        if self._loop is None or self._client_to_app is None:
            return
        self._loop.call_soon_threadsafe(self._client_to_app.put_nowait, message)


def pytest_configure() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import fastapi.testclient
        import starlette.testclient

    async def inline_run_sync(func, *args, **kwargs):
        kwargs.pop("abandon_on_cancel", None)
        kwargs.pop("cancellable", None)
        kwargs.pop("limiter", None)
        return func(*args, **kwargs)

    anyio.to_thread.run_sync = inline_run_sync
    fastapi.testclient.TestClient = _ASGISyncTestClient
    starlette.testclient.TestClient = _ASGISyncTestClient


@pytest.fixture(scope="session")
def media_fixture_factory(tmp_path_factory):
    return MediaFixtureFactory(tmp_path_factory.mktemp("media-fixtures"))
