from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from packages.core.config import (
    EphemeralObjectStoreSettings,
    ObjectStoreSettings,
    build_settings,
)


def object_store_from_env(*, client_factory: Callable[..., Any] | None = None):
    from packages.core.storage.tiered_object_store import TieredObjectStore

    config = build_settings().object_store
    durable = _durable_store(config, client_factory=client_factory)
    if not config.tiered:
        return durable
    ephemeral = _ephemeral_store(config.ephemeral, client_factory=client_factory)
    return TieredObjectStore(durable=durable, ephemeral=ephemeral)


def _durable_store(
    config: ObjectStoreSettings, *, client_factory: Callable[..., Any] | None
):
    from packages.core.storage.object_store import LocalObjectStore, S3ObjectStore

    backend = config.backend
    bucket = config.bucket
    if backend == "local":
        return LocalObjectStore(root=Path(config.local_path), bucket=bucket)
    if backend == "s3":
        s3 = config.s3
        return S3ObjectStore(
            endpoint_url=s3.endpoint_url,
            bucket=bucket,
            access_key=s3.access_key,
            secret_key=s3.secret_key,
            region_name=s3.region_name,
            addressing_style=s3.addressing_style,
            client_factory=client_factory,
            multipart_threshold_mb=s3.multipart_threshold_mb,
            multipart_chunk_mb=s3.multipart_chunk_mb,
            max_concurrency=s3.max_concurrency,
            connect_timeout=s3.connect_timeout,
            read_timeout=s3.read_timeout,
            max_attempts=s3.max_attempts,
        )
    raise ValueError(f"Unsupported object store backend: {backend}")


def _ephemeral_store(
    config: EphemeralObjectStoreSettings, *, client_factory: Callable[..., Any] | None
):
    from packages.core.storage.object_store import LocalObjectStore, S3ObjectStore

    backend = config.backend
    if backend == "local":
        return LocalObjectStore(root=Path(config.local_path), bucket=config.bucket)
    if backend == "s3":
        return S3ObjectStore(
            endpoint_url=config.endpoint_url,
            bucket=config.bucket,
            access_key=config.access_key,
            secret_key=config.secret_key,
            region_name=config.region_name,
            addressing_style=config.addressing_style,
            client_factory=client_factory,
        )
    raise ValueError(f"Unsupported ephemeral object store backend: {backend}")
