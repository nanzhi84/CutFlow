from __future__ import annotations

import hashlib
from datetime import timedelta

from packages.core.contracts import SignedUrlResponse, utcnow
from packages.core.storage.object_store import (
    LocalObjectStore,
    ObjectRef,
    ObjectStore,
    StoredObject,
)
from packages.media.assets import store_file


class RecordingObjectStore(ObjectStore):
    def __init__(self) -> None:
        self.prepare_calls: list[tuple[str, str, str | None, str]] = []

    def prepare_upload(
        self,
        filename: str,
        purpose: str,
        *,
        content_key: str | None = None,
        tier: str = "durable",
    ) -> ObjectRef:
        self.prepare_calls.append((filename, purpose, content_key, tier))
        return ObjectRef(
            bucket=f"cutagent-{tier}",
            key=f"{purpose}/{filename}",
            uri=f"local://cutagent-{tier}/{purpose}/{filename}",
        )

    def put_bytes(self, ref: ObjectRef, content: bytes) -> StoredObject:
        return StoredObject(
            ref=ref,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def get_bytes(self, ref: ObjectRef) -> bytes:
        raise NotImplementedError

    def exists(self, ref: ObjectRef) -> bool:
        raise NotImplementedError

    def signed_url(
        self,
        uri: str,
        *,
        expires_in: timedelta = timedelta(minutes=15),
    ) -> SignedUrlResponse:
        return SignedUrlResponse(url=uri, expires_at=utcnow() + expires_in, request_id="req_test")


def test_store_file_addressed_reuses_same_object_key_for_same_content(tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    source = tmp_path / "seed.mp4"
    source.write_bytes(b"same seed media bytes")

    first = store_file(object_store, source, purpose="seed-media", addressed=True)
    second = store_file(object_store, source, purpose="seed-media", addressed=True)

    assert second.ref.uri == first.ref.uri
    assert second.sha256 == hashlib.sha256(b"same seed media bytes").hexdigest()
    assert [path for path in (tmp_path / "objects").rglob("*") if path.is_file()] == [
        tmp_path / "objects" / first.ref.key
    ]


def test_store_file_forwards_tier_to_prepare_upload(tmp_path):
    object_store = RecordingObjectStore()
    source = tmp_path / "rendered.mp4"
    source.write_bytes(b"rendered bytes")

    stored = store_file(object_store, source, purpose="generated-video", tier="ephemeral")

    assert object_store.prepare_calls == [
        ("rendered.mp4", "generated-video", None, "ephemeral")
    ]
    assert stored.ref.bucket == "cutagent-ephemeral"
