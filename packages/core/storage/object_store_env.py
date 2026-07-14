from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from packages.core.config import (
    EphemeralObjectStoreSettings,
    ObjectStoreSettings,
    build_object_store_settings,
    build_redis_url,
    build_workflow_settings,
)


def object_store_from_env(*, client_factory: Callable[..., Any] | None = None):
    """Build an object store from the current environment.

    Thin convenience over :func:`object_store_from_settings` that reads the
    object-store + workflow settings groups from ``os.environ`` at call time.
    """
    return object_store_from_settings(
        build_object_store_settings(),
        workflow_runtime=build_workflow_settings().runtime,
        client_factory=client_factory,
        redis_url=build_redis_url(),
    )


def object_store_from_settings(
    config: ObjectStoreSettings,
    *,
    workflow_runtime: str,
    client_factory: Callable[..., Any] | None = None,
    redis_url: str | None = None,
):
    """Build an object store from an already-built settings snapshot.

    The explicit-settings counterpart to :func:`object_store_from_env`, so the
    API lifespan / worker can construct the store from the ``Settings`` they
    already hold (and inject it via ``configure_object_store``) instead of
    re-reading the environment. See issue #64.

    Every tier shares ONE signed-URL cache (issue #206): cache keys carry the
    bucket, and a single cache means a single Redis client and a single LRU, so
    all replicas of the API hand the browser the same URL for the same object.
    """
    from packages.core.storage.object_store import SignedUrlCache
    from packages.core.storage.tiered_object_store import TieredObjectStore

    signing = _SigningConfig(
        signed_get_ttl=timedelta(seconds=config.signed_get_ttl_seconds),
        signed_url_cache=SignedUrlCache(redis_url=redis_url),
    )
    # The durable store must also be able to READ material-bucket refs (when the
    # tiered store is off, or as a fallback), so fold materials_bucket into its read
    # set; in tiered mode material refs still route to the materials sub-store.
    durable_read_buckets = tuple(config.read_buckets)
    if config.materials_bucket:
        durable_read_buckets += (config.materials_bucket,)
    durable = _durable_store(
        config,
        client_factory=client_factory,
        read_buckets=durable_read_buckets,
        signing=signing,
    )
    if not config.tiered:
        return durable
    ephemeral = _ephemeral_store(
        config.ephemeral,
        workflow_runtime=workflow_runtime,
        client_factory=client_factory,
        signing=signing,
    )
    materials = None
    if config.materials_bucket:
        materials = _durable_store(
            config,
            client_factory=client_factory,
            bucket=config.materials_bucket,
            signing=signing,
        )
    return TieredObjectStore(durable=durable, ephemeral=ephemeral, materials=materials)


class _SigningConfig:
    """The signed-GET knobs every tier is built with (TTL + the shared URL cache)."""

    __slots__ = ("signed_get_ttl", "signed_url_cache")

    def __init__(
        self, *, signed_get_ttl: timedelta | None = None, signed_url_cache: Any = None
    ) -> None:
        from packages.core.storage.object_store import DEFAULT_SIGNED_GET_TTL, SignedUrlCache

        self.signed_get_ttl = signed_get_ttl or DEFAULT_SIGNED_GET_TTL
        self.signed_url_cache = signed_url_cache or SignedUrlCache()


def _durable_store(
    config: ObjectStoreSettings,
    *,
    client_factory: Callable[..., Any] | None,
    signing: _SigningConfig | None = None,
    bucket: str | None = None,
    read_buckets: tuple[str, ...] = (),
):
    signing = signing or _SigningConfig()
    from packages.core.storage.object_store import LocalObjectStore, S3ObjectStore

    backend = config.backend
    bucket = bucket or config.bucket
    if backend == "local":
        return LocalObjectStore(
            root=Path(config.local_path),
            bucket=bucket,
            signed_get_ttl=signing.signed_get_ttl,
            signed_url_cache=signing.signed_url_cache,
        )
    if backend == "s3":
        s3 = config.s3
        return S3ObjectStore(
            endpoint_url=s3.endpoint_url,
            bucket=bucket,
            read_buckets=read_buckets,
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
            signed_get_ttl=signing.signed_get_ttl,
            signed_url_cache=signing.signed_url_cache,
        )
    raise ValueError(f"Unsupported object store backend: {backend}")


def _ephemeral_store(
    config: EphemeralObjectStoreSettings,
    *,
    workflow_runtime: str,
    client_factory: Callable[..., Any] | None,
    signing: _SigningConfig | None = None,
):
    from packages.core.storage.object_store import LocalObjectStore, S3ObjectStore

    signing = signing or _SigningConfig()
    backend = config.backend
    if backend == "local":
        # Fail fast under Temporal: a node-local ephemeral tier is invisible to
        # activities running on other workers, causing silent mid-pipeline
        # failures. The operator must point the ephemeral tier at shared
        # MinIO/S3. Local runtime keeps the local default.
        if workflow_runtime == "temporal":
            raise RuntimeError(
                "Invalid ObjectStore configuration: ephemeral tier resolves to a "
                "node-local 'local' backend while CUTAGENT_WORKFLOW_RUNTIME=temporal. "
                "Under multi-worker Temporal, ephemeral artifacts written by one "
                "worker are unreadable by activities on another worker, causing "
                "silent mid-pipeline failures. Point the ephemeral tier at shared "
                "MinIO/S3: set CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND=s3 (and the "
                "related CUTAGENT_EPHEMERAL_OBJECTSTORE_* endpoint/bucket/credential "
                "variables)."
            )
        # Honor the configured bucket for the local backend too (routed through
        # Settings); defaults to "cutagent-ephemeral" when unset. For LocalObjectStore
        # the bucket is not part of the on-disk path, so the default is unchanged.
        return LocalObjectStore(
            root=Path(config.local_path),
            bucket=config.bucket,
            signed_get_ttl=signing.signed_get_ttl,
            signed_url_cache=signing.signed_url_cache,
        )
    if backend == "s3":
        return S3ObjectStore(
            endpoint_url=config.endpoint_url,
            bucket=config.bucket,
            access_key=config.access_key,
            secret_key=config.secret_key,
            region_name=config.region_name,
            addressing_style=config.addressing_style,
            client_factory=client_factory,
            signed_get_ttl=signing.signed_get_ttl,
            signed_url_cache=signing.signed_url_cache,
        )
    raise ValueError(f"Unsupported ephemeral object store backend: {backend}")
