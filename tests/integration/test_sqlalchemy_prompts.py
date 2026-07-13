from uuid import uuid4

from fastapi.testclient import TestClient


from apps.api.main import app
from packages.ai.prompts import PromptRegistry
from packages.ai.prompts.sqlalchemy_repository import SqlAlchemyPromptRuntimeRepository
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory
from packages.core.storage.database import (
    PromptBindingRow,
    PromptExperimentRow,
    PromptTemplateRow,
    PromptVersionRow,
)
from packages.core.storage.repository import Repository


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory()
    return session_factory


def test_sqlalchemy_prompt_template_version_and_binding_flow_is_persisted():
    session_factory = sqlalchemy_session_factory()
    suffix = uuid4().hex[:8]

    with TestClient(app) as client:
        viewer_login = client.post(
            "/api/auth/login",
            json={"email": "viewer@local.cutagent", "password": "local-viewer"},
        )
        assert viewer_login.status_code == 200, viewer_login.text
        forbidden = client.post(
            "/api/prompts",
            json={
                "name": f"Forbidden Prompt {suffix}",
                "purpose": "integration_test",
                "variables_schema_ref": {"schema_id": "integration.variables", "schema_version": "v1"},
                "output_schema_ref": {"schema_id": "integration.output", "schema_version": "v1"},
            },
        )
        assert forbidden.status_code == 403

        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        created = client.post(
            "/api/prompts",
            json={
                "name": f"Integration Prompt {suffix}",
                "purpose": "integration_test",
                "variables_schema_ref": {"schema_id": "integration.variables", "schema_version": "v1"},
                "output_schema_ref": {"schema_id": "integration.output", "schema_version": "v1"},
            },
        )
        assert created.status_code == 201, created.text
        template = created.json()["template"]
        assert template["status"] == "draft"

        version_response = client.post(
            f"/api/prompts/{template['id']}/versions",
            json={"content": "Write a concise script for {topic}.", "changelog": "Initial draft"},
        )
        assert version_response.status_code == 201, version_response.text
        version = version_response.json()["version"]
        assert version["status"] == "draft"

        challenger_response = client.post(
            f"/api/prompts/{template['id']}/versions",
            json={"content": "Open with a sharper contrast before {{topic}}.", "changelog": "Experiment variant"},
        )
        assert challenger_response.status_code == 201, challenger_response.text
        challenger = challenger_response.json()["version"]

        approved = client.post(
            f"/api/prompts/{template['id']}/versions/{version['id']}/approve",
            json={"reason": "Reviewed in integration test"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["version"]["status"] == "approved"

        published = client.post(
            f"/api/prompts/{template['id']}/versions/{version['id']}/publish",
            json={"reason": "Publish for integration test"},
        )
        assert published.status_code == 200, published.text
        assert published.json()["version"]["status"] == "published"
        assert published.json()["template"]["status"] == "active"

        listed = client.get("/api/prompts")
        assert listed.status_code == 200, listed.text
        prompt_view = next(item for item in listed.json()["items"] if item["template"]["id"] == template["id"])
        assert prompt_view["published_version"]["id"] == version["id"]
        seeded_view = next(
            item
            for item in listed.json()["items"]
            if item["template"]["id"] == "prompt_script_ip_persona_fresh_generate"
        )
        assert "ip_persona" in seeded_view["variable_hints"]
        assert "duration" in seeded_view["variable_hints"]

        filtered_templates = client.get(
            "/api/prompts",
            params={"status": "active", "purpose": "integration_test"},
        )
        assert filtered_templates.status_code == 200, filtered_templates.text
        assert any(item["template"]["id"] == template["id"] for item in filtered_templates.json()["items"])
        assert all(
            item["template"]["status"] == "active" and item["template"]["purpose"] == "integration_test"
            for item in filtered_templates.json()["items"]
        )

        versions = client.get(f"/api/prompts/{template['id']}/versions")
        assert versions.status_code == 200, versions.text
        assert any(item["version"]["id"] == version["id"] for item in versions.json()["items"])

        binding_response = client.post(
            "/api/prompts/bindings",
            json={
                "prompt_template_id": template["id"],
                "prompt_version_id": version["id"],
                "case_id": "case_demo",
                "node_id": "write_script",
                "priority": 10,
            },
        )
        assert binding_response.status_code == 201, binding_response.text
        binding = binding_response.json()["binding"]
        assert binding_response.json()["resolved_version"]["id"] == version["id"]

        runtime_registry = PromptRegistry(
            Repository(),
            prompt_reader=SqlAlchemyPromptRuntimeRepository(session_factory),
        )
        invocation, rendered = runtime_registry.render(
            node_id="write_script",
            variables={"topic": "single source DB prompt"},
            case_id="case_demo",
        )
        assert invocation.prompt_version_id == version["id"]
        assert rendered == "Write a concise script for single source DB prompt."

        patched_binding = client.patch(
            f"/api/prompts/bindings/{binding['id']}",
            json={"enabled": False, "priority": 50},
        )
        assert patched_binding.status_code == 200, patched_binding.text
        assert patched_binding.json()["binding"]["enabled"] is False
        assert patched_binding.json()["binding"]["priority"] == 50

        created_experiment = client.post(
            "/api/prompts/experiments",
            json={
                "prompt_template_id": template["id"],
                "variants": [version["id"], challenger["id"]],
                "traffic_split": {version["id"]: 0.5, challenger["id"]: 0.5},
                "scope": {"case_id": "case_demo", "node_id": "write_script"},
            },
        )
        assert created_experiment.status_code == 201, created_experiment.text
        experiment = created_experiment.json()
        assert experiment["status"] == "draft"

        patched_experiment = client.patch(
            f"/api/prompts/experiments/{experiment['id']}",
            json={"status": "running", "traffic_split": {version["id"]: 0.75, challenger["id"]: 0.25}},
        )
        assert patched_experiment.status_code == 200, patched_experiment.text
        assert patched_experiment.json()["status"] == "running"
        assert patched_experiment.json()["traffic_split"][version["id"]] == 0.75

        listed_experiments = client.get(
            "/api/prompts/experiments",
            params={"prompt_template_id": template["id"], "status": "running"},
        )
        assert listed_experiments.status_code == 200, listed_experiments.text
        assert any(item["id"] == experiment["id"] for item in listed_experiments.json()["items"])

    with session_factory() as session:
        template_row = session.get(PromptTemplateRow, template["id"])
        version_row = session.get(PromptVersionRow, version["id"])
        binding_row = session.get(PromptBindingRow, binding["id"])
        experiment_row = session.get(PromptExperimentRow, experiment["id"])
        assert template_row is not None
        assert template_row.status == "active"
        assert version_row is not None
        assert version_row.status == "published"
        assert version_row.published_at is not None
        assert binding_row is not None
        assert binding_row.enabled is False
        assert binding_row.priority == 50
        assert experiment_row is not None
        assert experiment_row.status == "running"
        assert experiment_row.traffic_split[version["id"]] == 0.75
