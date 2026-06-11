from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c
from packages.core.storage.repository import new_id


def _login(client: TestClient, email: str = "admin@local.cutagent", password: str = "local-admin") -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text


def _create_case(client: TestClient, name: str = "Delete Probe") -> dict:
    created = client.post("/api/cases", json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()


def test_delete_case_requires_authenticated_operator() -> None:
    with TestClient(create_app()) as client:
        unauthenticated = client.delete("/api/cases/case_demo")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["error"]["code"] == "auth.unauthorized"

        _login(client, "viewer@local.cutagent", "local-viewer")
        forbidden = client.delete("/api/cases/case_demo")
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "auth.forbidden"


def test_delete_case_removes_unreferenced_case_from_listing() -> None:
    with TestClient(create_app()) as client:
        _login(client)
        case = _create_case(client, "Disposable Case")

        deleted = client.delete(f"/api/cases/{case['id']}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["ok"] is True

        listed = client.get("/api/cases", params={"search": "Disposable Case"})
        assert listed.status_code == 200, listed.text
        assert all(item["id"] != case["id"] for item in listed.json()["items"])


def test_delete_case_rejects_active_run_reference() -> None:
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        case = _create_case(client, "Case With Active Run")
        job = c.Job(
            id=new_id("job"),
            type=c.JobType.digital_human_video,
            case_id=case["id"],
            created_by="usr_admin",
            request_schema="v1",
            request=c.DigitalHumanVideoRequest(
                case_id=case["id"],
                script="active run",
                voice={"voice_id": "voice_sandbox"},
            ),
        )
        run = c.WorkflowRun(
            id=new_id("run"),
            job_id=job.id,
            case_id=case["id"],
            workflow_template_id="digital-human-video",
            workflow_version="v1",
            status=c.RunStatus.running,
        )
        app.state.repository.jobs[job.id] = job
        app.state.repository.runs[run.id] = run

        rejected = client.delete(f"/api/cases/{case['id']}")
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "validation.conflict"


def test_delete_case_rejects_finished_video_reference() -> None:
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        case = _create_case(client, "Case With Finished Video")
        artifact = c.Artifact(
            id=new_id("art"),
            case_id=case["id"],
            kind=c.ArtifactKind.video_final,
            uri="sandbox://final.mp4",
            payload_schema="video.final.v1",
            payload={},
        )
        app.state.repository.artifacts[artifact.id] = artifact
        video = c.FinishedVideo(
            id=new_id("fv"),
            case_id=case["id"],
            title="Finished reference",
            video_artifact=app.state.repository.artifact_ref(artifact.id),
        )
        app.state.repository.finished_videos[video.id] = video

        rejected = client.delete(f"/api/cases/{case['id']}")
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "validation.conflict"
