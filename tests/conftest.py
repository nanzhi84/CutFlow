from __future__ import annotations

import os
import atexit
import shutil
import tempfile

_TEST_OBJECTSTORE_PATH = tempfile.mkdtemp(prefix="cutagent-test-objstore-")
os.environ.setdefault("CUTAGENT_LOCAL_OBJECTSTORE_PATH", _TEST_OBJECTSTORE_PATH)
atexit.register(shutil.rmtree, _TEST_OBJECTSTORE_PATH, ignore_errors=True)

# Isolate the secret store to an empty per-session directory so a developer's real
# armed secrets under ``.data/secrets`` never leak into the suite (which would make
# real providers resolve as "active" and shadow the sandbox path -> local-only
# golden/provider false failures). This mirrors CI, which has no real secrets.
_TEST_SECRET_STORE_PATH = tempfile.mkdtemp(prefix="cutagent-test-secrets-")
os.environ.setdefault("CUTAGENT_SECRET_STORE_DIR", _TEST_SECRET_STORE_PATH)
atexit.register(shutil.rmtree, _TEST_SECRET_STORE_PATH, ignore_errors=True)

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


os.environ.setdefault("CUTAGENT_STORAGE_BACKEND", "sqlalchemy")
# The whole suite runs against a REAL Postgres database (the memory storage
# backend has been removed). Default to a throwaway `cutagent_test` database so a
# developer's dev `cutagent` DB is never truncated; CI and ci_gate.sh override
# CUTAGENT_DATABASE_URL explicitly. The per-test isolation fixture TRUNCATEs all
# tables, so this MUST point at a disposable database.
os.environ.setdefault(
    "CUTAGENT_DATABASE_URL",
    "postgresql+psycopg://cutagent:cutagent@127.0.0.1:55432/cutagent_test",
)
os.environ.setdefault("CUTAGENT_DISABLE_BACKGROUND_DISPATCHER", "1")
# Point the 小V猫 CDP probe at a closed port so publish-login/account tests resolve
# deterministically to "小V猫 unavailable" (matching CI, which has no 小V猫 desktop
# app). Without this, a developer running a live 小V猫/CatBridge on :9222 turns the
# probe into a real call -> local-only false failures.
os.environ.setdefault("CUTAGENT_XIAOVMAO_CDP_PORT", "1")
# Tests build many short-lived apps (TestClient(create_app())), each with its own
# engine pool. Keep per-engine pools small so a long suite — and several suites
# sharing one Postgres server — never exhaust max_connections.
os.environ.setdefault("CUTAGENT_DB_POOL_SIZE", "2")
os.environ.setdefault("CUTAGENT_DB_MAX_OVERFLOW", "3")
# The golden / fallback fixtures deliberately run without armed real provider
# secrets and rely on the seeded sandbox providers. Production defaults to no
# sandbox fallback (fail loudly when no real provider is armed); the suite opts
# in so those fixtures keep exercising the sandbox path.
os.environ.setdefault("CUTAGENT_ALLOW_SANDBOX_FALLBACK", "1")
# The production default publish adapter is the 小V猫 CDP adapter, which honestly
# fails without a live 小V猫 (no desktop app in CI). The suite exercises the publish
# *flow* deterministically via the sandbox adapter; production selects xiaovmao.cdp
# by leaving this unset.
os.environ.setdefault("CUTAGENT_PUBLISH_ADAPTER", "sandbox.publish")


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

    def __exit__(self, _exc_type, _exc, _tb) -> None:
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

    def __exit__(self, _exc_type, _exc, _tb) -> None:
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


def pytest_configure(config) -> None:
    # The suite shares one throwaway `cutagent_test` database and isolates tests
    # via an autouse TRUNCATE-and-reseed step; pytest-xdist would let parallel
    # workers TRUNCATE each other's data mid-test. Fail loudly, never produce
    # flaky garbage. (issue #87 / A5 — see tests/CLAUDE.md)
    if getattr(config, "workerinput", None) is not None or (
        config.pluginmanager.hasplugin("xdist")
        and getattr(config.option, "numprocesses", None)
    ):
        raise pytest.UsageError(
            "This suite must run serially: it shares one cutagent_test database "
            "with TRUNCATE+reseed isolation, which pytest-xdist (-n/--dist) "
            "corrupts. Remove -n/--dist (see tests/CLAUDE.md)."
        )
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


# ---------------------------------------------------------------------------
# Real-Postgres test harness (the memory storage backend has been removed).
#
# Every test runs against a real Postgres database. Schema is migrated to head
# once per session; base seed rows (users/registration codes/providers/media)
# are inserted once; each test gets a clean database via a TRUNCATE-and-reseed
# isolation step so tests never leak state into one another.
# ---------------------------------------------------------------------------
from sqlalchemy import event as _sa_event  # noqa: E402
from sqlalchemy.engine import Engine as _SAEngine  # noqa: E402

# Whether the current test checked out a database connection from *any* engine
# (the app builds its own engine, so we listen on the Engine base class). Tests
# that never touch the database skip the truncate-and-reseed reset entirely and
# therefore do not require a running Postgres. The schema itself is provisioned
# out-of-band (CI / ci_gate.sh run scripts/bootstrap_database.py before pytest;
# locally the throwaway cutagent_test database is bootstrapped once).
_DB_TOUCHED = {"flag": False}


@_sa_event.listens_for(_SAEngine, "checkout")
def _mark_db_touched(*_args, **_kwargs):
    _DB_TOUCHED["flag"] = True


def _seed_base(session_factory) -> None:
    """(Re)insert the deterministic base seed. Idempotent."""
    from packages.core.storage.object_store import get_object_store
    from packages.core.storage.seed import seed_database
    from packages.core.storage.seed_media import seed_media_assets

    with session_factory() as session:
        seed_database(session)
        seed_media_assets(session, get_object_store())
        session.commit()


@pytest.fixture(scope="session")
def _db_engine():
    """Session-scoped engine used only by the isolation reset (lazily created)."""
    from packages.core.storage.database import create_database_engine

    engine = create_database_engine()
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _db_isolation(_db_engine):
    """Reset the database after any test that touched it.

    Autouse + dirty-detection: every test starts with the flag cleared; only
    tests that actually opened a DB connection trigger a truncate-and-reseed in
    teardown, so the next test sees the clean seeded baseline. Pure-logic tests
    never connect, so they pay nothing (the engine is created but never dialed).
    """
    _DB_TOUCHED["flag"] = False
    yield
    if not _DB_TOUCHED["flag"]:
        return
    from sqlalchemy import text

    from packages.core.storage.database import Base, create_session_factory

    table_list = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))
    _seed_base(create_session_factory(_db_engine))


@pytest.fixture
def db_session_factory(_db_engine):
    """A real Postgres ``sessionmaker`` bound to the test database."""
    from packages.core.storage.database import create_session_factory

    return create_session_factory(_db_engine)


@pytest.fixture
def seeded_app():
    """A fresh FastAPI app wired to the SQLAlchemy backend + real database."""
    from apps.api.app import create_app

    return create_app()


@pytest.fixture
def client(seeded_app):
    """``TestClient`` (in-process ASGI) over a SQL-backed app."""
    from fastapi.testclient import TestClient

    with TestClient(seeded_app) as test_client:
        yield test_client
