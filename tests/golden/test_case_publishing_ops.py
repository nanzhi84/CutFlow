from contextlib import contextmanager

from fastapi.testclient import TestClient

from apps.api.app import create_app
from tests.api._upload_helpers import direct_upload, minimal_ttf_bytes
from tests.golden._caption_font_fixture import register_default_caption_fonts


@contextmanager
def fresh_client():
    """A per-test app/client whose SQLAlchemy engine is disposed on exit.

    Each test gets its own in-memory workflow ``runtime_repository`` (aligned with
    the per-test SQL reset) and releases its connection pool before teardown
    TRUNCATEs the database, so a long golden run never deadlocks against it.
    """
    app = create_app()
    register_default_caption_fonts(app)
    # Do NOT enter the TestClient lifespan: app.state is fully configured at build
    # time. Entering would re-run bootstrap_sqlalchemy_storage (a full seed_database
    # merge over users/registration_codes/provider_profiles/media_assets) on every
    # test, contending on exactly the tables the conftest teardown TRUNCATEs and
    # deadlocking under a long golden run. Skipping it also avoids extra engine churn.
    active_client = TestClient(app)
    try:
        yield active_client
    finally:
        engine = app.state.sqlalchemy_session_factory.kw.get("bind")
        if engine is not None:
            engine.dispose()


def login_admin_for(active_client):
    response = active_client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def video_payload(title: str):
    return {
        "case_id": "case_demo",
        "title": title,
        "script": "用一个简短脚本补齐发布测试。",
        "voice": {"voice_id": "voice_sandbox"},
        "strictness": {"strict_timestamps": False},
    }


def create_finished_video(active_client, title: str = "Publishing seed") -> str:
    created = active_client.post("/api/jobs/digital-human-video", json=video_payload(title))
    assert created.status_code == 201, created.text
    assert created.json()["initial_run"]["status"] == "succeeded"
    videos = active_client.get("/api/cases/case_demo/finished-videos").json()["items"]
    return videos[-1]["id"]


def create_publish_batch(active_client, finished_video_id: str):
    package = active_client.post(
        "/api/publish/packages",
        json={
            "source_finished_video_id": finished_video_id,
            "title": "Publish me",
            "description": "",
        },
    )
    assert package.status_code == 201, package.text
    batch = active_client.post(
        "/api/publish/batches",
        json={"publish_package_ids": [package.json()["id"]], "platform_targets": ["douyin"]},
    )
    assert batch.status_code == 201, batch.text
    return batch.json()


def upload_cover_artifact(active_client) -> str:
    # The cover artifact is only referenced by id when PATCHed onto the package
    # (ensure_artifact_ref validates existence, not kind/media), so an incidental
    # ffprobe-free font upload is sufficient and avoids an auto-created MediaAsset.
    _prepared, completed = direct_upload(
        active_client,
        kind="font",
        filename="cover.ttf",
        content_type="font/ttf",
        body=minimal_ttf_bytes(family="Cover Artifact"),
        case_id="case_demo",
        metadata={"template_mode": "replace"},
    )
    assert completed is not None and completed.status_code == 200, completed.text
    return completed.json()["artifact"]["artifact_id"]


def test_case_publish_flow_reaches_ops_dashboard():
    with fresh_client() as client:
        login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert login.status_code == 200, login.text

        videos = client.get("/api/cases/case_demo/finished-videos").json()["items"]
        if not videos:
            client.post(
                "/api/jobs/digital-human-video",
                json={
                    "case_id": "case_demo",
                    "title": "Publishing seed",
                    "script": "用一个简短脚本补齐发布测试。",
                    "voice": {"voice_id": "voice_sandbox"},
                    "strictness": {"strict_timestamps": False},
                },
            )
            videos = client.get("/api/cases/case_demo/finished-videos").json()["items"]
        package = client.post(
            "/api/publish/packages",
            json={
                "source_finished_video_id": videos[-1]["id"],
                "title": "Publish me",
                "description": "",
            },
        ).json()
        batch = client.post(
            "/api/publish/batches",
            json={"publish_package_ids": [package["id"]], "platform_targets": ["douyin"]},
        ).json()
        submitted = client.post(
            f"/api/publish/batches/{batch['id']}/submit", json={"dry_run": False}
        ).json()
        assert submitted["status"] == "completed"
        assert submitted["items"][0]["status"] == "published"

        ops = client.get("/api/ops/dashboard").json()
        assert "usage" in ops
        assert "yield_funnel" in ops


def test_spec_20_2_12_publish_failure_can_retry_publish_successfully():
    """Spec 20.2 #12: sandbox publish failure can be retried through retry-publish."""
    with fresh_client() as active_client:
        login_admin_for(active_client)
        finished_video_id = create_finished_video(active_client, "Retry publish seed")
        batch = create_publish_batch(active_client, finished_video_id)
        failed = active_client.post(
            f"/api/publish/batches/{batch['id']}/submit",
            json={"dry_run": False, "simulate_publish_failure": True},
        )
        assert failed.status_code == 202, failed.text
        failed_body = failed.json()
        assert failed_body["status"] == "partial_failed"
        item = failed_body["items"][0]
        assert item["status"] == "publish_failed"

        retried = active_client.post(
            f"/api/publish/batches/{batch['id']}/items/{item['id']}/retry-publish",
            json={},
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "published"


def test_publish_package_cover_can_be_uploaded_and_cleared():
    with fresh_client() as active_client:
        login_admin_for(active_client)
        finished_video_id = create_finished_video(active_client, "Cover upload seed")
        package = active_client.post(
            "/api/publish/packages",
            json={
                "source_finished_video_id": finished_video_id,
                "title": "Cover package",
                "description": "",
            },
        )
        assert package.status_code == 201, package.text
        artifact_id = upload_cover_artifact(active_client)

        patched = active_client.patch(
            f"/api/publish/packages/{package.json()['id']}",
            json={"cover_artifact_id": artifact_id},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["cover_artifact"]["artifact_id"] == artifact_id

        cleared = active_client.patch(
            f"/api/publish/packages/{package.json()['id']}",
            json={"cover_artifact_id": None},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["cover_artifact"] is None


def test_publish_batch_can_be_deleted_from_recent_list():
    with fresh_client() as active_client:
        login_admin_for(active_client)
        finished_video_id = create_finished_video(active_client, "Delete batch seed")
        batch = create_publish_batch(active_client, finished_video_id)

        deleted = active_client.delete(f"/api/publish/batches/{batch['id']}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["ok"] is True

        detail = active_client.get(f"/api/publish/batches/{batch['id']}")
        assert detail.status_code == 404


def test_publish_batch_item_can_be_deleted_before_submit():
    with fresh_client() as active_client:
        login_admin_for(active_client)
        finished_video_id = create_finished_video(active_client, "Delete item seed")
        batch = create_publish_batch(active_client, finished_video_id)
        item_id = batch["items"][0]["id"]

        deleted = active_client.delete(f"/api/publish/items/{item_id}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["ok"] is True

        detail = active_client.get(f"/api/publish/batches/{batch['id']}")
        assert detail.status_code == 200, detail.text
        assert all(item["id"] != item_id for item in detail.json()["items"])


def test_publish_attempts_can_be_listed_by_batch():
    with fresh_client() as active_client:
        login_admin_for(active_client)
        finished_video_id = create_finished_video(active_client, "Attempt list seed")
        batch = create_publish_batch(active_client, finished_video_id)
        submitted = active_client.post(
            f"/api/publish/batches/{batch['id']}/submit", json={"dry_run": True}
        )
        assert submitted.status_code == 202, submitted.text

        attempts = active_client.get(f"/api/publish/batches/{batch['id']}/attempts")
        assert attempts.status_code == 200, attempts.text
        assert attempts.json()["items"][0]["status"] == "manual_review_ready"
        assert attempts.json()["items"][0]["adapter_id"] == "sandbox.publish"


# The exact §9.5 funnel taxonomy (spec 树影 v3 §9.5 成品率漏斗). Tests assert the
# funnel emits these spec strings, not retired workflow_*/publish_attempt_* names.
SPEC_9_5_FUNNEL_STAGES = {
    "submitted",
    "admitted",
    "started",
    "node_started",
    "node_succeeded",
    "node_failed",
    "finished_video_created",
    "qc_started",
    "qc_passed",
    "qc_failed",
    "manual_approved",
    "manual_rejected",
    "publish_started",
    "published",
    "publish_failed",
}


def _funnel_event_types(active_client) -> set[str]:
    funnel = active_client.get("/api/ops/yield-funnel")
    assert funnel.status_code == 200, funnel.text
    return {event["event_type"] for event in funnel.json()["events"]}


def _finished_video(active_client, finished_video_id: str) -> dict:
    videos = active_client.get("/api/cases/case_demo/finished-videos").json()["items"]
    return next(v for v in videos if v["id"] == finished_video_id)


def test_yield_funnel_records_full_lifecycle_stages():
    """G3: the §9.5 funnel records the run/node/finished-video/qc/publish stages.

    A run that goes submit -> start -> nodes -> finished video -> publish surfaces
    the bare §9.5 spec strings (submitted/admitted/started/node_*/published/...),
    and a manual approval surfaces manual_approved.
    """
    with fresh_client() as active_client:
        login_admin_for(active_client)
        finished_video_id = create_finished_video(active_client, "Funnel coverage seed")
        run_id = _finished_video(active_client, finished_video_id)["run_id"]

        # A passing run-level quality check -> qc_started + qc_passed.
        qc = active_client.post(
            f"/api/runs/{run_id}/quality-checks",
            json={"check_type": "manual", "result": "passed"},
        )
        assert qc.status_code == 201, qc.text
        # A manual approval decision -> manual_approved.
        approved = active_client.post(
            "/api/approval-requests/ar_funnel_ok/approve", json={"reason": "looks good"}
        )
        assert approved.status_code == 200, approved.text

        batch = create_publish_batch(active_client, finished_video_id)
        submitted = active_client.post(
            f"/api/publish/batches/{batch['id']}/submit", json={"dry_run": False}
        )
        assert submitted.status_code == 202, submitted.text
        assert submitted.json()["items"][0]["status"] == "published"

        event_types = _funnel_event_types(active_client)

        # Run admission + start.
        assert "submitted" in event_types
        assert "admitted" in event_types
        assert "started" in event_types
        # Node runner lifecycle (the core missing piece before this fix).
        assert "node_started" in event_types
        assert "node_succeeded" in event_types
        # Finished video.
        assert "finished_video_created" in event_types
        # QC + manual review stages.
        assert "qc_started" in event_types
        assert "qc_passed" in event_types
        assert "manual_approved" in event_types
        # Publish stages.
        assert "publish_started" in event_types
        assert "published" in event_types
        # Every emitted stage is a §9.5 spec string.
        assert event_types <= SPEC_9_5_FUNNEL_STAGES
        assert "workflow_succeeded" not in event_types
        assert "publish_attempt_submitted" not in event_types
        assert "publish_package_created" not in event_types


def test_yield_funnel_records_publish_failure_stage():
    """G3: a simulated publish failure surfaces publish_failed."""
    with fresh_client() as active_client:
        login_admin_for(active_client)
        finished_video_id = create_finished_video(active_client, "Funnel failure seed")
        batch = create_publish_batch(active_client, finished_video_id)
        failed = active_client.post(
            f"/api/publish/batches/{batch['id']}/submit",
            json={"dry_run": False, "simulate_publish_failure": True},
        )
        assert failed.status_code == 202, failed.text
        assert failed.json()["items"][0]["status"] == "publish_failed"

        event_types = _funnel_event_types(active_client)
        assert "publish_started" in event_types
        assert "publish_failed" in event_types
        assert "published" not in event_types


def test_true_yield_rate_is_run_scoped_and_excludes_qc_failed_run():
    """G3 / §9.5: true_yield_rate counts DISTINCT runs that reached ``published``
    and were not ``qc_failed`` — NOT successes/total_events. A qc_failed run is
    excluded even though it published, so a single run that qc-fails yields 0.0."""
    with fresh_client() as active_client:
        login_admin_for(active_client)
        finished_video_id = create_finished_video(active_client, "True yield qc-fail seed")
        run_id = _finished_video(active_client, finished_video_id)["run_id"]

        # Publish the run -> it reaches ``published`` (technically successful).
        batch = create_publish_batch(active_client, finished_video_id)
        published = active_client.post(
            f"/api/publish/batches/{batch['id']}/submit", json={"dry_run": False}
        )
        assert published.status_code == 202, published.text

        baseline = active_client.get("/api/ops/yield-funnel").json()
        assert "published" in {e["event_type"] for e in baseline["events"]}
        # Before the qc failure, the published run counts as true yield (1.0).
        assert baseline["true_yield_rate"] == 1.0

        # A failing QC on that run excludes it from true yield.
        qc = active_client.post(
            f"/api/runs/{run_id}/quality-checks",
            json={"check_type": "manual", "result": "failed"},
        )
        assert qc.status_code == 201, qc.text

        after = active_client.get("/api/ops/yield-funnel").json()
        event_types = {e["event_type"] for e in after["events"]}
        assert "qc_failed" in event_types
        # 技术成功但 QC 不通过不能计入 true yield -> the only run is excluded -> 0.0.
        assert after["true_yield_rate"] == 0.0


def test_yield_funnel_records_manual_rejected_stage():
    """G3 / §9.5: a manual rejection surfaces manual_rejected."""
    with fresh_client() as active_client:
        login_admin_for(active_client)
        rejected = active_client.post(
            "/api/approval-requests/ar_funnel_reject/reject", json={"reason": "off-brand"}
        )
        assert rejected.status_code == 200, rejected.text
        event_types = _funnel_event_types(active_client)
        assert "manual_rejected" in event_types
