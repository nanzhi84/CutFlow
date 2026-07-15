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

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Small env helpers (single point that knows how to read os.environ).


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


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var, or ``default`` when unset / blank."""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_positive_int(name: str, default: int) -> int:
    """Parse a positive int env var; unset / non-integer / non-positive -> default.

    Mirrors the previous provider-limiter ``_max_inflight``/``_max_qps`` reads,
    which fell back to the default rather than disabling backpressure. This is
    intentionally more lenient than :func:`_env_int` (which raises on a
    set-but-invalid value)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_unit_float(name: str, default: float) -> float:
    """Parse a float env var clamped to ``[0.0, 1.0]``; unset / invalid -> default.

    Mirrors the previous circuit-breaker ``_float_env``: an unset or unparseable
    value falls back to ``default``; a valid value is clamped to the unit interval."""
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return min(max(value, 0.0), 1.0)


def _env_min_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Parse an int env var floored at ``minimum``; unset / invalid -> default.

    Mirrors the previous circuit-breaker ``_int_env``: an unset or unparseable
    value falls back to ``default``; a valid value is floored at ``minimum``."""
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return max(minimum, value)


def _env_optional_min_int(name: str, *, minimum: int = 1) -> int | None:
    """Parse an optional int env var floored at ``minimum``; unset / blank -> None."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return max(minimum, int(raw))


def _default_ephemeral_local_path() -> str:
    """Default ephemeral object-store root under the OS temp dir.

    Matches the previous default in ``object_store_env._ephemeral_store_from_env``
    (``<tempdir>/cutagent-ephemeral``)."""
    return str(Path(tempfile.gettempdir()) / "cutagent-ephemeral")


# Nested infra settings groups.


class StorageSettings(BaseModel):
    """Persistence backend selection (``settings.storage.*``)."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_STORAGE_BACKEND: "sqlalchemy" | "postgres". Stored lower-cased.
    # The in-memory backend has been removed;
    # _reject_memory_backend below fails loudly if "memory" is still configured.
    backend: str = "sqlalchemy"

    @field_validator("backend")
    @classmethod
    def _reject_memory_backend(cls, value: str) -> str:
        if value == "memory":
            raise ValueError(
                "CUTAGENT_STORAGE_BACKEND=memory is no longer supported; the in-memory "
                "storage backend has been removed. Use 'sqlalchemy' (or 'postgres') with a "
                "real CUTAGENT_DATABASE_URL."
            )
        return value

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
    bucket: str = "cutagent-local"  # CUTAGENT_OBJECTSTORE_BUCKET (durable OUTPUT bucket)
    # Optional dedicated bucket for 'material' purposes (shared source-asset
    # library). Empty = disabled (materials write to the durable OUTPUT bucket,
    # i.e. prior single-bucket behaviour). CUTAGENT_OBJECTSTORE_MATERIALS_BUCKET
    materials_bucket: str = ""
    # Extra read-only buckets the durable store may READ from (never write).
    # CUTAGENT_OBJECTSTORE_READ_BUCKETS (comma-separated). Empty = write bucket only.
    read_buckets: tuple[str, ...] = ()
    local_path: str = ".data/objectstore"  # CUTAGENT_LOCAL_OBJECTSTORE_PATH
    # Local S3 download-cache governance (issue #76). 0 = unbounded (current
    # behaviour). The sweep (scripts/cache_status.py) evicts by TTL then size.
    # CUTAGENT_OBJECTSTORE_CACHE_MAX_BYTES / _CACHE_TTL_HOURS.
    cache_max_bytes: int = 0
    cache_ttl_hours: float = 0
    # How long a presigned GET (read) URL stays valid, and therefore how long the
    # browser can keep the object cached before the URL rotates (issue #206).
    # 7 days is the SigV4 ceiling. Distinct from ``upload.presign_ttl_seconds``,
    # which is the presigned PUT lifetime. CUTAGENT_OBJECTSTORE_SIGNED_GET_TTL_SECONDS
    signed_get_ttl_seconds: int = 604800
    s3: S3TransportSettings = Field(default_factory=S3TransportSettings)
    ephemeral: EphemeralObjectStoreSettings = Field(default_factory=EphemeralObjectStoreSettings)


class WorkflowSettings(BaseModel):
    """Workflow runtime + Temporal connection (``settings.workflow.*``).

    Field names/defaults mirror the previous ``WorkflowRuntimeSettings`` so the
    workflow package can build its runtime settings straight from this group."""

    model_config = ConfigDict(frozen=True)

    runtime: Literal["local", "temporal"] = "local"  # CUTAGENT_WORKFLOW_RUNTIME
    temporal_address: str = "127.0.0.1:7233"  # CUTAGENT_TEMPORAL_ADDRESS
    temporal_namespace: str = "default"  # CUTAGENT_TEMPORAL_NAMESPACE
    temporal_task_queue: str = "cutagent-production"  # CUTAGENT_TEMPORAL_TASK_QUEUE
    case_max_inflight_runs: int = 3  # CUTAGENT_CASE_MAX_INFLIGHT_RUNS
    worker_max_activities: int = 8  # CUTAGENT_WORKER_MAX_ACTIVITIES


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
    # CUTAGENT_SEED_LOCAL_AUTH: seed local bootstrap users/codes
    # (admin@local.cutagent / viewer@local.cutagent). Kept on by default for
    # local dev and tests; production can disable it after real admins exist.
    seed_local_auth: bool = True
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
    # CUTAGENT_FFMPEG_THREADS: optional global ffmpeg ``-threads`` value. None
    # preserves ffmpeg's default auto-threading and existing command strings.
    ffmpeg_threads: int | None = None


class ProviderLimitConfig(BaseModel):
    """Per-provider/concurrency-key limiter override."""

    model_config = ConfigDict(frozen=True)

    max_inflight: int | None = None
    max_qps: int | None = None


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

    Uploads go browser-direct to OSS via presigned single or multipart PUTs; the
    API never receives the bytes. The hard 200 MiB per-file cap also lives in the
    request contracts and is re-checked by the reconciler's exact-size HEAD."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_UPLOAD_PRESIGN_TTL_SECONDS: lifetime of a presigned PUT URL.
    presign_ttl_seconds: int = Field(900, ge=60, le=7 * 24 * 60 * 60)
    max_size_bytes: int = Field(200 * 1024 * 1024, gt=0, le=200 * 1024 * 1024)
    multipart_threshold_bytes: int = Field(16 * 1024 * 1024, gt=0)
    # S3/OSS requires every non-final part to be at least 5 MiB.
    part_size_bytes: int = Field(8 * 1024 * 1024, ge=5 * 1024 * 1024)
    reconcile_interval_seconds: int = Field(5, ge=1)
    reconcile_lease_seconds: int = Field(300, ge=1)
    reconcile_max_retries: int = Field(8, ge=1, le=100)
    # CUTAGENT_UPLOAD_CORS_ALLOWED_ORIGINS: comma-separated web origins allowed to
    # PUT directly to the durable upload bucket (provisioned onto OSS bucket CORS).
    cors_allowed_origins: tuple[str, ...] = ()
    # CUTAGENT_UPLOAD_NORMALIZE_VIDEO: "1" normalizes portrait/b-roll uploads to
    # the strict delivery profile (rotation/cropdetect/1080p/bt709 + post-encode
    # validation) before admitting them. Off by default so the existing upload
    # flow / tests are unchanged unless a deployment opts in.
    normalize_video: bool = False

    @model_validator(mode="after")
    def validate_multipart_geometry(self) -> "UploadSettings":
        if self.multipart_threshold_bytes > self.max_size_bytes:
            raise ValueError("multipart threshold must not exceed the upload size limit")
        if self.part_size_bytes > self.max_size_bytes:
            raise ValueError("multipart part size must not exceed the upload size limit")
        return self


class ApiSettings(BaseModel):
    """API process behaviour (``settings.api.*``)."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_DISABLE_BACKGROUND_DISPATCHER: "1" disables the in-process outbox
    # dispatcher background task (tests set this for determinism).
    disable_background_dispatcher: bool = False
    # Idempotency middleware guards (issue #65). The middleware buffers the whole
    # request body (to hash for replay) and the whole 2xx response body (to cache
    # for replay) ONLY for authenticated writes carrying an Idempotency-Key. These
    # cap that buffering so a large/binary body or response cannot blow up memory
    # via the middleware. 1 MiB each — Idempotency-Key is a control-plane (small
    # JSON) feature. CUTAGENT_IDEMPOTENCY_MAX_BODY_BYTES / _MAX_RESPONSE_BYTES.
    idempotency_max_body_bytes: int = 1024 * 1024
    idempotency_max_response_bytes: int = 1024 * 1024


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


class ProvidersSettings(BaseModel):
    """Provider gateway backpressure, circuit breaker, and outbound-host policy
    (``settings.providers.*``).

    Infra/policy knobs (NOT secrets) previously read via scattered ``os.getenv``
    in ``packages/ai/gateway/provider_limiter.py``, ``packages/ai/netpolicy.py``,
    ``packages/ai/gateway/provider_gateway.py`` and
    ``packages/ops/circuit_breaker.py``. Defaults are byte-for-byte identical to
    those call sites, so consolidating them here is behaviour preserving."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_PROVIDER_MAX_INFLIGHT: per concurrency_key in-flight cap. Unset or
    # non-positive falls back to 4 (never disables backpressure).
    max_inflight: int = 4
    # CUTAGENT_PROVIDER_MAX_QPS: per-key token-bucket rate (enforced with Redis).
    max_qps: int = 4
    # CUTAGENT_PROVIDER_CIRCUIT_BREAKER: "1" enables the provider circuit breaker.
    circuit_breaker_enabled: bool = False
    # CUTAGENT_PROVIDER_CIRCUIT_ERROR_RATE: error-rate threshold, clamped to [0,1].
    circuit_error_rate_threshold: float = 0.5
    # CUTAGENT_PROVIDER_CIRCUIT_WINDOW: health-metrics window in hours (>= 1).
    circuit_window_hours: int = 24
    # CUTAGENT_ALLOWED_API_HOSTS: comma-separated extra outbound hosts appended to
    # the built-in SSRF allow-list (``netpolicy.DEFAULT_ALLOWED_HOSTS``).
    allowed_api_hosts: str = ""
    # CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST: "1" turns on the opt-in
    # gateway-level base_url host re-check before the secret is delivered.
    enforce_host_allowlist: bool = False
    # CUTAGENT_PROVIDER_LIMITS JSON:
    # {"dashscope:llm.chat":{"max_inflight":2,"max_qps":1},"runninghub":{"max_inflight":1}}
    # Keys match ProviderProfile.concurrency_key first, then provider_id.
    limits: dict[str, ProviderLimitConfig] = Field(default_factory=dict)


class PublishingSettings(BaseModel):
    """Publishing-center integration knobs (``settings.publishing.*``).

    The 小V猫 (XiaoVmao) CDP endpoint the publishing QR-login manager attaches to.
    Platform sessions live in 小V猫, never in ``SecretStore``/DB, so only the
    non-secret host/port live here."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_XIAOVMAO_CDP_HOST: 小V猫 CDP server host.
    xiaovmao_cdp_host: str = "127.0.0.1"
    # CUTAGENT_XIAOVMAO_CDP_PORT: kept raw so unrelated Settings consumers do not
    # fail while building a snapshot; actual 小V猫 call sites still parse strictly
    # through the property below, matching the old int(os.getenv(...)) behavior.
    xiaovmao_cdp_port_raw: str = Field("9222", exclude=True)
    # CUTAGENT_XIAOVMAO_AUTO_LAUNCH: best-effort macOS local launch before CDP
    # connect/probe/publish attempts. Non-macOS or non-local hosts are ignored.
    xiaovmao_auto_launch: bool = True

    @property
    def xiaovmao_cdp_port(self) -> int:
        return int(self.xiaovmao_cdp_port_raw)


class DeploymentSettings(BaseModel):
    """Deployment environment + topology knobs (``settings.deployment.*``).

    Drives the production startup preflight (``validate_startup_settings``):
    ``environment == "production"`` flips a set of dev-friendly defaults from
    advisory to fail-closed. None of these are secrets."""

    model_config = ConfigDict(frozen=True)

    # CUTAGENT_ENV: "local" (default) | "staging" | "production". Only
    # "production" triggers the fail-closed startup preflight.
    environment: str = "local"
    # CUTAGENT_REPLICA_COUNT: number of API replicas behind the LB. >1 requires a
    # shared Redis for cross-replica fanout / stream tokens / provider limiter.
    replica_count: int = 1
    # CUTAGENT_PUBLISHING_LOCAL_PROXY: acknowledge that the publishing CDP host is
    # intentionally a machine-local proxy (小V猫 on the deploy host), so the
    # preflight does not flag a 127.0.0.1 CDP host in production.
    publishing_local_proxy: bool = False

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"


class Settings(BaseModel):
    """Typed, immutable snapshot of all infrastructure configuration.

    Build instances with :func:`build_settings` (reads ``os.environ``); never
    instantiate a cached module-level singleton. The API/worker construct one
    snapshot and expose it via ``app.state.settings`` for dependency injection."""

    model_config = ConfigDict(frozen=True)

    deployment: DeploymentSettings = Field(default_factory=DeploymentSettings)
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
    learning: LearningSettings = Field(default_factory=LearningSettings)
    providers: ProvidersSettings = Field(default_factory=ProvidersSettings)
    publishing: PublishingSettings = Field(default_factory=PublishingSettings)
    # Optional shared coordination backend (cross-process limiter / fanout /
    # ephemeral token store). When unset, those layers stay per-process. See
    # packages/ai/gateway/provider_limiter.py and packages/core/observability/events.py.
    redis_url: str | None = None
    # CUTAGENT_REDIS_REQUIRED: when true (multi-replica production), a degraded
    # Redis must fail readiness rather than silently fall back to per-process
    # state — cross-replica fanout / stream tokens / provider limiting would
    # otherwise break invisibly. The layers still keep serving via the local
    # fallback (so a single request does not hard-fail), but readiness reports
    # not-ready so the LB stops routing until Redis recovers. See issue #67.
    redis_required: bool = False
    # CUTAGENT_HEALTH_PROBE_TIMEOUT: per-hop wall-clock budget (seconds) for the
    # public /api/health/network segment probes (issue #77 / #87 C3). The OSS and
    # Temporal hops are synchronous round-trips run on a throwaway worker thread
    # and abandoned past this budget, so a slow / hung dependency cannot turn this
    # unauthenticated endpoint into a DoS amplifier. Default 2.0s.
    health_probe_timeout_seconds: float = 2.0


# Builder: read os.environ once and assemble a Settings snapshot.


def build_object_store_settings() -> ObjectStoreSettings:
    """Build only the object-store settings group.

    This keeps low-level storage initialization independent from unrelated infra
    knobs such as publishing-center CDP settings while preserving call-time env
    reads.
    """
    return ObjectStoreSettings(
        tiered=os.getenv("CUTAGENT_OBJECTSTORE_TIERED", "1") != "0",
        backend=_env_str("CUTAGENT_OBJECTSTORE_BACKEND", "local").lower(),
        bucket=_env_str("CUTAGENT_OBJECTSTORE_BUCKET", "cutagent-local"),
        materials_bucket=_env_str("CUTAGENT_OBJECTSTORE_MATERIALS_BUCKET", ""),
        read_buckets=tuple(
            b.strip()
            for b in _env_str("CUTAGENT_OBJECTSTORE_READ_BUCKETS", "").split(",")
            if b.strip()
        ),
        local_path=_env_str("CUTAGENT_LOCAL_OBJECTSTORE_PATH", ".data/objectstore"),
        cache_max_bytes=_env_int("CUTAGENT_OBJECTSTORE_CACHE_MAX_BYTES", 0),
        cache_ttl_hours=_env_float("CUTAGENT_OBJECTSTORE_CACHE_TTL_HOURS", 0),
        signed_get_ttl_seconds=_env_int("CUTAGENT_OBJECTSTORE_SIGNED_GET_TTL_SECONDS", 604800),
        s3=S3TransportSettings(
            endpoint_url=_env_str("CUTAGENT_OBJECTSTORE_ENDPOINT", "http://127.0.0.1:9000"),
            access_key=_env_str("CUTAGENT_OBJECTSTORE_ACCESS_KEY", ""),
            secret_key=_env_str("CUTAGENT_OBJECTSTORE_SECRET_KEY", ""),
            region_name=_env_str("CUTAGENT_OBJECTSTORE_REGION", "us-east-1"),
            addressing_style=_env_str("CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE", "path"),
            multipart_threshold_mb=_env_int("CUTAGENT_OBJECTSTORE_MULTIPART_THRESHOLD_MB", 8),
            multipart_chunk_mb=_env_int("CUTAGENT_OBJECTSTORE_MULTIPART_CHUNK_MB", 8),
            max_concurrency=_env_int("CUTAGENT_OBJECTSTORE_MAX_CONCURRENCY", 4),
            connect_timeout=_env_int("CUTAGENT_OBJECTSTORE_CONNECT_TIMEOUT", 10),
            read_timeout=_env_int("CUTAGENT_OBJECTSTORE_READ_TIMEOUT", 120),
            max_attempts=_env_int("CUTAGENT_OBJECTSTORE_MAX_ATTEMPTS", 5),
        ),
        ephemeral=EphemeralObjectStoreSettings(
            backend=_env_str("CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND", "local").lower(),
            bucket=_env_str("CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET", "cutagent-ephemeral"),
            local_path=_env_str(
                "CUTAGENT_OBJECTSTORE_EPHEMERAL_PATH",
                _default_ephemeral_local_path(),
            ),
            endpoint_url=_env_str(
                "CUTAGENT_EPHEMERAL_OBJECTSTORE_ENDPOINT", "http://127.0.0.1:9000"
            ),
            access_key=_env_str("CUTAGENT_EPHEMERAL_OBJECTSTORE_ACCESS_KEY", ""),
            secret_key=_env_str("CUTAGENT_EPHEMERAL_OBJECTSTORE_SECRET_KEY", ""),
            region_name=_env_str("CUTAGENT_EPHEMERAL_OBJECTSTORE_REGION", "us-east-1"),
            addressing_style=_env_str("CUTAGENT_EPHEMERAL_OBJECTSTORE_ADDRESSING_STYLE", "path"),
        ),
    )


def build_workflow_settings() -> WorkflowSettings:
    return WorkflowSettings(
        runtime=_env_str("CUTAGENT_WORKFLOW_RUNTIME", "local").lower(),
        temporal_address=_env_str("CUTAGENT_TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        temporal_namespace=_env_str("CUTAGENT_TEMPORAL_NAMESPACE", "default"),
        temporal_task_queue=_env_str("CUTAGENT_TEMPORAL_TASK_QUEUE", "cutagent-production"),
        case_max_inflight_runs=_env_min_int("CUTAGENT_CASE_MAX_INFLIGHT_RUNS", 3),
        worker_max_activities=_env_min_int("CUTAGENT_WORKER_MAX_ACTIVITIES", 8),
    )


def _provider_limits_from_env() -> dict[str, ProviderLimitConfig]:
    raw = os.getenv("CUTAGENT_PROVIDER_LIMITS")
    if raw is None or raw.strip() == "":
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("CUTAGENT_PROVIDER_LIMITS must be a JSON object.")
    limits: dict[str, ProviderLimitConfig] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(value, dict):
            raise ValueError("CUTAGENT_PROVIDER_LIMITS values must be objects.")
        limit = ProviderLimitConfig.model_validate(value)
        limits[key.strip()] = limit
    return limits


def build_providers_settings() -> ProvidersSettings:
    return ProvidersSettings(
        max_inflight=_env_positive_int("CUTAGENT_PROVIDER_MAX_INFLIGHT", 4),
        max_qps=_env_positive_int("CUTAGENT_PROVIDER_MAX_QPS", 4),
        circuit_breaker_enabled=os.getenv("CUTAGENT_PROVIDER_CIRCUIT_BREAKER") == "1",
        circuit_error_rate_threshold=_env_unit_float("CUTAGENT_PROVIDER_CIRCUIT_ERROR_RATE", 0.5),
        circuit_window_hours=_env_min_int("CUTAGENT_PROVIDER_CIRCUIT_WINDOW", 24),
        allowed_api_hosts=_env_str("CUTAGENT_ALLOWED_API_HOSTS", ""),
        enforce_host_allowlist=os.getenv("CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST") == "1",
        limits=_provider_limits_from_env(),
    )


def build_publishing_settings() -> PublishingSettings:
    return PublishingSettings(
        xiaovmao_cdp_host=_env_str("CUTAGENT_XIAOVMAO_CDP_HOST", "127.0.0.1"),
        xiaovmao_cdp_port_raw=_env_str("CUTAGENT_XIAOVMAO_CDP_PORT", "9222"),
        xiaovmao_auto_launch=_env_bool("CUTAGENT_XIAOVMAO_AUTO_LAUNCH", True),
    )


def build_redis_url() -> str | None:
    return os.getenv("CUTAGENT_REDIS_URL")


def build_settings() -> Settings:
    """Read ``os.environ`` and return an infra ``Settings`` snapshot.

    Called at runtime (not import time). Each call re-reads the environment, so
    tests that ``monkeypatch.setenv`` before invoking a factory observe the
    override — preserving the call-time semantics of the previous ``os.getenv``
    sites."""
    return Settings(
        deployment=DeploymentSettings(
            environment=_env_str("CUTAGENT_ENV", "local").strip().lower(),
            replica_count=_env_int("CUTAGENT_REPLICA_COUNT", 1),
            publishing_local_proxy=_env_str("CUTAGENT_PUBLISHING_LOCAL_PROXY", "").strip().lower()
            in {"1", "true", "yes", "on"},
        ),
        storage=StorageSettings(
            backend=_env_str("CUTAGENT_STORAGE_BACKEND", "sqlalchemy").lower(),
            database_url=os.getenv("CUTAGENT_DATABASE_URL"),
            pool_size=_env_int_blank_default("CUTAGENT_DB_POOL_SIZE", 5),
            max_overflow=_env_int_blank_default("CUTAGENT_DB_MAX_OVERFLOW", 10),
            pool_recycle=_env_int_blank_default("CUTAGENT_DB_POOL_RECYCLE", 1800),
            pool_timeout=_env_int_blank_default("CUTAGENT_DB_POOL_TIMEOUT", 30),
        ),
        object_store=build_object_store_settings(),
        workflow=build_workflow_settings(),
        auth=AuthSettings(
            registration_open=_env_str("CUTAGENT_REGISTRATION_OPEN", "true").lower() == "true",
            registration_code_salt=_env_str(
                "CUTAGENT_REGISTRATION_CODE_SALT", "local-dev-registration-code-salt"
            ),
            seed_local_auth=_env_str("CUTAGENT_SEED_LOCAL_AUTH", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            max_login_attempts=_env_int("CUTAGENT_AUTH_MAX_LOGIN_ATTEMPTS", 8),
            login_window_minutes=_env_int("CUTAGENT_AUTH_LOGIN_WINDOW_MINUTES", 15),
            max_registration_attempts=_env_int("CUTAGENT_AUTH_MAX_REGISTRATION_ATTEMPTS", 5),
            registration_window_minutes=_env_int("CUTAGENT_AUTH_REGISTRATION_WINDOW_MINUTES", 60),
            trust_forwarded_for=_env_str("CUTAGENT_AUTH_TRUST_FORWARDED_FOR", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            cookie_secure=_env_bool_optional("CUTAGENT_AUTH_COOKIE_SECURE"),
        ),
        secret_store=SecretStoreSettings(
            dir=_env_str("CUTAGENT_SECRET_STORE_DIR", ".data/secrets"),
        ),
        media=MediaSettings(
            ffmpeg_bin=os.getenv("CUTAGENT_FFMPEG_BIN"),
            ffprobe_bin=os.getenv("CUTAGENT_FFPROBE_BIN"),
            ffmpeg_threads=_env_optional_min_int("CUTAGENT_FFMPEG_THREADS"),
        ),
        motion_guard=MotionGuardSettings(
            sample_fps=_env_float("CUTAGENT_MOTION_GUARD_SAMPLE_FPS", 10.0),
            width=_env_int("CUTAGENT_MOTION_GUARD_WIDTH", 360),
            window_sec=_env_float("CUTAGENT_MOTION_GUARD_WINDOW_SEC", 1.5),
            hop_sec=_env_float("CUTAGENT_MOTION_GUARD_HOP_SEC", 0.75),
            active_px=_env_float("CUTAGENT_MOTION_GUARD_ACTIVE_PX", 1.5),
            hard_px=_env_float("CUTAGENT_MOTION_GUARD_HARD_PX", 3.0),
            p95_hard_px=_env_float("CUTAGENT_MOTION_GUARD_P95_HARD_PX", 7.0),
            tail_y_range_hard_px=_env_float("CUTAGENT_MOTION_GUARD_TAIL_Y_RANGE_HARD_PX", 70.0),
            tail_net_y_hard_px=_env_float("CUTAGENT_MOTION_GUARD_TAIL_NET_Y_HARD_PX", 65.0),
            smooth_move_straightness=_env_float(
                "CUTAGENT_MOTION_GUARD_SMOOTH_MOVE_STRAIGHTNESS", 0.88
            ),
            smooth_move_flip_ratio=_env_float("CUTAGENT_MOTION_GUARD_SMOOTH_MOVE_FLIP_RATIO", 0.16),
            sweep_axis_ratio=_env_float("CUTAGENT_MOTION_GUARD_SWEEP_AXIS_RATIO", 2.3),
            jitter_flip_ratio=_env_float("CUTAGENT_MOTION_GUARD_JITTER_FLIP_RATIO", 0.22),
            jitter_jerk_ratio=_env_float("CUTAGENT_MOTION_GUARD_JITTER_JERK_RATIO", 0.65),
            refine_min_duration=_env_float("CUTAGENT_MOTION_GUARD_REFINE_MIN_DURATION", 0.8),
            refine_round_sec=_env_float("CUTAGENT_MOTION_GUARD_REFINE_ROUND_SEC", 0.1),
        ),
        upload=UploadSettings(
            presign_ttl_seconds=_env_int("CUTAGENT_UPLOAD_PRESIGN_TTL_SECONDS", 900),
            max_size_bytes=_env_int("CUTAGENT_UPLOAD_MAX_SIZE_BYTES", 200 * 1024 * 1024),
            multipart_threshold_bytes=_env_int(
                "CUTAGENT_UPLOAD_MULTIPART_THRESHOLD_BYTES", 16 * 1024 * 1024
            ),
            part_size_bytes=_env_int("CUTAGENT_UPLOAD_PART_SIZE_BYTES", 8 * 1024 * 1024),
            reconcile_interval_seconds=_env_int("CUTAGENT_UPLOAD_RECONCILE_INTERVAL_SECONDS", 5),
            reconcile_lease_seconds=_env_int("CUTAGENT_UPLOAD_RECONCILE_LEASE_SECONDS", 300),
            reconcile_max_retries=_env_int("CUTAGENT_UPLOAD_RECONCILE_MAX_RETRIES", 8),
            cors_allowed_origins=tuple(
                o.strip()
                for o in _env_str(
                    "CUTAGENT_UPLOAD_CORS_ALLOWED_ORIGINS",
                    "https://app.shuying.cyou,http://localhost:5173",
                ).split(",")
                if o.strip()
            ),
            normalize_video=os.getenv("CUTAGENT_UPLOAD_NORMALIZE_VIDEO") == "1",
        ),
        api=ApiSettings(
            disable_background_dispatcher=os.getenv("CUTAGENT_DISABLE_BACKGROUND_DISPATCHER")
            == "1",
            idempotency_max_body_bytes=_env_int("CUTAGENT_IDEMPOTENCY_MAX_BODY_BYTES", 1024 * 1024),
            idempotency_max_response_bytes=_env_int(
                "CUTAGENT_IDEMPOTENCY_MAX_RESPONSE_BYTES", 1024 * 1024
            ),
        ),
        balance=BalanceSettings(
            poller_enabled=os.getenv("CUTAGENT_BALANCE_POLLER_ENABLED") == "1",
            poll_interval_seconds=_env_int("CUTAGENT_BALANCE_POLL_INTERVAL_SECONDS", 900),
            request_timeout_seconds=_env_int("CUTAGENT_BALANCE_REQUEST_TIMEOUT_SECONDS", 10),
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
            reward_stale_unpublished=_env_float("CUTAGENT_LEARNING_REWARD_STALE_UNPUBLISHED", -0.1),
            bump_min_samples=_env_int("CUTAGENT_LEARNING_BUMP_MIN_SAMPLES", 5),
            bump_miss_streak=_env_int("CUTAGENT_LEARNING_BUMP_MISS_STREAK", 3),
            bump_consistency_floor=_env_float("CUTAGENT_LEARNING_BUMP_CONSISTENCY_FLOOR", 0.6),
        ),
        providers=build_providers_settings(),
        publishing=build_publishing_settings(),
        redis_url=build_redis_url(),
        redis_required=_env_str("CUTAGENT_REDIS_REQUIRED", "").strip().lower()
        in {"1", "true", "yes", "on"},
        health_probe_timeout_seconds=_env_float("CUTAGENT_HEALTH_PROBE_TIMEOUT", 2.0),
    )


def sandbox_fallback_allowed() -> bool:
    """Whether silent fallback to the seeded sandbox providers is permitted.

    Reads ``CUTAGENT_ALLOW_SANDBOX_FALLBACK`` at call time (same semantics as the
    other infra knobs). OFF by default: the running app must route to real
    providers and raise when none is armed, never silently producing sandbox
    output. The test suite opts in via conftest so its golden/fallback fixtures
    keep exercising the sandbox path. Read straight from the environment so the
    provider-resolution hot paths stay cheap (no full settings snapshot built)."""
    return os.getenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK") == "1"
