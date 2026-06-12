from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from packages.core.contracts import SignedUrlResponse
from packages.core.storage.object_store import (
    ObjectRef,
    ObjectStore,
    StoredObject,
    parse_object_uri,
)


class TieredObjectStore(ObjectStore):
    def __init__(self, *, durable: ObjectStore, ephemeral: ObjectStore) -> None:
        self.durable = durable
        self.ephemeral = ephemeral
        durable_bucket = getattr(durable, "bucket", None)
        ephemeral_bucket = getattr(ephemeral, "bucket", None)
        if durable_bucket is not None and durable_bucket == ephemeral_bucket:
            raise ValueError("Tiered object stores must use different bucket names.")

    def prepare_upload(
        self,
        filename: str,
        purpose: str,
        *,
        content_key: str | None = None,
        tier: str = "durable",
    ) -> ObjectRef:
        store = self.ephemeral if tier == "ephemeral" else self.durable
        return store.prepare_upload(filename, purpose, content_key=content_key, tier=tier)

    def put_bytes(self, ref: ObjectRef, content: bytes) -> StoredObject:
        return self._store_for_ref(ref).put_bytes(ref, content)

    def get_bytes(self, ref: ObjectRef) -> bytes:
        return self._store_for_ref(ref).get_bytes(ref)

    def exists(self, ref: ObjectRef) -> bool:
        return self._store_for_ref(ref).exists(ref)

    def signed_url(
        self,
        uri: str,
        *,
        expires_in: timedelta = timedelta(minutes=15),
    ) -> SignedUrlResponse:
        try:
            ref = parse_object_uri(uri)
        except ValueError:
            return self.durable.signed_url(uri, expires_in=expires_in)
        return self._store_for_ref(ref).signed_url(uri, expires_in=expires_in)

    def delete(self, uri: str) -> None:
        ref = parse_object_uri(uri)
        self._store_for_ref(ref).delete(uri)

    def _path(self, ref: ObjectRef) -> Path:
        path_method = getattr(self._store_for_ref(ref), "_path", None)
        if not callable(path_method):
            raise ValueError(f"Object store cannot resolve local paths for URI: {ref.uri}")
        return path_method(ref)

    def _store_for_ref(self, ref: ObjectRef) -> ObjectStore:
        ephemeral_bucket = getattr(self.ephemeral, "bucket", None)
        durable_bucket = getattr(self.durable, "bucket", None)
        if ref.bucket == ephemeral_bucket:
            return self.ephemeral
        if ref.bucket == durable_bucket:
            return self.durable
        raise ValueError(f"Object bucket {ref.bucket} is not managed by this tiered store.")
