"""Contract tests for the central infra Settings (packages.core.config).

These pin the built-in defaults (which must equal the defaults the previous
scattered os.getenv calls used) and the env-override / call-time-read semantics
that the rest of the codebase relies on.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from packages.core.config import build_providers_settings, build_publishing_settings, build_settings

# Every infra env var Settings reads — cleared so we observe the built-in
# defaults rather than whatever the surrounding process/conftest exported.
_INFRA_ENV_VARS = (
    "CUTAGENT_STORAGE_BACKEND",
    "CUTAGENT_DATABASE_URL",
    "CUTAGENT_DB_POOL_SIZE",
    "CUTAGENT_DB_MAX_OVERFLOW",
    "CUTAGENT_DB_POOL_RECYCLE",
    "CUTAGENT_DB_POOL_TIMEOUT",
    "CUTAGENT_OBJECTSTORE_TIERED",
    "CUTAGENT_OBJECTSTORE_BACKEND",
    "CUTAGENT_OBJECTSTORE_BUCKET",
    "CUTAGENT_OBJECTSTORE_MATERIALS_BUCKET",
    "CUTAGENT_OBJECTSTORE_READ_BUCKETS",
    "CUTAGENT_LOCAL_OBJECTSTORE_PATH",
    "CUTAGENT_OBJECTSTORE_ENDPOINT",
    "CUTAGENT_OBJECTSTORE_ACCESS_KEY",
    "CUTAGENT_OBJECTSTORE_SECRET_KEY",
    "CUTAGENT_OBJECTSTORE_REGION",
    "CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE",
    "CUTAGENT_OBJECTSTORE_MULTIPART_THRESHOLD_MB",
    "CUTAGENT_OBJECTSTORE_MULTIPART_CHUNK_MB",
    "CUTAGENT_OBJECTSTORE_MAX_CONCURRENCY",
    "CUTAGENT_OBJECTSTORE_CONNECT_TIMEOUT",
    "CUTAGENT_OBJECTSTORE_READ_TIMEOUT",
    "CUTAGENT_OBJECTSTORE_MAX_ATTEMPTS",
    "CUTAGENT_OBJECTSTORE_CACHE_MAX_BYTES",
    "CUTAGENT_OBJECTSTORE_CACHE_TTL_HOURS",
    "CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND",
    "CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET",
    "CUTAGENT_OBJECTSTORE_EPHEMERAL_PATH",
    "CUTAGENT_EPHEMERAL_OBJECTSTORE_ENDPOINT",
    "CUTAGENT_EPHEMERAL_OBJECTSTORE_ACCESS_KEY",
    "CUTAGENT_EPHEMERAL_OBJECTSTORE_SECRET_KEY",
    "CUTAGENT_EPHEMERAL_OBJECTSTORE_REGION",
    "CUTAGENT_EPHEMERAL_OBJECTSTORE_ADDRESSING_STYLE",
    "CUTAGENT_WORKFLOW_RUNTIME",
    "CUTAGENT_TEMPORAL_ADDRESS",
    "CUTAGENT_TEMPORAL_NAMESPACE",
    "CUTAGENT_TEMPORAL_TASK_QUEUE",
    "CUTAGENT_REGISTRATION_OPEN",
    "CUTAGENT_REGISTRATION_CODE_SALT",
    "CUTAGENT_SEED_LOCAL_AUTH",
    "CUTAGENT_AUTH_MAX_LOGIN_ATTEMPTS",
    "CUTAGENT_AUTH_LOGIN_WINDOW_MINUTES",
    "CUTAGENT_AUTH_MAX_REGISTRATION_ATTEMPTS",
    "CUTAGENT_AUTH_REGISTRATION_WINDOW_MINUTES",
    "CUTAGENT_AUTH_TRUST_FORWARDED_FOR",
    "CUTAGENT_AUTH_COOKIE_SECURE",
    "CUTAGENT_SECRET_STORE_DIR",
    "CUTAGENT_FFMPEG_BIN",
    "CUTAGENT_FFPROBE_BIN",
    "CUTAGENT_DISABLE_BACKGROUND_DISPATCHER",
    "CUTAGENT_MOTION_GUARD_SAMPLE_FPS",
    "CUTAGENT_MOTION_GUARD_WIDTH",
    "CUTAGENT_MOTION_GUARD_WINDOW_SEC",
    "CUTAGENT_MOTION_GUARD_HOP_SEC",
    "CUTAGENT_MOTION_GUARD_ACTIVE_PX",
    "CUTAGENT_MOTION_GUARD_HARD_PX",
    "CUTAGENT_MOTION_GUARD_P95_HARD_PX",
    "CUTAGENT_MOTION_GUARD_TAIL_Y_RANGE_HARD_PX",
    "CUTAGENT_MOTION_GUARD_TAIL_NET_Y_HARD_PX",
    "CUTAGENT_MOTION_GUARD_SMOOTH_MOVE_STRAIGHTNESS",
    "CUTAGENT_MOTION_GUARD_SMOOTH_MOVE_FLIP_RATIO",
    "CUTAGENT_MOTION_GUARD_SWEEP_AXIS_RATIO",
    "CUTAGENT_MOTION_GUARD_JITTER_FLIP_RATIO",
    "CUTAGENT_MOTION_GUARD_JITTER_JERK_RATIO",
    "CUTAGENT_MOTION_GUARD_REFINE_MIN_DURATION",
    "CUTAGENT_MOTION_GUARD_REFINE_ROUND_SEC",
    "CUTAGENT_UPLOAD_PRESIGN_TTL_SECONDS",
    "CUTAGENT_UPLOAD_CORS_ALLOWED_ORIGINS",
    "CUTAGENT_UPLOAD_NORMALIZE_VIDEO",
    "CUTAGENT_BALANCE_POLLER_ENABLED",
    "CUTAGENT_BALANCE_POLL_INTERVAL_SECONDS",
    "CUTAGENT_BALANCE_REQUEST_TIMEOUT_SECONDS",
    "CUTAGENT_LEARNING_RETRO_WINDOW_DAYS",
    "CUTAGENT_LEARNING_REWARD_DRAFT_ADOPTED",
    "CUTAGENT_LEARNING_REWARD_DRAFT_PICK",
    "CUTAGENT_LEARNING_REWARD_VIDEO_PRODUCED",
    "CUTAGENT_LEARNING_REWARD_PUBLISHED",
    "CUTAGENT_LEARNING_REWARD_VIDEO_DISCARDED_SCRIPT",
    "CUTAGENT_LEARNING_REWARD_STALE_UNPUBLISHED",
    "CUTAGENT_LEARNING_BUMP_MIN_SAMPLES",
    "CUTAGENT_LEARNING_BUMP_MISS_STREAK",
    "CUTAGENT_LEARNING_BUMP_CONSISTENCY_FLOOR",
    "CUTAGENT_PROVIDER_MAX_INFLIGHT",
    "CUTAGENT_PROVIDER_MAX_QPS",
    "CUTAGENT_PROVIDER_CIRCUIT_BREAKER",
    "CUTAGENT_PROVIDER_CIRCUIT_ERROR_RATE",
    "CUTAGENT_PROVIDER_CIRCUIT_WINDOW",
    "CUTAGENT_ALLOWED_API_HOSTS",
    "CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST",
    "CUTAGENT_XIAOVMAO_CDP_HOST",
    "CUTAGENT_XIAOVMAO_CDP_PORT",
    "CUTAGENT_REDIS_URL",
    "CUTAGENT_REDIS_REQUIRED",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _INFRA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _env_example_names() -> set[str]:
    names: set[str] = set()
    for line in Path(".env.example").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped[1:].strip()
        if stripped.startswith("CUTAGENT_") and "=" in stripped:
            names.add(stripped.split("=", 1)[0])
    return names


def test_env_example_documents_every_settings_env_var() -> None:
    missing = set(_INFRA_ENV_VARS) - _env_example_names()
    assert missing == set()


def test_settings_built_in_defaults() -> None:
    settings = build_settings()

    assert settings.storage.backend == "sqlalchemy"
    assert settings.storage.database_url is None
    assert settings.storage.pool_size == 5
    assert settings.storage.max_overflow == 10
    assert settings.storage.pool_recycle == 1800
    assert settings.storage.pool_timeout == 30

    obj = settings.object_store
    assert obj.tiered is True
    assert obj.backend == "local"
    assert obj.bucket == "cutagent-local"
    assert obj.materials_bucket == ""
    assert obj.read_buckets == ()
    assert obj.local_path == ".data/objectstore"
    assert obj.s3.endpoint_url == "http://127.0.0.1:9000"
    assert obj.s3.access_key == ""
    assert obj.s3.secret_key == ""
    assert obj.s3.region_name == "us-east-1"
    assert obj.s3.addressing_style == "path"
    assert obj.s3.multipart_threshold_mb == 8
    assert obj.s3.multipart_chunk_mb == 8
    assert obj.s3.max_concurrency == 4
    assert obj.s3.connect_timeout == 10
    assert obj.s3.read_timeout == 120
    assert obj.s3.max_attempts == 5

    eph = obj.ephemeral
    assert eph.backend == "local"
    assert eph.bucket == "cutagent-ephemeral"
    assert eph.local_path == str(Path(tempfile.gettempdir()) / "cutagent-ephemeral")
    assert eph.endpoint_url == "http://127.0.0.1:9000"
    assert eph.region_name == "us-east-1"
    assert eph.addressing_style == "path"

    assert settings.workflow.runtime == "local"
    assert settings.workflow.temporal_address == "127.0.0.1:7233"
    assert settings.workflow.temporal_namespace == "default"
    assert settings.workflow.temporal_task_queue == "cutagent-production"

    assert settings.auth.registration_open is True
    assert settings.auth.registration_code_salt == "local-dev-registration-code-salt"
    assert settings.auth.seed_local_auth is True
    assert settings.auth.max_login_attempts == 8
    assert settings.auth.login_window_minutes == 15
    assert settings.auth.max_registration_attempts == 5
    assert settings.auth.registration_window_minutes == 60
    assert settings.auth.trust_forwarded_for is False
    assert settings.auth.cookie_secure is None

    assert settings.secret_store.dir == ".data/secrets"
    assert settings.media.ffmpeg_bin is None
    assert settings.media.ffprobe_bin is None
    assert settings.api.disable_background_dispatcher is False

    assert settings.motion_guard.sample_fps == 10.0
    assert settings.motion_guard.width == 360
    assert settings.motion_guard.window_sec == 1.5
    assert settings.motion_guard.hop_sec == 0.75
    assert settings.motion_guard.active_px == 1.5
    assert settings.motion_guard.hard_px == 3.0
    assert settings.motion_guard.p95_hard_px == 7.0
    assert settings.motion_guard.tail_y_range_hard_px == 70.0
    assert settings.motion_guard.tail_net_y_hard_px == 65.0
    assert settings.motion_guard.smooth_move_straightness == 0.88
    assert settings.motion_guard.smooth_move_flip_ratio == 0.16
    assert settings.motion_guard.sweep_axis_ratio == 2.3
    assert settings.motion_guard.jitter_flip_ratio == 0.22
    assert settings.motion_guard.jitter_jerk_ratio == 0.65
    assert settings.motion_guard.refine_min_duration == 0.8
    assert settings.motion_guard.refine_round_sec == 0.1

    assert settings.upload.presign_ttl_seconds == 900
    assert settings.upload.normalize_video is False
    assert settings.balance.poller_enabled is False
    assert settings.balance.poll_interval_seconds == 900
    assert settings.balance.request_timeout_seconds == 10
    assert settings.learning.retro_window_days == 3
    assert settings.learning.reward_draft_adopted == 0.2
    assert settings.learning.reward_draft_pick == -0.05
    assert settings.learning.reward_video_produced == 0.4
    assert settings.learning.reward_published == 0.7
    assert settings.learning.reward_video_discarded_script == -0.3
    assert settings.learning.reward_stale_unpublished == -0.1
    assert settings.learning.bump_min_samples == 5
    assert settings.learning.bump_miss_streak == 3
    assert settings.learning.bump_consistency_floor == 0.6

    # Provider gateway knobs (consolidated from packages/ai + packages/ops): these
    # defaults must equal the previous scattered os.getenv defaults byte-for-byte.
    prov = settings.providers
    assert prov.max_inflight == 4
    assert prov.max_qps == 4
    assert prov.circuit_breaker_enabled is False
    assert prov.circuit_error_rate_threshold == 0.5
    assert prov.circuit_window_hours == 24
    assert prov.allowed_api_hosts == ""
    assert prov.enforce_host_allowlist is False

    # Publishing 小V猫 CDP endpoint (consolidated from apps/api + packages/publishing).
    assert settings.publishing.xiaovmao_cdp_host == "127.0.0.1"
    assert settings.publishing.xiaovmao_cdp_port == 9222
    assert settings.redis_url is None


def test_settings_reads_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUTAGENT_STORAGE_BACKEND", "POSTGRES")  # lower-cased
    monkeypatch.setenv("CUTAGENT_DATABASE_URL", "postgresql+psycopg://x/db")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_TIERED", "0")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BACKEND", "S3")  # lower-cased
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_MAX_ATTEMPTS", "9")
    monkeypatch.setenv("CUTAGENT_WORKFLOW_RUNTIME", "TEMPORAL")  # lower-cased
    monkeypatch.setenv("CUTAGENT_REGISTRATION_OPEN", "false")
    monkeypatch.setenv("CUTAGENT_SEED_LOCAL_AUTH", "false")
    monkeypatch.setenv("CUTAGENT_FFMPEG_BIN", "/opt/ffmpeg")
    monkeypatch.setenv("CUTAGENT_DISABLE_BACKGROUND_DISPATCHER", "1")

    settings = build_settings()

    assert settings.storage.backend == "postgres"
    assert settings.storage.database_url == "postgresql+psycopg://x/db"
    assert settings.object_store.tiered is False
    assert settings.object_store.backend == "s3"
    assert settings.object_store.s3.max_attempts == 9
    assert settings.workflow.runtime == "temporal"
    assert settings.auth.registration_open is False
    assert settings.auth.seed_local_auth is False
    assert settings.media.ffmpeg_bin == "/opt/ffmpeg"
    assert settings.api.disable_background_dispatcher is True


def test_provider_publishing_settings_read_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUTAGENT_PROVIDER_MAX_INFLIGHT", "7")
    monkeypatch.setenv("CUTAGENT_PROVIDER_MAX_QPS", "9")
    monkeypatch.setenv("CUTAGENT_PROVIDER_CIRCUIT_BREAKER", "1")
    monkeypatch.setenv("CUTAGENT_PROVIDER_CIRCUIT_ERROR_RATE", "0.7")
    monkeypatch.setenv("CUTAGENT_PROVIDER_CIRCUIT_WINDOW", "6")
    monkeypatch.setenv("CUTAGENT_ALLOWED_API_HOSTS", "proxy.internal")
    monkeypatch.setenv("CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST", "1")
    monkeypatch.setenv("CUTAGENT_XIAOVMAO_CDP_HOST", "10.0.0.5")
    monkeypatch.setenv("CUTAGENT_XIAOVMAO_CDP_PORT", "9333")

    prov = build_providers_settings()
    assert prov.max_inflight == 7
    assert prov.max_qps == 9
    assert prov.circuit_breaker_enabled is True
    assert prov.circuit_error_rate_threshold == 0.7
    assert prov.circuit_window_hours == 6
    assert prov.allowed_api_hosts == "proxy.internal"
    assert prov.enforce_host_allowlist is True

    pub = build_publishing_settings()
    assert pub.xiaovmao_cdp_host == "10.0.0.5"
    assert pub.xiaovmao_cdp_port == 9333


def test_invalid_publishing_port_does_not_break_unrelated_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Before the settings refactor, a bad 小V猫 port only failed at the 小V猫
    # call sites that parsed it with int(os.getenv(...)); provider/object-store
    # paths must not become coupled to that unrelated env var.
    monkeypatch.setenv("CUTAGENT_XIAOVMAO_CDP_PORT", "not-an-int")

    assert build_providers_settings().max_inflight == 4
    assert build_settings().providers.max_qps == 4

    with pytest.raises(ValueError):
        _ = build_settings().publishing.xiaovmao_cdp_port


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("8", 8), ("0", 4), ("-3", 4), ("abc", 4), ("", 4)],
)
def test_provider_max_inflight_defensive_parse(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    # Mirror the previous provider_limiter._max_inflight: unset / non-integer /
    # non-positive falls back to the default rather than disabling backpressure.
    monkeypatch.setenv("CUTAGENT_PROVIDER_MAX_INFLIGHT", raw)
    assert build_settings().providers.max_inflight == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.7", 0.7), ("2", 1.0), ("-1", 0.0), ("abc", 0.5), ("", 0.5)],
)
def test_circuit_error_rate_clamped_to_unit_interval(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    # Mirror the previous circuit_breaker._float_env: invalid/unset -> default,
    # valid -> clamped to [0, 1].
    monkeypatch.setenv("CUTAGENT_PROVIDER_CIRCUIT_ERROR_RATE", raw)
    assert build_settings().providers.circuit_error_rate_threshold == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("5", 5), ("0", 1), ("-9", 1), ("abc", 24), ("", 24)],
)
def test_circuit_window_floored_at_one(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    # Mirror the previous circuit_breaker._int_env: invalid/unset -> default,
    # valid -> floored at 1.
    monkeypatch.setenv("CUTAGENT_PROVIDER_CIRCUIT_WINDOW", raw)
    assert build_settings().providers.circuit_window_hours == expected


def test_circuit_breaker_enabled_only_on_exact_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mirror the previous strict "== '1'" check (truthy strings do not enable it).
    monkeypatch.setenv("CUTAGENT_PROVIDER_CIRCUIT_BREAKER", "true")
    assert build_settings().providers.circuit_breaker_enabled is False
    monkeypatch.setenv("CUTAGENT_PROVIDER_CIRCUIT_BREAKER", "1")
    assert build_settings().providers.circuit_breaker_enabled is True


def test_build_settings_reads_env_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # The call-time-read contract: a snapshot reflects env at the moment it is
    # built, and a later env change is only visible to a fresh build.
    first = build_settings()
    assert first.object_store.bucket == "cutagent-local"

    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BUCKET", "cutagent-prod")
    assert first.object_store.bucket == "cutagent-local"  # old snapshot unchanged
    assert build_settings().object_store.bucket == "cutagent-prod"


def test_settings_is_immutable() -> None:
    settings = build_settings()
    with pytest.raises(Exception):
        settings.storage.backend = "memory"  # type: ignore[misc]


def test_local_ephemeral_store_honors_configured_bucket(tmp_path) -> None:
    # Intentional behavior of the Settings consolidation: the LOCAL ephemeral object
    # store honors a configured bucket (routed through Settings; previously hard-coded
    # for the local backend), while the default stays byte-identical. Locked here so
    # the non-default case is covered rather than being a silent, untested drift.
    from packages.core.config import EphemeralObjectStoreSettings
    from packages.core.storage.object_store_env import _ephemeral_store

    assert EphemeralObjectStoreSettings().bucket == "cutagent-ephemeral"

    cfg = EphemeralObjectStoreSettings(
        backend="local", local_path=str(tmp_path), bucket="custom-ephemeral"
    )
    store = _ephemeral_store(cfg, workflow_runtime="local", client_factory=None)
    assert store.bucket == "custom-ephemeral"
