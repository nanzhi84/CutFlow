"""OSS download integrity + cache governance + network diagnostics (#76, #77)."""

from __future__ import annotations

import concurrent.futures
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core.storage.object_store import (
    LocalObjectStore,
    ObjectRef,
    S3ObjectStore,
    object_cache_status,
    sweep_object_cache,
)


class _DownloadFakeS3:
    """Minimal fake whose download_file writes to the given Filename."""

    def __init__(self, *, fail: bool = False) -> None:
        self.bucket_created = True
        self.fail = fail
        self.objects = {("cutagent-demo", "k/obj.bin"): b"x" * 1024}

    def head_bucket(self, *, Bucket: str) -> None:
        return None

    def head_object(self, *, Bucket: str, Key: str) -> None:
        return None

    def download_file(self, Bucket: str, Key: str, Filename: str, Config: object) -> None:
        if self.fail:
            # Simulate a killed/failed transfer that left a truncated temp file.
            with open(Filename, "wb") as handle:
                handle.write(b"partial")
            raise RuntimeError("transfer aborted")
        with open(Filename, "wb") as handle:
            handle.write(self.objects[(Bucket, Key)])


def _store(tmp_path: Path, *, fail: bool = False) -> S3ObjectStore:
    return S3ObjectStore(
        endpoint_url="http://minio.local:9000",
        bucket="cutagent-demo",
        access_key="k",
        secret_key="s",
        client=_DownloadFakeS3(fail=fail),
        cache_root=tmp_path / "cache",
    )


def test_download_file_is_atomic_on_success(tmp_path):
    store = _store(tmp_path)
    ref = ObjectRef(bucket="cutagent-demo", key="k/obj.bin", uri="s3://cutagent-demo/k/obj.bin")
    target = tmp_path / "out" / "obj.bin"
    result = store.download_file(ref, target)
    assert result.read_bytes() == b"x" * 1024
    # No leftover .part sidecar after a successful atomic rename (unique temp name).
    assert not list(target.parent.glob("*.part"))


def test_download_file_failure_leaves_no_final_or_part_file(tmp_path):
    store = _store(tmp_path, fail=True)
    ref = ObjectRef(bucket="cutagent-demo", key="k/obj.bin", uri="s3://cutagent-demo/k/obj.bin")
    target = tmp_path / "out" / "obj.bin"
    try:
        store.download_file(ref, target)
    except RuntimeError:
        pass
    # A failed transfer must NOT leave a truncated file at the final path (which
    # _path() exists()-checks would return as a valid cache hit), nor a .part sidecar.
    assert not target.exists()
    assert not list(target.parent.glob("*.part"))


def test_concurrent_downloads_of_same_key_use_unique_part_files(tmp_path):
    # Two concurrent downloads of the same key must each use a unique .part temp,
    # so neither races os.replace into a FileNotFoundError. (#87 C1)
    barrier = threading.Barrier(2)

    class _BarrierFakeS3(_DownloadFakeS3):
        def download_file(self, Bucket, Key, Filename, Config):
            with open(Filename, "wb") as handle:
                handle.write(self.objects[(Bucket, Key)])
            barrier.wait()  # hold both writes so both threads reach os.replace together

    store = S3ObjectStore(
        endpoint_url="http://minio.local:9000",
        bucket="cutagent-demo",
        access_key="k",
        secret_key="s",
        client=_BarrierFakeS3(),
        cache_root=tmp_path / "cache",
    )
    ref = ObjectRef(bucket="cutagent-demo", key="k/obj.bin", uri="s3://cutagent-demo/k/obj.bin")
    target = tmp_path / "out" / "obj.bin"
    errors: list[Exception] = []

    def _download():
        try:
            store.download_file(ref, target)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_download) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent download raced: {errors}"
    assert target.read_bytes() == b"x" * 1024
    assert not list(target.parent.glob("*.part")), "leftover .part sidecar"


def test_sweep_object_cache_evicts_by_ttl(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    old = cache / "old.bin"
    new = cache / "new.bin"
    old.write_bytes(b"a" * 100)
    new.write_bytes(b"b" * 100)
    # Age `old` past a 1-hour TTL.
    stale = time.time() - 3 * 3600
    os.utime(old, (stale, stale))

    result = sweep_object_cache(cache, max_bytes=0, ttl_hours=1)
    assert result.deleted_files == 1
    assert not old.exists()
    assert new.exists()


def test_sweep_object_cache_evicts_oldest_over_size_budget(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    files = []
    for i in range(5):
        f = cache / f"f{i}.bin"
        f.write_bytes(b"z" * 100)
        mtime = time.time() - (5 - i) * 60  # f0 oldest, f4 newest
        os.utime(f, (mtime, mtime))
        files.append(f)

    # Budget fits ~3 files (300 bytes); the 2 oldest must be evicted.
    result = sweep_object_cache(cache, max_bytes=300, ttl_hours=0)
    assert result.deleted_files == 2
    assert not files[0].exists() and not files[1].exists()
    assert files[4].exists()
    assert result.remaining_bytes <= 300


def test_object_cache_status_reports_without_deleting(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.bin").write_bytes(b"a" * 50)
    status = object_cache_status(cache)
    assert status.examined_files == 1
    assert status.total_bytes == 50
    assert status.deleted_files == 0
    assert (cache / "a.bin").exists()


def test_health_network_reports_segment_hops():
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/health/network")  # public, unauthenticated
        assert response.status_code == 200, response.text
        hops = response.json()["hops"]
        # OSS / Temporal are now LIVE-probed (no static "configured" echo): the
        # local object store HEADs a non-existent probe key (cheap round-trip) and
        # reports ok + latency; Temporal is skipped under the local runtime. The
        # SQL session factory is always mounted (PR#72), so postgres is probed
        # unconditionally — never "not_configured".
        assert hops["postgres"]["status"] in {"ok", "failed"}
        assert hops["oss"]["status"] in {"ok", "failed"}
        assert "latency_ms" in hops["oss"]
        assert hops["oss"]["backend"] == "local"
        assert hops["temporal"]["status"] == "skipped"
        assert hops["temporal"]["runtime"] == "local"


def test_health_network_oss_probe_is_a_real_round_trip(tmp_path):
    # The OSS hop is a live HEAD against the configured bucket, not a static echo.
    # A LocalObjectStore reports a missing probe key as absent (exists() -> False)
    # without raising, so the hop is ok with a measured latency.
    store = LocalObjectStore(tmp_path / "oss")
    probe = ObjectRef(
        bucket=store.bucket,
        key="_cutagent_healthcheck/network-probe",
        uri=f"local://{store.bucket}/_cutagent_healthcheck/network-probe",
    )
    assert store.exists(probe) is False

    app = create_app()
    with TestClient(app) as client:
        oss = client.get("/api/health/network").json()["hops"]["oss"]
    assert oss["status"] == "ok"
    assert isinstance(oss["latency_ms"], (int, float))
    assert oss["backend"] == "local"


def test_bounded_probe_times_out_without_blocking():
    # The DoS guard: a hung synchronous probe must raise a timeout AND let the
    # handler return promptly (it abandons the worker thread rather than waiting
    # on shutdown), well before the 5s sleep would have finished.
    from apps.api.services.core import _bounded_probe

    started = time.monotonic()
    with pytest.raises(concurrent.futures.TimeoutError):
        _bounded_probe(lambda: time.sleep(5), 0.1)
    assert time.monotonic() - started < 2
