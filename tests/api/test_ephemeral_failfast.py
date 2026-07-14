"""Fail-fast guard: refuse a node-local ephemeral tier under Temporal runtime.

Under multi-worker Temporal, an ephemeral artifact written by one worker (to a
node-local temp dir) is unreadable by an activity on another worker, producing a
silent mid-pipeline failure. The store builder must refuse to start in that
configuration and instruct the operator to use shared MinIO/S3.

Tests are isolated: no shared Postgres/Temporal/OSS — only env vars + a fake S3
client factory.
"""

from __future__ import annotations

from io import BytesIO

import pytest

from packages.core.storage.object_store import (
    LocalObjectStore,
    S3ObjectStore,
    TieredObjectStore,
    object_store_from_env,
)


class _FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeS3Client:
    def __init__(self) -> None:
        self.bucket_created = False
        self.objects: dict[tuple[str, str], bytes] = {}

    def head_bucket(self, *, Bucket: str) -> None:
        if not self.bucket_created:
            raise _FakeS3Error("404")

    def create_bucket(self, *, Bucket: str) -> None:
        self.bucket_created = True

    def upload_fileobj(
        self,
        Fileobj: BytesIO,
        Bucket: str,
        Key: str,
        Config: object,
        ExtraArgs: dict | None = None,
    ) -> None:
        self.objects[(Bucket, Key)] = Fileobj.read()

    def download_fileobj(self, Bucket: str, Key: str, Fileobj: BytesIO, Config: object) -> None:
        Fileobj.write(self.objects[(Bucket, Key)])

    def head_object(self, *, Bucket: str, Key: str) -> None:
        if (Bucket, Key) not in self.objects:
            raise _FakeS3Error("404")

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop((Bucket, Key), None)


def test_temporal_runtime_with_local_ephemeral_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setenv("CUTAGENT_WORKFLOW_RUNTIME", "temporal")
    # Ephemeral defaults to 'local' backend; durable stays local so only the
    # ephemeral guard can be the failure cause.
    monkeypatch.delenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND", raising=False)
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_BACKEND", raising=False)
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_TIERED", raising=False)
    monkeypatch.setenv("CUTAGENT_LOCAL_OBJECTSTORE_PATH", str(tmp_path / "durable"))

    with pytest.raises(RuntimeError, match="ephemeral"):
        object_store_from_env()


def test_temporal_runtime_with_explicit_local_ephemeral_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setenv("CUTAGENT_WORKFLOW_RUNTIME", "temporal")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND", "local")
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_BACKEND", raising=False)
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_TIERED", raising=False)
    monkeypatch.setenv("CUTAGENT_LOCAL_OBJECTSTORE_PATH", str(tmp_path / "durable"))

    with pytest.raises(RuntimeError) as exc:
        object_store_from_env()

    message = str(exc.value)
    assert "temporal" in message.lower()
    assert "CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND" in message


def test_temporal_runtime_with_s3_ephemeral_is_ok(
    monkeypatch: pytest.MonkeyPatch,
):
    clients: dict[str, _FakeS3Client] = {}

    def client_factory(service_name: str, **kwargs):
        assert service_name == "s3"
        client = _FakeS3Client()
        clients[str(kwargs["aws_access_key_id"])] = client
        return client

    monkeypatch.setenv("CUTAGENT_WORKFLOW_RUNTIME", "temporal")
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_TIERED", raising=False)
    # Durable on shared S3.
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BACKEND", "s3")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_ENDPOINT", "https://oss.example")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BUCKET", "cutagent-durable")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_ACCESS_KEY", "durable-key")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_SECRET_KEY", "durable-secret")
    # Ephemeral on shared S3 — the operator-recommended configuration.
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND", "s3")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_ENDPOINT", "http://minio.local:9000")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET", "cutagent-ephemeral")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_ACCESS_KEY", "ephemeral-key")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_SECRET_KEY", "ephemeral-secret")

    store = object_store_from_env(client_factory=client_factory)

    assert isinstance(store, TieredObjectStore)
    assert isinstance(store.durable, S3ObjectStore)
    assert isinstance(store.ephemeral, S3ObjectStore)
    assert store.ephemeral.bucket == "cutagent-ephemeral"


def test_local_runtime_with_local_ephemeral_is_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.setenv("CUTAGENT_WORKFLOW_RUNTIME", "local")
    monkeypatch.delenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND", raising=False)
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_BACKEND", raising=False)
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_TIERED", raising=False)
    monkeypatch.setenv("CUTAGENT_LOCAL_OBJECTSTORE_PATH", str(tmp_path / "durable"))

    store = object_store_from_env()

    assert isinstance(store, TieredObjectStore)
    assert isinstance(store.ephemeral, LocalObjectStore)


def test_unset_runtime_defaults_to_local_and_allows_local_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.delenv("CUTAGENT_WORKFLOW_RUNTIME", raising=False)
    monkeypatch.delenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND", raising=False)
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_BACKEND", raising=False)
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_TIERED", raising=False)
    monkeypatch.setenv("CUTAGENT_LOCAL_OBJECTSTORE_PATH", str(tmp_path / "durable"))

    store = object_store_from_env()

    assert isinstance(store, TieredObjectStore)
    assert isinstance(store.ephemeral, LocalObjectStore)
