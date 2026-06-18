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


def _env_float(name: str, default: float) -> float:
    """Return the env var parsed as float, or ``default`` when unset."""
    value = os.getenv(name)
    return float(value) if value is not None else default


def _env_int_blank_default(name: str, default: int) -> int:
    """Like :func:`_env_int` but an unset OR blank var falls back to ``default``.

    Used for the DB connection-pool knobs, where a present-but-empty env value
    should behave as "not configured" rather than raising."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _env_bool_optional(name: str) -> bool | None:
    """Parse a tri-state boolean env var.

    Returns ``None`` when the var is unset or blank (caller derives a default),
    ``True`` for ``1/true/yes/on`` and ``False`` for ``0/false/no/off`` (case-
    insensitive). Used by the cookie-Secure knob, whose "unset" state means
    "derive from the request scheme" rather than a fixed boolean."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    # CUTAGENT_AUTH_TRUST_FORWARDED_FOR: trust the X-Forwarded-For header for
    # rate-limit client bucketing. OFF by default — the header is client-supplied,
    # so trusting it lets an attacker rotate it to mint a fresh limiter bucket per
    # request and bypass the brute-force throttle. Enable ONLY when the API sits
    # behind a trusted proxy/LB that overwrites the header.
    trust_forwarded_for: bool = False
    # CUTAGENT_AUTH_COOKIE_SECURE: force the session cookie's ``Secure`` flag.
    # Spec §33.2: the session cookie MUST be HttpOnly and, in production, Secure.
    # Three-state knob:
    #   - "true"/"1"  -> always emit Secure (production / TLS-terminating deploys);
    #   - "false"/"0" -> never emit Secure (local plain-HTTP dev only);
    #   - None (unset, the default) -> derive per-request from the connection
    #     scheme (request.url.scheme == "https", or the X-Forwarded-Proto first hop
    #     when ``trust_forwarded_for`` is enabled for a trusted proxy/LB). Deriving
    #     keeps local HTTP dev working while a TLS prod deployment automatically
    #     marks the cookie Secure.
    cookie_secure: bool | None = None


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


class MotionGuardSettings(BaseModel):
    """Deterministic motion-guard sensor knobs (``settings.motion_guard.*``)."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_MOTION_GUARD_SAMPLE_FPS / _WIDTH / _WINDOW_SEC / _HOP_SEC.
    sample_fps: float = 10.0
    width: int = 360
    window_sec: float = 1.5
    hop_sec: float = 0.75
    # Pixel thresholds are normalized to a 360px-wide grayscale stream (px360).
    active_px: float = 1.5
    hard_px: float = 3.0
    p95_hard_px: float = 7.0
    tail_y_range_hard_px: float = 70.0
    tail_net_y_hard_px: float = 65.0
    smooth_move_straightness: float = 0.88
    smooth_move_flip_ratio: float = 0.16
    sweep_axis_ratio: float = 2.3
    jitter_flip_ratio: float = 0.22
    jitter_jerk_ratio: float = 0.65
    refine_min_duration: float = 0.8
    refine_round_sec: float = 0.1


class UploadSettings(BaseModel):
    """Upload ingestion knobs (``settings.upload.*``).

    The HTTP upload path streams the body to disk in chunks and enforces a hard
    size cap so a single oversized request can never buffer hundreds of MB in
    RAM. The cap is a defence-in-depth ceiling: a per-session declared
    ``size_bytes`` (when present) tightens it further, but this guards against a
    session that omits / under-declares its size."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_UPLOAD_MAX_SIZE_BYTES: hard ceiling for a single uploaded file.
    # Default 2 GiB. Exceeding it aborts the stream early with upload.too_large.
    max_size_bytes: int = 2 * 1024 * 1024 * 1024
    # CUTAGENT_UPLOAD_CHUNK_BYTES: streaming chunk size (read granularity).
    chunk_bytes: int = 1024 * 1024
    # CUTAGENT_UPLOAD_NORMALIZE_VIDEO: "1" normalizes portrait/b-roll uploads to
    # the strict delivery profile (rotation/cropdetect/1080p/bt709 + post-encode
    # validation) before admitting them. Off by default so the existing upload
    # flow / tests are unchanged unless a deployment opts in.
    normalize_video: bool = False


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


class ProviderSettings(BaseModel):
    """Provider-routing policy (``settings.providers.*``)."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_ALLOW_SANDBOX_FALLBACK: "1" permits silently routing to the seeded
    # sandbox providers (sandbox.tts.default / sandbox.llm.default) when no real
    # provider is armed. OFF by default — the running app uses real providers only
    # and fails loudly when none is configured, never silently degrading to sandbox
    # output. The test suite opts in (conftest) so its golden/fallback fixtures keep
    # exercising the sandbox path.
    allow_sandbox_fallback: bool = False
    # CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST: "1" turns on the OPT-IN, defense-in-
    # depth host allow-list re-check in the provider gateway (before the bearer
    # secret is delivered to the profile's base_url). The AUTHORITATIVE SSRF gate is
    # always-on at provider-profile create/patch; this gateway re-check is OFF by
    # default so fixtures/seeds that build profiles directly with synthetic hosts
    # keep working. Enable in production for belt-and-suspenders enforcement.
    enforce_provider_host_allowlist: bool = False


class LearningSettings(BaseModel):
    """Case-rubric self-evolution knobs (``settings.learning.*``, §5/§6).

    Reward-shaping values and bump thresholds live here (not as scattered magic
    numbers) so a deployment can tune the learning loop without code changes."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_LEARNING_RETRO_WINDOW_DAYS: a published video enters "待复盘" this
    # many days after publish (the blind-prediction settle window). Default 3.
    retro_window_days: int = 3
    # Reward shaping (§5.2): stage rewards rise with how far a script advanced; a
    # discarded-because-script is the only negative human signal (others don't
    # blame the script). Values are normalized into a comparable range.
    reward_draft_adopted: float = 0.2
    reward_draft_pick: float = -0.05
    reward_video_produced: float = 0.4
    reward_published: float = 0.7
    reward_video_discarded_script: float = -0.3
    reward_stale_unpublished: float = -0.1
    # Bump gate (§6.4): a bump needs enough calibration samples AND either a
    # ranking consistency below the floor or a run of same-direction mispredicts.
    bump_min_samples: int = 5
    bump_miss_streak: int = 3
    bump_consistency_floor: float = 0.6


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
    motion_guard: MotionGuardSettings = Field(default_factory=MotionGuardSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    balance: BalanceSettings = Field(default_factory=BalanceSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    learning: LearningSettings = Field(default_factory=LearningSettings)
    # Optional shared coordination backend (cross-process limiter / fanout /
    # ephemeral token store). When unset, those layers stay per-process. See
    # packages/ai/gateway/provider_limiter.py and packages/core/observability/events.py.
    redis_url: str | None = None


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
            trust_forwarded_for=_env_str(
                "CUTAGENT_AUTH_TRUST_FORWARDED_FOR", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            cookie_secure=_env_bool_optional("CUTAGENT_AUTH_COOKIE_SECURE"),
        ),
        secret_store=SecretStoreSettings(
            dir=_env_str("CUTAGENT_SECRET_STORE_DIR", ".data/secrets"),
        ),
        media=MediaSettings(
            ffmpeg_bin=os.getenv("CUTAGENT_FFMPEG_BIN"),
            ffprobe_bin=os.getenv("CUTAGENT_FFPROBE_BIN"),
        ),
        motion_guard=MotionGuardSettings(
            sample_fps=_env_float("CUTAGENT_MOTION_GUARD_SAMPLE_FPS", 10.0),
            width=_env_int("CUTAGENT_MOTION_GUARD_WIDTH", 360),
            window_sec=_env_float("CUTAGENT_MOTION_GUARD_WINDOW_SEC", 1.5),
            hop_sec=_env_float("CUTAGENT_MOTION_GUARD_HOP_SEC", 0.75),
            active_px=_env_float("CUTAGENT_MOTION_GUARD_ACTIVE_PX", 1.5),
            hard_px=_env_float("CUTAGENT_MOTION_GUARD_HARD_PX", 3.0),
            p95_hard_px=_env_float("CUTAGENT_MOTION_GUARD_P95_HARD_PX", 7.0),
            tail_y_range_hard_px=_env_float(
                "CUTAGENT_MOTION_GUARD_TAIL_Y_RANGE_HARD_PX", 70.0
            ),
            tail_net_y_hard_px=_env_float(
                "CUTAGENT_MOTION_GUARD_TAIL_NET_Y_HARD_PX", 65.0
            ),
            smooth_move_straightness=_env_float(
                "CUTAGENT_MOTION_GUARD_SMOOTH_MOVE_STRAIGHTNESS", 0.88
            ),
            smooth_move_flip_ratio=_env_float(
                "CUTAGENT_MOTION_GUARD_SMOOTH_MOVE_FLIP_RATIO", 0.16
            ),
            sweep_axis_ratio=_env_float("CUTAGENT_MOTION_GUARD_SWEEP_AXIS_RATIO", 2.3),
            jitter_flip_ratio=_env_float(
                "CUTAGENT_MOTION_GUARD_JITTER_FLIP_RATIO", 0.22
            ),
            jitter_jerk_ratio=_env_float(
                "CUTAGENT_MOTION_GUARD_JITTER_JERK_RATIO", 0.65
            ),
            refine_min_duration=_env_float(
                "CUTAGENT_MOTION_GUARD_REFINE_MIN_DURATION", 0.8
            ),
            refine_round_sec=_env_float("CUTAGENT_MOTION_GUARD_REFINE_ROUND_SEC", 0.1),
        ),
        upload=UploadSettings(
            max_size_bytes=_env_int(
                "CUTAGENT_UPLOAD_MAX_SIZE_BYTES", 2 * 1024 * 1024 * 1024
            ),
            chunk_bytes=_env_int("CUTAGENT_UPLOAD_CHUNK_BYTES", 1024 * 1024),
            normalize_video=os.getenv("CUTAGENT_UPLOAD_NORMALIZE_VIDEO") == "1",
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
        providers=ProviderSettings(
            allow_sandbox_fallback=os.getenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK") == "1",
            enforce_provider_host_allowlist=os.getenv(
                "CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST"
            )
            == "1",
        ),
        learning=LearningSettings(
            retro_window_days=_env_int("CUTAGENT_LEARNING_RETRO_WINDOW_DAYS", 3),
            reward_draft_adopted=_env_float("CUTAGENT_LEARNING_REWARD_DRAFT_ADOPTED", 0.2),
            reward_draft_pick=_env_float("CUTAGENT_LEARNING_REWARD_DRAFT_PICK", -0.05),
            reward_video_produced=_env_float("CUTAGENT_LEARNING_REWARD_VIDEO_PRODUCED", 0.4),
            reward_published=_env_float("CUTAGENT_LEARNING_REWARD_PUBLISHED", 0.7),
            reward_video_discarded_script=_env_float(
                "CUTAGENT_LEARNING_REWARD_VIDEO_DISCARDED_SCRIPT", -0.3
            ),
            reward_stale_unpublished=_env_float(
                "CUTAGENT_LEARNING_REWARD_STALE_UNPUBLISHED", -0.1
            ),
            bump_min_samples=_env_int("CUTAGENT_LEARNING_BUMP_MIN_SAMPLES", 5),
            bump_miss_streak=_env_int("CUTAGENT_LEARNING_BUMP_MISS_STREAK", 3),
            bump_consistency_floor=_env_float(
                "CUTAGENT_LEARNING_BUMP_CONSISTENCY_FLOOR", 0.6
            ),
        ),
        redis_url=os.getenv("CUTAGENT_REDIS_URL"),
    )


def get_settings() -> Settings:
    """Accessor returning a freshly-built infra ``Settings`` snapshot.

    Provided for symmetry with the genesis DI conventions. Prefer reading
    ``app.state.settings`` inside request handlers; reach for ``get_settings()``
    only in standalone/CLI contexts that lack an ``app.state``."""
    return build_settings()


def sandbox_fallback_allowed() -> bool:
    """Whether silent fallback to the seeded sandbox providers is permitted.

    Reads ``CUTAGENT_ALLOW_SANDBOX_FALLBACK`` at call time (same semantics as the
    other infra knobs). OFF by default: the running app must route to real
    providers and raise when none is armed, never silently producing sandbox
    output. Mirrors ``build_settings().providers.allow_sandbox_fallback`` without
    building the full snapshot, so the provider-resolution hot paths stay cheap."""
    return os.getenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK") == "1"
