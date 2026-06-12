from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest

from packages.core.storage.object_store import (
    get_object_store,
    LocalObjectStore,
    S3ObjectStore,
    object_store_from_env,
    parse_object_uri,
)


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(self) -> None:
        self.bucket_created = False
        self.objects: dict[tuple[str, str], bytes] = {}
        self.presign_calls: list[tuple[str, dict[str, str], int]] = []

    def head_bucket(self, *, Bucket: str) -> None:
        if not self.bucket_created:
            raise FakeS3Error("404")

    def create_bucket(self, *, Bucket: str) -> None:
        self.bucket_created = True

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, BytesIO]:
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    def head_object(self, *, Bucket: str, Key: str) -> None:
        if (Bucket, Key) not in self.objects:
            raise FakeS3Error("404")

    def generate_presigned_url(self, ClientMethod: str, Params: dict[str, str], ExpiresIn: int) -> str:
        self.presign_calls.append((ClientMethod, Params, ExpiresIn))
        return f"http://minio.local/{Params['Bucket']}/{Params['Key']}?X-Amz-Signature=fake"


def test_object_store_from_env_defaults_to_local(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_BACKEND", raising=False)
    monkeypatch.setenv("CUTAGENT_LOCAL_OBJECTSTORE_PATH", str(tmp_path))
    store = object_store_from_env()

    assert isinstance(store, LocalObjectStore)
    ref = store.prepare_upload("clip.mp4", "generated-video")
    stored = store.put_bytes(ref, b"local-bytes")

    assert stored.ref.uri.startswith("local://")
    assert store.exists(ref) is True
    assert store.get_bytes(ref) == b"local-bytes"


def test_prepare_upload_accepts_content_key_for_deterministic_local_key(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")

    first = store.prepare_upload("../clip.mp4", "seed-media", content_key="abc123")
    second = store.prepare_upload("../clip.mp4", "seed-media", content_key="abc123")

    assert first == second
    assert first.key == "seed-media/abc123/.._clip.mp4"
    assert first.uri == "local://cutagent-local/seed-media/abc123/.._clip.mp4"


def test_get_object_store_uses_pytest_temp_root():
    store = get_object_store()
    assert isinstance(store, LocalObjectStore)

    root = Path(store.root).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    repository_objectstore = (Path(__file__).resolve().parents[2] / ".data" / "objectstore").resolve()

    assert root.is_relative_to(temp_root)
    assert not root.is_relative_to(repository_objectstore)


def test_parse_object_uri_supports_local_and_s3():
    local_ref = parse_object_uri("local://cutagent-local/uploads/a.txt")
    s3_ref = parse_object_uri("s3://cutagent-demo/generated-video/b.mp4")

    assert local_ref.bucket == "cutagent-local"
    assert local_ref.key == "uploads/a.txt"
    assert s3_ref.bucket == "cutagent-demo"
    assert s3_ref.key == "generated-video/b.mp4"


def test_s3_object_store_put_get_exists_signed_url_and_bucket_creation(tmp_path):
    fake_client = FakeS3Client()
    store = S3ObjectStore(
        endpoint_url="http://minio.local:9000",
        bucket="cutagent-demo",
        access_key="minioadmin",
        secret_key="minioadmin",
        client=fake_client,
        cache_root=tmp_path / "cache",
    )

    ref = store.prepare_upload("../clip.mp4", "generated-video")
    stored = store.put_bytes(ref, b"s3-bytes")
    signed = store.signed_url(ref.uri, expires_in=timedelta(minutes=7))

    assert fake_client.bucket_created is True
    assert ref.uri.startswith("s3://cutagent-demo/generated-video/")
    assert ref.key.endswith("/.._clip.mp4")
    assert stored.size_bytes == len(b"s3-bytes")
    assert store.exists(ref) is True
    assert store.get_bytes(ref) == b"s3-bytes"
    assert signed.url.startswith("http://minio.local")
    assert "X-Amz-Signature=" in signed.url
    assert fake_client.presign_calls == [
        ("get_object", {"Bucket": "cutagent-demo", "Key": ref.key}, 420)
    ]
    assert (tmp_path / "cache" / ref.key).read_bytes() == b"s3-bytes"


def test_prepare_upload_accepts_content_key_for_deterministic_s3_key(tmp_path):
    store = S3ObjectStore(
        endpoint_url="http://minio.local:9000",
        bucket="cutagent-demo",
        access_key="minioadmin",
        secret_key="minioadmin",
        client=FakeS3Client(),
        cache_root=tmp_path / "cache",
    )

    first = store.prepare_upload("../clip.mp4", "seed-media", content_key="abc123")
    second = store.prepare_upload("../clip.mp4", "seed-media", content_key="abc123")

    assert first == second
    assert first.key == "seed-media/abc123/.._clip.mp4"
    assert first.uri == "s3://cutagent-demo/seed-media/abc123/.._clip.mp4"


def test_s3_object_store_roundtrip_with_minio(tmp_path):
    if os.getenv("CUTAGENT_RUN_S3_TESTS") != "1":
        pytest.skip("Set CUTAGENT_RUN_S3_TESTS=1 to run MinIO-backed ObjectStore tests.")

    bucket = f"cutagent-test-{uuid4().hex}"
    store = S3ObjectStore(
        endpoint_url=os.getenv("CUTAGENT_OBJECTSTORE_ENDPOINT", "http://127.0.0.1:9000"),
        bucket=bucket,
        access_key=os.getenv("CUTAGENT_OBJECTSTORE_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("CUTAGENT_OBJECTSTORE_SECRET_KEY", "minioadmin"),
        cache_root=tmp_path / "cache",
    )

    ref = store.prepare_upload("acceptance.txt", "acceptance")
    store.put_bytes(ref, b"minio-roundtrip")
    signed = store.signed_url(ref.uri)

    assert store.exists(ref) is True
    assert store.get_bytes(ref) == b"minio-roundtrip"
    assert signed.url.startswith(os.getenv("CUTAGENT_OBJECTSTORE_ENDPOINT", "http://127.0.0.1:9000"))
    assert "X-Amz-" in signed.url
