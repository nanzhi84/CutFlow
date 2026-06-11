from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c
from packages.core.storage.repository import new_id


def _login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _seed_run(app, *, status: c.RunStatus) -> tuple[c.Job, c.WorkflowRun]:
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
        ),
    )
    run = c.WorkflowRun(
        id=new_id("run"),
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id="digital-human-video",
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
