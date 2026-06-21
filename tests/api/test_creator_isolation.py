"""Creator-based isolation (spec §3): operator/viewer only see their own
jobs/runs/finished-videos + overview counts; admin sees all; guessing another
user's resource id returns 404. Cases stay shared (NOT isolated)."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c
from packages.core.observability import record_funnel_event
from packages.core.storage.repository import new_id

SESSION_COOKIE = "cutagent_session"


def _make_user(app, *, role: c.UserRole) -> tuple[c.AuthUser, str]:
    """Seed a user + session straight into the in-memory repo and return its
    session token (used as the cookie value)."""
    repo = app.state.repository
    user = c.AuthUser(
        id=new_id("usr"),
        email=f"{new_id('u')}@local.test",
        display_name="Iso User",
        role=role,
    )
    repo.users[user.id] = user
    token = new_id("sess")
    repo.sessions[token] = {"user_id": user.id, "expires_at": c.utcnow() + timedelta(days=7)}
    return user, token


def _seed_finished_video_for(app, *, owner: str, case_id: str) -> tuple[c.Job, c.WorkflowRun, c.FinishedVideo]:
    repo = app.state.repository
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
    artifact = c.Artifact(
        id=new_id("art"),
        case_id=case_id,
        run_id=run.id,
        kind=c.ArtifactKind.video_final,
        uri="sandbox://final.mp4",
        payload_schema="video.final.v1",
        payload={},
    )
    repo.artifacts[artifact.id] = artifact
    video = c.FinishedVideo(
        id=new_id("fv"),
        case_id=case_id,
        run_id=run.id,
        owner_user_id=owner,
        title="iso video",
        video_artifact=repo.artifact_ref(artifact.id),
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
    return job, run, video


def _cookie(client: TestClient, token: str) -> None:
    client.cookies.set(SESSION_COOKIE, token)


def _seed_case(app, case_id: str) -> None:
    repo = app.state.repository
    if case_id in repo.cases:
        return
    repo.cases[case_id] = c.CaseDetail(
        id=case_id,
        name="共享案例",
        owner_user_id="usr_admin",
    )


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
        assert client.get(f"/api/finished-videos/{video_a.id}/download").status_code == 200

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
    repo = app.state.repository
    report = c.RunPublicReportArtifact(
        run_id=run.id,
        status=run.status,
        summary="iso report",
        node_statuses={},
    )
    artifact = c.Artifact(
        id=new_id("art"),
        case_id=run.case_id,
        run_id=run.id,
        kind=c.ArtifactKind.run_report_public,
        uri="sandbox://report.json",
        payload_schema="run.report.public.v1",
        payload=report.model_dump(mode="json"),
    )
    repo.artifacts[artifact.id] = artifact
    repo.runs[run.id] = repo.runs[run.id].model_copy(
        update={"public_report_artifact_id": artifact.id}
    )


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
        app.state.repository.cases[case_id] = c.CaseDetail(
            id=case_id,
            name="A 的案例",
            owner_user_id=user_a.id,
        )

        _cookie(client, token_b)
        listed = client.get("/api/cases")
        assert listed.status_code == 200, listed.text
        assert any(item["id"] == case_id for item in listed.json()["items"])

        detail = client.get(f"/api/cases/{case_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["id"] == case_id
