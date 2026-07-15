from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from packages.core.storage.object_store import LocalObjectStore, ObjectHead, S3ObjectStore
from packages.core.storage.tiered_object_store import TieredObjectStore


class FakeS3:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def head_bucket(self, **kwargs):  # called by _ensure_bucket
        pass

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.calls.append(("presign", op, Params, ExpiresIn))
        return f"https://host/{Params['Key']}?sig=1"

    def head_object(self, Bucket, Key):
        return {"ContentLength": 123, "ETag": '"abc"', "ContentType": "video/mp4"}

    def copy_object(self, Bucket, Key, CopySource, MetadataDirective):
        self.calls.append(("copy", Bucket, Key, CopySource, MetadataDirective))

    def put_bucket_cors(self, Bucket, CORSConfiguration):
        self.calls.append(("cors", Bucket, CORSConfiguration))

    def create_multipart_upload(self, **kwargs):
        self.calls.append(("create_multipart", kwargs))
        return {"UploadId": "mpu-1"}

    def list_parts(self, **kwargs):
        self.calls.append(("list_parts", kwargs))
        return {
            "Parts": [{"PartNumber": 1, "ETag": '"etag"', "Size": 8}],
            "IsTruncated": False,
        }

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete_multipart", kwargs))

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort_multipart", kwargs))


def _store(bucket: str = "cutagent-dev", read: tuple[str, ...] = ()):
    fake = FakeS3()
    store = S3ObjectStore(
        endpoint_url="https://e",
        bucket=bucket,
        read_buckets=read,
        access_key="k",
        secret_key="s",
        region_name="r",
        addressing_style="virtual",
        client=fake,
    )
    return store, fake


def test_supports_presign(tmp_path: Path):
    s3, _ = _store()
    assert s3.supports_presign() is True
    # Local is a presign-capable test/dev double (writes through the store).
    assert LocalObjectStore(root=tmp_path).supports_presign() is True


def test_local_presign_head_and_copy_roundtrip(tmp_path: Path):
    store = LocalObjectStore(root=tmp_path, bucket="cutagent-local")
    staging = store.prepare_upload("v.mp4", "incoming/uploads", content_key="u1")
    # the "browser PUT" writes through the store
    store.put_bytes(staging, b"hello-local")
    put = store.signed_put_url(
        staging.uri, content_type="video/mp4", expires_in=timedelta(minutes=15)
    )
    assert put.url == staging.uri
    head = store.head(staging.uri)
    assert head.size == len("hello-local")
    final = store.prepare_upload("v.mp4", "video", content_key="u1")
    store.copy(staging.uri, final.uri)
    assert store.head(final.uri).size == len("hello-local")
    store.ensure_cors(["https://app.shuying.cyou"])  # no-op, must not raise


def test_signed_put_url_signs_put_object_with_content_type():
    s3, fake = _store()
    result = s3.signed_put_url(
        "s3://cutagent-dev/incoming/uploads/u1/v.mp4",
        content_type="video/mp4",
        expires_in=timedelta(minutes=15),
    )
    op = fake.calls[0]
    assert op[1] == "put_object"
    assert op[2]["Bucket"] == "cutagent-dev"
    assert op[2]["ContentType"] == "video/mp4"
    assert result.url.endswith("sig=1")


def test_head_returns_metadata():
    s3, _ = _store(read=("cutagent-dev",))
    head = s3.head("s3://cutagent-dev/k")
    assert isinstance(head, ObjectHead)
    assert head.size == 123
    assert head.content_type == "video/mp4"


def test_copy_cross_bucket_does_not_read_validate_src():
    # materials store's read set is only itself; a cross-bucket copy from the
    # durable bucket must NOT be blocked by _validate_read_ref(src).
    s3, fake = _store(bucket="cutagent-materials", read=())
    s3.copy(
        "s3://cutagent-dev/incoming/uploads/u1/v.mp4",
        "s3://cutagent-materials/portrait/u1/v.mp4",
    )
    copy_call = next(c for c in fake.calls if c[0] == "copy")
    assert copy_call[1] == "cutagent-materials"
    assert copy_call[3] == {"Bucket": "cutagent-dev", "Key": "incoming/uploads/u1/v.mp4"}
    assert copy_call[4] == "COPY"


def test_copy_rejects_dst_not_self_bucket():
    s3, _ = _store(bucket="cutagent-dev")
    with pytest.raises(ValueError):
        s3.copy("s3://cutagent-dev/a", "s3://cutagent-other/b")


def test_ensure_cors_puts_rule_with_expose_etag():
    s3, fake = _store()
    s3.ensure_cors(["https://app.shuying.cyou"])
    cors_call = next(c for c in fake.calls if c[0] == "cors")
    rule = cors_call[2]["CORSRules"][0]
    assert rule["AllowedOrigins"] == ["https://app.shuying.cyou"]
    assert "PUT" in rule["AllowedMethods"]
    assert "ETag" in rule["ExposeHeaders"]


def _s3(bucket: str, fake: FakeS3, read: tuple[str, ...] = ()):
    return S3ObjectStore(
        endpoint_url="https://e",
        bucket=bucket,
        read_buckets=read,
        access_key="k",
        secret_key="s",
        client=fake,
    )


def test_tiered_routes_presign_and_cross_bucket_copy():
    dfake, efake, mfake = FakeS3(), FakeS3(), FakeS3()
    tiered = TieredObjectStore(
        durable=_s3("cutagent-dev", dfake, read=("cutagent-materials",)),
        ephemeral=_s3("cutagent-ephemeral", efake),
        materials=_s3("cutagent-materials", mfake),
    )
    assert tiered.supports_presign() is True
    # staging PUT URL (durable bucket) routes to the durable sub-store
    tiered.signed_put_url(
        "s3://cutagent-dev/incoming/uploads/u1/v.mp4",
        content_type="video/mp4",
        expires_in=timedelta(minutes=15),
    )
    assert any(c[0] == "presign" for c in dfake.calls)
    # cross-bucket copy dev->materials is executed by the materials (dst) sub-store
    tiered.copy(
        "s3://cutagent-dev/incoming/uploads/u1/v.mp4",
        "s3://cutagent-materials/portrait/u1/v.mp4",
    )
    assert any(c[0] == "copy" for c in mfake.calls)
    assert not any(c[0] == "copy" for c in dfake.calls)


def test_tiered_routes_entire_multipart_protocol_to_uri_bucket():
    dfake, efake = FakeS3(), FakeS3()
    tiered = TieredObjectStore(
        durable=_s3("cutagent-dev", dfake),
        ephemeral=_s3("cutagent-ephemeral", efake),
    )
    uri = "s3://cutagent-dev/incoming/uploads/u1/v.mp4"

    upload_id = tiered.create_multipart_upload(uri, content_type="video/mp4")
    tiered.sign_upload_part(
        uri,
        upload_id=upload_id,
        part_number=1,
        expires_in=timedelta(minutes=15),
    )
    parts = tiered.list_parts(uri, upload_id=upload_id)
    tiered.complete_multipart_upload(uri, upload_id=upload_id, parts=parts)
    tiered.abort_multipart_upload(uri, upload_id=upload_id)

    assert upload_id == "mpu-1"
    assert [call[0] for call in dfake.calls] == [
        "create_multipart",
        "presign",
        "list_parts",
        "complete_multipart",
        "abort_multipart",
    ]
    assert efake.calls == []
