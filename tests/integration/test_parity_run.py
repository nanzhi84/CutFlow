from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import os
from typing import Any

import pytest

RUN_DB_TESTS = os.getenv("CUTAGENT_RUN_DB_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_DB_TESTS,
    reason="Set CUTAGENT_RUN_DB_TESTS=1 to run backend parity integration tests.",
)

if RUN_DB_TESTS:
    from fastapi.testclient import TestClient

    from apps.api.app import create_app
    from packages.production.pipeline.node_sequence import NODE_SEQUENCE


_NONDETERMINISTIC_KEYS = {
    "id",
    "artifact_id",
    "job_id",
    "run_id",
    "node_run_id",
    "request_id",
    "uri",
    "local_path",
    "oss_uri",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "published_at",
    "sha256",
    "duration_ms",
}


@contextmanager
def _storage_backend(name: str):
    previous = os.environ.get("CUTAGENT_STORAGE_BACKEND")
    os.environ["CUTAGENT_STORAGE_BACKEND"] = name
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("CUTAGENT_STORAGE_BACKEND", None)
        else:
            os.environ["CUTAGENT_STORAGE_BACKEND"] = previous


def _login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _fixed_request() -> dict[str, Any]:
    return {
        "case_id": "case_demo",
        "title": "Backend parity guard",
        "script": "先说明内容生产低效，再展示历史经验复用，最后邀请运营查看报告。",
        "publish_content": "Backend parity guard handoff.",
        "voice": {"voice_id": "voice_sandbox"},
        "portrait": {"template_mode": "agent"},
        "broll": {"enabled": False, "max_inserts": 1},
        "bgm": {"enabled": False},
        "subtitle": {"enabled": True},
        "lipsync": {"enabled": True, "provider_profile_id": "runninghub.heygem.default"},
        "strictness": {"strict_timestamps": False},
    }


def _strip_nondeterministic(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_nondeterministic(item)
            for key, item in value.items()
            if key not in _NONDETERMINISTIC_KEYS
        }
    if isinstance(value, list):
        return [_strip_nondeterministic(item) for item in value]
    return value


def _collect_usage_roles(value: Any) -> set[str]:
    roles: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "role" and isinstance(item, str):
                roles.add(item)
            roles.update(_collect_usage_roles(item))
    elif isinstance(value, list):
        for item in value:
            roles.update(_collect_usage_roles(item))
    return roles


def _normalize_run(client: TestClient, run_id: str) -> dict[str, Any]:
    report_response = client.get(f"/api/runs/{run_id}/report")
    assert report_response.status_code == 200, report_response.text
    report = _strip_nondeterministic(report_response.json())

    detail_response = client.get(f"/api/runs/{run_id}")
    assert detail_response.status_code == 200, detail_response.text
    detail = _strip_nondeterministic(detail_response.json())

    node_status_by_id = {
        node["node_id"]: node["status"]
        for node in detail["node_runs"]
    }
    node_status_sequence = [
        (node_id, node_status_by_id[node_id])
        for node_id in NODE_SEQUENCE
        if node_id in node_status_by_id
    ]
    assert [node_id for node_id, _ in node_status_sequence] == NODE_SEQUENCE

    artifact_counts = sorted(
        Counter(artifact["kind"] for artifact in detail["artifacts"]).items()
    )
    degradation_codes = set(report["public_report"]["degradations"])
    for node in detail["node_runs"]:
        degradation_codes.update(item["code"] for item in node.get("degradations", []))

    return {
        "report_status": report["public_report"]["status"],
        "node_status_sequence": node_status_sequence,
        "artifact_counts": artifact_counts,
        "degradation_codes": sorted(degradation_codes),
        "usage_roles": sorted(_collect_usage_roles(detail.get("artifact_payloads", {}))),
    }


def _run_video(backend: str) -> dict[str, Any]:
    with _storage_backend(backend), TestClient(create_app()) as client:
        _login_admin(client)
        created = client.post("/api/jobs/digital-human-video", json=_fixed_request())
        assert created.status_code == 201, created.text
        run = created.json()["initial_run"]
        assert run["status"] in {"succeeded", "degraded"}
        return _normalize_run(client, run["id"])


def test_memory_and_sqlalchemy_backends_have_matching_run_semantics():
    memory = _run_video("memory")
    sqlalchemy = _run_video("sqlalchemy")

    assert sqlalchemy == memory
