import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select


from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory
from packages.core.storage.database import AnnotationRow, MediaAssetRow
from tests.api._upload_helpers import direct_upload


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def create_completed_media_asset(client: TestClient) -> dict:
    admin_login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert admin_login.status_code == 200, admin_login.text

    content = b"media asset payload"
    prepared, completed = direct_upload(
        client,
        kind="font",
        filename="clip.ttf",
        content_type="font/ttf",
        body=content,
        case_id="case_demo",
        metadata={"template_mode": "replace"},
    )
    assert prepared.status_code == 201, prepared.text
    assert completed.status_code == 200, completed.text
    upload_session = completed.json()["upload_session"]
    created = client.post(
        "/api/media/assets",
        json={
            "upload_session_id": upload_session["id"],
            "case_id": "case_demo",
            "title": "SQLAlchemy media asset",
            "kind": "broll",
            "tags": ["db", "media"],
        },
    )
    assert created.status_code == 201, created.text
    return {
        "asset": created.json(),
        "artifact_id": completed.json()["artifact"]["artifact_id"],
        "object_uri": upload_session["object_uri"],
    }


def test_sqlalchemy_media_asset_flow_links_completed_upload_artifact():
    session_factory = sqlalchemy_session_factory()

    with TestClient(app) as client:
        context = create_completed_media_asset(client)
        asset = context["asset"]
        artifact_id = context["artifact_id"]
        assert asset["source_artifact_id"] == artifact_id
        assert asset["annotation_status"] == "pending"

        listed = client.get(
            "/api/media/assets",
            params={"case_id": "case_demo", "kind": "broll", "annotation_status": "pending"},
        )
        assert listed.status_code == 200, listed.text
        assert any(item["asset"]["id"] == asset["id"] for item in listed.json()["items"])

        detail = client.get(f"/api/media/assets/{asset['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["asset"]["title"] == "SQLAlchemy media asset"

        preview = client.get(f"/api/media/assets/{asset['id']}/preview-url")
        assert preview.status_code == 200, preview.text
        assert preview.json()["url"] == f"/api/media/assets/{asset['id']}/content"

    with session_factory() as session:
        row = session.get(MediaAssetRow, asset["id"])
        assert row is not None
        assert row.source_artifact_id == artifact_id
        assert row.tags == ["db", "media"]


def test_sqlalchemy_annotation_editor_patch_and_rerun_are_persisted():
    session_factory = sqlalchemy_session_factory()

    with TestClient(app) as client:
        asset = create_completed_media_asset(client)["asset"]
        editor = client.get(f"/api/annotations/{asset['id']}")
        assert editor.status_code == 200, editor.text
        editor_body = editor.json()
        assert editor_body["canonical"]["labels"] == ["db", "media"]

        patched = client.patch(
            f"/api/annotations/{asset['id']}",
            json={
                "etag": editor_body["etag"],
                "patch": {
                    "operations": [
                        {"op": "replace", "path": "/title", "value": "Annotated media asset"},
                        {"op": "replace", "path": "/labels", "value": ["hero", "usable"]},
                    ]
                },
            },
        )
        assert patched.status_code == 200, patched.text
        patched_body = patched.json()
        assert patched_body["etag"] != editor_body["etag"]
        assert patched_body["projection"]["title"] == "Annotated media asset"
        # Spec §12.2: labels are an editor projection tag list (the strict canonical
        # AnnotationV4 owns only the seven V4 layers, no free 'labels' field).
        assert patched_body["projection"]["labels"] == ["hero", "usable"]
        assert patched_body["asset"]["annotation_status"] == "annotated"

        rerun = client.post(f"/api/annotations/{asset['id']}/rerun", json={})
        assert rerun.status_code == 202, rerun.text
        # The DB rerun now drives the gated V4 pipeline; without a real vlm.annotation
        # provider it degrades (completed, sensor-only) rather than failing.
        assert rerun.json()["status"] == "completed"

    with session_factory() as session:
        asset_row = session.get(MediaAssetRow, asset["id"])
        annotation_row = session.scalar(
            select(AnnotationRow)
            .where(AnnotationRow.asset_id == asset["id"])
            .order_by(AnnotationRow.updated_at.desc())
        )
        assert asset_row is not None
        # The rerun persists a real AnnotationV4 canonical (the must-retain flow).
        assert asset_row.annotation_status in {"annotated", "annotation_failed"}
        assert annotation_row is not None
        assert annotation_row.canonical_schema == "AnnotationV4.v1"
        assert "meta" in annotation_row.canonical
        assert annotation_row.canonical["meta"]["asset_id"] == asset["id"]
