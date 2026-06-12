from __future__ import annotations

import hashlib
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from packages.core.contracts import SignedUrlResponse, utcnow


def _is_bucket_absent_error(exc: Exception) -> bool:
    # head_bucket on a missing bucket raises ClientError with a 404 / NoSuchBucket
    # code; anything else (auth, network) must propagate.
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {}) if isinstance(response.get("Error"), dict) else {}
        if str(error.get("Code")) in {"404", "NoSuchBucket", "NotFound"}:
            return True
        status = response.get("ResponseMetadata", {})
        if isinstance(status, dict) and status.get("HTTPStatusCode") == 404:
            return True
    return False


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    uri: str


@dataclass(frozen=True)
class StoredObject:
    ref: ObjectRef
    size_bytes: int
    sha256: str


class ObjectStore:
    def prepare_upload(
        self,
        filename: str,
        purpose: str,
        *,
        content_key: str | None = None,
        tier: str = "durable",
    ) -> ObjectRef:
        raise NotImplementedError

    def put_bytes(self, ref: ObjectRef, content: bytes) -> StoredObject:
        raise NotImplementedError

    def get_bytes(self, ref: ObjectRef) -> bytes:
        raise NotImplementedError

    def exists(self, ref: ObjectRef) -> bool:
        raise NotImplementedError

    def signed_url(self, uri: str, *, expires_in: timedelta = timedelta(minutes=15)) -> SignedUrlResponse:
        raise NotImplementedError

    def delete(self, uri: str) -> None:
        raise NotImplementedError


class LocalObjectStore(ObjectStore):
    def __init__(self, root: Path, bucket: str = "cutagent-local") -> None:
        self.root = root
        self.bucket = bucket
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare_upload(
        self,
        filename: str,
        purpose: str,
        *,
        content_key: str | None = None,
        tier: str = "durable",
    ) -> ObjectRef:
        safe_name = filename.replace("\\", "_").replace("/", "_")
        key_segment = content_key if content_key is not None else uuid4().hex
        key = f"{purpose}/{key_segment}/{safe_name}"
        return ObjectRef(bucket=self.bucket, key=key, uri=f"local://{self.bucket}/{key}")

    def put_bytes(self, ref: ObjectRef, content: bytes) -> StoredObject:
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return StoredObject(
            ref=ref,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def get_bytes(self, ref: ObjectRef) -> bytes:
        return self._path(ref).read_bytes()

    def exists(self, ref: ObjectRef) -> bool:
        return self._path(ref).exists()

    def signed_url(self, uri: str, *, expires_in: timedelta = timedelta(minutes=15)) -> SignedUrlResponse:
        return SignedUrlResponse(
            url=uri,
            expires_at=utcnow() + expires_in,
            request_id="req_local",
        )

    def delete(self, uri: str) -> None:
        ref = parse_local_uri(uri)
        path = self._path(ref)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        parent = path.parent
        while parent != self.root and parent.is_relative_to(self.root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _path(self, ref: ObjectRef) -> Path:
        if ref.bucket != self.bucket:
            raise ValueError(f"Object bucket {ref.bucket} is not managed by this store.")
        return self.root / ref.key


class S3ObjectStore(ObjectStore):
    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region_name: str = "us-east-1",
        addressing_style: str = "path",
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
        cache_root: Path | None = None,
        multipart_threshold_mb: int = 8,
        multipart_chunk_mb: int = 8,
        max_concurrency: int = 4,
        connect_timeout: int = 10,
        read_timeout: int = 120,
        max_attempts: int = 5,
    ) -> None:
        from boto3.s3.transfer import TransferConfig

        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.cache_root = cache_root or Path(".data/objectstore-cache")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._transfer_config = TransferConfig(
            multipart_threshold=multipart_threshold_mb * 1024 * 1024,
            multipart_chunksize=multipart_chunk_mb * 1024 * 1024,
            max_concurrency=max_concurrency,
            use_threads=True,
        )
        self._client = client or self._build_client(
            client_factory=client_factory,
            endpoint_url=endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            region_name=region_name,
            addressing_style=addressing_style,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_attempts=max_attempts,
        )
        self._ensure_bucket()

    def prepare_upload(
        self,
        filename: str,
        purpose: str,
        *,
        content_key: str | None = None,
        tier: str = "durable",
    ) -> ObjectRef:
        safe_name = filename.replace("\\", "_").replace("/", "_")
        key_segment = content_key if content_key is not None else uuid4().hex
        key = f"{purpose}/{key_segment}/{safe_name}"
        return ObjectRef(bucket=self.bucket, key=key, uri=f"s3://{self.bucket}/{key}")

    def put_bytes(self, ref: ObjectRef, content: bytes) -> StoredObject:
        self._validate_ref(ref)
        self._client.upload_fileobj(
            io.BytesIO(content),
            ref.bucket,
            ref.key,
            Config=self._transfer_config,
        )
        path = self._cache_path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return StoredObject(
            ref=ref,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def get_bytes(self, ref: ObjectRef) -> bytes:
        self._validate_ref(ref)
        buf = io.BytesIO()
        self._client.download_fileobj(ref.bucket, ref.key, buf, Config=self._transfer_config)
        return buf.getvalue()

    def exists(self, ref: ObjectRef) -> bool:
        self._validate_ref(ref)
        try:
            self._client.head_object(Bucket=ref.bucket, Key=ref.key)
        except Exception as exc:
            if _is_not_found_error(exc):
                return False
            raise
        return True

    def signed_url(self, uri: str, *, expires_in: timedelta = timedelta(minutes=15)) -> SignedUrlResponse:
        ref = parse_object_uri(uri)
        self._validate_ref(ref)
        url = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": ref.bucket, "Key": ref.key},
            ExpiresIn=int(expires_in.total_seconds()),
        )
        return SignedUrlResponse(url=url, expires_at=utcnow() + expires_in, request_id="req_s3")

    def delete(self, uri: str) -> None:
        ref = parse_object_uri(uri)
        self._validate_ref(ref)
        self._client.delete_object(Bucket=ref.bucket, Key=ref.key)
        try:
            self._cache_path(ref).unlink()
        except FileNotFoundError:
            pass

    def _path(self, ref: ObjectRef) -> Path:
        self._validate_ref(ref)
        path = self._cache_path(ref)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.get_bytes(ref))
        return path

    def _cache_path(self, ref: ObjectRef) -> Path:
        return self.cache_root / ref.key

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            if not _is_bucket_absent_error(exc):
                raise
            self._client.create_bucket(Bucket=self.bucket)

    def _validate_ref(self, ref: ObjectRef) -> None:
        if ref.bucket != self.bucket:
            raise ValueError(f"Object bucket {ref.bucket} is not managed by this store.")

    @staticmethod
    def _build_client(
        *,
        client_factory: Callable[..., Any] | None,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region_name: str,
        addressing_style: str,
        connect_timeout: int,
        read_timeout: int,
        max_attempts: int,
    ) -> Any:
        from botocore.config import Config

        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"max_attempts": max_attempts, "mode": "standard"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
        if client_factory is None:
            import boto3

            # Force SigV4 presigned URLs (current standard; SigV2 is deprecated).
            return boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region_name,
                config=config,
            )
        return client_factory(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
            config=config,
        )


def parse_local_uri(uri: str) -> ObjectRef:
    for prefix in ("local://", "s3://"):
        if uri.startswith(prefix):
            return _parse_uri_tail(uri, prefix)
    else:
        raise ValueError(f"Unsupported local object URI: {uri}")


def parse_object_uri(uri: str) -> ObjectRef:
    for prefix in ("local://", "s3://"):
        if uri.startswith(prefix):
            return _parse_uri_tail(uri, prefix)
    raise ValueError(f"Unsupported object URI: {uri}")


def _parse_uri_tail(uri: str, prefix: str) -> ObjectRef:
    tail = uri[len(prefix) :]
    bucket, _, key = tail.partition("/")
    if not bucket or not key:
        raise ValueError(f"Invalid object URI: {uri}")
    return ObjectRef(bucket=bucket, key=key, uri=uri)


def _is_not_found_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        return str(code) in {"404", "NoSuchBucket", "NoSuchKey", "NotFound"}
    return False


from packages.core.storage.tiered_object_store import TieredObjectStore  # noqa: E402
from packages.core.storage.object_store_env import object_store_from_env  # noqa: E402


_OBJECT_STORE = object_store_from_env()


def get_object_store() -> ObjectStore:
    return _OBJECT_STORE
