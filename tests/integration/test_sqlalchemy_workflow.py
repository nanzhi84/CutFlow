
from fastapi.testclient import TestClient
from sqlalchemy import select


from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory
from packages.core.storage.database import (
    ArtifactRow,
    JobRow,
    NodeRunRow,
    OutboxEventRow,
    PromptInvocationRow,
    ProviderInvocationRow,
    PublishPackageRow,
    ScriptVersionRow,
    UsageMeterRecordRow,
    VideoVersionRow,
    WorkflowRunRow,
)


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory()
    return session_factory


def test_sqlalchemy_workflow_job_run_report_and_artifacts_are_persisted():
    session_factory = sqlalchemy_session_factory()

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        created = client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "title": "SQLAlchemy workflow video",
                "script": "用一个简短脚本验证数据库工作流持久化。",
                "publish_content": "Database workflow handoff.",
                "voice": {"voice_id": "voice_sandbox"},
                "strictness": {"strict_timestamps": False},
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        job_id = body["job"]["id"]
        run_id = body["initial_run"]["id"]
        assert body["initial_run"]["status"] in {"succeeded", "degraded"}

        job_detail = client.get(f"/api/jobs/{job_id}")
        assert job_detail.status_code == 200, job_detail.text
        assert job_detail.json()["job"]["active_run_id"] == run_id
        assert job_detail.json()["latest_report_artifact_id"]

        run_detail = client.get(f"/api/runs/{run_id}")
        assert run_detail.status_code == 200, run_detail.text
        run_body = run_detail.json()
        assert run_body["run"]["id"] == run_id
        assert len(run_body["node_runs"]) >= 10
        assert run_body["artifacts"]

        report = client.get(f"/api/runs/{run_id}/report")
        assert report.status_code == 200, report.text
        assert report.json()["public_report"]["run_id"] == run_id

        artifacts = client.get(f"/api/runs/{run_id}/artifacts")
        assert artifacts.status_code == 200, artifacts.text
        assert len(artifacts.json()["artifacts"]) == len(run_body["artifacts"])

        events = client.get(f"/api/runs/{run_id}/events")
        assert events.status_code == 200, events.text
        assert events.json()["stream_url"] == f"/ws/runs/{run_id}"

        finished = client.get("/api/cases/case_demo/finished-videos")
        assert finished.status_code == 200, finished.text
        assert any(item["run_id"] == run_id for item in finished.json()["items"])

        packages = client.get("/api/publish/packages")
        assert packages.status_code == 200, packages.text
        assert any(item["source_finished_video_id"] for item in packages.json()["items"])

    with session_factory() as session:
        assert session.get(JobRow, job_id) is not None
        assert session.get(WorkflowRunRow, run_id) is not None
        node_runs = list(session.scalars(select(NodeRunRow).where(NodeRunRow.run_id == run_id)))
        artifact_rows = list(session.scalars(select(ArtifactRow).where(ArtifactRow.run_id == run_id)))
        provider_rows = list(
            session.scalars(select(ProviderInvocationRow).where(ProviderInvocationRow.run_id == run_id))
        )
        provider_ids = {row.id for row in provider_rows}
        usage_rows = list(
            session.scalars(
                select(UsageMeterRecordRow).where(
                    UsageMeterRecordRow.provider_invocation_id.in_(provider_ids)
                )
            )
        )
        prompt_rows = list(
            session.scalars(select(PromptInvocationRow).where(PromptInvocationRow.run_id == run_id))
        )
        outbox_row = session.scalar(
            select(OutboxEventRow)
            .where(OutboxEventRow.topic == "workflow.run.updated")
            .where(OutboxEventRow.aggregate_id == run_id)
        )
        package_rows = list(session.scalars(select(PublishPackageRow)))
        assert len(node_runs) >= 10
        assert artifact_rows
        assert provider_rows
        assert usage_rows
        assert {row.provider_invocation_id for row in usage_rows}.issubset(provider_ids)
        assert prompt_rows
        assert any(row.provider_invocation_id in provider_ids for row in prompt_rows)
        assert outbox_row is not None
        assert outbox_row.status in {"pending", "published"}
        assert outbox_row.payload["run_id"] == run_id
        assert any(row.source_finished_video_id for row in package_rows)


def test_sqlalchemy_job_links_adopted_script_version_not_orphaned():
    session_factory = sqlalchemy_session_factory()

    # Seed an adopted ScriptVersion (as the Case Agent draft-adoption flow would).
    script_id = "script_link_test"
    with session_factory() as session:
        session.merge(
            ScriptVersionRow(
                id=script_id,
                case_id="case_demo",
                title="Adopted draft title",
                script="Adopted draft body.",
                adopted_from_draft_id="draft_link_test",
            )
        )
        session.commit()

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        created = client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "title": "Script-linked workflow video",
                "script": "携带 script_version_id 的请求脚本。",
                "script_version_id": script_id,
                "voice": {"voice_id": "voice_sandbox"},
                "strictness": {"strict_timestamps": False},
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        job_id = body["job"]["id"]

        # The job row persists the link (inside the request payload) and GET surfaces it.
        assert body["job"]["request"]["script_version_id"] == script_id
        detail = client.get(f"/api/jobs/{job_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["job"]["request"]["script_version_id"] == script_id

    with session_factory() as session:
        # The adopted ScriptVersion is preserved (provenance intact), not overwritten.
        script_row = session.get(ScriptVersionRow, script_id)
        assert script_row is not None
        assert script_row.adopted_from_draft_id == "draft_link_test"
        assert script_row.title == "Adopted draft title"

        # A VideoVersion links back to the adopted ScriptVersion id (no orphan).
        version_rows = list(
            session.scalars(
                select(VideoVersionRow).where(VideoVersionRow.script_version_id == script_id)
            )
        )
        assert version_rows, "expected a VideoVersion linked to the adopted ScriptVersion"


def test_hydrate_workflow_runtime_snapshot_loads_adopted_script():
    """Temporal-path regression guard for the adopted-script provenance fix.

    Under the Temporal runtime every run_node activity builds a FRESH Repository
    and populates it ONLY through ``hydrate_workflow_runtime_snapshot`` (see
    packages/core/workflow/temporal_adapter.py). If that snapshot does not load
    the adopted ScriptVersion, the export node mints a fresh row and overwrites
    ``adopted_from_draft_id`` — the exact orphaning this fix prevents. This test
    drives the snapshot hydration directly (no in-process API-state reuse) and
    asserts the adopted ScriptVersion lands in the worker's runtime repo intact.
    """
    from packages.core.storage import Repository
    from packages.production import SqlAlchemyProductionRepository

    session_factory = sqlalchemy_session_factory()

    script_id = "script_hydrate_test"
    with session_factory() as session:
        session.merge(
            ScriptVersionRow(
                id=script_id,
                case_id="case_demo",
                title="Hydrate adopted title",
                script="Hydrate adopted body.",
                adopted_from_draft_id="draft_hydrate_test",
            )
        )
        session.commit()

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text
        created = client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "title": "Snapshot-hydrate workflow video",
                "script": "携带 script_version_id 的请求脚本（快照水合）。",
                "script_version_id": script_id,
                "voice": {"voice_id": "voice_sandbox"},
                "strictness": {"strict_timestamps": False},
            },
        )
        assert created.status_code == 201, created.text
        run_id = created.json()["initial_run"]["id"]

    # Simulate the Temporal worker activity: a brand-new runtime Repository
    # populated solely by the snapshot hydrate must already carry the adopted
    # ScriptVersion (with provenance), so _resolve_script_version reuses it.
    fresh = Repository()
    SqlAlchemyProductionRepository(session_factory).hydrate_workflow_runtime_snapshot(fresh, run_id)
    assert script_id in fresh.scripts, "snapshot hydrate did not load the adopted ScriptVersion"
    assert fresh.scripts[script_id].adopted_from_draft_id == "draft_hydrate_test"
    assert fresh.scripts[script_id].title == "Hydrate adopted title"
