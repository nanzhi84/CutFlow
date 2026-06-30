from __future__ import annotations

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Callable, TypeVar

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from apps.api.common import (
    repository,
    request_id,
)
from packages.ai.gateway.provider_limiter import default_limiter_redis_degraded
from packages.core import contracts as c
from packages.core.config import validate_startup_settings
from packages.core.observability import metric_snapshot
from packages.core.storage.object_store import ObjectRef

_T = TypeVar("_T")


def _bounded_probe(fn: Callable[[], _T], timeout: float) -> _T:
    """Run a synchronous probe on a throwaway thread, abandoning it past ``timeout``.

    A blocking round-trip (an OSS HEAD, a TCP connect) cannot be interrupted in
    place, so we run it on a worker thread and stop waiting after the budget. On
    timeout the orphaned thread is left to finish (or die) on its own and a
    ``concurrent.futures.TimeoutError`` is raised; the request returns promptly so
    a hung dependency can't pin this public, unauthenticated endpoint. Shutdown is
    non-blocking for the same reason."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn).result(timeout=timeout)
    finally:
        pool.shutdown(wait=False)


def _tcp_connect(address: str, timeout: float) -> None:
    """Open and immediately close a TCP connection to ``host:port`` (liveness only)."""
    host, _, port = address.rpartition(":")
    with socket.create_connection((host or "127.0.0.1", int(port)), timeout=timeout):
        pass

def health(request: Request) -> c.OkResponse:

    return c.OkResponse(request_id=request_id())


def readiness(request: Request) -> JSONResponse:
    """Operational readiness probe (not part of the OpenAPI contract).

    In production this surfaces any unsafe-config preflight findings as a 503 so
    an orchestrator never routes traffic to a misconfigured replica. Outside
    production the preflight is a no-op, so this reflects a plain liveness-ish
    ready.

    When ``CUTAGENT_REDIS_REQUIRED`` is set (#81), the fail-closed contract is
    enforced here: if Redis is required but any Redis-backed singleton (event
    fan-out hub, event-stream token store, provider rate limiter) has fallen
    back to its per-process degraded mode, this replica is reported not-ready
    (503) so the orchestrator drains it rather than serving with cross-replica
    guarantees silently broken.
    """
    settings = request.app.state.settings
    issues = validate_startup_settings(settings)
    redis_degradations: list[str] = []
    if settings.redis_required:
        state = request.app.state
        for name in ("event_hub", "event_tokens"):
            component = getattr(state, name, None)
            checker = getattr(component, "is_redis_degraded", None)
            if checker is not None and checker():
                redis_degradations.append(name)
        if default_limiter_redis_degraded():
            redis_degradations.append("provider_limiter")
    not_ready = bool(issues) or bool(redis_degradations)
    payload = {
        "status": "not_ready" if not_ready else "ready",
        "environment": settings.deployment.environment,
        "preflight_issues": issues,
        "redis_required": settings.redis_required,
        "redis_degradations": redis_degradations,
        "request_id": request_id(),
    }
    return JSONResponse(status_code=503 if not_ready else 200, content=payload)


def metrics(request: Request) -> str:

    return metric_snapshot(repository(request))


def network_diagnostics(request: Request) -> JSONResponse:
    """Per-dependency segment health for the Web→VPS→Mac→OSS topology (issue #77).

    Live-probes every hop with per-hop latency: Postgres + Redis (cheap local
    pings), the OSS HEAD round-trip (Mac→object-store, the segment that actually
    fails in production), and a Temporal connect probe when that runtime is
    active. Every probe is time-bounded by ``CUTAGENT_HEALTH_PROBE_TIMEOUT`` and
    exception-safe — this is a public, unauthenticated endpoint, so a slow
    dependency must never be able to hang or DoS it. Operational only — not part
    of the OpenAPI contract.
    """
    settings = request.app.state.settings
    probe_timeout = settings.health_probe_timeout_seconds
    hops: dict[str, dict] = {}

    # Postgres hop. The SQL session factory is always mounted (PR#72: the SQL
    # backend is mandatory), so probe it unconditionally — the round-trip itself
    # stays exception-safe.
    started = time.monotonic()
    try:
        with request.app.state.sqlalchemy_session_factory() as session:
            session.execute(text("SELECT 1"))
        hops["postgres"] = {
            "status": "ok",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except Exception as exc:  # noqa: BLE001 — diagnostics must not raise.
        hops["postgres"] = {"status": "failed", "error": str(exc)}

    if settings.redis_url:
        started = time.monotonic()
        try:
            import redis

            client = redis.Redis.from_url(
                settings.redis_url, socket_connect_timeout=0.5, socket_timeout=1.0
            )
            client.ping()
            hops["redis"] = {
                "status": "ok",
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }
        except Exception as exc:  # noqa: BLE001
            hops["redis"] = {"status": "failed", "error": str(exc)}
    else:
        hops["redis"] = {"status": "not_configured"}

    hops["oss"] = _probe_oss(request, settings, probe_timeout)
    hops["temporal"] = _probe_temporal(settings, probe_timeout)

    overall = "degraded" if any(h.get("status") == "failed" for h in hops.values()) else "ok"
    return JSONResponse(
        status_code=200 if overall == "ok" else 503,
        content={"status": overall, "hops": hops, "request_id": request_id()},
    )


def _probe_oss(request: Request, settings, probe_timeout: float) -> dict:
    """Time-bounded HEAD round-trip against the configured object-store bucket.

    Probes a deliberately non-existent key (``exists`` is a cheap HEAD): a healthy
    backend answers "absent" without raising, which proves the Mac→OSS hop is
    reachable and measures its latency. Bounded + exception-safe per the endpoint's
    DoS contract."""
    backend = settings.object_store.backend
    bucket = settings.object_store.bucket
    key = "_cutagent_healthcheck/network-probe"
    scheme = "s3" if backend == "s3" else "local"
    probe_ref = ObjectRef(bucket=bucket, key=key, uri=f"{scheme}://{bucket}/{key}")
    store = request.app.state.object_store
    started = time.monotonic()
    try:
        _bounded_probe(lambda: store.exists(probe_ref), probe_timeout)
    except FuturesTimeout:
        return {"status": "failed", "error": f"probe timed out after {probe_timeout}s"}
    except Exception as exc:  # noqa: BLE001 — diagnostics must not raise.
        return {"status": "failed", "error": str(exc)}
    return {
        "status": "ok",
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
        "backend": backend,
    }


def _probe_temporal(settings, probe_timeout: float) -> dict:
    """Time-bounded TCP connect probe to the Temporal frontend.

    Only meaningful when the Temporal runtime is active; under the local runtime
    there is no Temporal to reach, so the hop is reported as skipped (not a
    failure)."""
    if settings.workflow.runtime != "temporal":
        return {"status": "skipped", "runtime": settings.workflow.runtime}
    address = settings.workflow.temporal_address
    started = time.monotonic()
    try:
        _bounded_probe(lambda: _tcp_connect(address, probe_timeout), probe_timeout)
    except FuturesTimeout:
        return {"status": "failed", "error": f"probe timed out after {probe_timeout}s"}
    except Exception as exc:  # noqa: BLE001 — diagnostics must not raise.
        return {"status": "failed", "error": str(exc)}
    return {
        "status": "ok",
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
        "runtime": "temporal",
        "address": address,
    }
