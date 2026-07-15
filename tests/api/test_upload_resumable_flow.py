from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.main import app
from packages.core import contracts as c
from packages.core.storage.database import ArtifactRow
from packages.core.storage.object_store import parse_object_uri, sha256_file
from packages.media.assets import local_object_path
from tests.api._upload_helpers import minimal_ttf_bytes

MIB = 1024 * 1024
client = TestClient(app)


def login_admin(target: TestClient = client) -> None:
    response = target.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def prepare_payload(*, client_upload_id: str, size_bytes: int, filename: str = "large.ttf"):
    return {
        "client_upload_id": client_upload_id,
        "kind": "font",
        "filename": filename,
        "content_type": "font/ttf",
        "size_bytes": size_bytes,
        "sha256": None,
        "stabilize": False,
    }


def test_prepare_boundaries_strategy_and_stricter_kind_limit():
    login_admin()
    token = uuid4().hex
    below = client.post(
        "/api/uploads/prepare",
        json=prepare_payload(
            client_upload_id=f"client_below_threshold_{token}", size_bytes=16 * MIB - 1
        ),
    )
    assert below.status_code == 201, below.text
    assert below.json()["upload_strategy"] == "single"
    assert below.json()["put_url"]

    threshold = client.post(
        "/api/uploads/prepare",
        json=prepare_payload(client_upload_id=f"client_at_threshold_{token}", size_bytes=16 * MIB),
    )
    assert threshold.status_code == 201, threshold.text
    assert threshold.json()["upload_strategy"] == "multipart"
    assert threshold.json()["part_size_bytes"] == 8 * MIB
    assert threshold.json()["part_count"] == 2
    assert threshold.json()["put_url"] is None

    font_too_large = client.post(
        "/api/uploads/prepare",
        json=prepare_payload(
            client_upload_id=f"client_font_too_large_{token}", size_bytes=40 * MIB + 1
        ),
    )
    assert font_too_large.status_code == 413, font_too_large.text
    assert font_too_large.json()["error"]["code"] == "upload.too_large"

    exactly_global_cap = client.post(
        "/api/uploads/prepare",
        json={
            **prepare_payload(
                client_upload_id=f"client_exact_global_cap_{token}", size_bytes=200 * MIB
            ),
            "kind": "video",
            "filename": "cap.mp4",
            "content_type": "video/mp4",
        },
    )
    assert exactly_global_cap.status_code == 201, exactly_global_cap.text
    assert exactly_global_cap.json()["upload_strategy"] == "multipart"

    above_global_cap = client.post(
        "/api/uploads/prepare",
        json={
            **prepare_payload(
                client_upload_id=f"client_above_global_cap_{token}", size_bytes=200 * MIB + 1
            ),
            "kind": "video",
            "filename": "cap.mp4",
            "content_type": "video/mp4",
        },
    )
    assert above_global_cap.status_code == 422

    for response in (below, threshold, exactly_global_cap):
        cancelled = client.post(f"/api/uploads/{response.json()['upload_session']['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text


def test_prepare_is_idempotent_by_stable_client_id_and_rejects_identity_conflict():
    login_admin()
    payload = prepare_payload(
        client_upload_id=f"client_prepare_idempotent_{uuid4().hex}", size_bytes=16 * MIB
    )
    payload["sha256"] = "a" * 64
    first = client.post("/api/uploads/prepare", json=payload)
    second = client.post("/api/uploads/prepare", json=payload)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert second.json()["upload_session"]["id"] == first.json()["upload_session"]["id"]
    assert second.json()["part_count"] == first.json()["part_count"]

    conflict = client.post(
        "/api/uploads/prepare",
        json={**payload, "filename": "different.ttf"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "idempotency.conflict"

    hash_conflict = client.post(
        "/api/uploads/prepare",
        json={**payload, "sha256": "b" * 64},
    )
    assert hash_conflict.status_code == 409, hash_conflict.text
    assert hash_conflict.json()["error"]["code"] == "idempotency.conflict"


def test_expired_sessions_cannot_be_reprepared_or_resigned():
    login_admin()
    token = uuid4().hex
    repository = app.state.sqlalchemy_upload_repository

    single_payload = prepare_payload(
        client_upload_id=f"client_expired_single_{token}",
        size_bytes=1024,
    )
    single = client.post("/api/uploads/prepare", json=single_payload)
    assert single.status_code == 201, single.text
    single_id = single.json()["upload_session"]["id"]
    repository.patch_upload(
        single_id,
        {"expires_at": c.utcnow() - timedelta(seconds=1)},
    )

    repeated = client.post("/api/uploads/prepare", json=single_payload)
    assert repeated.status_code == 400, repeated.text
    assert repeated.json()["error"]["code"] == "upload.invalid_state"
    assert client.get(f"/api/uploads/{single_id}").json()["status"] == "expired"

    multipart_payload = prepare_payload(
        client_upload_id=f"client_expired_multipart_{token}",
        size_bytes=16 * MIB,
    )
    multipart = client.post("/api/uploads/prepare", json=multipart_payload)
    assert multipart.status_code == 201, multipart.text
    upload = multipart.json()["upload_session"]
    upload_id = upload["id"]
    multipart_upload_id = repository.multipart_upload_id(upload_id)
    assert multipart_upload_id is not None
    repository.patch_upload(
        upload_id,
        {"expires_at": c.utcnow() - timedelta(seconds=1)},
    )

    signed = client.post(
        f"/api/uploads/{upload_id}/parts/sign",
        json={"part_numbers": [1]},
    )
    assert signed.status_code == 400, signed.text
    assert signed.json()["error"]["code"] == "upload.invalid_state"
    with pytest.raises(FileNotFoundError):
        app.state.object_store.list_parts(
            upload["staging_uri"],
            upload_id=multipart_upload_id,
        )


def test_presigned_put_expires_before_the_upload_session():
    login_admin()
    payload = prepare_payload(
        client_upload_id=f"client_bounded_presign_{uuid4().hex}",
        size_bytes=1024,
    )
    prepared = client.post("/api/uploads/prepare", json=payload)
    assert prepared.status_code == 201, prepared.text
    upload_id = prepared.json()["upload_session"]["id"]
    session_expiry = c.utcnow() + timedelta(minutes=2)
    app.state.sqlalchemy_upload_repository.patch_upload(
        upload_id,
        {"expires_at": session_expiry},
    )

    refreshed = client.post("/api/uploads/prepare", json=payload)
    assert refreshed.status_code == 201, refreshed.text
    signed_expiry = datetime.fromisoformat(refreshed.json()["expires_at"])
    assert signed_expiry < session_expiry


def test_multipart_resume_uses_list_parts_and_completion_is_repeatable():
    login_admin()
    size_bytes = 16 * MIB
    seed = minimal_ttf_bytes(family="Resumable Upload")
    content = seed + bytes(size_bytes - len(seed))
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            **prepare_payload(
                client_upload_id=f"client_multipart_roundtrip_{uuid4().hex}",
                size_bytes=size_bytes,
            ),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    ticket = prepared.json()
    session_id = ticket["upload_session"]["id"]

    signed = client.post(f"/api/uploads/{session_id}/parts/sign", json={"part_numbers": [1, 2]})
    assert signed.status_code == 200, signed.text
    part_urls = {part["part_number"]: part["put_url"] for part in signed.json()["parts"]}
    app.state.object_store.put_bytes(parse_object_uri(part_urls[1]), content[: 8 * MIB])

    resumed = client.get(f"/api/uploads/{session_id}/resume")
    assert resumed.status_code == 200, resumed.text
    assert [part["part_number"] for part in resumed.json()["completed_parts"]] == [1]
    re_signed = client.post(f"/api/uploads/{session_id}/parts/sign", json={"part_numbers": [1, 2]})
    assert re_signed.status_code == 200, re_signed.text
    assert [part["part_number"] for part in re_signed.json()["parts"]] == [2]
    app.state.object_store.put_bytes(
        parse_object_uri(re_signed.json()["parts"][0]["put_url"]), content[8 * MIB :]
    )

    completed = client.post(
        f"/api/uploads/{session_id}/object-complete",
        json={"size_bytes": size_bytes, "sha256": digest, "metadata": {"title": "large"}},
    )
    assert completed.status_code == 202, completed.text
    ready = client.get(f"/api/uploads/{session_id}/resume")
    assert ready.status_code == 200, ready.text
    ready_body = ready.json()
    assert ready_body["upload_session"]["status"] == "ready"
    assert ready_body["artifact"]["artifact_id"]
    assert (
        sha256_file(
            local_object_path(app.state.object_store, ready_body["upload_session"]["final_uri"])
        )
        == digest
    )

    repeated = client.post(
        f"/api/uploads/{session_id}/object-complete",
        json={"size_bytes": size_bytes, "sha256": digest, "metadata": {"title": "large"}},
    )
    assert repeated.status_code == 202, repeated.text
    assert repeated.json()["upload_session"]["status"] == "ready"
    assert repeated.json()["artifact"]["artifact_id"] == ready_body["artifact"]["artifact_id"]
    with app.state.sqlalchemy_session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(ArtifactRow)
            .where(ArtifactRow.source_upload_session_id == session_id)
        )
    assert count == 1


def test_upload_resume_is_isolated_by_owner():
    login_admin()
    token = uuid4().hex
    owner_email = f"upload-owner-a-{token}@example.test"
    other_email = f"upload-owner-b-{token}@example.test"
    for email in (owner_email, other_email):
        created = client.post(
            "/api/auth/users",
            json={
                "email": email,
                "display_name": email,
                "role": "operator",
                "password": "correct horse battery staple",
            },
        )
        assert created.status_code == 201, created.text

    owner = TestClient(app)
    other = TestClient(app)
    assert (
        owner.post(
            "/api/auth/login",
            json={"email": owner_email, "password": "correct horse battery staple"},
        ).status_code
        == 200
    )
    assert (
        other.post(
            "/api/auth/login",
            json={"email": other_email, "password": "correct horse battery staple"},
        ).status_code
        == 200
    )
    prepared = owner.post(
        "/api/uploads/prepare",
        json=prepare_payload(client_upload_id=f"client_owner_isolation_{token}", size_bytes=1024),
    )
    assert prepared.status_code == 201, prepared.text
    session_id = prepared.json()["upload_session"]["id"]

    hidden = other.get(f"/api/uploads/{session_id}/resume")
    assert hidden.status_code == 404, hidden.text
    assert hidden.json()["error"]["code"] == "artifact.missing"
