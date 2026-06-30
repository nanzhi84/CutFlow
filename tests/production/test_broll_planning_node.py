from __future__ import annotations

import pytest

from packages.core.contracts import (
    AnnotationMetaV4,
    AnnotationV4,
    Artifact,
    ArtifactKind,
    ClipRetrievalV4,
    ClipSemanticsV4,
    ClipUsageV4,
    ClipV4,
    DigitalHumanVideoRequest,
    NodeRun,
    NodeStatus,
    RunStatus,
    UsageRole,
    WorkflowRun,
)
from packages.core.contracts.media import AnnotationEditorVm, MediaAssetRecord
from packages.core.storage.repository import Repository
from packages.planning.material.broll_plan import BrollInsertion
from packages.production.pipeline import nodes
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter


def _artifact(kind: ArtifactKind, payload: dict) -> Artifact:
    return Artifact(
        id=f"art_{kind.value.replace('.', '_')}",
        case_id="case_demo",
        run_id="run_broll",
        node_run_id="nr_input",
        kind=kind,
        payload=payload,
        payload_schema=f"{kind.value}.v1",
    )


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_broll",
        job_id="job_broll",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_broll",
        run_id="run_broll",
        node_id="BrollPlanning",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def test_broll_planning_outputs_clip_id_on_overlays(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = Repository()
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
            broll={"enabled": True, "max_inserts": 1},
        ),
        artifacts={
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack,
                {"broll_candidates": [{"asset_id": "asset_broll_demo"}]},
            ),
            ArtifactKind.narration_units: _artifact(
                ArtifactKind.narration_units,
                {
                    "units": [
                        {
                            "unit_id": "unit_1",
                            "text": "hello",
                            "start": 0.0,
                            "end": 3.0,
                            "confidence": 0.9,
                        }
                    ]
                },
            ),
        },
    )
    insertion = BrollInsertion(
        asset_id="asset_broll_demo",
        clip_id="cover_a",
        timeline_start=0.0,
        timeline_end=2.0,
        source_start=1.0,
        source_end=3.0,
        confidence=0.8,
        matched_keywords=("hello",),
        scene_name="demo",
        reason="matched",
        diversity_key="scene:demo",
    )
    monkeypatch.setattr(nodes.broll_planning, "rank_broll_candidates", lambda **_: [])
    monkeypatch.setattr(nodes.broll_planning, "plan_insertions", lambda **_: [insertion])

    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    output = nodes.broll_planning.run(ctx)
    payload = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_broll
    )

    # overlays is the single canonical structure; segments is no longer written.
    assert "segments" not in payload
    assert payload["overlays"][0]["clip_id"] == "cover_a"
    assert payload["overlays"][0]["overlay_id"] == "broll_1"
    assert payload["overlays"][0]["timeline_start"] == 0.0
    assert payload["overlays"][0]["timeline_end"] == 2.0


def _state_with_clean_unrelated_clip(*, allow_generic_coverage: bool):
    """A digital_human_v2 run whose only b-roll asset is a person-free clean clip
    that shares NO keyword with the narration — usable only via generic coverage."""
    asset = MediaAssetRecord(id="asset_clean", case_id="case_demo", title="clean", kind="video")
    annotation = AnnotationV4(
        meta=AnnotationMetaV4(
            asset_id="asset_clean", case_id="case_demo", material_type="video", duration=12.0
        ),
        clips=[
            ClipV4(
                segment_id="cover_scene",
                start=0.0,
                end=4.0,
                duration=4.0,
                semantics=ClipSemanticsV4(scene_type="场景", subject_type="interior_room"),
                usage=ClipUsageV4(role=UsageRole.cover, recommended_for_lip_sync=False),
                retrieval=ClipRetrievalV4(
                    summary="窗外 绿植", keywords=["窗外", "绿植"], retrieval_sentence="窗外 绿植"
                ),
                confidence=0.9,
            )
        ],
        quality_report={"usable_ratio": 0.9},
    )
    repository = Repository()
    repository.annotations["asset_clean"] = AnnotationEditorVm(
        asset=asset, etag="etag1", canonical=annotation, projection={}
    )
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="今天聊聊我们的服务理念。",
            voice={"voice_id": "voice_sandbox"},
            broll={
                "enabled": True,
                "max_inserts": 2,
                "allow_generic_coverage": allow_generic_coverage,
            },
        ),
        artifacts={
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack,
                {"broll_candidates": [{"asset_id": "asset_clean"}]},
            ),
            ArtifactKind.narration_units: _artifact(
                ArtifactKind.narration_units,
                {
                    "units": [
                        {
                            "unit_id": "u1",
                            "text": "今天聊聊我们的服务理念。",
                            "start": 0.0,
                            "end": 4.0,
                            "confidence": 0.9,
                        }
                    ]
                },
            ),
        },
    )
    return adapter, state


def _broll_payload(output):
    return next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_broll)


def test_generic_coverage_fills_broll_on_default_template_when_enabled():
    # End-to-end through BOTH gates (material-pack ranking already done upstream +
    # BrollPlanning's re-rank): a clean clip with zero keyword overlap must surface
    # as a real b-roll overlay when allow_generic_coverage is on (the default).
    adapter, state = _state_with_clean_unrelated_clip(allow_generic_coverage=True)
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    output = nodes.broll_planning.run(ctx)
    payload = _broll_payload(output)
    assert payload["overlays"], "clean no-keyword clip should fill b-roll via generic coverage"
    assert payload["overlays"][0]["asset_id"] == "asset_clean"
    assert output.status != NodeStatus.degraded


def test_generic_coverage_off_reverts_to_soft_degrade():
    # With the knob off, the same unrelated clean clip is honestly dropped (the
    # node soft-degrades) — proving the behaviour is opt-out, not hardcoded.
    adapter, state = _state_with_clean_unrelated_clip(allow_generic_coverage=False)
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    output = nodes.broll_planning.run(ctx)
    payload = _broll_payload(output)
    assert payload["overlays"] == []
    assert output.status == NodeStatus.degraded


def test_broll_planning_never_reads_selection_ledger(monkeypatch: pytest.MonkeyPatch):
    # The selection ledger is read once, in MaterialPackPlanning. BrollPlanning must
    # re-rank against the real narration WITHOUT touching the ledger, while still
    # producing real narration-anchored overlays.
    adapter, state = _state_with_clean_unrelated_clip(allow_generic_coverage=True)
    # Even with prior broll history for this case on the ledger, the node must not read
    # it (recency now arrives via the material pack metadata).
    from packages.core.contracts import SelectionLedgerEntry

    adapter.repository.record_selection_ledger_entries(
        [
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_prev",
                medium="broll",
                asset_id="asset_clean",
                slot_phase="broll_1",
            )
        ]
    )
    ledger_calls: list = []
    real_recent_selections = adapter.repository.recent_selections

    def _spy_recent_selections(*args, **kwargs):
        ledger_calls.append((args, kwargs))
        return real_recent_selections(*args, **kwargs)

    monkeypatch.setattr(adapter.repository, "recent_selections", _spy_recent_selections)

    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    output = nodes.broll_planning.run(ctx)
    payload = _broll_payload(output)

    assert ledger_calls == []
    assert payload["overlays"], "still selects a narration-anchored insert without the ledger"
    assert payload["overlays"][0]["asset_id"] == "asset_clean"
