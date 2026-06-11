from __future__ import annotations

import os

import anyio
import httpx
from functools import partial
from urllib.parse import urlsplit
import warnings


os.environ.setdefault("CUTAGENT_STORAGE_BACKEND", "memory")


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
            anyio.run(self._lifespan_cm.__aexit__, exc_type, exc, tb)
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

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                response_started["status"] = message["status"]
                response_started["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))

        await self.app(scope, receive, send)
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
