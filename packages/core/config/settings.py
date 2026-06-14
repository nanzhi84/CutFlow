"""Central typed infrastructure configuration.

This module consolidates the INFRA knobs that were previously read via scattered
``os.getenv`` calls across ``packages`` and ``apps`` into a single typed
``Settings`` contract (Pydantic v2, matching the genesis contract convention).

Design notes
------------
- **Infra only.** ``Settings`` carries deployment/runtime configuration
  (storage backend, object-store transport, temporal, database url, ffmpeg,
  secret-store location, registration policy). Provider SECRETS (API keys) stay
  in ``SecretStore`` / ``ProviderProfile`` and are deliberately NOT modelled
  here.
- **Read env at build time, not import time.** ``build_settings()`` reads
  ``os.environ`` afresh on every call and returns an immutable snapshot. There
  is intentionally NO cached module-level singleton: call sites resolve their
  config at call time (the same semantics the previous ``os.getenv`` calls had),
  and the API/worker thread a single snapshot through ``app.state`` for DI.
- **Defaults are byte-for-byte identical** to the defaults the replaced
  ``os.getenv(..., default)`` calls used, so this refactor is behaviour
  preserving.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ----------------------------------------------------------------------------
# Small env helpers (single point that knows how to read os.environ).
# ----------------------------------------------------------------------------


def _env_str(name: str, default: str) -> str:
    """Return the env var value, or ``default`` when unset."""
    value = os.getenv(name)
    return value if value is not None else default


def _env_int(name: str, default: int) -> int:
    """Return the env var parsed as int, or ``default`` when unset.

    Mirrors the previous ``int(os.getenv(name, str(default)))`` call sites: an
    unset var uses the default; a set-but-invalid var raises ``ValueError`` (the
    same loud failure the old code produced)."""
    value = os.getenv(name)
    return int(value) if value is not None else default


def _env_int_blank_default(name: str, default: int) -> int:
    """Like :func:`_env_int` but an unset OR blank var falls back to ``default``.

    Used for the DB connection-pool knobs, where a present-but-empty env value
    should behave as "not configured" rather than raising."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _default_ephemeral_local_path() -> str:
    """Default ephemeral object-store root under the OS temp dir.

    Matches the previous default in ``object_store_env._ephemeral_store_from_env``
    (``<tempdir>/cutagent-ephemeral``)."""
    return str(Path(tempfile.gettempdir()) / "cutagent-ephemeral")


# ----------------------------------------------------------------------------
# Nested infra settings groups.
# ----------------------------------------------------------------------------


class StorageSettings(BaseModel):
    """Persistence backend selection (``settings.storage.*``)."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_STORAGE_BACKEND: "sqlalchemy" | "postgres" | "memory".
    # Stored lower-cased, matching bootstrap.storage_backend().
    backend: str = "sqlalchemy"

    # CUTAGENT_DATABASE_URL: required (no default) when the SQLAlchemy backend is
    # active; None here means "unset" so call sites raise their explicit error.
    database_url: str | None = None

    # Engine connection-pool tuning for non-sqlite backends (sqlite ignores these).
    # CUTAGENT_DB_POOL_SIZE / _MAX_OVERFLOW / _POOL_RECYCLE / _POOL_TIMEOUT.
    pool_size: int = 5
    max_overflow: int = 10
    pool_recycle: int = 1800
    pool_timeout: int = 30


class S3TransportSettings(BaseModel):
    """boto3 transport / multipart tuning for an S3-compatible store.

    Defaults match the durable S3 store knobs previously read in
    ``object_store_env._durable_store_from_env``. Credentials (access/secret key)
    are infra connection params for MinIO/OSS, not provider API secrets, so they
    stay here (defaulting to empty, exactly as before)."""

    model_config = ConfigDict(frozen=True)

    endpoint_url: str = "http://127.0.0.1:9000"  # CUTAGENT_OBJECTSTORE_ENDPOINT
    access_key: str = ""  # CUTAGENT_OBJECTSTORE_ACCESS_KEY
    secret_key: str = ""  # CUTAGENT_OBJECTSTORE_SECRET_KEY
    region_name: str = "us-east-1"  # CUTAGENT_OBJECTSTORE_REGION
    addressing_style: str = "path"  # CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE
    multipart_threshold_mb: int = 8  # CUTAGENT_OBJECTSTORE_MULTIPART_THRESHOLD_MB
    multipart_chunk_mb: int = 8  # CUTAGENT_OBJECTSTORE_MULTIPART_CHUNK_MB
    max_concurrency: int = 4  # CUTAGENT_OBJECTSTORE_MAX_CONCURRENCY
    connect_timeout: int = 10  # CUTAGENT_OBJECTSTORE_CONNECT_TIMEOUT
    read_timeout: int = 120  # CUTAGENT_OBJECTSTORE_READ_TIMEOUT
    max_attempts: int = 5  # CUTAGENT_OBJECTSTORE_MAX_ATTEMPTS


class EphemeralObjectStoreSettings(BaseModel):
    """Ephemeral (scratch) tier of the tiered object store.

    Defaults match ``object_store_env._ephemeral_store_from_env``. The ephemeral
    bucket defaults to ``cutagent-ephemeral`` and is honored for BOTH the local and
    s3 backends (overridable via ``CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET``); this is
    the single source of truth for it. For the local backend the bucket is not part
    of the on-disk path, so the default behavior is unchanged."""

    model_config = ConfigDict(frozen=True)

    backend: str = "local"  # CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND
    bucket: str = "cutagent-ephemeral"  # CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET
    # CUTAGENT_OBJECTSTORE_EPHEMERAL_PATH (local backend root).
    local_path: str = Field(default_factory=_default_ephemeral_local_path)
    # s3 backend connection params (used only when backend == "s3").
    endpoint_url: str = "http://127.0.0.1:9000"  # CUTAGENT_EPHEMERAL_OBJECTSTORE_ENDPOINT
    access_key: str = ""  # CUTAGENT_EPHEMERAL_OBJECTSTORE_ACCESS_KEY
    secret_key: str = ""  # CUTAGENT_EPHEMERAL_OBJECTSTORE_SECRET_KEY
    region_name: str = "us-east-1"  # CUTAGENT_EPHEMERAL_OBJECTSTORE_REGION
    addressing_style: str = "path"  # CUTAGENT_EPHEMERAL_OBJECTSTORE_ADDRESSING_STYLE


class ObjectStoreSettings(BaseModel):
    """Durable object-store selection + tiering (``settings.object_store.*``)."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_OBJECTSTORE_TIERED: "1" enables the tiered store; "0" returns the
    # durable store alone. Modelled as a bool: True == "1" (the on default).
    tiered: bool = True
    backend: str = "local"  # CUTAGENT_OBJECTSTORE_BACKEND ("local" | "s3")
    bucket: str = "cutagent-local"  # CUTAGENT_OBJECTSTORE_BUCKET
    local_path: str = ".data/objectstore"  # CUTAGENT_LOCAL_OBJECTSTORE_PATH
    s3: S3TransportSettings = Field(default_factory=S3TransportSettings)
    ephemeral: EphemeralObjectStoreSettings = Field(
        default_factory=EphemeralObjectStoreSettings
    )


class WorkflowSettings(BaseModel):
    """Workflow runtime + Temporal connection (``settings.workflow.*``).

    Field names/defaults mirror the previous ``WorkflowRuntimeSettings`` so the
    workflow package can build its runtime settings straight from this group."""

    model_config = ConfigDict(frozen=True)

    runtime: Literal["local", "temporal"] = "local"  # CUTAGENT_WORKFLOW_RUNTIME
    temporal_address: str = "127.0.0.1:7233"  # CUTAGENT_TEMPORAL_ADDRESS
    temporal_namespace: str = "default"  # CUTAGENT_TEMPORAL_NAMESPACE
    temporal_task_queue: str = "cutagent-production"  # CUTAGENT_TEMPORAL_TASK_QUEUE


class AuthSettings(BaseModel):
    """Registration policy + registration-code hashing (``settings.auth.*``).

    NOTE: this is infra POLICY, not a secret. The registration-code salt is a
    local-dev convenience default; production overrides it via env. Actual
    provider API keys never live here."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_REGISTRATION_OPEN: "true" (case-insensitive) opens public
    # self-service registration.
    registration_open: bool = True
    # CUTAGENT_REGISTRATION_CODE_SALT: salt mixed into registration-code hashes.
    registration_code_salt: str = "local-dev-registration-code-salt"
    # Brute-force rate-limit knobs (R2). Sliding window per client/identifier.
    # CUTAGENT_AUTH_MAX_LOGIN_ATTEMPTS / _LOGIN_WINDOW_MINUTES /
    # _MAX_REGISTRATION_ATTEMPTS / _REGISTRATION_WINDOW_MINUTES.
    max_login_attempts: int = 8
    login_window_minutes: int = 15
    max_registration_attempts: int = 5
    registration_window_minutes: int = 60


class SecretStoreSettings(BaseModel):
    """Local secret-store location (``settings.secret_store.*``).

    This is only the on-disk DIRECTORY for the local dev secret envelope store;
    the secret material itself is owned by ``SecretStore``."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_SECRET_STORE_DIR
    dir: str = ".data/secrets"


class MediaSettings(BaseModel):
    """External media tooling locations (``settings.media.*``)."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_FFMPEG_BIN / CUTAGENT_FFPROBE_BIN: explicit binary paths. None
    # means "unset" so the resolver falls back to PATH / ~/.local/bin / name.
    ffmpeg_bin: str | None = None
    ffprobe_bin: str | None = None


class ApiSettings(BaseModel):
    """API process behaviour (``settings.api.*``)."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_DISABLE_BACKGROUND_DISPATCHER: "1" disables the in-process outbox
    # dispatcher background task (tests set this for determinism).
    disable_background_dispatcher: bool = False


class BalanceSettings(BaseModel):
    """Provider balance polling knobs (``settings.balance.*``).

    The pollers themselves never need a secret to be SAFE: a missing provider
    secret degrades to ``unconfigured`` rather than erroring. These settings only
    govern the OPTIONAL periodic background poller and the per-request HTTP
    timeout. The periodic poller is OFF by default — it is opt-in infra that
    fans out real (gated) provider calls, so it must be explicitly enabled per
    deployment."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_BALANCE_POLLER_ENABLED: "1" turns on the background periodic
    # poller. Off by default so no-key / test deployments never fan out.
    poller_enabled: bool = False
    # CUTAGENT_BALANCE_POLL_INTERVAL_SECONDS: seconds between periodic refreshes.
    poll_interval_seconds: int = 900
    # CUTAGENT_BALANCE_REQUEST_TIMEOUT_SECONDS: per-provider HTTP timeout.
    request_timeout_seconds: int = 10


class Settings(BaseModel):
    """Typed, immutable snapshot of all infrastructure configuration.

    Build instances with :func:`build_settings` (reads ``os.environ``); never
    instantiate a cached module-level singleton. The API/worker construct one
    snapshot and expose it via ``app.state.settings`` for dependency injection."""

    model_config = ConfigDict(frozen=True)

    storage: StorageSettings = Field(default_factory=StorageSettings)
    object_store: ObjectStoreSettings = Field(default_factory=ObjectStoreSettings)
    workflow: WorkflowSettings = Field(default_factory=WorkflowSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    secret_store: SecretStoreSettings = Field(default_factory=SecretStoreSettings)
    media: MediaSettings = Field(default_factory=MediaSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    balance: BalanceSettings = Field(default_factory=BalanceSettings)


# ----------------------------------------------------------------------------
# Builder: read os.environ once and assemble a Settings snapshot.
# ----------------------------------------------------------------------------


def build_settings() -> Settings:
    """Read ``os.environ`` and return an infra ``Settings`` snapshot.

    Called at runtime (not import time). Each call re-reads the environment, so
    tests that ``monkeypatch.setenv`` before invoking a factory observe the
    override — preserving the call-time semantics of the previous ``os.getenv``
    sites."""
    return Settings(
        storage=StorageSettings(
            backend=_env_str("CUTAGENT_STORAGE_BACKEND", "sqlalchemy").lower(),
            database_url=os.getenv("CUTAGENT_DATABASE_URL"),
            pool_size=_env_int_blank_default("CUTAGENT_DB_POOL_SIZE", 5),
            max_overflow=_env_int_blank_default("CUTAGENT_DB_MAX_OVERFLOW", 10),
            pool_recycle=_env_int_blank_default("CUTAGENT_DB_POOL_RECYCLE", 1800),
            pool_timeout=_env_int_blank_default("CUTAGENT_DB_POOL_TIMEOUT", 30),
        ),
        object_store=ObjectStoreSettings(
            tiered=os.getenv("CUTAGENT_OBJECTSTORE_TIERED", "1") != "0",
            backend=_env_str("CUTAGENT_OBJECTSTORE_BACKEND", "local").lower(),
            bucket=_env_str("CUTAGENT_OBJECTSTORE_BUCKET", "cutagent-local"),
            local_path=_env_str("CUTAGENT_LOCAL_OBJECTSTORE_PATH", ".data/objectstore"),
            s3=S3TransportSettings(
                endpoint_url=_env_str(
                    "CUTAGENT_OBJECTSTORE_ENDPOINT", "http://127.0.0.1:9000"
                ),
                access_key=_env_str("CUTAGENT_OBJECTSTORE_ACCESS_KEY", ""),
                secret_key=_env_str("CUTAGENT_OBJECTSTORE_SECRET_KEY", ""),
                region_name=_env_str("CUTAGENT_OBJECTSTORE_REGION", "us-east-1"),
                addressing_style=_env_str(
                    "CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE", "path"
                ),
                multipart_threshold_mb=_env_int(
                    "CUTAGENT_OBJECTSTORE_MULTIPART_THRESHOLD_MB", 8
                ),
                multipart_chunk_mb=_env_int("CUTAGENT_OBJECTSTORE_MULTIPART_CHUNK_MB", 8),
                max_concurrency=_env_int("CUTAGENT_OBJECTSTORE_MAX_CONCURRENCY", 4),
                connect_timeout=_env_int("CUTAGENT_OBJECTSTORE_CONNECT_TIMEOUT", 10),
                read_timeout=_env_int("CUTAGENT_OBJECTSTORE_READ_TIMEOUT", 120),
                max_attempts=_env_int("CUTAGENT_OBJECTSTORE_MAX_ATTEMPTS", 5),
            ),
            ephemeral=EphemeralObjectStoreSettings(
                backend=_env_str(
                    "CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND", "local"
                ).lower(),
                bucket=_env_str(
                    "CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET", "cutagent-ephemeral"
                ),
                local_path=_env_str(
                    "CUTAGENT_OBJECTSTORE_EPHEMERAL_PATH",
                    _default_ephemeral_local_path(),
                ),
                endpoint_url=_env_str(
                    "CUTAGENT_EPHEMERAL_OBJECTSTORE_ENDPOINT", "http://127.0.0.1:9000"
                ),
                access_key=_env_str("CUTAGENT_EPHEMERAL_OBJECTSTORE_ACCESS_KEY", ""),
                secret_key=_env_str("CUTAGENT_EPHEMERAL_OBJECTSTORE_SECRET_KEY", ""),
                region_name=_env_str(
                    "CUTAGENT_EPHEMERAL_OBJECTSTORE_REGION", "us-east-1"
                ),
                addressing_style=_env_str(
                    "CUTAGENT_EPHEMERAL_OBJECTSTORE_ADDRESSING_STYLE", "path"
                ),
            ),
        ),
        workflow=WorkflowSettings(
            runtime=_env_str("CUTAGENT_WORKFLOW_RUNTIME", "local").lower(),
            temporal_address=_env_str("CUTAGENT_TEMPORAL_ADDRESS", "127.0.0.1:7233"),
            temporal_namespace=_env_str("CUTAGENT_TEMPORAL_NAMESPACE", "default"),
            temporal_task_queue=_env_str(
                "CUTAGENT_TEMPORAL_TASK_QUEUE", "cutagent-production"
            ),
        ),
        auth=AuthSettings(
            registration_open=_env_str("CUTAGENT_REGISTRATION_OPEN", "true").lower()
            == "true",
            registration_code_salt=_env_str(
                "CUTAGENT_REGISTRATION_CODE_SALT", "local-dev-registration-code-salt"
            ),
            max_login_attempts=_env_int("CUTAGENT_AUTH_MAX_LOGIN_ATTEMPTS", 8),
            login_window_minutes=_env_int("CUTAGENT_AUTH_LOGIN_WINDOW_MINUTES", 15),
            max_registration_attempts=_env_int(
                "CUTAGENT_AUTH_MAX_REGISTRATION_ATTEMPTS", 5
            ),
            registration_window_minutes=_env_int(
                "CUTAGENT_AUTH_REGISTRATION_WINDOW_MINUTES", 60
            ),
        ),
        secret_store=SecretStoreSettings(
            dir=_env_str("CUTAGENT_SECRET_STORE_DIR", ".data/secrets"),
        ),
        media=MediaSettings(
            ffmpeg_bin=os.getenv("CUTAGENT_FFMPEG_BIN"),
            ffprobe_bin=os.getenv("CUTAGENT_FFPROBE_BIN"),
        ),
        api=ApiSettings(
            disable_background_dispatcher=os.getenv(
                "CUTAGENT_DISABLE_BACKGROUND_DISPATCHER"
            )
            == "1",
        ),
        balance=BalanceSettings(
            poller_enabled=os.getenv("CUTAGENT_BALANCE_POLLER_ENABLED") == "1",
            poll_interval_seconds=_env_int(
                "CUTAGENT_BALANCE_POLL_INTERVAL_SECONDS", 900
            ),
            request_timeout_seconds=_env_int(
                "CUTAGENT_BALANCE_REQUEST_TIMEOUT_SECONDS", 10
            ),
        ),
    )


def get_settings() -> Settings:
    """Accessor returning a freshly-built infra ``Settings`` snapshot.

    Provided for symmetry with the genesis DI conventions. Prefer reading
    ``app.state.settings`` inside request handlers; reach for ``get_settings()``
    only in standalone/CLI contexts that lack an ``app.state``."""
    return build_settings()
