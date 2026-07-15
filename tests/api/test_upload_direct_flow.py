"""Behavioral tests for the browser-direct upload flow (prepare -> PUT -> complete).

Uses the ``font`` kind (no ffprobe, no ffmpeg) so the core flow — presigned PUT,
staging->final move, sha256 recompute, HEAD size/type checks — is covered on the
default in-memory backend without media fixtures.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.core.storage.object_store import parse_local_uri
from tests.api._upload_helpers import direct_upload, minimal_ttf_bytes

client = TestClient(app)


def login_admin():
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def test_direct_upload_font_flow_moves_to_final_and_recomputes_sha256():
    login_admin()
    body = minimal_ttf_bytes()
    prepared, completed = direct_upload(
        client, kind="font", filename="x.ttf", content_type="font/ttf", body=body
    )
    assert prepared.status_code == 201, prepared.text
    ticket = prepared.json()
    assert ticket["put_url"]
    assert ticket["put_content_type"] == "font/ttf"
    assert "/incoming/uploads/" in ticket["upload_session"]["object_uri"]

    assert completed.status_code == 200, completed.text
    body_json = completed.json()
    assert body_json["artifact"]["kind"] == "uploaded.file"
    final_uri = body_json["upload_session"]["object_uri"]
    # Moved out of the browser-writable staging prefix to a kind-routed final key.
    assert "/incoming/uploads/" not in final_uri
    assert "/font/" in final_uri
    # sha256 recomputed server-side from the object and persisted.
    assert body_json["upload_session"]["sha256"] == hashlib.sha256(body).hexdigest()


def test_prepare_rejects_disallowed_content_type():
    login_admin()
    prepared, completed = direct_upload(
        client, kind="publish_video", filename="v.txt", content_type="text/plain", body=b"x"
    )
    assert prepared.status_code == 400, prepared.text
    assert prepared.json()["error"]["code"] == "upload.unsupported_type"
    assert completed is None


def test_complete_rejects_size_mismatch_via_head():
    login_admin()
    body = b"hello font bytes"
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "client_upload_id": "client_direct_size_mismatch",
            "kind": "font",
            "filename": "y.ttf",
            "content_type": "font/ttf",
            "size_bytes": len(body),
        },
    )
    assert prepared.status_code == 201, prepared.text
    ticket = prepared.json()
    # The "browser" PUTs more bytes than declared; complete's HEAD check rejects it.
    app.state.object_store.put_bytes(parse_local_uri(ticket["put_url"]), body + b"EXTRA")
    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": ticket["upload_session"]["id"], "size_bytes": len(body)},
    )
    assert completed.status_code == 400, completed.text
    assert completed.json()["error"]["code"] == "upload.size_mismatch"
