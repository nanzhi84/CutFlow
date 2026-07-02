from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.app import create_app
from apps.api.main import app
from packages.core.storage.database import ArtifactRow, MediaAssetRow, ScriptVersionRow


client = TestClient(app)


def login_admin(active_client):
    login = active_client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text


def import_one(active_client, import_type: str, row: dict):
    response = active_client.post(
        "/api/import/batches",
        json={"import_type": import_type, "rows": [row]},
    )
    assert response.status_code == 202, response.text
    report = response.json()
    assert report["status"] == "completed"
    return report["results"][0]["internal_id"]


def test_media_import_with_uri_creates_uploaded_file_source_artifact():
    with TestClient(create_app()) as active_client:
        login_admin(active_client)
        row = {
            "external_id": "media_uri_ext",
            "case_id": "case_demo",
            "title": "Imported URI media",
            "kind": "broll",
            "uri": "s3://cutagent-durable/imports/case_demo/broll.mp4",
            "mime": "video/mp4",
            "sha256": "abc123",
            "duration_sec": 12.5,
            "width": 1920,
            "height": 1080,
        }

        response = active_client.post("/api/import/batches", json={"import_type": "media", "rows": [row]})

        assert response.status_code == 202, response.text
        report = response.json()
        assert report["created_count"] == 1
        asset_id = report["results"][0]["internal_id"]
        with active_client.app.state.sqlalchemy_session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            # Issue #133: a legacy ``broll`` import kind converges to the unified
            # ``video`` bucket with a ``legacy_kind:<x>`` provenance tag, so the import
            # path can never re-introduce a legacy asset-kind row.
            assert asset.kind == "video"
            assert "legacy_kind:broll" in (asset.tags or [])
            assert asset.source_artifact_id is not None
            artifact = session.get(ArtifactRow, asset.source_artifact_id)
            assert artifact.kind == "uploaded.file"
            assert artifact.payload_schema == "UploadedFileArtifact.v1"
            assert artifact.uri == row["uri"]
            assert artifact.sha256 == row["sha256"]
            assert artifact.payload["content_type"] == row["mime"]
            assert artifact.payload["sha256"] == row["sha256"]
            assert artifact.payload["metadata"]["duration_sec"] == row["duration_sec"]
            assert artifact.payload["metadata"]["width"] == row["width"]
            assert artifact.payload["metadata"]["height"] == row["height"]


def test_media_import_with_uri_is_idempotent_by_sha256_or_uri():
    with TestClient(create_app()) as active_client:
        login_admin(active_client)
        row = {
            "external_id": "media_uri_idempotent",
            "case_id": "case_demo",
            "title": "Imported URI media",
            "kind": "video",
            "uri": "s3://cutagent-durable/imports/case_demo/reused.mp4",
            "mime": "video/mp4",
            "sha256": "dedupe-sha",
        }

        first = active_client.post(
            "/api/import/batches",
            json={"import_type": "media", "rows": [row, {**row, "external_id": "media_uri_idempotent_2"}]},
        )
        second = active_client.post("/api/import/batches", json={"import_type": "media", "rows": [row]})

        assert first.status_code == 202, first.text
        assert second.status_code == 202, second.text
        assert first.json()["created_count"] == 1
        assert first.json()["skipped_count"] == 1
        assert first.json()["results"][1]["status"] == "skipped"
        assert second.json()["created_count"] == 0
        assert second.json()["skipped_count"] == 1
        with active_client.app.state.sqlalchemy_session_factory() as session:
            matching_assets = session.scalars(
                select(MediaAssetRow).where(
                    MediaAssetRow.case_id == row["case_id"],
                    MediaAssetRow.kind == row["kind"],
                    MediaAssetRow.title == row["title"],
                )
            ).all()
            assert len(matching_assets) == 1
            matching_artifacts = session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.kind == "uploaded.file",
                    ArtifactRow.sha256 == row["sha256"],
                    ArtifactRow.uri == row["uri"],
                )
            ).all()
            assert len(matching_artifacts) == 1


def test_fresh_import_accepts_all_spec_types():
    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text
    import_types = [
        "case",
        "script",
        "media",
        "finished_video",
        "video_version",
        "publish_record",
        "performance",
        "prompt_seed",
        "provider_price",
    ]
    for import_type in import_types:
        row = {"external_id": f"ext_{import_type}"}
        if import_type == "media":
            row["uri"] = "s3://cutagent-durable/import-smoke/media.mp4"
        response = client.post(
            "/api/import/batches",
            json={"import_type": import_type, "rows": [row]},
        )
        assert response.status_code == 202, response.text
        report = response.json()
        assert report["status"] == "completed"
        assert report["created_count"] == 1


def test_spec_20_2_13_imported_case_script_and_media_are_frontend_visible():
    """Spec 20.2 #13: imported Case + ScriptVersion + MediaAsset are visible through frontend read APIs."""
    with TestClient(create_app()) as active_client:
        login_admin(active_client)
        case_id = import_one(
            active_client,
            "case",
            {"external_id": "case_ext", "name": "Imported showcase case", "description": "Imported"},
        )
        script_id = import_one(
            active_client,
            "script",
            {
                "external_id": "script_ext",
                "case_id": case_id,
                "title": "Imported script",
                "script": "导入脚本可以进入知识页。",
            },
        )
        media_id = import_one(
            active_client,
            "media",
            {
                "external_id": "media_ext",
                "case_id": case_id,
                "title": "Imported media",
                "kind": "video",
                "uri": "s3://cutagent-durable/import-visible/media.mp4",
            },
        )

        case_detail = active_client.get(f"/api/cases/{case_id}")
        assert case_detail.status_code == 200, case_detail.text
        assert case_detail.json()["name"] == "Imported showcase case"
        with active_client.app.state.sqlalchemy_session_factory() as session:
            assert session.get(ScriptVersionRow, script_id) is not None
        media = active_client.get(f"/api/media/assets?case_id={case_id}")
        assert media.status_code == 200, media.text
        assert any(item["asset"]["id"] == media_id for item in media.json()["items"])


def test_spec_20_2_14_imported_media_can_rerun_annotation_open_editor_and_save_patch():
    """Spec 20.2 #14: imported MediaAsset can be annotated, opened in editor, and patched."""
    with TestClient(create_app()) as active_client:
        login_admin(active_client)
        media_id = import_one(
            active_client,
            "media",
            {
                "external_id": "media_annotate",
                "case_id": "case_demo",
                "title": "Patch me",
                "kind": "video",
                "uri": "s3://cutagent-durable/import-annotation/media.mp4",
            },
        )
        rerun = active_client.post(f"/api/annotations/{media_id}/rerun", json={"force": True})
        assert rerun.status_code == 202, rerun.text
        assert rerun.json()["status"] == "completed"
        editor = active_client.get(f"/api/annotations/{media_id}")
        assert editor.status_code == 200, editor.text
        patch = active_client.patch(
            f"/api/annotations/{media_id}",
            json={
                "etag": editor.json()["etag"],
                "patch": {"operations": [{"op": "replace", "path": "/usable", "value": True}]},
            },
        )
        assert patch.status_code == 200, patch.text
        assert patch.json()["etag"] != editor.json()["etag"]
        detail = active_client.get(f"/api/media/assets/{media_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["asset"]["annotation_status"] == "annotated"


def test_spec_20_2_15_imported_finished_publish_performance_reaches_performance_api():
    """Spec 20.2 #15: imported finished video, publish record, and performance data stay queryable."""
    with TestClient(create_app()) as active_client:
        login_admin(active_client)
        finished_video_id = import_one(
            active_client,
            "finished_video",
            {
                "external_id": "finished_ext",
                "case_id": "case_demo",
                "title": "Imported finished",
                "duration_sec": 12,
            },
        )
        video_version_id = import_one(
            active_client,
            "video_version",
            {
                "external_id": "version_ext",
                "case_id": "case_demo",
                "finished_video_id": finished_video_id,
            },
        )
        publish_record_id = import_one(
            active_client,
            "publish_record",
            {
                "external_id": "publish_ext",
                "case_id": "case_demo",
                "video_version_id": video_version_id,
                "platform": "douyin",
                "status": "published",
            },
        )
        performance_id = import_one(
            active_client,
            "performance",
            {
                "external_id": "performance_ext",
                "case_id": "case_demo",
                "publish_record_id": publish_record_id,
                "metric_name": "views",
                "metric_value": 1200,
            },
        )

        detail = active_client.get(f"/api/finished-videos/{finished_video_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["video_version"]["id"] == video_version_id
        assert detail.json()["publish_records"][0]["id"] == publish_record_id
        attribution = active_client.get(f"/api/videos/{video_version_id}/performance-attribution")
        assert attribution.status_code == 200, attribution.text
        assert any(item["id"] == performance_id for item in attribution.json()["observations"])
        performance = active_client.get("/api/cases/case_demo/performance")
        assert performance.status_code == 200, performance.text
        assert any(item["id"] == performance_id for item in performance.json()["observations"])


def test_prometheus_metrics_contract_is_exposed():
    response = client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    assert "provider_cost_estimated_total" in body
    assert "yield_funnel_events_total" in body
    assert "temporal_activity_failures_total" in body
