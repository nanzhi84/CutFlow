"""Production startup preflight (issue #66).

``.env.example`` ships dev-friendly defaults (open registration, local-dev salt,
seeded local admin, local object store, no Redis, ...). Those are fine for local
dev but dangerous in production. ``validate_startup_settings`` aggregates the
unsafe-in-production settings into one fail-closed report instead of letting each
land as a separate, easily-missed surprise at runtime.

Checks run ONLY when ``settings.deployment.environment == "production"``; outside
production the function returns ``[]`` so local dev / tests are never blocked.
"""

from __future__ import annotations

from packages.core.config.settings import Settings, sandbox_fallback_allowed

_LOCAL_DEV_REGISTRATION_SALT = "local-dev-registration-code-salt"
_LOCAL_CDP_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


def validate_startup_settings(settings: Settings) -> list[str]:
    """Return the list of production-unsafe configuration findings.

    An empty list means the configuration is safe to start in production. The
    caller decides what to do with a non-empty list (the API/worker fail closed;
    ``scripts/preflight.py`` reports and exits non-zero).
    """
    if not settings.deployment.is_production:
        return []

    issues: list[str] = []

    def add(code: str, message: str) -> None:
        issues.append(f"{code}: {message}")

    # --- persistence -------------------------------------------------------
    if settings.storage.backend not in {"sqlalchemy", "postgres"}:
        add(
            "storage_backend",
            f"storage backend must be sqlalchemy/postgres in production, got "
            f"{settings.storage.backend!r} (set CUTAGENT_STORAGE_BACKEND).",
        )
    if settings.storage.backend in {"sqlalchemy", "postgres"} and not settings.storage.database_url:
        add(
            "database_url",
            "CUTAGENT_DATABASE_URL must be set for the SQLAlchemy/Postgres backend.",
        )

    # --- object store ------------------------------------------------------
    if settings.deployment.replica_count > 1 and settings.object_store.backend != "s3":
        add(
            "durable_object_store",
            "durable object store must be s3/MinIO when replica_count > 1, got "
            f"{settings.object_store.backend!r} (set CUTAGENT_OBJECTSTORE_BACKEND=s3).",
        )
    if (
        settings.workflow.runtime == "temporal"
        and settings.object_store.tiered
        and settings.object_store.ephemeral.backend != "s3"
    ):
        add(
            "ephemeral_object_store",
            "Temporal runtime requires a shared (s3/MinIO) ephemeral object store; "
            f"got {settings.object_store.ephemeral.backend!r} "
            "(set CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND=s3).",
        )

    # --- auth / registration ----------------------------------------------
    if settings.auth.registration_open:
        add(
            "registration_open",
            "public registration must be closed in production "
            "(set CUTAGENT_REGISTRATION_OPEN=false).",
        )
    if settings.auth.registration_code_salt == _LOCAL_DEV_REGISTRATION_SALT:
        add(
            "registration_code_salt",
            "registration-code salt is still the local-dev default "
            "(set CUTAGENT_REGISTRATION_CODE_SALT).",
        )
    if settings.auth.seed_local_auth:
        add(
            "seed_local_auth",
            "local auth seed (hardcoded admin/viewer credentials) must be disabled "
            "in production (set CUTAGENT_SEED_LOCAL_AUTH=false).",
        )
    if settings.auth.cookie_secure is not True:
        add(
            "cookie_secure",
            "session cookie Secure flag must be explicitly enabled in production "
            "(set CUTAGENT_AUTH_COOKIE_SECURE=true).",
        )

    # --- multi-replica coordination ---------------------------------------
    if settings.deployment.replica_count > 1 and not settings.redis_url:
        add(
            "redis_required",
            "replica_count > 1 requires a shared Redis for fanout / stream tokens / "
            "provider limiter (set CUTAGENT_REDIS_URL).",
        )

    # --- provider safety ---------------------------------------------------
    if not settings.providers.enforce_host_allowlist:
        add(
            "provider_host_allowlist",
            "provider host allowlist second-check must be enabled in production "
            "(set CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST=1).",
        )
    if sandbox_fallback_allowed():
        add(
            "sandbox_fallback",
            "sandbox provider fallback must be disabled in production "
            "(unset CUTAGENT_ALLOW_SANDBOX_FALLBACK).",
        )

    # --- publishing --------------------------------------------------------
    if (
        settings.publishing.xiaovmao_cdp_host.strip().lower() in _LOCAL_CDP_HOSTS
        and not settings.deployment.publishing_local_proxy
    ):
        add(
            "publishing_cdp_host",
            "publishing CDP host is a machine-local address; set a real host or, if "
            "the deploy host runs the publishing proxy, set CUTAGENT_PUBLISHING_LOCAL_PROXY=1.",
        )

    return issues


def format_preflight_report(issues: list[str]) -> str:
    """Human-readable multi-line report for ``scripts/preflight.py`` / logs."""
    if not issues:
        return "Production preflight passed: no unsafe settings detected."
    lines = [f"Production preflight FAILED with {len(issues)} unsafe setting(s):"]
    lines.extend(f"  - {issue}" for issue in issues)
    return "\n".join(lines)
