import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

if os.getenv("CUTAGENT_RUN_DB_TESTS") != "1":
    pytest.skip("Set CUTAGENT_RUN_DB_TESTS=1 to run database integration tests.", allow_module_level=True)

from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
from packages.core.storage.database import (
    FinishedVideoRow,
    PerformanceObservationRow,
    PublishAttemptRow,
    PublishRecordRow,
    VideoVersionRow,
)
from packages.media.assets import local_object_path

FINISHED_VIDEO_URI = "local://cutagent-local/imported/sqlalchemy-finished-video.mp4"


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def test_sqlalchemy_finished_video_publish_record_and_performance_flow_are_persisted(media_fixture_factory):
    session_factory = sqlalchemy_session_factory()
    sample_video = media_fixture_factory.video(duration_sec=2, filename="sqlalchemy-finished-video.mp4")

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        # The import API only registers the URI; the bytes must already exist in the
        # object store for editor-handoff (copies the file) and jianying-draft
        # (ffprobes it) to read. Materialize a real, probe-able mp4 at that URI.
        materialized = local_object_path(app.state.object_store, FINISHED_VIDEO_URI)
        materialized.parent.mkdir(parents=True, exist_ok=True)
        materialized.write_bytes(sample_video.read_bytes())

        imported_finished = client.post(
            "/api/import/batches",
            json={
                "import_type": "finished_video",
                "rows": [
                    {
                        "case_id": "case_demo",
                        "external_id": "finished-video-db-flow",
                        "title": "Imported SQLAlchemy finished video",
                        "uri": FINISHED_VIDEO_URI,
                        "duration_sec": 42,
                        "qc_status": "passed",
                    }
                ],
            },
        )
        assert imported_finished.status_code == 202, imported_finished.text
        finished_report = imported_finished.json()
        finished_video_id = finished_report["results"][0]["internal_id"]

        imported_version = client.post(
            "/api/import/batches",
            json={
                "import_type": "video_version",
                "rows": [
                    {
                        "case_id": "case_demo",
                        "finished_video_id": finished_video_id,
                        "timeline_plan_artifact_id": "timeline_imported",
                        "style_plan_artifact_id": "style_imported",
                    }
                ],
            },
        )
        assert imported_version.status_code == 202, imported_version.text
        version_id = imported_version.json()["results"][0]["internal_id"]

        listed = client.get("/api/cases/case_demo/finished-videos")
        assert listed.status_code == 200, listed.text
        assert any(item["id"] == finished_video_id for item in listed.json()["items"])

        detail = client.get(f"/api/finished-videos/{finished_video_id}")
        assert detail.status_code == 200, detail.text
        detail_body = detail.json()
        assert detail_body["finished_video"]["title"] == "Imported SQLAlchemy finished video"
        assert detail_body["video_version"]["id"] == version_id

        preview = client.get(f"/api/finished-videos/{finished_video_id}/preview-url")
        assert preview.status_code == 200, preview.text
        assert preview.json()["url"].endswith("sqlalchemy-finished-video.mp4")

        handoff = client.post(f"/api/finished-videos/{finished_video_id}/editor-handoff", json={})
        assert handoff.status_code == 201, handoff.text
        assert handoff.json()["manifest"]["finished_video_id"] == finished_video_id

        jianying = client.post(
            f"/api/finished-videos/{finished_video_id}/jianying-draft",
            json={"template_id": "clean-template"},
        )
        assert jianying.status_code == 201, jianying.text
        assert jianying.json()["draft_manifest"]["template_id"] == "clean-template"

        created_package = client.post(
            "/api/publish/packages",
            json={
                "source_finished_video_id": finished_video_id,
                "title": "Finished video publish package",
                "description": "Publish from imported finished video",
            },
        )
        assert created_package.status_code == 201, created_package.text
        package = created_package.json()
        assert package["case_id"] == "case_demo"

        created_batch = client.post(
            "/api/publish/batches",
            json={"publish_package_ids": [package["id"]], "platform_targets": ["douyin"]},
        )
        assert created_batch.status_code == 201, created_batch.text
        batch = created_batch.json()
        item_id = batch["items"][0]["id"]

        submitted = client.post(f"/api/publish/batches/{batch['id']}/submit", json={"dry_run": False})
        assert submitted.status_code == 202, submitted.text
        assert submitted.json()["items"][0]["status"] == "published"

        with session_factory() as session:
            attempt = session.scalar(select(PublishAttemptRow).where(PublishAttemptRow.item_id == item_id))
            record = session.scalar(
                select(PublishRecordRow)
                .where(PublishRecordRow.publish_batch_id == batch["id"])
                .where(PublishRecordRow.publish_package_id == package["id"])
            )
            assert attempt is not None
            assert record is not None
            assert record.video_version_id == version_id
            publish_record_id = record.id
            attempt_id = attempt.id

        attempt_detail = client.get(f"/api/publish/attempts/{attempt_id}")
        assert attempt_detail.status_code == 200, attempt_detail.text
        assert attempt_detail.json()["record"]["id"] == publish_record_id

        metrics_import = client.post(
            "/api/cases/case_demo/metrics/import",
            json={
                "rows": [
                    {"publish_record_id": publish_record_id, "metric_name": "views", "metric_value": 100},
                    {"publish_record_id": publish_record_id, "metric_name": "likes", "metric_value": 7},
                ]
            },
        )
        assert metrics_import.status_code == 202, metrics_import.text
        assert metrics_import.json()["created_count"] == 2

        performance = client.get("/api/cases/case_demo/performance")
        assert performance.status_code == 200, performance.text
        assert performance.json()["metrics"]["views"] >= 100
        assert performance.json()["metrics"]["likes"] >= 7

        attribution = client.get(f"/api/videos/{version_id}/performance-attribution")
        assert attribution.status_code == 200, attribution.text
        assert any(
            item["publish_record_id"] == publish_record_id
            for item in attribution.json()["observations"]
        )

        stored_report = client.get(f"/api/import/batches/{finished_report['batch_id']}")
        assert stored_report.status_code == 200, stored_report.text
        assert stored_report.json()["batch_id"] == finished_report["batch_id"]

    with session_factory() as session:
        assert session.get(FinishedVideoRow, finished_video_id) is not None
        assert session.get(VideoVersionRow, version_id) is not None
        observations = list(
            session.scalars(
                select(PerformanceObservationRow).where(
                    PerformanceObservationRow.publish_record_id == publish_record_id
                )
            )
        )
        assert len(observations) == 2
