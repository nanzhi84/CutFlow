from __future__ import annotations

from packages.core.contracts import ArtifactKind, NodeRun, NodeStatus
from packages.production.pipeline import nodes
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._editing_agent import build_agent_input, index_candidates
from tests.production import test_portrait_planning_node as portrait_test


def _node_run(node_id: str) -> NodeRun:
    return NodeRun(
        id=f"nr_{node_id.lower()}",
        run_id="run_1",
        node_id=node_id,
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _timeline_output(adapter, state):
    ctx = NodeContext(
        adapter=adapter,
        run=portrait_test._run(),
        node_run=_node_run("TimelineWindowPlanning"),
        state=state,
    )
    return nodes.timeline_window_planning.run(ctx)


def _payload(output, kind: ArtifactKind) -> dict:
    return next(artifact.payload for artifact in output.artifacts if artifact.kind == kind)


def _agent_boundary_from_windows(payload: dict) -> dict:
    return {
        "safe_cut_boundaries": [],
        "portrait_slots": [
            {
                "slot_id": window["window_id"],
                "start_frame": window["start_frame"],
                "end_frame": window["end_frame"],
                "unit_ids": list(window.get("unit_ids") or []),
                "boundary_source": window.get("boundary_source"),
            }
            for window in payload["portrait_windows"]
        ],
        "broll_slots": [],
    }


def test_timeline_windows_have_no_concrete_frames_invented(monkeypatch, tmp_path):
    object_store = portrait_test.LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = portrait_test._adapter(object_store)
    state = portrait_test._state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
    )

    payload = _payload(_timeline_output(adapter, state), ArtifactKind.plan_timeline_windows)

    segment_frames = [
        (seg["timeline_start_frame"], seg["timeline_end_frame"])
        for seg in payload["default_assignment"]["portrait_plan_payload"]["segments"]
    ]
    window_frames = [
        (window["start_frame"], window["end_frame"])
        for window in payload["portrait_windows"]
    ]
    assignment_frames = [
        (
            item["segment_payload"]["timeline_start_frame"],
            item["segment_payload"]["timeline_end_frame"],
        )
        for item in payload["default_assignment"]["portrait"]
    ]
    assert window_frames == segment_frames
    assert assignment_frames == segment_frames
    assert all(
        item["window_id"].startswith(str(item["segment_payload"]["asset_id"]))
        for item in payload["default_assignment"]["portrait"]
    )
    assert all(
        item["window_id"] != window["window_id"]
        for item, window in zip(
            payload["default_assignment"]["portrait"], payload["portrait_windows"]
        )
    )


def test_v2_portrait_plan_identical_through_windows_split(monkeypatch, tmp_path):
    object_store = portrait_test.LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = portrait_test._adapter(object_store)
    state = portrait_test._state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
    )
    timeline_output = _timeline_output(adapter, state)
    windows_payload = _payload(timeline_output, ArtifactKind.plan_timeline_windows)
    for artifact in timeline_output.artifacts:
        state.artifacts[artifact.kind] = artifact

    portrait_ctx = NodeContext(
        adapter=adapter,
        run=portrait_test._run(),
        node_run=_node_run("PortraitPlanning"),
        state=state,
    )
    portrait_output = nodes.portrait_planning.run(portrait_ctx)
    portrait_payload = _payload(portrait_output, ArtifactKind.plan_portrait)

    assert portrait_payload == windows_payload["default_assignment"]["portrait_plan_payload"]


def test_agent_windows_always_feasible(monkeypatch, tmp_path):
    object_store = portrait_test.LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = portrait_test._adapter(object_store)
    state = portrait_test._state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
    )
    payload = _payload(_timeline_output(adapter, state), ArtifactKind.plan_timeline_windows)
    material = state.artifacts[ArtifactKind.plan_material_pack].payload

    agent_input = build_agent_input(
        request=state.request,
        boundary=_agent_boundary_from_windows(payload),
        candidates=index_candidates(material),
        narration_units=state.artifacts[ArtifactKind.narration_units].payload["units"],
        duration=payload["total_frames"] / payload["fps"],
    )

    assert agent_input["portrait_slots"]
    assert all(slot["legal_window_ids"] for slot in agent_input["portrait_slots"])
