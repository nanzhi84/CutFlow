"""Contract tests for the central infra Settings (packages.core.config).

These pin the built-in defaults (which must equal the defaults the previous
scattered os.getenv calls used) and the env-override / call-time-read semantics
that the rest of the codebase relies on.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from packages.core.config import Settings, build_settings, get_settings

# Every infra env var Settings reads — cleared so we observe the built-in
# defaults rather than whatever the surrounding process/conftest exported.
_INFRA_ENV_VARS = (
    "CUTAGENT_STORAGE_BACKEND",
    "CUTAGENT_DATABASE_URL",
    "CUTAGENT_OBJECTSTORE_TIERED",
    "CUTAGENT_OBJECTSTORE_BACKEND",
    "CUTAGENT_OBJECTSTORE_BUCKET",
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
    "CUTAGENT_SECRET_STORE_DIR",
    "CUTAGENT_FFMPEG_BIN",
    "CUTAGENT_FFPROBE_BIN",
    "CUTAGENT_DISABLE_BACKGROUND_DISPATCHER",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _INFRA_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_settings_built_in_defaults(clean_env) -> None:
    settings = build_settings()

    assert settings.storage.backend == "sqlalchemy"
    assert settings.storage.database_url is None

    obj = settings.object_store
    assert obj.tiered is True
    assert obj.backend == "local"
    assert obj.bucket == "cutagent-local"
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

    assert settings.secret_store.dir == ".data/secrets"
    assert settings.media.ffmpeg_bin is None
    assert settings.media.ffprobe_bin is None
    assert settings.api.disable_background_dispatcher is False


def test_settings_reads_env_overrides(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUTAGENT_STORAGE_BACKEND", "MEMORY")  # lower-cased
    monkeypatch.setenv("CUTAGENT_DATABASE_URL", "postgresql+psycopg://x/db")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_TIERED", "0")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BACKEND", "S3")  # lower-cased
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_MAX_ATTEMPTS", "9")
    monkeypatch.setenv("CUTAGENT_WORKFLOW_RUNTIME", "TEMPORAL")  # lower-cased
    monkeypatch.setenv("CUTAGENT_REGISTRATION_OPEN", "false")
    monkeypatch.setenv("CUTAGENT_FFMPEG_BIN", "/opt/ffmpeg")
    monkeypatch.setenv("CUTAGENT_DISABLE_BACKGROUND_DISPATCHER", "1")

    settings = build_settings()

    assert settings.storage.backend == "memory"
    assert settings.storage.database_url == "postgresql+psycopg://x/db"
    assert settings.object_store.tiered is False
    assert settings.object_store.backend == "s3"
    assert settings.object_store.s3.max_attempts == 9
    assert settings.workflow.runtime == "temporal"
    assert settings.auth.registration_open is False
    assert settings.media.ffmpeg_bin == "/opt/ffmpeg"
    assert settings.api.disable_background_dispatcher is True


def test_build_settings_reads_env_at_call_time(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    # The call-time-read contract: a snapshot reflects env at the moment it is
    # built, and a later env change is only visible to a fresh build.
    first = build_settings()
    assert first.object_store.bucket == "cutagent-local"

    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BUCKET", "cutagent-prod")
    assert first.object_store.bucket == "cutagent-local"  # old snapshot unchanged
    assert build_settings().object_store.bucket == "cutagent-prod"


def test_get_settings_returns_settings_snapshot(clean_env) -> None:
    assert isinstance(get_settings(), Settings)


def test_settings_is_immutable(clean_env) -> None:
    settings = build_settings()
    with pytest.raises(Exception):
        settings.storage.backend = "memory"  # type: ignore[misc]


def test_local_ephemeral_store_honors_configured_bucket(clean_env, tmp_path) -> None:
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
