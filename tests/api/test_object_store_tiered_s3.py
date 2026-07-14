from __future__ import annotations

from io import BytesIO

import pytest

from packages.core.storage.object_store import (
    LocalObjectStore,
    S3ObjectStore,
    TieredObjectStore,
    object_store_from_env,
)


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(self, endpoint_url: str) -> None:
        self.endpoint_url = endpoint_url
        self.bucket_created = False
        self.objects: dict[tuple[str, str], bytes] = {}
        self.upload_calls: list[tuple[str, str]] = []
        self.download_calls: list[tuple[str, str]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def head_bucket(self, *, Bucket: str) -> None:
        if not self.bucket_created:
            raise FakeS3Error("404")

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
        self.upload_calls.append((Bucket, Key))
        self.objects[(Bucket, Key)] = Fileobj.read()

    def download_fileobj(self, Bucket: str, Key: str, Fileobj: BytesIO, Config: object) -> None:
        self.download_calls.append((Bucket, Key))
        Fileobj.write(self.objects[(Bucket, Key)])

    def head_object(self, *, Bucket: str, Key: str) -> None:
        if (Bucket, Key) not in self.objects:
            raise FakeS3Error("404")

    def generate_presigned_url(self, ClientMethod: str, Params: dict[str, str], ExpiresIn: int) -> str:
        return f"{self.endpoint_url}/{Params['Bucket']}/{Params['Key']}?X-Amz-Signature=fake"

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.delete_calls.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)


def test_object_store_from_env_builds_s3_ephemeral_and_routes_two_s3_buckets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    clients: dict[str, FakeS3Client] = {}
    observed: list[dict[str, object]] = []

    def client_factory(service_name: str, **kwargs):
        assert service_name == "s3"
        observed.append(kwargs)
        client = FakeS3Client(str(kwargs["endpoint_url"]))
        clients[str(kwargs["aws_access_key_id"])] = client
        return client

    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BACKEND", "s3")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_ENDPOINT", "https://oss.example")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BUCKET", "videoretalk-test-bucket")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_ACCESS_KEY", "durable-key")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_SECRET_KEY", "durable-secret")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_REGION", "oss-cn-shanghai")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE", "virtual")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND", "s3")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_ENDPOINT", "http://127.0.0.1:9000")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET", "cutagent-ephemeral")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_ACCESS_KEY", "ephemeral-key")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_SECRET_KEY", "ephemeral-secret")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_REGION", "us-east-1")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_ADDRESSING_STYLE", "path")
    monkeypatch.delenv("CUTAGENT_OBJECTSTORE_TIERED", raising=False)
    monkeypatch.chdir(tmp_path)

    store = object_store_from_env(client_factory=client_factory)

    assert isinstance(store, TieredObjectStore)
    assert isinstance(store.durable, S3ObjectStore)
    assert isinstance(store.ephemeral, S3ObjectStore)
    assert store.durable.bucket == "videoretalk-test-bucket"
    assert store.ephemeral.bucket == "cutagent-ephemeral"
    assert [call["endpoint_url"] for call in observed] == [
        "https://oss.example",
        "http://127.0.0.1:9000",
    ]
    assert observed[0]["config"].s3 == {"addressing_style": "virtual"}
    assert observed[1]["config"].s3 == {"addressing_style": "path"}

    durable_ref = store.prepare_upload("final.mp4", "finished-video")
    ephemeral_ref = store.prepare_upload("lipsync.mp4", "generated-video", tier="ephemeral")
    store.put_bytes(durable_ref, b"durable")
    store.put_bytes(ephemeral_ref, b"ephemeral")

    durable_client = clients["durable-key"]
    ephemeral_client = clients["ephemeral-key"]
    assert durable_client.objects[(durable_ref.bucket, durable_ref.key)] == b"durable"
    assert ephemeral_client.objects[(ephemeral_ref.bucket, ephemeral_ref.key)] == b"ephemeral"
    assert (ephemeral_ref.bucket, ephemeral_ref.key) not in durable_client.objects
    assert (durable_ref.bucket, durable_ref.key) not in ephemeral_client.objects
    assert store.get_bytes(ephemeral_ref) == b"ephemeral"
    assert store.signed_url(ephemeral_ref.uri).url.startswith("http://127.0.0.1:9000/")

    store.delete(ephemeral_ref.uri)

    assert ephemeral_client.delete_calls == [(ephemeral_ref.bucket, ephemeral_ref.key)]
    assert durable_client.delete_calls == []
    assert store.exists(durable_ref) is True
    assert store.exists(ephemeral_ref) is False


def test_tiered_object_store_rejects_same_bucket(tmp_path):
    durable = LocalObjectStore(tmp_path / "durable", bucket="shared")
    ephemeral = LocalObjectStore(tmp_path / "ephemeral", bucket="shared")

    with pytest.raises(ValueError, match="different bucket"):
        TieredObjectStore(durable=durable, ephemeral=ephemeral)
