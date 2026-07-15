from __future__ import annotations

import hashlib

import pytest

from packages.core.storage.object_store import (
    LocalObjectStore,
    S3ObjectStore,
    parse_object_uri,
    sha256_file,
)
from packages.media.assets import store_file


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    """Mock boto3 S3 client.

    Records path-based (streaming) and fileobj-based (full buffer) calls
    separately so tests can assert the streaming path is taken.
    """

    def __init__(self) -> None:
        self.bucket_created = False
        self.objects: dict[tuple[str, str], bytes] = {}
        # path-based, streaming multipart transfers (no full RAM buffer)
        self.upload_file_calls: list[tuple[str, str, str]] = []
        self.extra_args: list[dict | None] = []
        self.download_file_calls: list[tuple[str, str, str]] = []
        # fileobj-based transfers that buffer whole objects in RAM
        self.upload_fileobj_calls: list[tuple[str, str]] = []
        self.download_fileobj_calls: list[tuple[str, str]] = []

    def head_bucket(self, *, Bucket: str) -> None:
        if not self.bucket_created:
            raise FakeS3Error("404")

    def create_bucket(self, *, Bucket: str) -> None:
        self.bucket_created = True

    def upload_file(
        self,
        Filename: str,
        Bucket: str,
        Key: str,
        Config: object,
        ExtraArgs: dict | None = None,
    ) -> None:
        self.upload_file_calls.append((Filename, Bucket, Key))
        self.extra_args.append(ExtraArgs)
        with open(Filename, "rb") as handle:
            self.objects[(Bucket, Key)] = handle.read()

    def download_file(self, Bucket: str, Key: str, Filename: str, Config: object) -> None:
        self.download_file_calls.append((Bucket, Key, Filename))
        with open(Filename, "wb") as handle:
            handle.write(self.objects[(Bucket, Key)])

    def upload_fileobj(
        self, Fileobj, Bucket: str, Key: str, Config: object, ExtraArgs: dict | None = None
    ) -> None:
        self.upload_fileobj_calls.append((Bucket, Key))
        self.objects[(Bucket, Key)] = Fileobj.read()

    def download_fileobj(self, Bucket: str, Key: str, Fileobj, Config: object) -> None:
        self.download_fileobj_calls.append((Bucket, Key))
        Fileobj.write(self.objects[(Bucket, Key)])

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise FakeS3Error("404")
        payload = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(payload),
            "Metadata": {"sha256": hashlib.sha256(payload).hexdigest()},
        }


def _make_s3_store(tmp_path) -> tuple[S3ObjectStore, FakeS3Client]:
    client = FakeS3Client()
    store = S3ObjectStore(
        endpoint_url="http://minio.local:9000",
        bucket="cutagent-demo",
        access_key="minioadmin",
        secret_key="minioadmin",
        client=client,
        cache_root=tmp_path / "cache",
    )
    return store, client


def test_s3_store_file_uses_path_based_upload_not_full_buffer(tmp_path):
    store, client = _make_s3_store(tmp_path)
    source = tmp_path / "movie.mp4"
    payload = b"a-long-video-payload" * 4096
    source.write_bytes(payload)
    expected_sha = hashlib.sha256(payload).hexdigest()

    stored = store_file(store, source, purpose="generated-video")

    # Streaming, path-based multipart upload was used.
    assert len(client.upload_file_calls) == 1
    upload_filename, bucket, key = client.upload_file_calls[0]
    assert upload_filename == str(source)
    assert (bucket, key) == (stored.ref.bucket, stored.ref.key)
    assert client.extra_args[0]["Metadata"] == {"sha256": expected_sha}
    # The full-buffer (BytesIO) path was NOT used.
    assert client.upload_fileobj_calls == []
    # sha256/size computed by streaming off disk, not read_bytes().
    assert stored.sha256 == expected_sha
    assert stored.size_bytes == len(payload)


def test_s3_store_file_rejects_remote_checksum_mismatch(tmp_path, monkeypatch):
    store, client = _make_s3_store(tmp_path)
    source = tmp_path / "corrupt.mp4"
    source.write_bytes(b"local-content")
    monkeypatch.setattr(
        client,
        "head_object",
        lambda **_kwargs: {
            "ContentLength": source.stat().st_size,
            "Metadata": {"sha256": "wrong"},
        },
    )

    with pytest.raises(OSError, match="size/checksum verification"):
        store_file(store, source, purpose="generated-video")


def test_s3_store_file_addressed_key_is_streamed_content_sha(tmp_path):
    store, client = _make_s3_store(tmp_path)
    source = tmp_path / "seed.mp4"
    payload = b"seed-media-bytes" * 1000
    source.write_bytes(payload)
    expected_sha = sha256_file(source)
    assert expected_sha == hashlib.sha256(payload).hexdigest()

    stored = store_file(store, source, purpose="seed-media", addressed=True)

    # Content-addressed key embeds the streamed sha256.
    assert stored.ref.key == f"seed-media/{expected_sha}/seed.mp4"
    assert stored.sha256 == expected_sha
    assert client.upload_file_calls[0][0] == str(source)
    assert client.upload_fileobj_calls == []


def test_s3_store_file_addressed_reuses_existing_object_without_reupload(tmp_path):
    store, client = _make_s3_store(tmp_path)
    source = tmp_path / "seed.mp4"
    source.write_bytes(b"dedup-payload" * 500)

    first = store_file(store, source, purpose="seed-media", addressed=True)
    second = store_file(store, source, purpose="seed-media", addressed=True)

    assert first.ref.uri == second.ref.uri
    # Second call short-circuits on exists() and does not re-upload.
    assert len(client.upload_file_calls) == 1
    assert second.sha256 == first.sha256


def test_s3_path_readback_uses_path_based_download_not_full_buffer(tmp_path):
    store, client = _make_s3_store(tmp_path)
    payload = b"downloadable-video" * 2048
    # Seed the object directly into the fake remote (no local cache yet).
    ref = store.prepare_upload("clip.mp4", "generated-video")
    client.objects[(ref.bucket, ref.key)] = payload

    path = store._path(ref)

    assert path.read_bytes() == payload
    # Streaming, path-based download into the disk cache was used.
    assert len(client.download_file_calls) == 1
    bucket, key, filename = client.download_file_calls[0]
    assert (bucket, key) == (ref.bucket, ref.key)
    # Atomic download (#76 / #87 C1): bytes land in a per-call-unique sibling
    # .part file ({path}.<uuid>.part), then are renamed into place — so the
    # download target is a .part sibling (not the final path, not a full buffer),
    # and no .part lingers afterward.
    assert filename.startswith(f"{path}.") and filename.endswith(".part")
    assert not list(path.parent.glob("*.part"))
    # The full-buffer (BytesIO) path was NOT used.
    assert client.download_fileobj_calls == []
    # Cached on disk; a second resolution does not re-download.
    assert store._path(ref) == path
    assert len(client.download_file_calls) == 1


def test_local_store_file_falls_back_to_bytes_path(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    source = tmp_path / "clip.mp4"
    payload = b"local-bytes-payload"
    source.write_bytes(payload)

    stored = store_file(store, source, purpose="generated-video", addressed=True)

    # Local backend still works through the bytes fallback and keys by sha.
    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    assert stored.size_bytes == len(payload)
    assert store.get_bytes(parse_object_uri(stored.ref.uri)) == payload


def test_s3_upload_file_copies_into_disk_cache(tmp_path):
    # Guard the shutil.copyfile path: cache is populated even when the source
    # is not already the cache path.
    store, client = _make_s3_store(tmp_path)
    source = tmp_path / "render.mp4"
    payload = b"render-output" * 100
    source.write_bytes(payload)

    stored = store_file(store, source, purpose="generated-video")
    cache_path = store._cache_path(stored.ref)

    assert cache_path.read_bytes() == payload
    # Resolving the path again hits the warm cache (no download).
    assert store._path(stored.ref) == cache_path
    assert client.download_file_calls == []
