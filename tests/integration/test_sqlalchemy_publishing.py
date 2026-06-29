import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select


from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
from packages.core.storage.database import (
    PublishAttemptRow,
    PublishBatchItemRow,
    PublishBatchRow,
    PublishPackageRow,
)


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def create_completed_upload_artifact(client: TestClient) -> str:
    admin_login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert admin_login.status_code == 200, admin_login.text

    content = b"publishing package payload"
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "publish_video",
            "case_id": "case_demo",
            "filename": "publishable-video.txt",
            "content_type": "text/plain",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()

    uploaded = client.put(
        f"/api/uploads/{upload['id']}/file",
        files={"file": ("publishable-video.txt", content, "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text

    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()["artifact"]["artifact_id"]


def test_sqlalchemy_publish_package_batch_item_and_attempt_flow_are_persisted():
    session_factory = sqlalchemy_session_factory()

    with TestClient(app) as client:
        artifact_id = create_completed_upload_artifact(client)
        viewer_login = client.post(
            "/api/auth/login",
            json={"email": "viewer@local.cutagent", "password": "local-viewer"},
        )
        assert viewer_login.status_code == 200, viewer_login.text
        forbidden = client.post(
            "/api/publish/packages",
            json={
                "upload_artifact_id": artifact_id,
                "title": "Forbidden publish package",
                "description": "Viewer cannot create packages",
            },
        )
        assert forbidden.status_code == 403

        operator_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert operator_login.status_code == 200, operator_login.text

        created_package = client.post(
            "/api/publish/packages",
            json={
                "upload_artifact_id": artifact_id,
                "title": "SQLAlchemy publish package",
                "description": "Ready for channel handoff",
            },
        )
        assert created_package.status_code == 201, created_package.text
        package = created_package.json()
        assert package["upload_artifact_id"] == artifact_id
        assert package["video_artifact"]["artifact_id"] == artifact_id

        listed_packages = client.get("/api/publish/packages")
        assert listed_packages.status_code == 200, listed_packages.text
        assert any(item["id"] == package["id"] for item in listed_packages.json()["items"])

        created_batch = client.post(
            "/api/publish/batches",
            json={"publish_package_ids": [package["id"]], "platform_targets": ["douyin", "xiaohongshu"]},
        )
        assert created_batch.status_code == 201, created_batch.text
        batch = created_batch.json()
        assert batch["status"] == "draft"
        assert len(batch["items"]) == 2
        assert {item["status"] for item in batch["items"]} == {"uploaded"}

        first_item, second_item = batch["items"]
        patched_item = client.patch(
            f"/api/publish/items/{first_item['id']}",
            json={"title": "Do not submit this platform", "selected": False},
        )
        assert patched_item.status_code == 200, patched_item.text
        assert patched_item.json()["selected"] is False

        submitted = client.post(f"/api/publish/batches/{batch['id']}/submit", json={"dry_run": False})
        assert submitted.status_code == 202, submitted.text
        submitted_body = submitted.json()
        assert submitted_body["status"] == "completed"
        item_statuses = {item["id"]: item["status"] for item in submitted_body["items"]}
        assert item_statuses[first_item["id"]] == "uploaded"
        assert item_statuses[second_item["id"]] == "published"

        batch_detail = client.get(f"/api/publish/batches/{batch['id']}")
        assert batch_detail.status_code == 200, batch_detail.text
        assert batch_detail.json()["id"] == batch["id"]

        with session_factory() as session:
            attempts = list(
                session.scalars(
                    select(PublishAttemptRow).where(PublishAttemptRow.item_id == second_item["id"])
                )
            )
            skipped_attempts = list(
                session.scalars(select(PublishAttemptRow).where(PublishAttemptRow.item_id == first_item["id"]))
            )
            assert len(attempts) == 1
            assert skipped_attempts == []
            attempt_id = attempts[0].id

        attempt_detail = client.get(f"/api/publish/attempts/{attempt_id}")
        assert attempt_detail.status_code == 200, attempt_detail.text
        assert attempt_detail.json()["attempt"]["status"] == "published"

    with session_factory() as session:
        package_row = session.get(PublishPackageRow, package["id"])
        batch_row = session.get(PublishBatchRow, batch["id"])
        item_rows = list(
            session.scalars(
                select(PublishBatchItemRow).where(PublishBatchItemRow.batch_id == batch["id"])
            )
        )
        assert package_row is not None
        assert package_row.video_artifact["artifact_id"] == artifact_id
        assert batch_row is not None
        assert batch_row.status == "completed"
        assert len(item_rows) == 2
