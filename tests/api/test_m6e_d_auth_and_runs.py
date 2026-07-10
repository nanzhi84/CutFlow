from fastapi import Request
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.services import jobs_runs
from packages.core import contracts as c
from packages.core.storage.repository import new_id


def _login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _seed_run(
    app,
    *,
    status: c.RunStatus,
    workflow_template_id: str = "digital_human_v2",
) -> tuple[c.Job, c.WorkflowRun]:
    job = c.Job(
        id=new_id("job"),
        type=c.JobType.digital_human_video,
        case_id="case_demo",
        created_by="usr_admin",
        request_schema="v1",
        request=c.DigitalHumanVideoRequest(
            case_id="case_demo",
            script="seed run",
            voice={"voice_id": "voice_sandbox"},
            workflow_template_id=workflow_template_id,
        ),
    )
    run = c.WorkflowRun(
        id=new_id("run"),
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id=workflow_template_id,
        workflow_version="v1",
        status=status,
    )
    app.state.repository.jobs[job.id] = job.model_copy(update={"active_run_id": run.id})
    app.state.repository.runs[run.id] = run
    app.state.repository.node_runs[run.id] = [
        c.NodeRun(
            id=new_id("node"),
            run_id=run.id,
            node_id="TTS",
            node_version="v1",
            status=c.NodeStatus.succeeded,
            input_manifest_hash="hash_seed",
        )
    ]
    return job, run


class _NoopWorkflow:
    def start_run(self, *, job, run, template):
        return None

    def resume_run(self, *, source_run_id, new_run, reuse_plan):
        return None

    def cancel_run(self, run_id, *, force: bool = False, reason: str | None = None):
        return None


def test_login_accepts_identifier_email_or_display_name() -> None:
    with TestClient(create_app()) as client:
        by_name = client.post(
            "/api/auth/login",
            json={"identifier": "Local Admin", "password": "local-admin"},
        )
        assert by_name.status_code == 200, by_name.text
        assert by_name.json()["user"]["email"] == "admin@local.cutagent"

    with TestClient(create_app()) as client:
        by_email = client.post(
            "/api/auth/login",
            json={"identifier": "admin@local.cutagent", "password": "local-admin"},
        )
        assert by_email.status_code == 200, by_email.text


def test_registration_code_custom_code_and_purpose_round_trip() -> None:
    with TestClient(create_app()) as client:
        _login_admin(client)

        created = client.post(
            "/api/auth/registration-codes",
            json={
                "role": "operator",
                "max_uses": 1,
                "custom_code": "TEAM-2026",
                "purpose": "交付团队入职",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["plaintext_code"] == "TEAM-2026"
        assert body["purpose"] == "交付团队入职"

        listed = client.get("/api/auth/registration-codes")
        assert listed.status_code == 200, listed.text
        listed_code = next(item for item in listed.json()["items"] if item["id"] == body["id"])
        assert listed_code["purpose"] == "交付团队入职"

        registered = client.post(
            "/api/auth/register",
            json={
                "email": "team-2026@example.test",
                "password": "correct horse battery staple",
                "display_name": "Team Member",
                "registration_code": "TEAM-2026",
            },
        )
        assert registered.status_code == 201, registered.text
        assert registered.json()["user"]["role"] == "operator"


def test_delete_run_record_rejects_processing_runs() -> None:
    app = create_app()
    with TestClient(app) as client:
        _login_admin(client)
        _, run = _seed_run(app, status=c.RunStatus.running)

        response = client.delete(f"/api/runs/{run.id}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "validation.conflict"
        assert run.id in app.state.repository.runs


def test_delete_run_record_removes_terminal_record_but_keeps_finished_video() -> None:
    app = create_app()
    with TestClient(app) as client:
        _login_admin(client)
        _, run = _seed_run(app, status=c.RunStatus.succeeded)
        artifact = c.Artifact(
            id=new_id("art"),
            case_id="case_demo",
            run_id=run.id,
            kind=c.ArtifactKind.video_final,
            uri="sandbox://final.mp4",
            payload_schema="video.final.v1",
            payload={},
        )
        app.state.repository.artifacts[artifact.id] = artifact
        video = c.FinishedVideo(
            id=new_id("fv"),
            case_id="case_demo",
            run_id=run.id,
            title="完成成片",
            video_artifact=app.state.repository.artifact_ref(artifact.id),
        )
        app.state.repository.finished_videos[video.id] = video

        response = client.delete(f"/api/runs/{run.id}")
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert run.id not in app.state.repository.runs
        assert video.id in app.state.repository.finished_videos
        assert app.state.repository.finished_videos[video.id].run_id is None

        listed = client.get("/api/cases/case_demo/runs")
        assert listed.status_code == 200, listed.text
        assert all(item["run_id"] != run.id for item in listed.json()["items"])


def test_retry_failed_run_requeues_failed_job_without_invalid_transition() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.workflow = _NoopWorkflow()
        _login_admin(client)
        job, run = _seed_run(app, status=c.RunStatus.failed)
        app.state.repository.jobs[job.id] = job.model_copy(
            update={
                "active_run_id": run.id,
                "status": c.JobStatus.failed,
            }
        )

        response = client.post(f"/api/runs/{run.id}/retry", json={"reason": "pytest"})

        assert response.status_code == 201, response.text
        retry_runs = [
            item for item in app.state.repository.runs.values() if item.retry_of_run_id == run.id
        ]
        assert len(retry_runs) == 1
        assert app.state.repository.jobs[job.id].status == c.JobStatus.queued


def test_new_legacy_editing_agent_job_is_rejected_without_orphan_job() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.workflow = _NoopWorkflow()
        _login_admin(client)

        response = client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "script": "legacy admission must fail",
                "voice": {"voice_id": "voice_sandbox"},
                "workflow_template_id": "digital_human_editing_agent_v1",
            },
        )

        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "validation.invalid_options"
        assert all(
            job.request.workflow_template_id != "digital_human_editing_agent_v1"
            for job in app.state.repository.jobs.values()
        )


def test_legacy_editing_agent_retry_or_resume_requires_real_v1_lineage() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.workflow = _NoopWorkflow()
        _login_admin(client)
        orphan = c.Job(
            id=new_id("job"),
            type=c.JobType.digital_human_video,
            case_id="case_demo",
            created_by="usr_admin",
            request_schema="v1",
            request=c.DigitalHumanVideoRequest(
                case_id="case_demo",
                script="legacy lineage is mandatory",
                voice={"voice_id": "voice_sandbox"},
                workflow_template_id="digital_human_editing_agent_v1",
            ),
        )
        app.state.repository.jobs[orphan.id] = orphan

        for mode in ("retry", "resume"):
            response = client.post(f"/api/jobs/{orphan.id}/runs", json={"mode": mode})
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "validation.invalid_options"
        assert not any(run.job_id == orphan.id for run in app.state.repository.runs.values())


def test_legacy_editing_agent_history_can_retry_and_resume() -> None:
    retry_app = create_app()
    with TestClient(retry_app) as client:
        retry_app.state.workflow = _NoopWorkflow()
        _login_admin(client)
        failed_job, failed_run = _seed_run(
            retry_app,
            status=c.RunStatus.failed,
            workflow_template_id="digital_human_editing_agent_v1",
        )
        retry_app.state.repository.jobs[failed_job.id] = failed_job.model_copy(
            update={"active_run_id": failed_run.id, "status": c.JobStatus.failed}
        )

        retried = client.post(f"/api/runs/{failed_run.id}/retry", json={"reason": "history"})
        assert retried.status_code == 201, retried.text
        assert retried.json()["run"]["workflow_template_id"] == "digital_human_editing_agent_v1"

    resume_app = create_app()
    with TestClient(resume_app) as client:
        resume_app.state.workflow = _NoopWorkflow()
        _login_admin(client)
        succeeded_job, succeeded_run = _seed_run(
            resume_app,
            status=c.RunStatus.succeeded,
            workflow_template_id="digital_human_editing_agent_v1",
        )
        resume_app.state.repository.jobs[succeeded_job.id] = succeeded_job.model_copy(
            update={"active_run_id": succeeded_run.id, "status": c.JobStatus.succeeded}
        )
        _job, resumed, template = jobs_runs._admit_run(
            Request({"type": "http", "app": resume_app}),
            job_id=succeeded_job.id,
            mode="resume",
            from_run_id=succeeded_run.id,
            reason="history",
        )
        assert resumed.workflow_template_id == "digital_human_editing_agent_v1"
        assert resumed.resume_from_run_id == succeeded_run.id
        assert template.workflow_template_id == "digital_human_editing_agent_v1"


def test_legacy_resume_without_artifact_reuse_keeps_source_lineage() -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.workflow = _NoopWorkflow()
        _login_admin(client)
        job, source = _seed_run(
            app,
            status=c.RunStatus.succeeded,
            workflow_template_id="digital_human_editing_agent_v1",
        )
        app.state.repository.jobs[job.id] = job.model_copy(
            update={"active_run_id": source.id, "status": c.JobStatus.succeeded}
        )

        response = client.post(
            f"/api/runs/{source.id}/resume",
            json={"reason": "clean recompute", "reuse_valid_artifacts": False},
        )

        assert response.status_code == 201, response.text
        assert response.json()["run"]["resume_from_run_id"] == source.id
