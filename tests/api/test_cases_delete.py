from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c
from packages.core.storage.database import FinishedVideoRow
from packages.core.storage.repository import Repository, new_id


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


def test_case_detail_missing_case_raises_missing_case() -> None:
    # get_case (apps.api.common) folds its dual-track dispatch in #87 A1: when the
    # SQL case repository has no such case it must raise validation_missing_case,
    # not fall through to the (now-empty) in-memory runtime repo. Pin that 404.
    with TestClient(create_app()) as client:
        _login(client)
        resp = client.get("/api/cases/case_nonexistent")
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "validation.missing_case", resp.text


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


def test_delete_case_rejects_unknown_body_without_deleting() -> None:
    with TestClient(create_app()) as client:
        _login(client)
        case = _create_case(client, "Delete Validation Guard")

        rejected = client.request(
            "DELETE",
            f"/api/cases/{case['id']}",
            json={"__unexpected": True},
        )

        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["error"]["code"] == "validation.invalid_options"
        assert client.get(f"/api/cases/{case['id']}").status_code == 200


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
        # Flush the job + (active) run into Postgres so the SQL delete-guard sees a
        # blocking reference (the in-memory repo is no longer a storage backend).
        repo = Repository()
        repo.jobs[job.id] = job
        repo.runs[run.id] = run
        repo.node_runs[run.id] = []
        app.state.sqlalchemy_production_repository.sync_workflow_snapshot(
            job=job, run=run, repository=repo
        )

        rejected = client.delete(f"/api/cases/{case['id']}")
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "validation.conflict"


def test_delete_case_rejects_finished_video_reference() -> None:
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        case = _create_case(client, "Case With Finished Video")
        # A finished video referencing the case must block deletion. Persist it
        # directly into Postgres (the SQL delete-guard reads ``finished_videos``).
        video_ref = c.ArtifactRef(
            artifact_id=new_id("art"),
            kind=c.ArtifactKind.video_final,
            uri="sandbox://final.mp4",
        )
        with app.state.sqlalchemy_session_factory() as session:
            session.add(
                FinishedVideoRow(
                    id=new_id("fv"),
                    case_id=case["id"],
                    run_id=None,
                    owner_user_id="usr_admin",
                    title="Finished reference",
                    video_artifact=video_ref.model_dump(mode="json"),
                    duration_sec=0,
                    qc_status="passed",
                )
            )
            session.commit()

        rejected = client.delete(f"/api/cases/{case['id']}")
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "validation.conflict"
