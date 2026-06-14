"""In-process sliding-window brute-force limiter for auth (R2).

Tracks failed-login and registration attempts per ``(scope, client, identifier)``
key in process memory and rejects once the configured threshold inside the
window is exceeded. Thresholds/windows come from ``AuthSettings``
(``max_login_attempts`` / ``login_window_minutes`` / ``max_registration_attempts``
/ ``registration_window_minutes``) read freshly per call via ``build_settings``.

TODO(multi-worker): this limiter is PROCESS-LOCAL. Under multiple Gunicorn/
Uvicorn workers each process keeps its own counters, so the effective global
threshold is ``workers * max_attempts`` and an attacker who is load-balanced
across workers gets more tries. There is no shared attempts store in this repo
(no Redis, and adding a DB table was explicitly out of scope for the auth lane),
so this is the pragmatic in-process choice. For a hard global limit, back this
with Redis or a DB attempts table behind the same ``LoginRateLimiter`` /
``RegistrationRateLimiter`` interface.

A module-level :func:`reset` clears all buckets; tests call it in setup so
per-``TestClient`` runs do not leak attempt state into each other.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from packages.core.config import build_settings
from packages.core.contracts import ErrorCode, utcnow
from packages.core.workflow import NodeExecutionError

# Hard floors so a misconfigured (e.g. 0) env value never disables the limiter
# or sets a zero-length window. Mirrors the old repo's max(...) clamps.
_MIN_ATTEMPTS = 1
_MIN_WINDOW_MINUTES = 1


@dataclass
class _SlidingWindowLimiter:
    """Per-key sliding-window counter.

    ``buckets`` maps a composite key to the timestamps of recorded attempts
    within the active window; entries older than the window are pruned lazily on
    each access. Guarded by a lock so concurrent requests cannot race the
    read-modify-write."""

    _buckets: dict[str, list[datetime]] = field(
        default_factory=lambda: defaultdict(list)
    )
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def _prune(self, key: str, *, now: datetime, window: timedelta) -> list[datetime]:
        attempts = [item for item in self._buckets.get(key, []) if now - item < window]
        if attempts:
            self._buckets[key] = attempts
        else:
            self._buckets.pop(key, None)
        return attempts

    def check(self, key: str, *, max_attempts: int, window_minutes: int) -> bool:
        """Return True if a further attempt is allowed (under the threshold)."""
        now = utcnow()
        window = timedelta(minutes=max(_MIN_WINDOW_MINUTES, window_minutes))
        with self._lock:
            attempts = self._prune(key, now=now, window=window)
            return len(attempts) < max(_MIN_ATTEMPTS, max_attempts)

    def record(self, key: str, *, window_minutes: int) -> None:
        """Record one attempt against ``key`` (used to count toward the limit)."""
        now = utcnow()
        window = timedelta(minutes=max(_MIN_WINDOW_MINUTES, window_minutes))
        with self._lock:
            attempts = self._prune(key, now=now, window=window)
            attempts.append(now)
            self._buckets[key] = attempts

    def clear_key(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# Two independent buckets: failed logins vs registration attempts.
_login_limiter = _SlidingWindowLimiter()
_registration_limiter = _SlidingWindowLimiter()


def _normalize(value: str | None) -> str:
    return (value or "unknown").strip().lower()


def _login_key(client_id: str | None, identifier: str) -> str:
    return f"login:{_normalize(client_id)}:{_normalize(identifier)}"


def _registration_key(client_id: str | None) -> str:
    return f"register:{_normalize(client_id)}"


def check_login_rate_limit(client_id: str | None, identifier: str) -> None:
    """Raise if the login attempt is currently throttled.

    Uses ``ErrorCode.auth_invalid_credentials`` (401) with a distinct message so
    callers cannot distinguish throttling from a bad password (anti-enumeration)
    and no new ErrorCode is needed."""
    settings = build_settings().auth
    if not _login_limiter.check(
        _login_key(client_id, identifier),
        max_attempts=settings.max_login_attempts,
        window_minutes=settings.login_window_minutes,
    ):
        raise NodeExecutionError(
            ErrorCode.auth_invalid_credentials,
            "Too many failed login attempts; try again later.",
        )


def record_login_failure(client_id: str | None, identifier: str) -> None:
    settings = build_settings().auth
    _login_limiter.record(
        _login_key(client_id, identifier),
        window_minutes=settings.login_window_minutes,
    )


def record_login_success(client_id: str | None, identifier: str) -> None:
    """Clear the failure counter on a successful login."""
    _login_limiter.clear_key(_login_key(client_id, identifier))


def check_registration_rate_limit(client_id: str | None) -> None:
    """Raise if registration from this client is currently throttled.

    Uses ``ErrorCode.validation_invalid_options`` (400) per the locked lane."""
    settings = build_settings().auth
    if not _registration_limiter.check(
        _registration_key(client_id),
        max_attempts=settings.max_registration_attempts,
        window_minutes=settings.registration_window_minutes,
    ):
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "Too many registration attempts; try again later.",
        )


def record_registration_attempt(client_id: str | None) -> None:
    settings = build_settings().auth
    _registration_limiter.record(
        _registration_key(client_id),
        window_minutes=settings.registration_window_minutes,
    )


def reset() -> None:
    """Clear ALL limiter state. Call in test setup to avoid cross-test leakage."""
    _login_limiter.reset()
    _registration_limiter.reset()
