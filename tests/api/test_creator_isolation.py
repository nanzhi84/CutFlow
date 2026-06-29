"""Creator-based isolation (spec §3): operator/viewer only see their own
jobs/runs/finished-videos + overview counts; admin sees all; guessing another
user's resource id returns 404. Cases stay shared (NOT isolated)."""

from __future__ import annotations

import io
import zipfile
from datetime import timedelta

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c
from packages.core.auth.sqlalchemy_service import hash_session_token
from packages.core.observability import record_funnel_event
from packages.core.storage.database import (
    ArtifactRow,
    CaseRow,
    SessionRow,
    UserRow,
    WorkflowRunRow,
)
from packages.core.storage.repository import Repository, new_id

SESSION_COOKIE = "cutagent_session"


def _make_user(app, *, role: c.UserRole) -> tuple[c.AuthUser, str]:
    """Create a real user + session row in Postgres and return its session token
    (used as the cookie value). Auth reads the SQL ``sessions``/``users`` tables."""
    user = c.AuthUser(
        id=new_id("usr"),
        email=f"{new_id('u')}@local.test",
        display_name="Iso User",
        role=role,
    )
    token = new_id("sess")
    with app.state.sqlalchemy_session_factory() as session:
        session.add(
            UserRow(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                password_hash="unused-session-auth",
                role=role.value,
                status="active",
            )
        )
        session.flush()
        session.add(
            SessionRow(
                id=hash_session_token(token),
                user_id=user.id,
                expires_at=c.utcnow() + timedelta(days=7),
            )
        )
        session.commit()
    return user, token


def _seed_finished_video_for(app, *, owner: str, case_id: str) -> tuple[c.Job, c.WorkflowRun, c.FinishedVideo]:
    repo = Repository()
    job = c.Job(
        id=new_id("job"),
        type=c.JobType.digital_human_video,
        case_id=case_id,
        created_by=owner,
        request_schema="v1",
        request=c.DigitalHumanVideoRequest(
            case_id=case_id,
            script="iso seed",
            voice={"voice_id": "voice_sandbox"},
        ),
    )
    run = c.WorkflowRun(
        id=new_id("run"),
        job_id=job.id,
        case_id=case_id,
        workflow_template_id="digital-human-video",
        workflow_version="v1",
        status=c.RunStatus.succeeded,
        requested_by=owner,
    )
    repo.jobs[job.id] = job.model_copy(update={"active_run_id": run.id})
    repo.runs[run.id] = run
    repo.node_runs[run.id] = []
    video_object = app.state.object_store.prepare_upload("final.mp4", "tests")
    stored_video = app.state.object_store.put_bytes(video_object, b"fake mp4")
    artifact = repo.create_artifact(
        kind=c.ArtifactKind.video_final,
        payload_schema="video.final.v1",
        payload={},
        case_id=case_id,
        run_id=run.id,
        uri=stored_video.ref.uri,
        sha256=stored_video.sha256,
    )
    cover_object = app.state.object_store.prepare_upload("cover.jpg", "tests")
    stored_cover = app.state.object_store.put_bytes(cover_object, b"fake jpeg")
    cover_artifact = repo.create_artifact(
        kind=c.ArtifactKind.cover_image,
        payload_schema="uri-only",
        payload=None,
        case_id=case_id,
        run_id=run.id,
        uri=stored_cover.ref.uri,
        sha256=stored_cover.sha256,
    )
    video = c.FinishedVideo(
        id=new_id("fv"),
        case_id=case_id,
        run_id=run.id,
        owner_user_id=owner,
        title="iso video",
        video_artifact=repo.artifact_ref(artifact.id),
        cover_artifact=repo.artifact_ref(cover_artifact.id),
    )
    repo.finished_videos[video.id] = video
    # Funnel event so the overview dashboard counts this run (processing bucket).
    record_funnel_event(
        repo,
        event_type=c.RunStatus.running.value if hasattr(c.RunStatus.running, "value") else "running",
        job_id=job.id,
        run_id=run.id,
        dedupe_aggregate_id=run.id,
        event_time=run.updated_at,
    )
    # Flush the assembled run snapshot (job/run/artifacts/finished-video/funnel)
    # into Postgres so the SQL-backed read paths can see it.
    app.state.sqlalchemy_production_repository.sync_workflow_snapshot(
        job=repo.jobs[job.id], run=run, repository=repo
    )
    return repo.jobs[job.id], run, video


def _cookie(client: TestClient, token: str) -> None:
    client.cookies.set(SESSION_COOKIE, token)


def _seed_case(app, case_id: str, *, owner_user_id: str | None = "usr_admin") -> None:
    with app.state.sqlalchemy_session_factory() as session:
        if session.get(CaseRow, case_id) is not None:
            return
        session.add(
            CaseRow(
                id=case_id,
                name="共享案例",
                owner_user_id=owner_user_id,
                status="active",
            )
        )
        session.commit()


def test_run_cards_isolated_by_creator() -> None:
    app = create_app()
    with TestClient(app) as client:
        _seed_case(app, "case_iso")
        user_a, token_a = _make_user(app, role=c.UserRole.operator)
        _user_b, token_b = _make_user(app, role=c.UserRole.operator)
        job_a, run_a, _ = _seed_finished_video_for(app, owner=user_a.id, case_id="case_iso")

        _cookie(client, token_a)
        own = client.get("/api/cases/case_iso/runs")
        assert own.status_code == 200, own.text
        assert any(item["runId"] == run_a.id for item in own.json()["items"])

        _cookie(client, token_b)
        other = client.get("/api/cases/case_iso/runs")
        assert other.status_code == 200, other.text
        assert all(item["runId"] != run_a.id for item in other.json()["items"])


def test_run_cards_admin_sees_all() -> None:
    app = create_app()
    with TestClient(app) as client:
        _seed_case(app, "case_iso")
        user_a, _ = _make_user(app, role=c.UserRole.operator)
        _admin, token_admin = _make_user(app, role=c.UserRole.admin)
        _job_a, run_a, _ = _seed_finished_video_for(app, owner=user_a.id, case_id="case_iso")

        _cookie(client, token_admin)
        listed = client.get("/api/cases/case_iso/runs")
        assert listed.status_code == 200, listed.text
        assert any(item["runId"] == run_a.id for item in listed.json()["items"])


def test_finished_video_list_isolated() -> None:
    app = create_app()
    with TestClient(app) as client:
        _seed_case(app, "case_iso")
        user_a, token_a = _make_user(app, role=c.UserRole.operator)
        _user_b, token_b = _make_user(app, role=c.UserRole.operator)
        _job_a, _run_a, video_a = _seed_finished_video_for(app, owner=user_a.id, case_id="case_iso")

        _cookie(client, token_a)
        own = client.get("/api/cases/case_iso/finished-videos")
        assert own.status_code == 200, own.text
        assert any(item["id"] == video_a.id for item in own.json()["items"])

        _cookie(client, token_b)
        other = client.get("/api/cases/case_iso/finished-videos")
        assert other.status_code == 200, other.text
        assert all(item["id"] != video_a.id for item in other.json()["items"])


def test_detail_preview_download_cross_user_404() -> None:
    app = create_app()
    with TestClient(app) as client:
        _seed_case(app, "case_iso")
        user_a, token_a = _make_user(app, role=c.UserRole.operator)
        _user_b, token_b = _make_user(app, role=c.UserRole.operator)
        job_a, run_a, video_a = _seed_finished_video_for(app, owner=user_a.id, case_id="case_iso")

        # Owner can read.
        _cookie(client, token_a)
        assert client.get(f"/api/jobs/{job_a.id}").status_code == 200
        assert client.get(f"/api/runs/{run_a.id}").status_code == 200
        assert client.get(f"/api/finished-videos/{video_a.id}").status_code == 200
        assert client.get(f"/api/finished-videos/{video_a.id}/preview-url").status_code == 200
        download_meta = client.get(f"/api/finished-videos/{video_a.id}/download")
        assert download_meta.status_code == 200
        assert download_meta.json()["content_type"] == "application/zip"
        package = client.get(download_meta.json()["url"])
        assert package.status_code == 200, package.text
        with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
            names = set(archive.namelist())
            assert "title.txt" in names
            assert any(name.startswith("cover.") for name in names)
            assert any(name.startswith("video.") for name in names)

        # Cross-user => 404 (do not leak existence).
        _cookie(client, token_b)
        assert client.get(f"/api/jobs/{job_a.id}").status_code == 404
        assert client.get(f"/api/runs/{run_a.id}").status_code == 404
        assert client.get(f"/api/finished-videos/{video_a.id}").status_code == 404
        assert client.get(f"/api/finished-videos/{video_a.id}/preview-url").status_code == 404
        assert client.get(f"/api/finished-videos/{video_a.id}/download").status_code == 404


def test_finished_video_export_cross_user_404() -> None:
    """editor-handoff / jianying-draft are operator-gated, so a non-owner operator
    must NOT be able to export another user's finished video — owner-gated to 404
    (no existence leak). (delete is admin-only and thus not creator-isolated.)"""
    app = create_app()
    with TestClient(app) as client:
        _seed_case(app, "case_iso")
        user_a, _token_a = _make_user(app, role=c.UserRole.operator)
        _user_b, token_b = _make_user(app, role=c.UserRole.operator)
        _job_a, _run_a, video_a = _seed_finished_video_for(app, owner=user_a.id, case_id="case_iso")

        # Cross-user export of another operator's video => 404 (owner gate).
        _cookie(client, token_b)
        assert client.post(f"/api/finished-videos/{video_a.id}/editor-handoff", json={}).status_code == 404
        assert client.post(f"/api/finished-videos/{video_a.id}/jianying-draft", json={}).status_code == 404


def test_detail_admin_sees_all() -> None:
    app = create_app()
    with TestClient(app) as client:
        _seed_case(app, "case_iso")
        user_a, _ = _make_user(app, role=c.UserRole.operator)
        _admin, token_admin = _make_user(app, role=c.UserRole.admin)
        job_a, run_a, video_a = _seed_finished_video_for(app, owner=user_a.id, case_id="case_iso")

        _cookie(client, token_admin)
        assert client.get(f"/api/jobs/{job_a.id}").status_code == 200
        assert client.get(f"/api/runs/{run_a.id}").status_code == 200
        assert client.get(f"/api/finished-videos/{video_a.id}").status_code == 200


def test_overview_dashboard_counts_isolated() -> None:
    app = create_app()
    with TestClient(app) as client:
        _seed_case(app, "case_iso")
        user_a, token_a = _make_user(app, role=c.UserRole.operator)
        _user_b, token_b = _make_user(app, role=c.UserRole.operator)
        _job_a, run_a, _ = _seed_finished_video_for(app, owner=user_a.id, case_id="case_iso")

        _cookie(client, token_a)
        own = client.get("/api/ops/dashboard")
        assert own.status_code == 200, own.text
        own_runs = {e["run_id"] for e in own.json()["yield_funnel"]["events"] if e.get("run_id")}
        assert run_a.id in own_runs

        _cookie(client, token_b)
        other = client.get("/api/ops/dashboard")
        assert other.status_code == 200, other.text
        other_runs = {e["run_id"] for e in other.json()["yield_funnel"]["events"] if e.get("run_id")}
        assert run_a.id not in other_runs


def test_overview_dashboard_admin_sees_all() -> None:
    app = create_app()
    with TestClient(app) as client:
        _seed_case(app, "case_iso")
        user_a, _ = _make_user(app, role=c.UserRole.operator)
        _admin, token_admin = _make_user(app, role=c.UserRole.admin)
        _job_a, run_a, _ = _seed_finished_video_for(app, owner=user_a.id, case_id="case_iso")

        _cookie(client, token_admin)
        listed = client.get("/api/ops/dashboard")
        assert listed.status_code == 200, listed.text
        admin_runs = {e["run_id"] for e in listed.json()["yield_funnel"]["events"] if e.get("run_id")}
        assert run_a.id in admin_runs


def _seed_public_report(app, run: c.WorkflowRun) -> None:
    """Attach a minimal public report artifact so run_report returns 200 for the
    owner (otherwise it 404s on missing report, masking the owner-gate check)."""
    report = c.RunPublicReportArtifact(
        run_id=run.id,
        status=run.status,
        summary="iso report",
        node_statuses={},
    )
    artifact_id = new_id("art")
    with app.state.sqlalchemy_session_factory() as session:
        session.add(
            ArtifactRow(
                id=artifact_id,
                case_id=run.case_id,
                run_id=run.id,
                kind=c.ArtifactKind.run_report_public.value,
                uri="sandbox://report.json",
                payload_schema="run.report.public.v1",
                payload=report.model_dump(mode="json"),
            )
        )
        run_row = session.get(WorkflowRunRow, run.id)
        run_row.public_report_artifact_id = artifact_id
        session.commit()


def test_run_report_artifacts_events_cross_user_404() -> None:
    """report/artifacts/events are owner-gated: owner + admin 200, other user 404."""
    app = create_app()
    with TestClient(app) as client:
        _seed_case(app, "case_iso")
        user_a, token_a = _make_user(app, role=c.UserRole.operator)
        _user_b, token_b = _make_user(app, role=c.UserRole.operator)
        _admin, token_admin = _make_user(app, role=c.UserRole.admin)
        _job_a, run_a, _ = _seed_finished_video_for(app, owner=user_a.id, case_id="case_iso")
        _seed_public_report(app, run_a)

        # Owner can read all three.
        _cookie(client, token_a)
        assert client.get(f"/api/runs/{run_a.id}/report").status_code == 200
        assert client.get(f"/api/runs/{run_a.id}/artifacts").status_code == 200
        assert client.get(f"/api/runs/{run_a.id}/events").status_code == 200

        # Admin can read all three.
        _cookie(client, token_admin)
        assert client.get(f"/api/runs/{run_a.id}/report").status_code == 200
        assert client.get(f"/api/runs/{run_a.id}/artifacts").status_code == 200
        assert client.get(f"/api/runs/{run_a.id}/events").status_code == 200

        # Cross-user => 404 (do not leak existence) on every owner-gated endpoint.
        _cookie(client, token_b)
        assert client.get(f"/api/runs/{run_a.id}/report").status_code == 404
        assert client.get(f"/api/runs/{run_a.id}/artifacts").status_code == 404
        assert client.get(f"/api/runs/{run_a.id}/events").status_code == 404


def test_cases_remain_shared() -> None:
    """Cases are NOT isolated: user B must still see user A's case (created by A)."""
    app = create_app()
    with TestClient(app) as client:
        user_a, _ = _make_user(app, role=c.UserRole.operator)
        _user_b, token_b = _make_user(app, role=c.UserRole.operator)
        case_id = new_id("case")
        _seed_case(app, case_id, owner_user_id=user_a.id)

        _cookie(client, token_b)
        listed = client.get("/api/cases")
        assert listed.status_code == 200, listed.text
        assert any(item["id"] == case_id for item in listed.json()["items"])

        detail = client.get(f"/api/cases/{case_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["id"] == case_id
