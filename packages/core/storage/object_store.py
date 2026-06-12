from __future__ import annotations

import hashlib
import os
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
        client: Any | None = None,
        client_factory: Callable[..., Any] | None = None,
        cache_root: Path | None = None,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.cache_root = cache_root or Path(".data/objectstore-cache")
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._client = client or self._build_client(
            client_factory=client_factory,
            endpoint_url=endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            region_name=region_name,
        )
        self._ensure_bucket()

    def prepare_upload(
        self,
        filename: str,
        purpose: str,
        *,
        content_key: str | None = None,
    ) -> ObjectRef:
        safe_name = filename.replace("\\", "_").replace("/", "_")
        key_segment = content_key if content_key is not None else uuid4().hex
        key = f"{purpose}/{key_segment}/{safe_name}"
        return ObjectRef(bucket=self.bucket, key=key, uri=f"s3://{self.bucket}/{key}")

    def put_bytes(self, ref: ObjectRef, content: bytes) -> StoredObject:
        self._validate_ref(ref)
        self._client.put_object(Bucket=ref.bucket, Key=ref.key, Body=content)
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
        response = self._client.get_object(Bucket=ref.bucket, Key=ref.key)
        body = response["Body"]
        try:
            return body.read()
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

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
    ) -> Any:
        if client_factory is None:
            import boto3
            from botocore.config import Config

            # Force SigV4 presigned URLs (current standard; SigV2 is deprecated).
            return boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region_name,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            )
        return client_factory(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
        )


def object_store_from_env() -> ObjectStore:
    backend = os.getenv("CUTAGENT_OBJECTSTORE_BACKEND", "local").lower()
    root = Path(os.getenv("CUTAGENT_LOCAL_OBJECTSTORE_PATH", ".data/objectstore"))
    bucket = os.getenv("CUTAGENT_OBJECTSTORE_BUCKET", "cutagent-local")
    if backend == "local":
        return LocalObjectStore(root=root, bucket=bucket)
    if backend == "s3":
        return S3ObjectStore(
            endpoint_url=os.getenv("CUTAGENT_OBJECTSTORE_ENDPOINT", "http://127.0.0.1:9000"),
            bucket=bucket,
            access_key=os.getenv("CUTAGENT_OBJECTSTORE_ACCESS_KEY", ""),
            secret_key=os.getenv("CUTAGENT_OBJECTSTORE_SECRET_KEY", ""),
            region_name=os.getenv("CUTAGENT_OBJECTSTORE_REGION", "us-east-1"),
        )
    raise ValueError(f"Unsupported object store backend: {backend}")


_OBJECT_STORE = object_store_from_env()


def get_object_store() -> ObjectStore:
    return _OBJECT_STORE


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
