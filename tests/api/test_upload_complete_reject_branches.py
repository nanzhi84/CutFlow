"""Independent unit coverage for complete()/cancel() guard branches.

Until now these branches were only exercised by gated real-OSS smoke tests. This
covers them on the default LocalObjectStore double, using the incidental ``font``
kind (no ffprobe, no ffmpeg, no auto-created MediaAsset):

- complete() sha256 mismatch  -> upload.sha256_mismatch
- complete() HEAD content-type mismatch -> upload.unsupported_type
- cancel() drops the browser-written staging object
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.core.storage.object_store import ObjectHead, parse_local_uri
from packages.media.assets import local_object_path
from tests.api._upload_helpers import direct_upload, minimal_ttf_bytes

client = TestClient(app)


def login_admin():
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def test_complete_rejects_sha256_mismatch():
    login_admin()
    # The browser PUTs ``body`` but the declared sha256 is for *other* bytes of the
    # same length: the size + content-type checks pass, then complete recomputes
    # the sha256 from the stored object and rejects the disagreement.
    body = minimal_ttf_bytes(family="Hash Mismatch")
    wrong_sha256 = hashlib.sha256(b"a completely different payload").hexdigest()
    prepared, completed = direct_upload(
        client,
        kind="font",
        filename="sha.ttf",
        content_type="font/ttf",
        body=body,
        sha256=wrong_sha256,
    )
    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 400, completed.text
    assert completed.json()["error"]["code"] == "upload.sha256_mismatch"


def test_complete_rejects_content_type_mismatch_from_head(monkeypatch):
    login_admin()
    body = minimal_ttf_bytes(family="Content Type Mismatch")
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "client_upload_id": "client_content_type_mismatch",
            "kind": "font",
            "filename": "ct.ttf",
            "content_type": "font/ttf",
            "size_bytes": len(body),
        },
    )
    assert prepared.status_code == 201, prepared.text
    ticket = prepared.json()
    store = app.state.object_store
    store.put_bytes(parse_local_uri(ticket["put_url"]), body)
    # LocalObjectStore.head reports content_type=None, so the mismatch branch is
    # otherwise unreachable in a unit test. Simulate an OSS HEAD reporting a
    # content-type that disagrees with the declared upload content-type (the size
    # is left matching so the earlier size guard passes first).
    real_head = store.head

    def fake_head(uri):
        return ObjectHead(size=real_head(uri).size, content_type="video/mp4")

    monkeypatch.setattr(store, "head", fake_head)
    response = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": ticket["upload_session"]["id"], "size_bytes": len(body)},
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "upload.unsupported_type"


def test_cancel_deletes_staging_object_and_marks_cancelled():
    login_admin()
    body = b"\x00 font bytes to cancel"
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "client_upload_id": "client_cancel_cleanup",
            "kind": "font",
            "filename": "cancel.ttf",
            "content_type": "font/ttf",
            "size_bytes": len(body),
        },
    )
    assert prepared.status_code == 201, prepared.text
    ticket = prepared.json()
    staging_uri = ticket["upload_session"]["object_uri"]
    store = app.state.object_store
    # Simulate the browser's direct PUT to the staging key.
    store.put_bytes(parse_local_uri(staging_uri), body)
    assert local_object_path(store, staging_uri).exists()

    response = client.post(f"/api/uploads/{ticket['upload_session']['id']}/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    # The browser-written staging object is dropped on cancel.
    assert not local_object_path(store, staging_uri).exists()
