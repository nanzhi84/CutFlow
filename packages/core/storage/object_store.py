from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

from packages.core.contracts import SignedUrlResponse, utcnow
from packages.core.storage.signed_url_cache import REFRESH_FRACTION, SignedUrlCache, cache_key

# How long a presigned GET URL stays valid (issue #206). Seven days is the SigV4
# ceiling (the OSS V1 signer has none), and it is what makes a cached URL worth
# caching: the browser is handed the same string for days instead of a fresh
# signature every poll. Overridden per-deployment via
# CUTAGENT_OBJECTSTORE_SIGNED_GET_TTL_SECONDS. Not to be confused with
# ``settings.upload.presign_ttl_seconds``, which governs presigned PUT only.
DEFAULT_SIGNED_GET_TTL = timedelta(days=7)


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Compute the sha256 of a file by streaming it, without buffering it in RAM."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


@dataclass(frozen=True)
class ObjectHead:
    size: int
    content_type: str | None


@dataclass(frozen=True)
class MultipartPart:
    part_number: int
    etag: str
    size_bytes: int


class ObjectStore:
    # Class-level fallbacks so a duck-typed test double that never calls
    # ``super().__init__`` still reads sane values.
    signed_get_ttl: timedelta = DEFAULT_SIGNED_GET_TTL
    _signed_url_cache: SignedUrlCache | None = None

    def __init__(
        self,
        *,
        signed_get_ttl: timedelta | None = None,
        signed_url_cache: SignedUrlCache | None = None,
    ) -> None:
        if signed_get_ttl is not None:
            self.signed_get_ttl = signed_get_ttl
        # Every real store caches its signed URLs. Without a Redis url the cache
        # is per-process, which is still enough to keep a URL byte-identical
        # across polls of one API replica.
        self._signed_url_cache = signed_url_cache or SignedUrlCache()

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

    def upload_file(
        self,
        local_path: Path,
        ref: ObjectRef,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        """Store a file by path. Default falls back to a full read; streaming
        backends (S3) override this to avoid buffering whole objects in RAM."""
        return self.put_bytes(ref, Path(local_path).read_bytes())

    def download_file(self, ref: ObjectRef, local_path: Path) -> Path:
        """Fetch an object to a local path. Default falls back to a full read;
        streaming backends (S3) override this to avoid buffering in RAM."""
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.get_bytes(ref))
        return target

    def exists(self, ref: ObjectRef) -> bool:
        raise NotImplementedError

    def signed_url(
        self,
        uri: str,
        *,
        expires_in: timedelta | None = None,
        response_content_disposition: str | None = None,
    ) -> SignedUrlResponse:
        """Return a presigned GET URL, reusing the previous signature when possible.

        Signing is delegated to ``_sign_get_url``; this wrapper exists so the
        same object always hands back the *same* URL string until the signature
        approaches expiry. Without it every call produces a fresh signature and
        the browser re-downloads the object on every poll (issue #206).

        ``expires_in`` defaults to the configured ``signed_get_ttl``; callers who
        pass it explicitly (URLs handed to external providers) get their own TTL
        and their own cache entry.
        """
        ttl = expires_in if expires_in is not None else self.signed_get_ttl

        def sign() -> SignedUrlResponse:
            return self._sign_get_url(
                uri,
                expires_in=ttl,
                response_content_disposition=response_content_disposition,
            )

        cache = self._signed_url_cache
        if cache is None:
            return sign()
        return cache.get_or_sign(
            cache_key(uri, ttl, response_content_disposition), ttl=ttl, sign=sign
        )

    def _sign_get_url(
        self,
        uri: str,
        *,
        expires_in: timedelta,
        response_content_disposition: str | None = None,
    ) -> SignedUrlResponse:
        """Actually sign a GET URL. Backends implement this; callers use ``signed_url``."""
        raise NotImplementedError

    def _cache_control(self) -> str:
        """The ``Cache-Control`` this store attaches to immutable objects.

        Object keys are content- or uuid-addressed, so the bytes at a key never
        change and ``immutable`` is always sound: the browser is told never to
        even revalidate. ``max-age`` is derived from the signing TTL so that it
        can never outlive the signature it is served with — see ``REFRESH_FRACTION``.
        """
        max_age = int(self.signed_get_ttl.total_seconds() * REFRESH_FRACTION)
        return f"public, max-age={max_age}, immutable"

    def delete(self, uri: str) -> None:
        raise NotImplementedError

    def supports_presign(self) -> bool:
        """Whether this backend can hand the browser a presigned upload URL.

        False backends (filesystem) are not valid browser-direct upload targets;
        callers must fail loudly rather than fall back to proxying bytes."""
        return False

    def signed_put_url(
        self, uri: str, *, content_type: str, expires_in: timedelta
    ) -> SignedUrlResponse:
        raise NotImplementedError

    def create_multipart_upload(self, uri: str, *, content_type: str) -> str:
        raise NotImplementedError

    def sign_upload_part(
        self,
        uri: str,
        *,
        upload_id: str,
        part_number: int,
        expires_in: timedelta,
    ) -> SignedUrlResponse:
        raise NotImplementedError

    def list_parts(self, uri: str, *, upload_id: str) -> list[MultipartPart]:
        raise NotImplementedError

    def complete_multipart_upload(
        self, uri: str, *, upload_id: str, parts: list[MultipartPart]
    ) -> None:
        raise NotImplementedError

    def abort_multipart_upload(self, uri: str, *, upload_id: str) -> None:
        raise NotImplementedError

    def head(self, uri: str) -> ObjectHead:
        raise NotImplementedError

    def copy(self, src_uri: str, dst_uri: str) -> None:
        raise NotImplementedError

    def ensure_cors(
        self, origins: list[str], *, expose: list[str] | None = None, max_age: int = 600
    ) -> None:
        raise NotImplementedError


class LocalObjectStore(ObjectStore):
    def __init__(
        self,
        root: Path,
        bucket: str = "cutagent-local",
        *,
        signed_get_ttl: timedelta | None = None,
        signed_url_cache: SignedUrlCache | None = None,
    ) -> None:
        super().__init__(signed_get_ttl=signed_get_ttl, signed_url_cache=signed_url_cache)
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

    def upload_file(
        self,
        local_path: Path,
        ref: ObjectRef,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        source = Path(local_path)
        target = self._path(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            temporary = target.parent / f"{target.name}.{uuid4().hex}.part"
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return StoredObject(ref=ref, size_bytes=source.stat().st_size, sha256=sha256_file(source))

    def download_file(self, ref: ObjectRef, local_path: Path) -> Path:
        source = self._path(ref)
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        return target

    def exists(self, ref: ObjectRef) -> bool:
        return self._path(ref).exists()

    def _sign_get_url(
        self,
        uri: str,
        *,
        expires_in: timedelta,
        response_content_disposition: str | None = None,
    ) -> SignedUrlResponse:
        # The local double serves the object URI directly; there is no HTTP layer
        # to attach a Content-Disposition header to, so the hint is ignored (the
        # API falls back to its own /download route for local URIs).
        return SignedUrlResponse(
            url=uri,
            expires_at=utcnow() + expires_in,
            request_id="req_local",
        )

    def supports_presign(self) -> bool:
        # The local backend is a test/dev double for the browser-direct flow: the
        # "presigned PUT URL" is just the object URI and the caller writes bytes
        # through the store (no HTTP PUT, no API byte-proxy). Production uses S3/OSS.
        return True

    def signed_put_url(
        self, uri: str, *, content_type: str, expires_in: timedelta
    ) -> SignedUrlResponse:
        return SignedUrlResponse(
            url=uri, expires_at=utcnow() + expires_in, request_id="req_local_put"
        )

    def create_multipart_upload(self, uri: str, *, content_type: str) -> str:
        ref = parse_local_uri(uri)
        self._path(ref)  # validates the bucket
        upload_id = f"mpu_{uuid4().hex}"
        upload_root = self._multipart_root(upload_id)
        upload_root.mkdir(parents=True, exist_ok=False)
        (upload_root / "metadata.json").write_text(
            json.dumps({"uri": uri, "content_type": content_type}), encoding="utf-8"
        )
        return upload_id

    def sign_upload_part(
        self,
        uri: str,
        *,
        upload_id: str,
        part_number: int,
        expires_in: timedelta,
    ) -> SignedUrlResponse:
        self._validate_multipart(upload_id, uri)
        part_ref = ObjectRef(
            bucket=self.bucket,
            key=f".multipart/{upload_id}/parts/{part_number}.part",
            uri=f"local://{self.bucket}/.multipart/{upload_id}/parts/{part_number}.part",
        )
        return SignedUrlResponse(
            url=part_ref.uri,
            expires_at=utcnow() + expires_in,
            request_id="req_local_part",
        )

    def list_parts(self, uri: str, *, upload_id: str) -> list[MultipartPart]:
        root = self._validate_multipart(upload_id, uri) / "parts"
        if not root.exists():
            return []
        result: list[MultipartPart] = []
        for path in root.glob("*.part"):
            try:
                part_number = int(path.stem)
            except ValueError:
                continue
            result.append(
                MultipartPart(
                    part_number=part_number,
                    etag=f'"{sha256_file(path)}"',
                    size_bytes=path.stat().st_size,
                )
            )
        return sorted(result, key=lambda item: item.part_number)

    def complete_multipart_upload(
        self, uri: str, *, upload_id: str, parts: list[MultipartPart]
    ) -> None:
        root = self._validate_multipart(upload_id, uri)
        target = self._path(parse_local_uri(uri))
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f"{target.name}.{uuid4().hex}.part"
        try:
            with temporary.open("wb") as output:
                for part in sorted(parts, key=lambda item: item.part_number):
                    part_path = root / "parts" / f"{part.part_number}.part"
                    if not part_path.exists():
                        raise FileNotFoundError(str(part_path))
                    with part_path.open("rb") as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        shutil.rmtree(root, ignore_errors=True)

    def abort_multipart_upload(self, uri: str, *, upload_id: str) -> None:
        try:
            self._validate_multipart(upload_id, uri)
        except FileNotFoundError:
            return
        shutil.rmtree(self._multipart_root(upload_id), ignore_errors=True)

    def head(self, uri: str) -> ObjectHead:
        path = self._path(parse_local_uri(uri))
        if not path.exists():
            raise FileNotFoundError(uri)
        return ObjectHead(size=path.stat().st_size, content_type=None)

    def copy(self, src_uri: str, dst_uri: str) -> None:
        source = self._path(parse_local_uri(src_uri))
        target_ref = parse_local_uri(dst_uri)
        self.upload_file(source, target_ref)

    def ensure_cors(
        self, origins: list[str], *, expose: list[str] | None = None, max_age: int = 600
    ) -> None:
        return None

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

    def _multipart_root(self, upload_id: str) -> Path:
        if not upload_id.startswith("mpu_") or any(ch in upload_id for ch in ("/", "\\")):
            raise ValueError("Invalid multipart upload id.")
        return self.root / ".multipart" / upload_id

    def _validate_multipart(self, upload_id: str, uri: str) -> Path:
        root = self._multipart_root(upload_id)
        metadata_path = root / "metadata.json"
        if not metadata_path.exists():
            raise FileNotFoundError(upload_id)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("uri") != uri:
            raise ValueError("Multipart upload does not belong to this object URI.")
        return root


class S3ObjectStore(ObjectStore):
    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        read_buckets: tuple[str, ...] = (),
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
        signed_get_ttl: timedelta | None = None,
        signed_url_cache: SignedUrlCache | None = None,
    ) -> None:
        from boto3.s3.transfer import TransferConfig

        super().__init__(signed_get_ttl=signed_get_ttl, signed_url_cache=signed_url_cache)
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self._access_key = access_key
        self._secret_key = secret_key
        # Buckets this store may READ from: the write bucket plus any read-only
        # source/materials buckets. Writes always target self.bucket only.
        self._read_buckets = frozenset({bucket, *read_buckets})
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
        self._validate_write_ref(ref)
        self._client.upload_fileobj(
            io.BytesIO(content),
            ref.bucket,
            ref.key,
            ExtraArgs={"CacheControl": self._cache_control()},
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
        self._validate_read_ref(ref)
        buf = io.BytesIO()
        self._client.download_fileobj(ref.bucket, ref.key, buf, Config=self._transfer_config)
        return buf.getvalue()

    def upload_file(
        self,
        local_path: Path,
        ref: ObjectRef,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        # Streaming, multipart upload by path: boto3's upload_file never reads the
        # whole object into RAM (it streams from disk in multipart chunks).
        self._validate_write_ref(ref)
        extra_args: dict[str, str] = {"CacheControl": self._cache_control()}
        if content_type:
            extra_args["ContentType"] = content_type
        source = Path(local_path)
        self._client.upload_file(
            str(source),
            ref.bucket,
            ref.key,
            ExtraArgs=extra_args,
            Config=self._transfer_config,
        )
        cache_path = self._cache_path(ref)
        if source.resolve() != cache_path.resolve():
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, cache_path)
        return StoredObject(
            ref=ref,
            size_bytes=source.stat().st_size,
            sha256=sha256_file(source),
        )

    def download_file(self, ref: ObjectRef, local_path: Path) -> Path:
        # Streaming download by path into the on-disk cache; no full BytesIO buffer.
        # Download to a sibling ``.part`` then atomically rename into place
        # (issue #76): a process killed (or OOM'd) mid-download must never leave a
        # truncated file at the final path, which ``_path()`` (exists()-only) would
        # otherwise hand back as a valid cache hit forever.
        self._validate_read_ref(ref)
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Per-call unique suffix so concurrent downloads of the same key never
        # share one .part: each writes its own temp and os.replace stays atomic
        # last-writer-wins (issue #87 / C1). Same dir as target → same filesystem.
        part = target.parent / f"{target.name}.{uuid4().hex}.part"
        try:
            self._client.download_file(
                ref.bucket,
                ref.key,
                str(part),
                Config=self._transfer_config,
            )
            os.replace(part, target)  # atomic on the same filesystem
        finally:
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass
        return target

    def exists(self, ref: ObjectRef) -> bool:
        self._validate_read_ref(ref)
        try:
            self._client.head_object(Bucket=ref.bucket, Key=ref.key)
        except Exception as exc:
            if _is_not_found_error(exc):
                return False
            raise
        return True

    def _sign_get_url(
        self,
        uri: str,
        *,
        expires_in: timedelta,
        response_content_disposition: str | None = None,
    ) -> SignedUrlResponse:
        ref = parse_object_uri(uri)
        self._validate_read_ref(ref)
        native_oss_url = self._native_oss_signed_url(
            ref,
            expires_in=expires_in,
            response_content_disposition=response_content_disposition,
        )
        if native_oss_url is not None:
            return SignedUrlResponse(
                url=native_oss_url,
                expires_at=utcnow() + expires_in,
                request_id="req_oss",
            )
        # ResponseCacheControl makes the GET response carry Cache-Control even for
        # objects stored before that header was written at upload time, so the
        # browser stops re-fetching historical covers too (issue #206).
        params = {
            "Bucket": ref.bucket,
            "Key": ref.key,
            "ResponseCacheControl": self._cache_control(),
        }
        if response_content_disposition:
            params["ResponseContentDisposition"] = response_content_disposition
        url = self._client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=int(expires_in.total_seconds()),
        )
        return SignedUrlResponse(url=url, expires_at=utcnow() + expires_in, request_id="req_s3")

    def supports_presign(self) -> bool:
        return True

    def signed_put_url(
        self, uri: str, *, content_type: str, expires_in: timedelta
    ) -> SignedUrlResponse:
        ref = parse_object_uri(uri)
        self._validate_write_ref(ref)
        url = self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": ref.bucket, "Key": ref.key, "ContentType": content_type},
            ExpiresIn=int(expires_in.total_seconds()),
        )
        return SignedUrlResponse(url=url, expires_at=utcnow() + expires_in, request_id="req_put")

    def create_multipart_upload(self, uri: str, *, content_type: str) -> str:
        ref = parse_object_uri(uri)
        self._validate_write_ref(ref)
        response = self._client.create_multipart_upload(
            Bucket=ref.bucket,
            Key=ref.key,
            ContentType=content_type,
            CacheControl=self._cache_control(),
        )
        return str(response["UploadId"])

    def sign_upload_part(
        self,
        uri: str,
        *,
        upload_id: str,
        part_number: int,
        expires_in: timedelta,
    ) -> SignedUrlResponse:
        ref = parse_object_uri(uri)
        self._validate_write_ref(ref)
        url = self._client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": ref.bucket,
                "Key": ref.key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=int(expires_in.total_seconds()),
        )
        return SignedUrlResponse(
            url=url,
            expires_at=utcnow() + expires_in,
            request_id="req_upload_part",
        )

    def list_parts(self, uri: str, *, upload_id: str) -> list[MultipartPart]:
        ref = parse_object_uri(uri)
        self._validate_write_ref(ref)
        parts: list[MultipartPart] = []
        marker: int | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": ref.bucket,
                "Key": ref.key,
                "UploadId": upload_id,
            }
            if marker is not None:
                kwargs["PartNumberMarker"] = marker
            response = self._client.list_parts(**kwargs)
            for part in response.get("Parts", []):
                parts.append(
                    MultipartPart(
                        part_number=int(part["PartNumber"]),
                        etag=str(part["ETag"]),
                        size_bytes=int(part["Size"]),
                    )
                )
            if not response.get("IsTruncated"):
                break
            marker = int(response["NextPartNumberMarker"])
        return sorted(parts, key=lambda item: item.part_number)

    def complete_multipart_upload(
        self, uri: str, *, upload_id: str, parts: list[MultipartPart]
    ) -> None:
        ref = parse_object_uri(uri)
        self._validate_write_ref(ref)
        self._client.complete_multipart_upload(
            Bucket=ref.bucket,
            Key=ref.key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": part.part_number, "ETag": part.etag}
                    for part in sorted(parts, key=lambda item: item.part_number)
                ]
            },
        )

    def abort_multipart_upload(self, uri: str, *, upload_id: str) -> None:
        ref = parse_object_uri(uri)
        self._validate_write_ref(ref)
        self._client.abort_multipart_upload(
            Bucket=ref.bucket,
            Key=ref.key,
            UploadId=upload_id,
        )

    def head(self, uri: str) -> ObjectHead:
        ref = parse_object_uri(uri)
        self._validate_read_ref(ref)
        resp = self._client.head_object(Bucket=ref.bucket, Key=ref.key)
        return ObjectHead(size=resp["ContentLength"], content_type=resp.get("ContentType"))

    def copy(self, src_uri: str, dst_uri: str) -> None:
        src = parse_object_uri(src_uri)
        dst = parse_object_uri(dst_uri)
        self._validate_write_ref(dst)
        # Deliberately NOT _validate_read_ref(src): src is only a CopySource
        # parameter, never read through this store, so it need not be in this
        # store's read set (cross-bucket staging->final copies from durable into
        # materials). Read access is granted by the same-account credentials.
        self._client.copy_object(
            Bucket=dst.bucket,
            Key=dst.key,
            CopySource={"Bucket": src.bucket, "Key": src.key},
            MetadataDirective="COPY",
        )

    def ensure_cors(
        self, origins: list[str], *, expose: list[str] | None = None, max_age: int = 600
    ) -> None:
        self._client.put_bucket_cors(
            Bucket=self.bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedOrigins": list(origins),
                        "AllowedMethods": ["PUT", "GET", "HEAD"],
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": expose or ["ETag", "x-oss-request-id"],
                        "MaxAgeSeconds": max_age,
                    }
                ]
            },
        )

    def delete(self, uri: str) -> None:
        ref = parse_object_uri(uri)
        self._validate_write_ref(ref)
        self._client.delete_object(Bucket=ref.bucket, Key=ref.key)
        try:
            self._cache_path(ref).unlink()
        except FileNotFoundError:
            pass

    def _path(self, ref: ObjectRef) -> Path:
        self._validate_read_ref(ref)
        path = self._cache_path(ref)
        if not path.exists():
            self.download_file(ref, path)
        return path

    def _cache_path(self, ref: ObjectRef) -> Path:
        return self.cache_root / ref.bucket / ref.key

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            if not _is_bucket_absent_error(exc):
                raise
            self._client.create_bucket(Bucket=self.bucket)

    def _native_oss_signed_url(
        self,
        ref: ObjectRef,
        *,
        expires_in: timedelta,
        response_content_disposition: str | None = None,
    ) -> str | None:
        endpoint = urlsplit(self.endpoint_url)
        if endpoint.scheme not in {"http", "https"} or "aliyuncs.com" not in endpoint.netloc:
            return None
        expires = int(time.time() + expires_in.total_seconds())
        canonical_resource = f"/{ref.bucket}/{ref.key}"
        query_params = {
            "OSSAccessKeyId": self._access_key,
            "Expires": str(expires),
        }
        # response-* overrides are signed OSS sub-resources: each must appear in
        # BOTH the canonicalized resource (for the V1 signature) and the URL query,
        # and the canonicalized resource must list them in lexicographic order, or
        # the returned link 403s.
        sub_resources = {"response-cache-control": self._cache_control()}
        if response_content_disposition:
            sub_resources["response-content-disposition"] = response_content_disposition
        canonical_resource += "?" + "&".join(
            f"{name}={sub_resources[name]}" for name in sorted(sub_resources)
        )
        query_params.update(sub_resources)
        string_to_sign = f"GET\n\n\n{expires}\n{canonical_resource}"
        signature = base64.b64encode(
            hmac.new(
                self._secret_key.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        query_params["Signature"] = signature
        # quote (=> %20) rather than urlencode's default quote_plus (=> +). The
        # string-to-sign carries literal spaces, so a "+" in the query only verifies
        # if OSS's decoder happens to be form-style. It currently is (verified against
        # the live endpoint), but Aliyun's own SDKs emit %20 and nothing documents the
        # "+" behaviour — and a URL signed wrong would be pinned in the cache for days
        # before anyone noticed. %20 is unambiguous under RFC 3986. Everything else
        # (the base64 Signature's +, /, =) percent-encodes identically either way.
        query = urlencode(query_params, quote_via=quote, safe="")
        host = f"{ref.bucket}.{endpoint.netloc}"
        path = "/" + quote(ref.key, safe="/")
        return f"{endpoint.scheme}://{host}{path}?{query}"

    def _validate_write_ref(self, ref: ObjectRef) -> None:
        if ref.bucket != self.bucket:
            raise ValueError(f"Object bucket {ref.bucket} is not writable by this store.")

    def _validate_read_ref(self, ref: ObjectRef) -> None:
        if ref.bucket not in self._read_buckets:
            raise ValueError(f"Object bucket {ref.bucket} is not readable by this store.")

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


@dataclass(frozen=True)
class CacheSweepResult:
    """Outcome of an object-store cache sweep (issue #76)."""

    examined_files: int
    total_bytes: int
    deleted_files: int
    freed_bytes: int
    remaining_bytes: int


def object_cache_status(cache_root: Path) -> CacheSweepResult:
    """Report current cache usage without deleting anything (deleted=0)."""
    return sweep_object_cache(cache_root, max_bytes=0, ttl_hours=0)


def sweep_object_cache(
    cache_root: Path, *, max_bytes: int = 0, ttl_hours: float = 0
) -> CacheSweepResult:
    """Bound the S3 object-store local cache by TTL then total size (issue #76).

    Mac mini disk fills up because the boto3 download cache grows without limit.
    This evicts (1) files older than ``ttl_hours`` then (2), if still over
    ``max_bytes``, the oldest files (LRU by mtime) until under budget. A value of
    0 disables that pass. Pure filesystem maintenance — call it from
    ``scripts/cache_status.py`` / a sweep job, never on the hot path. ``.part``
    files (in-flight downloads) are always eligible for TTL eviction but are not
    counted toward a fresh budget if newer than the TTL.
    """
    root = Path(cache_root)
    files: list[tuple[Path, int, float]] = []
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append((path, stat.st_size, stat.st_mtime))

    examined_files = len(files)
    total_bytes = sum(size for _, size, _ in files)
    deleted_files = 0
    freed_bytes = 0

    def _remove(path: Path, size: int) -> None:
        nonlocal deleted_files, freed_bytes
        try:
            path.unlink()
            deleted_files += 1
            freed_bytes += size
        except OSError:
            pass

    survivors = files
    if ttl_hours and ttl_hours > 0:
        cutoff = time.time() - ttl_hours * 3600
        kept: list[tuple[Path, int, float]] = []
        for path, size, mtime in files:
            if mtime < cutoff:
                _remove(path, size)
            else:
                kept.append((path, size, mtime))
        survivors = kept

    remaining_bytes = sum(size for _, size, _ in survivors)
    if max_bytes and max_bytes > 0 and remaining_bytes > max_bytes:
        for path, size, _ in sorted(survivors, key=lambda item: item[2]):  # oldest first
            if remaining_bytes <= max_bytes:
                break
            before = deleted_files
            _remove(path, size)
            if deleted_files > before:
                remaining_bytes -= size

    return CacheSweepResult(
        examined_files=examined_files,
        total_bytes=total_bytes,
        deleted_files=deleted_files,
        freed_bytes=freed_bytes,
        remaining_bytes=remaining_bytes,
    )


from packages.core.storage.tiered_object_store import TieredObjectStore
from packages.core.storage.object_store_env import (
    object_store_from_env,
    object_store_from_settings,
)


# Built lazily on first get_object_store() rather than at import time (issue #64):
# importing this module must NOT read the environment, open a network connection
# (S3 head_bucket in __init__), or trigger the Temporal ephemeral fail-fast — all
# of which are import-order/timing hazards in tests, scripts, and OpenAPI export.
# The store is constructed once at first use (API lifespan / worker startup /
# first node activity) and cached. Tests still monkeypatch this slot directly or
# patch ``digital_human.get_object_store``; both seams are preserved.
_OBJECT_STORE: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _OBJECT_STORE
    if _OBJECT_STORE is None:
        _OBJECT_STORE = object_store_from_env()
    return _OBJECT_STORE


def configure_object_store(store: ObjectStore) -> None:
    """Explicitly install the process object store.

    Lets the API lifespan / worker startup build the store from an already-built
    ``Settings`` (via ``object_store_from_settings``) and inject it, instead of
    relying on the lazy env-read default. Overrides any cached store.
    """
    global _OBJECT_STORE
    _OBJECT_STORE = store


def reset_object_store() -> None:
    """Drop the cached store so the next ``get_object_store()`` rebuilds from the
    current environment. For tests and explicit reconfiguration."""
    global _OBJECT_STORE
    _OBJECT_STORE = None


__all__ = [
    "DEFAULT_SIGNED_GET_TTL",
    "ObjectRef",
    "MultipartPart",
    "ObjectStore",
    "LocalObjectStore",
    "S3ObjectStore",
    "SignedUrlCache",
    "TieredObjectStore",
    "CacheSweepResult",
    "object_cache_status",
    "sweep_object_cache",
    "object_store_from_env",
    "object_store_from_settings",
    "get_object_store",
    "configure_object_store",
    "reset_object_store",
    "parse_object_uri",
]
