"""B7 regression: browser download URLs must carry the attachment disposition.

Real presigned URLs (S3/OSS) previously dropped the ``attachment`` intent, so a
"batch download" opened videos inline in the browser instead of downloading. The
disposition now rides into the presign (S3 ``ResponseContentDisposition``) and is
ignored by the local double, which keeps falling back to the ``/download`` route.
"""

from __future__ import annotations

from datetime import timedelta

from apps.api.services import finished_videos
from packages.core.contracts import SignedUrlResponse, utcnow
from packages.core.storage.object_store import LocalObjectStore, S3ObjectStore


class _RecordingStore:
    def __init__(self, url: str) -> None:
        self._url = url
        self.calls: list[tuple[str, str | None]] = []

    def signed_url(
        self,
        uri: str,
        *,
        expires_in: timedelta = timedelta(minutes=15),
        response_content_disposition: str | None = None,
    ) -> SignedUrlResponse:
        self.calls.append((uri, response_content_disposition))
        return SignedUrlResponse(
            url=self._url, expires_at=utcnow() + expires_in, request_id="req"
        )


def test_browser_download_fields_bakes_attachment_into_presigned_url(monkeypatch):
    store = _RecordingStore("https://bucket.example.com/v.mp4?sig=1")
    monkeypatch.setattr(finished_videos, "object_store", lambda _req: store)

    url, _expires = finished_videos._browser_download_fields(
        object(), "art_1", "s3://bucket/v.mp4", disposition="attachment"
    )

    assert url == "https://bucket.example.com/v.mp4?sig=1"
    assert store.calls == [("s3://bucket/v.mp4", "attachment")]


def test_browser_download_fields_local_falls_back_to_download_route(monkeypatch):
    store = _RecordingStore("local://cutagent-local/v.mp4")
    monkeypatch.setattr(finished_videos, "object_store", lambda _req: store)

    url, _expires = finished_videos._browser_download_fields(
        object(), "art_1", "local://cutagent-local/v.mp4", disposition="attachment"
    )

    assert url == "/api/artifacts/art_1/download?disposition=attachment"
    # The disposition is still forwarded (the local double ignores it).
    assert store.calls[0][1] == "attachment"


def test_browser_download_fields_default_omits_disposition(monkeypatch):
    store = _RecordingStore("https://bucket.example.com/v.mp4?sig=1")
    monkeypatch.setattr(finished_videos, "object_store", lambda _req: store)

    url, _expires = finished_videos._browser_download_fields(
        object(), "art_1", "s3://bucket/v.mp4"
    )

    assert url == "https://bucket.example.com/v.mp4?sig=1"
    assert store.calls == [("s3://bucket/v.mp4", None)]


class _FakeS3Client:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def head_bucket(self, **_kwargs):
        return None

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.calls.append({"op": op, "Params": Params, "ExpiresIn": ExpiresIn})
        return f"https://host/{Params['Key']}?sig=1"


def _s3_store(client: _FakeS3Client) -> S3ObjectStore:
    return S3ObjectStore(
        endpoint_url="https://e",
        bucket="cutagent-dev",
        read_buckets=(),
        access_key="k",
        secret_key="s",
        client=client,
    )


def test_s3_signed_url_passes_response_content_disposition():
    client = _FakeS3Client()
    store = _s3_store(client)

    store.signed_url("s3://cutagent-dev/k", response_content_disposition="attachment")
    assert client.calls[0]["Params"]["ResponseContentDisposition"] == "attachment"

    store.signed_url("s3://cutagent-dev/k")
    assert "ResponseContentDisposition" not in client.calls[1]["Params"]


def test_local_signed_url_ignores_disposition(tmp_path):
    store = LocalObjectStore(root=tmp_path, bucket="cutagent-local")
    resp = store.signed_url(
        "local://cutagent-local/k", response_content_disposition="attachment"
    )
    assert resp.url == "local://cutagent-local/k"
