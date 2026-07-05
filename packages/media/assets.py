from __future__ import annotations

from pathlib import Path

from packages.core.storage.object_store import (
    ObjectStore,
    StoredObject,
    parse_local_uri,
    sha256_file,
)


def local_object_path(object_store: ObjectStore, uri: str) -> Path:
    ref = parse_local_uri(uri)
    path_method = getattr(object_store, "_path", None)
    if callable(path_method):
        return path_method(ref)
    root = getattr(object_store, "root", None)
    if root is None:
        raise ValueError(f"Object store cannot resolve local paths for URI: {uri}")
    return Path(root) / ref.key


def store_file(
    object_store: ObjectStore,
    path: Path,
    *,
    purpose: str,
    addressed: bool = False,
    tier: str = "durable",
    content_type: str | None = None,
):
    # Stream the sha256 off disk instead of read_bytes() to avoid buffering the
    # whole (potentially minutes-long video) object in RAM for content addressing.
    content_key = sha256_file(path) if addressed else None
    ref = object_store.prepare_upload(path.name, purpose, content_key=content_key, tier=tier)
    if addressed and content_key is not None and object_store.exists(ref) and not content_type:
        return StoredObject(ref=ref, size_bytes=path.stat().st_size, sha256=content_key)
    # Path-based upload: S3 streams a multipart upload from disk; Local falls back
    # to a bytes read via the ObjectStore.upload_file default.
    return object_store.upload_file(path, ref, content_type=content_type)
