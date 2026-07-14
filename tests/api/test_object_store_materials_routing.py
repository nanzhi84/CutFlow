from __future__ import annotations

from io import BytesIO

import pytest

from packages.core.storage.object_store import (
    LocalObjectStore,
    ObjectRef,
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
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}

    def head_bucket(self, *, Bucket: str) -> None:
        if Bucket not in self.buckets:
            raise FakeS3Error("404")

    def create_bucket(self, *, Bucket: str) -> None:
        self.buckets.add(Bucket)

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
            raise FakeS3Error("404")

    def generate_presigned_url(self, ClientMethod: str, Params: dict[str, str], ExpiresIn: int) -> str:
        return f"{self.endpoint_url}/{Params['Bucket']}/{Params['Key']}?X-Amz-Signature=fake"

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop((Bucket, Key), None)


def _s3_env(monkeypatch, tmp_path) -> dict[str, FakeS3Client]:
    clients: dict[str, FakeS3Client] = {}

    def client_factory(service_name: str, **kwargs):
        assert service_name == "s3"
        key = str(kwargs["aws_access_key_id"])
        client = clients.get(key)
        if client is None:
            client = FakeS3Client(str(kwargs["endpoint_url"]))
            clients[key] = client
        return client

    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BACKEND", "s3")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_ENDPOINT", "https://oss.example")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_BUCKET", "cutagent-prod")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_MATERIALS_BUCKET", "cutagent-materials")
    monkeypatch.setenv("CUTAGENT_OBJECTSTORE_READ_BUCKETS", "cutagent-materials")
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
    clients["_factory"] = client_factory  # type: ignore[assignment]
    return clients


def test_purpose_routes_materials_outputs_ephemeral(monkeypatch, tmp_path):
    clients = _s3_env(monkeypatch, tmp_path)
    store = object_store_from_env(client_factory=clients.pop("_factory"))

    assert isinstance(store, TieredObjectStore)
    assert isinstance(store.materials, S3ObjectStore)
    assert store.durable.bucket == "cutagent-prod"
    assert store.materials.bucket == "cutagent-materials"
    assert store.ephemeral.bucket == "cutagent-ephemeral"

    material_ref = store.prepare_upload("portrait.mp4", "portrait")
    output_ref = store.prepare_upload("cover.png", "covers")
    ephemeral_ref = store.prepare_upload("scratch.mp4", "generated-video", tier="ephemeral")

    assert material_ref.bucket == "cutagent-materials"
    assert output_ref.bucket == "cutagent-prod"
    assert ephemeral_ref.bucket == "cutagent-ephemeral"

    store.put_bytes(material_ref, b"mat")
    store.put_bytes(output_ref, b"out")
    store.put_bytes(ephemeral_ref, b"eph")
    assert store.get_bytes(material_ref) == b"mat"
    assert store.get_bytes(output_ref) == b"out"
    assert store.get_bytes(ephemeral_ref) == b"eph"
    # materials + outputs share creds/client but land in distinct buckets
    durable_client = clients["durable-key"]
    assert ("cutagent-materials", material_ref.key) in durable_client.objects
    assert ("cutagent-prod", output_ref.key) in durable_client.objects


def test_s3_store_read_write_guard_split():
    client = FakeS3Client("http://x")
    store = S3ObjectStore(
        endpoint_url="http://x",
        bucket="wbucket",
        read_buckets=("rbucket",),
        access_key="k",
        secret_key="s",
        client=client,
    )
    own = ObjectRef(bucket="wbucket", key="a", uri="s3://wbucket/a")
    store.put_bytes(own, b"x")
    assert store.get_bytes(own) == b"x"

    client.objects[("rbucket", "b")] = b"y"
    read_only = ObjectRef(bucket="rbucket", key="b", uri="s3://rbucket/b")
    assert store.get_bytes(read_only) == b"y"
    with pytest.raises(ValueError, match="not writable"):
        store.put_bytes(read_only, b"z")

    unknown = ObjectRef(bucket="ubucket", key="c", uri="s3://ubucket/c")
    with pytest.raises(ValueError, match="not readable"):
        store.get_bytes(unknown)


def test_tiered_rejects_materials_bucket_collision(tmp_path):
    durable = LocalObjectStore(tmp_path / "d", bucket="out")
    ephemeral = LocalObjectStore(tmp_path / "e", bucket="eph")
    materials = LocalObjectStore(tmp_path / "m", bucket="out")
    with pytest.raises(ValueError, match="different bucket"):
        TieredObjectStore(durable=durable, ephemeral=ephemeral, materials=materials)
