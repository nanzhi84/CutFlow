from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c


class RecordingRuntimeAdapter:
    def __init__(self) -> None:
        self.started_run_id: str | None = None

    def start_run(self, *, job: c.Job, run: c.WorkflowRun, template: c.WorkflowTemplate) -> None:
        self.started_run_id = run.id

    def cancel_run(
        self, run_id: str, *, force: bool = False, reason: str | None = None
    ) -> c.WorkflowRun | None:
        return None

    def resume_run(
        self, *, source_run_id: str, new_run: c.WorkflowRun, reuse_plan
    ) -> None:
        self.started_run_id = new_run.id


def test_runtime_settings_default_to_local(monkeypatch):
    monkeypatch.delenv("CUTAGENT_WORKFLOW_RUNTIME", raising=False)
    monkeypatch.delenv("CUTAGENT_TEMPORAL_ADDRESS", raising=False)
    monkeypatch.delenv("CUTAGENT_TEMPORAL_NAMESPACE", raising=False)
    monkeypatch.delenv("CUTAGENT_TEMPORAL_TASK_QUEUE", raising=False)

    from packages.core.workflow import load_workflow_runtime_settings

    settings = load_workflow_runtime_settings()

    assert settings.runtime == "local"
    assert settings.temporal_address == "127.0.0.1:7233"
    assert settings.temporal_namespace == "default"
    assert settings.temporal_task_queue == "cutagent-production"


def test_api_job_creation_does_not_assume_adapter_reaches_terminal_state():
    app = create_app()
    adapter = RecordingRuntimeAdapter()
    app.state.workflow = adapter
    client = TestClient(app)

    login = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert login.status_code == 200, login.text

    response = client.post(
        "/api/jobs/digital-human-video",
        json={
            "case_id": "case_demo",
            "title": "Async submit contract",
            "script": "提交后运行时可以稍后完成。",
            "voice": {"voice_id": "voice_sandbox"},
            "strictness": {"strict_timestamps": False},
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["initial_run"]["status"] == "admitted"
    assert body["job"]["status"] == "queued"
    assert adapter.started_run_id == body["initial_run"]["id"]
