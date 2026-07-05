from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from packages.core.contracts import SignedUrlResponse
from packages.core.storage.object_store import (
    ObjectHead,
    ObjectRef,
    ObjectStore,
    StoredObject,
    parse_object_uri,
)

# 'material' purposes are user-provided SOURCE assets (a shared, reusable library).
# Everything else (pipeline-generated outputs, derived previews, thumbnails) is an
# OUTPUT and stays in the per-environment durable bucket. Default-to-output is the
# safe fallback for any unrecognised purpose.
_MATERIAL_PURPOSES = frozenset(
    {
        "portrait",
        "broll",
        "video",
        "voice_reference",
        "bgm",
        "font",
        "cover_template",
        "thumbnails",  # thumbnails of source assets travel with the material
    }
)


class TieredObjectStore(ObjectStore):
    def __init__(
        self,
        *,
        durable: ObjectStore,
        ephemeral: ObjectStore,
        materials: ObjectStore | None = None,
    ) -> None:
        self.durable = durable
        self.ephemeral = ephemeral
        # Optional third write tier: 'material' purposes (user-provided source
        # assets) route here so a shared materials bucket can be read by every
        # environment while pipeline OUTPUTS stay in each env's durable bucket.
        self.materials = materials
        named = [
            b
            for b in (
                getattr(durable, "bucket", None),
                getattr(ephemeral, "bucket", None),
                getattr(materials, "bucket", None) if materials is not None else None,
            )
            if b is not None
        ]
        if len(named) != len(set(named)):
            raise ValueError("Tiered object stores must use different bucket names.")

    def prepare_upload(
        self,
        filename: str,
        purpose: str,
        *,
        content_key: str | None = None,
        tier: str = "durable",
    ) -> ObjectRef:
        if tier == "ephemeral":
            store: ObjectStore = self.ephemeral
        elif self.materials is not None and purpose in _MATERIAL_PURPOSES:
            store = self.materials
        else:
            store = self.durable
        return store.prepare_upload(filename, purpose, content_key=content_key, tier=tier)

    def put_bytes(self, ref: ObjectRef, content: bytes) -> StoredObject:
        return self._store_for_ref(ref).put_bytes(ref, content)

    def get_bytes(self, ref: ObjectRef) -> bytes:
        return self._store_for_ref(ref).get_bytes(ref)

    def upload_file(
        self,
        local_path: Path,
        ref: ObjectRef,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        return self._store_for_ref(ref).upload_file(
            local_path,
            ref,
            content_type=content_type,
        )

    def download_file(self, ref: ObjectRef, local_path: Path) -> Path:
        return self._store_for_ref(ref).download_file(ref, local_path)

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

    def supports_presign(self) -> bool:
        # Staging uploads always land in the durable tier, so its capability decides.
        return self.durable.supports_presign()

    def signed_put_url(
        self, uri: str, *, content_type: str, expires_in: timedelta
    ) -> SignedUrlResponse:
        return self._store_for_ref(parse_object_uri(uri)).signed_put_url(
            uri, content_type=content_type, expires_in=expires_in
        )

    def head(self, uri: str) -> ObjectHead:
        return self._store_for_ref(parse_object_uri(uri)).head(uri)

    def copy(self, src_uri: str, dst_uri: str) -> None:
        # Route to the DESTINATION sub-store: it owns write access to its bucket.
        # The source bucket is only a CopySource param (see S3ObjectStore.copy).
        self._store_for_ref(parse_object_uri(dst_uri)).copy(src_uri, dst_uri)

    def ensure_cors(
        self, origins: list[str], *, expose: list[str] | None = None, max_age: int = 600
    ) -> None:
        # Browser PUTs only ever target the durable staging bucket.
        self.durable.ensure_cors(origins, expose=expose, max_age=max_age)

    def _path(self, ref: ObjectRef) -> Path:
        path_method = getattr(self._store_for_ref(ref), "_path", None)
        if not callable(path_method):
            raise ValueError(f"Object store cannot resolve local paths for URI: {ref.uri}")
        return path_method(ref)

    def _store_for_ref(self, ref: ObjectRef) -> ObjectStore:
        if ref.bucket == getattr(self.ephemeral, "bucket", None):
            return self.ephemeral
        if self.materials is not None and ref.bucket == getattr(
            self.materials, "bucket", None
        ):
            return self.materials
        # Default to the durable store; its own read/write guard accepts its write
        # bucket plus any configured read-only buckets, and raises otherwise.
        return self.durable
