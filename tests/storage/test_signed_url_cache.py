"""The signed-URL cache is what stops the browser re-downloading covers (issue #206).

Every assertion here is a cost guarantee, not a style preference: a regression in
any of them puts the OSS bill back to ~16 GB/hour per open Outputs tab.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from packages.core.contracts import SignedUrlResponse, utcnow
from packages.core.storage.object_store import (
    DEFAULT_SIGNED_GET_TTL,
    LocalObjectStore,
    S3ObjectStore,
)
from packages.core.storage.signed_url_cache import REFRESH_FRACTION, SignedUrlCache


class CountingSigner:
    """Stands in for a real presigner: every call returns a DIFFERENT url.

    Real OSS/SigV4 signers fold a wall-clock timestamp into the signature, so a
    fake that returns a constant url would hide the very bug under test.
    """

    def __init__(self, ttl: timedelta = DEFAULT_SIGNED_GET_TTL) -> None:
        self.calls = 0
        self.ttl = ttl

    def __call__(self) -> SignedUrlResponse:
        self.calls += 1
        return SignedUrlResponse(
            url=f"https://oss.example/obj?sig={self.calls}",
            expires_at=utcnow() + self.ttl,
            request_id="req_fake",
        )


def test_repeated_signing_of_one_object_returns_a_byte_identical_url():
    cache = SignedUrlCache()
    signer = CountingSigner()

    urls = {
        cache.get_or_sign("k", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer).url for _ in range(10)
    }

    assert urls == {"https://oss.example/obj?sig=1"}
    assert signer.calls == 1


def test_distinct_objects_get_distinct_urls():
    cache = SignedUrlCache()
    signer = CountingSigner()

    first = cache.get_or_sign("a", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer).url
    second = cache.get_or_sign("b", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer).url

    assert first != second
    assert signer.calls == 2


def test_signature_is_refreshed_once_less_than_half_its_ttl_remains():
    cache = SignedUrlCache()
    ttl = timedelta(days=7)
    # Signed long ago: only a sliver of validity is left, so it must be re-signed
    # rather than handed to a browser that would cache it past expiry.
    stale = SignedUrlResponse(
        url="https://oss.example/obj?sig=stale",
        expires_at=utcnow() + ttl * (REFRESH_FRACTION / 2),
        request_id="req_fake",
    )
    cache.get_or_sign("k", ttl=ttl, sign=lambda: stale)
    signer = CountingSigner(ttl)

    refreshed = cache.get_or_sign("k", ttl=ttl, sign=signer)

    assert signer.calls == 1
    assert refreshed.url == "https://oss.example/obj?sig=1"
    # The replacement carries a full TTL again.
    assert refreshed.expires_at - utcnow() > ttl * REFRESH_FRACTION


def test_a_signature_with_most_of_its_ttl_left_is_reused():
    cache = SignedUrlCache()
    ttl = timedelta(days=7)
    fresh = SignedUrlResponse(
        url="https://oss.example/obj?sig=fresh",
        expires_at=utcnow() + ttl,
        request_id="req_fake",
    )
    cache.get_or_sign("k", ttl=ttl, sign=lambda: fresh)
    signer = CountingSigner(ttl)

    assert cache.get_or_sign("k", ttl=ttl, sign=signer).url == "https://oss.example/obj?sig=fresh"
    assert signer.calls == 0


def test_disposition_and_ttl_are_part_of_the_cache_identity():
    # A preview link and an attachment-download link of the same object have
    # different signatures, so they must not collide in the cache.
    cache = SignedUrlCache()
    signer = CountingSigner()
    ttl = DEFAULT_SIGNED_GET_TTL

    from packages.core.storage.signed_url_cache import cache_key

    cache.get_or_sign(cache_key("s3://b/k", ttl, None), ttl=ttl, sign=signer)
    cache.get_or_sign(cache_key("s3://b/k", ttl, "attachment"), ttl=ttl, sign=signer)
    cache.get_or_sign(cache_key("s3://b/k", timedelta(hours=1), None), ttl=ttl, sign=signer)

    assert signer.calls == 3


def test_local_cache_evicts_oldest_entries_when_full():
    cache = SignedUrlCache(max_entries=2)
    signer = CountingSigner()

    cache.get_or_sign("a", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer)
    cache.get_or_sign("b", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer)
    cache.get_or_sign("c", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer)  # evicts "a"

    assert signer.calls == 3
    cache.get_or_sign("b", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer)  # still cached
    assert signer.calls == 3
    cache.get_or_sign("a", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer)  # evicted, re-signs
    assert signer.calls == 4


def test_redis_failure_degrades_to_the_per_process_cache_and_reports_it(caplog):
    def exploding_factory(_url: str):
        raise ConnectionError("redis is down")

    cache = SignedUrlCache(redis_url="redis://127.0.0.1:6379/0", redis_client_factory=exploding_factory)
    signer = CountingSigner()

    with caplog.at_level("WARNING"):
        first = cache.get_or_sign("k", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer).url
        second = cache.get_or_sign("k", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer).url

    # Degraded, but NOT silently: the URL is still stable within this process, so
    # the fix keeps working — it just stops being shared across replicas.
    assert first == second
    assert signer.calls == 1
    assert cache.is_redis_degraded() is True
    assert any(
        record.__dict__.get("event") == "storage.signed_url_cache.redis_degraded"
        and record.__dict__.get("degradation_level") == "fail_safe"
        for record in caplog.records
    )


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls = 0

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.set_calls += 1
        self.store[key] = value


def test_redis_backed_cache_is_shared_across_stores():
    # Two API replicas must hand the browser the SAME url, or each replica's poll
    # invalidates the other's cache entry in the browser.
    shared = _FakeRedis()
    replica_a = SignedUrlCache(redis_url="redis://x", redis_client_factory=lambda _u: shared)
    replica_b = SignedUrlCache(redis_url="redis://x", redis_client_factory=lambda _u: shared)
    signer_a = CountingSigner()
    signer_b = CountingSigner()

    from_a = replica_a.get_or_sign("k", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer_a).url
    from_b = replica_b.get_or_sign("k", ttl=DEFAULT_SIGNED_GET_TTL, sign=signer_b).url

    assert from_a == from_b
    assert signer_b.calls == 0


def test_local_object_store_signed_url_is_stable_and_still_echoes_the_uri(tmp_path: Path):
    store = LocalObjectStore(root=tmp_path)
    uri = "local://cutagent-local/covers/x/mid.png"

    assert store.signed_url(uri).url == uri
    assert store.signed_url(uri).url == uri


def test_object_store_defaults_to_a_seven_day_get_ttl():
    # 7 days is the SigV4 presign ceiling; anything shorter multiplies re-downloads.
    assert DEFAULT_SIGNED_GET_TTL == timedelta(days=7)
    assert LocalObjectStore.signed_get_ttl == timedelta(days=7)


@pytest.mark.parametrize(
    ("ttl", "expected"),
    [
        (timedelta(days=7), "public, max-age=302400, immutable"),
        (timedelta(hours=2), "public, max-age=3600, immutable"),
    ],
)
def test_cache_control_max_age_never_outlives_the_signature_it_rides_with(ttl, expected, tmp_path):
    # max-age == REFRESH_FRACTION * ttl. Larger and a browser could re-request an
    # expired url (403); smaller and it re-downloads before the url even rotates.
    store = LocalObjectStore(root=tmp_path, signed_get_ttl=ttl)
    assert store._cache_control() == expected
    assert int(expected.split("max-age=")[1].split(",")[0]) == int(
        ttl.total_seconds() * REFRESH_FRACTION
    )


def test_s3_store_signs_each_object_once_across_many_polls(tmp_path: Path):
    """The end-to-end guarantee: N list polls of the same cover => ONE presign."""

    class Client:
        def __init__(self) -> None:
            self.presigns = 0

        def head_bucket(self, **_kw) -> None:
            return None

        def generate_presigned_url(self, op: str, Params: dict, ExpiresIn: int) -> str:
            self.presigns += 1
            return f"https://minio.local/{Params['Key']}?X-Amz-Signature={self.presigns}"

    client = Client()
    store = S3ObjectStore(
        endpoint_url="http://minio.local:9000",
        bucket="cutagent-dev",
        access_key="k",
        secret_key="s",
        client=client,
        cache_root=tmp_path,
    )
    uri = "s3://cutagent-dev/covers/abc/mid.png"

    urls = {store.signed_url(uri).url for _ in range(20)}

    assert len(urls) == 1
    assert client.presigns == 1
