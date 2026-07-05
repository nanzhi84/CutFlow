from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from packages.core.storage.object_store import (
    get_object_store,
    LocalObjectStore,
    S3ObjectStore,
    TieredObjectStore,
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
        self.upload_calls: list[tuple[str, str, object]] = []
        self.download_calls: list[tuple[str, str, object]] = []
        self.presign_calls: list[tuple[str, dict[str, str], int]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def head_bucket(self, *, Bucket: str) -> None:
        if not self.bucket_created:
            raise FakeS3Error("404")

    def create_bucket(self, *, Bucket: str) -> None:
        self.bucket_created = True

    def upload_fileobj(self, Fileobj: BytesIO, Bucket: str, Key: str, Config: object) -> None:
        self.upload_calls.append((Bucket, Key, Config))
        self.objects[(Bucket, Key)] = Fileobj.read()

    def download_fileobj(self, Bucket: str, Key: str, Fileobj: BytesIO, Config: object) -> None:
        self.download_calls.append((Bucket, Key, Config))
        Fileobj.write(self.objects[(Bucket, Key)])

    def head_object(self, *, Bucket: str, Key: str) -> None:
        if (Bucket, Key) not in self.objects:
            raise FakeS3Error("404")

    def generate_presigned_url(self, ClientMethod: str, Params: dict[str, str], ExpiresIn: int) -> str:
        self.presign_calls.append((ClientMethod, Params, ExpiresIn))
        return f"http://minio.local/{Params['Bucket']}/{Params['Key']}?X-Amz-Signature=fake"

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.delete_calls.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)


def test_object_store_from_env_defaults_to_tiered_local(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_BACKEND", raising=False)
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_TIERED", raising=False)
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_EPHEMERAL_PATH", raising=False)
    monkeypatch.setenv("CUTAGENT_LOCAL_OBJECTSTORE_PATH", str(tmp_path / "durable"))
    store = object_store_from_env()

    assert isinstance(store, TieredObjectStore)
    assert isinstance(store.durable, LocalObjectStore)
    assert isinstance(store.ephemeral, LocalObjectStore)
    assert store.durable.root == tmp_path / "durable"
    assert store.ephemeral.bucket == "cutagent-ephemeral"
    assert store.ephemeral.root == Path(tempfile.gettempdir()) / "cutagent-ephemeral"
    ref = store.prepare_upload("clip.mp4", "generated-video")
    stored = store.put_bytes(ref, b"local-bytes")

    assert stored.ref.uri.startswith("local://")
    assert store.exists(ref) is True
    assert store.get_bytes(ref) == b"local-bytes"


def test_object_store_from_env_tiered_zero_returns_durable_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_BACKEND", raising=False)
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_TIERED", "0")
    monkeypatch.setenv("CUTAGENT_LOCAL_OBJECTSTORE_PATH", str(tmp_path / "durable"))

    store = object_store_from_env()

    assert isinstance(store, LocalObjectStore)
    assert store.root == tmp_path / "durable"


def test_tiered_object_store_routes_by_tier_and_bucket(tmp_path):
    durable = LocalObjectStore(tmp_path / "durable", bucket="cutagent-durable")
    ephemeral = LocalObjectStore(tmp_path / "ephemeral", bucket="cutagent-ephemeral")
    store = TieredObjectStore(durable=durable, ephemeral=ephemeral)

    durable_ref = store.prepare_upload("final.mp4", "generated-video", tier="durable")
    ephemeral_ref = store.prepare_upload("rendered.mp4", "generated-video", tier="ephemeral")

    assert durable_ref.bucket == "cutagent-durable"
    assert ephemeral_ref.bucket == "cutagent-ephemeral"

    store.put_bytes(durable_ref, b"durable-bytes")
    store.put_bytes(ephemeral_ref, b"ephemeral-bytes")

    assert (durable.root / durable_ref.key).read_bytes() == b"durable-bytes"
    assert (ephemeral.root / ephemeral_ref.key).read_bytes() == b"ephemeral-bytes"
    assert store.exists(durable_ref) is True
    assert store.exists(ephemeral_ref) is True
    assert store.get_bytes(durable_ref) == b"durable-bytes"
    assert store.get_bytes(ephemeral_ref) == b"ephemeral-bytes"
    assert store.signed_url(durable_ref.uri).url == durable_ref.uri
    assert store.signed_url(ephemeral_ref.uri).url == ephemeral_ref.uri

    store.delete(ephemeral_ref.uri)

    assert store.exists(ephemeral_ref) is False
    assert store.exists(durable_ref) is True

    store.delete(durable_ref.uri)

    assert store.exists(durable_ref) is False


def test_tiered_object_store_delegates_unparseable_signed_url_to_durable(tmp_path):
    durable = LocalObjectStore(tmp_path / "durable", bucket="cutagent-durable")
    ephemeral = LocalObjectStore(tmp_path / "ephemeral", bucket="cutagent-ephemeral")
    store = TieredObjectStore(durable=durable, ephemeral=ephemeral)

    signed = store.signed_url("https://media.example/tts.mp3")

    assert signed.url == "https://media.example/tts.mp3"


def test_prepare_upload_accepts_content_key_for_deterministic_local_key(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")

    first = store.prepare_upload("../clip.mp4", "seed-media", content_key="abc123")
    second = store.prepare_upload("../clip.mp4", "seed-media", content_key="abc123")

    assert first == second
    assert first.key == "seed-media/abc123/.._clip.mp4"
    assert first.uri == "local://cutagent-local/seed-media/abc123/.._clip.mp4"


def test_get_object_store_uses_pytest_temp_root():
    store = get_object_store()
    assert isinstance(store, TieredObjectStore)
    assert isinstance(store.durable, LocalObjectStore)
    assert isinstance(store.ephemeral, LocalObjectStore)

    root = Path(store.durable.root).resolve()
    ephemeral_root = Path(store.ephemeral.root).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    configured_root = Path(os.environ["CUTAGENT_LOCAL_OBJECTSTORE_PATH"]).resolve()
    repository_objectstore = (Path(__file__).resolve().parents[2] / ".data" / "objectstore").resolve()

    # The durable tier honors the configured throwaway local objectstore path
    # (the test harness points CUTAGENT_LOCAL_OBJECTSTORE_PATH at a temp dir), and
    # the ephemeral tier lives under the system temp dir — never the repository's
    # persistent .data/objectstore.
    assert root == configured_root
    assert ephemeral_root.is_relative_to(temp_root)
    assert not root.is_relative_to(repository_objectstore)
    assert not ephemeral_root.is_relative_to(repository_objectstore)


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
    assert stored.sha256 == hashlib.sha256(b"s3-bytes").hexdigest()
    assert fake_client.upload_calls == [(ref.bucket, ref.key, store._transfer_config)]
    assert store.exists(ref) is True
    assert store.get_bytes(ref) == b"s3-bytes"
    assert fake_client.download_calls == [(ref.bucket, ref.key, store._transfer_config)]
    assert signed.url.startswith("http://minio.local")
    assert "X-Amz-Signature=" in signed.url
    assert fake_client.presign_calls == [
        ("get_object", {"Bucket": "cutagent-demo", "Key": ref.key}, 420)
    ]
    assert (tmp_path / "cache" / ref.bucket / ref.key).read_bytes() == b"s3-bytes"

    store.delete(ref.uri)

    assert fake_client.delete_calls == [(ref.bucket, ref.key)]
    assert (tmp_path / "cache" / ref.bucket / ref.key).exists() is False
    assert store.exists(ref) is False


def test_s3_object_store_uses_native_oss_signed_get_url_for_aliyun_endpoint(tmp_path):
    fake_client = FakeS3Client()
    store = S3ObjectStore(
        endpoint_url="https://oss-cn-shanghai.aliyuncs.com",
        bucket="cutagent-demo",
        access_key="oss-key",
        secret_key="oss-secret",
        region_name="oss-cn-shanghai",
        addressing_style="virtual",
        client=fake_client,
        cache_root=tmp_path / "cache",
    )
    ref = store.prepare_upload("clip space.mp4", "clip-embeddings", content_key="sha")

    signed = store.signed_url(ref.uri, expires_in=timedelta(minutes=7))
    parsed = urlsplit(signed.url)
    query = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "cutagent-demo.oss-cn-shanghai.aliyuncs.com"
    assert parsed.path == "/clip-embeddings/sha/clip%20space.mp4"
    assert set(query) == {"OSSAccessKeyId", "Expires", "Signature"}
    assert query["OSSAccessKeyId"] == ["oss-key"]
    assert fake_client.presign_calls == []


def test_s3_object_store_passes_addressing_style_and_checksum_config_to_client_factory(tmp_path):
    observed: dict[str, object] = {}

    def client_factory(service_name: str, **kwargs):
        observed["service_name"] = service_name
        observed.update(kwargs)
        return FakeS3Client()

    S3ObjectStore(
        endpoint_url="https://oss-cn-shanghai.aliyuncs.com",
        bucket="cutagent-demo",
        access_key="oss-key",
        secret_key="oss-secret",
        region_name="oss-cn-shanghai",
        addressing_style="virtual",
        client_factory=client_factory,
        cache_root=tmp_path / "cache",
    )

    config = observed["config"]
    assert observed["service_name"] == "s3"
    assert config.s3 == {"addressing_style": "virtual"}
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
    assert config.connect_timeout == 10
    assert config.read_timeout == 120
    assert config.retries == {"max_attempts": 5, "mode": "standard"}


def test_s3_object_store_uses_transfer_config_defaults(tmp_path):
    fake_client = FakeS3Client()
    store = S3ObjectStore(
        endpoint_url="http://minio.local:9000",
        bucket="cutagent-demo",
        access_key="minioadmin",
        secret_key="minioadmin",
        client=fake_client,
        cache_root=tmp_path / "cache",
    )

    ref = store.prepare_upload("clip.mp4", "generated-video")
    store.put_bytes(ref, b"default-transfer")

    transfer_config = fake_client.upload_calls[0][2]
    assert transfer_config.multipart_threshold == 8 * 1024 * 1024
    assert transfer_config.multipart_chunksize == 8 * 1024 * 1024
    assert transfer_config.max_concurrency == 4
    assert transfer_config.use_threads is True


def test_s3_object_store_uses_custom_transfer_config_and_client_timeouts(tmp_path):
    observed: dict[str, object] = {}

    def client_factory(service_name: str, **kwargs):
        observed["service_name"] = service_name
        observed.update(kwargs)
        return FakeS3Client()

    store = S3ObjectStore(
        endpoint_url="https://oss-cn-shanghai.aliyuncs.com",
        bucket="cutagent-demo",
        access_key="oss-key",
        secret_key="oss-secret",
        region_name="oss-cn-shanghai",
        addressing_style="virtual",
        client_factory=client_factory,
        cache_root=tmp_path / "cache",
        multipart_threshold_mb=12,
        multipart_chunk_mb=16,
        max_concurrency=7,
        connect_timeout=3,
        read_timeout=45,
        max_attempts=9,
    )

    config = observed["config"]
    assert config.connect_timeout == 3
    assert config.read_timeout == 45
    assert config.retries == {"max_attempts": 9, "mode": "standard"}
    assert store._transfer_config.multipart_threshold == 12 * 1024 * 1024
    assert store._transfer_config.multipart_chunksize == 16 * 1024 * 1024
    assert store._transfer_config.max_concurrency == 7
    assert store._transfer_config.use_threads is True


def test_object_store_from_env_passes_s3_addressing_style(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    observed: dict[str, object] = {}

    def build_client(**kwargs):
        observed.update(kwargs)
        return FakeS3Client()

    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BACKEND", "s3")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_ENDPOINT", "https://oss-cn-shanghai.aliyuncs.com")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BUCKET", "cutagent-demo")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_ACCESS_KEY", "oss-key")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_SECRET_KEY", "oss-secret")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_REGION", "oss-cn-shanghai")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE", "virtual")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_MULTIPART_THRESHOLD_MB", "10")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_MULTIPART_CHUNK_MB", "12")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_MAX_CONCURRENCY", "6")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_CONNECT_TIMEOUT", "4")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_READ_TIMEOUT", "60")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_MAX_ATTEMPTS", "8")
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_TIERED", raising=False)
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_EPHEMERAL_PATH", str(tmp_path / "ephemeral"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(S3ObjectStore, "_build_client", staticmethod(build_client))

    store = object_store_from_env()

    assert isinstance(store, TieredObjectStore)
    assert isinstance(store.durable, S3ObjectStore)
    assert isinstance(store.ephemeral, LocalObjectStore)
    assert observed["addressing_style"] == "virtual"
    assert observed["connect_timeout"] == 4
    assert observed["read_timeout"] == 60
    assert observed["max_attempts"] == 8
    assert store.durable._transfer_config.multipart_threshold == 10 * 1024 * 1024
    assert store.durable._transfer_config.multipart_chunksize == 12 * 1024 * 1024
    assert store.durable._transfer_config.max_concurrency == 6
    assert store.durable._transfer_config.use_threads is True
    assert store.ephemeral.root == tmp_path / "ephemeral"


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
