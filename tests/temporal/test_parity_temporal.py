from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import os
from typing import Any

import pytest

RUN_TEMPORAL_TESTS = os.getenv("CUTAGENT_RUN_TEMPORAL_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_TEMPORAL_TESTS,
    reason="Set CUTAGENT_RUN_TEMPORAL_TESTS=1 to run Temporal parity tests.",
)

if RUN_TEMPORAL_TESTS:
    from fastapi.testclient import TestClient

    from apps.api.app import create_app
    from packages.production.pipeline.node_sequence import NODE_SEQUENCE
    from tests.temporal.test_temporal_runtime import (
        WorkerThread,
        _login,
        _payload,
        _session_factory,
        _wait_for_status,
    )
    from tests.golden._caption_font_fixture import register_default_caption_fonts


@contextmanager
def _env(**values: str):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _normalize_run_detail(body: dict[str, Any]) -> dict[str, Any]:
    node_status_by_id = {node["node_id"]: node["status"] for node in body["node_runs"]}
    node_status_sequence = [
        (node_id, node_status_by_id[node_id])
        for node_id in NODE_SEQUENCE
        if node_id in node_status_by_id
    ]
    assert [node_id for node_id, _ in node_status_sequence] == NODE_SEQUENCE
    return {
        "run_status": body["run"]["status"],
        "node_status_sequence": node_status_sequence,
        "artifact_counts": sorted(
            Counter(artifact["kind"] for artifact in body["artifacts"]).items()
        ),
    }


def _run_local_request(payload: dict[str, Any]) -> dict[str, Any]:
    # Both arms run on the real SQLAlchemy backend (the in-memory backend was
    # removed); this still guards local-runtime vs Temporal-runtime parity.
    with _env(CUTAGENT_WORKFLOW_RUNTIME="local", CUTAGENT_STORAGE_BACKEND="sqlalchemy"):
        local_app = create_app()
        with TestClient(local_app) as client:
            register_default_caption_fonts(local_app)
            _login(client)
            created = client.post("/api/jobs/digital-human-video", json=payload)
            assert created.status_code == 201, created.text
            run = created.json()["initial_run"]
            assert run["status"] in {"succeeded", "degraded"}
            detail = client.get(f"/api/runs/{run['id']}")
            assert detail.status_code == 200, detail.text
            return _normalize_run_detail(detail.json())


def _run_temporal_request(payload: dict[str, Any]) -> dict[str, Any]:
    session_factory = _session_factory()
    with _env(CUTAGENT_WORKFLOW_RUNTIME="temporal", CUTAGENT_STORAGE_BACKEND="sqlalchemy"):
        with WorkerThread(), TestClient(create_app()) as client:
            _login(client)
            created = client.post("/api/jobs/digital-human-video", json=payload)
            assert created.status_code == 201, created.text
            run_id = created.json()["initial_run"]["id"]
            assert created.json()["initial_run"]["status"] == "admitted"
            assert _wait_for_status(session_factory, run_id, {"succeeded", "degraded"}) in {
                "succeeded",
                "degraded",
            }
            detail = client.get(f"/api/runs/{run_id}")
            assert detail.status_code == 200, detail.text
            return _normalize_run_detail(detail.json())


def test_local_runtime_and_temporal_sql_have_matching_node_and_artifact_semantics():
    payload = _payload("Temporal parity guard")

    local = _run_local_request(payload)
    temporal = _run_temporal_request(payload)

    assert temporal == local
