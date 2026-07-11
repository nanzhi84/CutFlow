"""TimelineWindowPlanning publishes authoritative windows and a nested default plan.

These tests prove the node consumes narration units + material portrait candidates +
detected audio pauses and emits compiled windows containing a real frame-contiguous
default portrait plan — no seeded / placeholder timeline and no competing final
``plan.portrait`` artifact — and hard-fails honestly when material is insufficient.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.ai.gateway import ProviderGateway
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline._editing_agent import build_agent_input, index_candidates
from packages.production.pipeline._materialize import (
    materialize_full_coverage_broll_from_assignment,
)
from packages.production.pipeline import nodes
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter


SCRIPT = "先讲解打磨工艺的细节非常重要。再展示补漆效果对比清晰可见。最后欢迎点击咨询预约下单。"


def _adapter(object_store: LocalObjectStore) -> LocalRuntimeAdapter:
    repository = Repository()
    return LocalRuntimeAdapter(
        repository,
        provider_gateway=ProviderGateway(repository, object_store=object_store),
        prompt_registry=PromptRegistry(repository),
    )


def _units(duration: float = 12.0) -> list[dict]:
    parts = [p for p in SCRIPT.replace("！", "。").split("。") if p]
    step = duration / len(parts)
    units = []
    cursor = 0.0
    for index, text in enumerate(parts):
        end = duration if index == len(parts) - 1 else round(cursor + step, 3)
        units.append(
            {
                "unit_id": f"unit_{index + 1}",
                "text": text + "。",
                "start": round(cursor, 3),
                "end": end,
                "confidence": 0.8,
            }
        )
        cursor = end
    return units


def _back_portrait_sources(adapter: LocalRuntimeAdapter, asset_ids: list[str]) -> None:
    """Back each asset_id with the seeded 15s portrait source artifact.

    Only ``asset_portrait_demo`` is seeded with real demo media, but asset-level
    uniqueness (issue #102) means a multi-chunk timeline needs several DISTINCT
    portrait assets. Cloning the seeded media-asset record under new ids (sharing the
    same 15s source artifact) lets a test stand up N distinct portrait sources without
    generating N videos — every clone resolves through ``source_artifact_for_asset``.
    """
    base = adapter.repository.media_assets.get("asset_portrait_demo")
    if base is None:
        return
    for asset_id in asset_ids:
        if asset_id == "asset_portrait_demo":
            continue
        existing = adapter.repository.media_assets.get(asset_id)
        if existing is not None and existing.source_artifact_id:
            continue
        adapter.repository.media_assets[asset_id] = base.model_copy(
            update={"id": asset_id, "title": f"Demo portrait {asset_id}"}
        )


def _state(
    adapter: LocalRuntimeAdapter,
    *,
    candidate_ids: list[str],
    duration: float = 12.0,
    with_clip_metadata: bool = True,
    recent_usage: dict | None = None,
    source_window: tuple[float, float] = (0.0, 15.0),
    pause_windows: list[dict] | None = None,
    safe_cut_boundaries: list[dict] | None = None,
    broll_slots: list[dict] | None = None,
    broll_mode: str = "insert",
) -> RunState:
    _back_portrait_sources(adapter, candidate_ids)
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script=SCRIPT,
        voice={"voice_id": "voice_sandbox"},
        broll={"enabled": True, "mode": broll_mode},
        strictness={"strict_timestamps": False},
    )

    def _metadata(cid: str) -> dict:
        if not with_clip_metadata:
            return {}
        meta = {
            "clip_id": f"{cid}_talk",
            "source_start": source_window[0],
            "source_end": source_window[1],
        }
        if recent_usage is not None:
            # Mirror MaterialPackPlanning, which (as the single ledger reader) stamps the
            # recency context onto each portrait candidate's metadata.
            meta["recent_usage"] = recent_usage
        return meta

    material = {
        "portrait_candidates": [
            {
                "asset_id": cid,
                "score": 1.0,
                "metadata": _metadata(cid),
            }
            for cid in candidate_ids
        ],
        "broll_candidates": [
            {
                "asset_id": "broll_source",
                "score": 1.0,
                "metadata": {
                    "clip_id": "broll_source_clip",
                    "source_start": source_window[0],
                    "source_end": source_window[1],
                },
            }
        ],
    }
    narration = {"source": "estimated", "units": _units(duration), "strict": False}
    material_artifact = Artifact(
        id="art_material",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_material",
        kind=ArtifactKind.plan_material_pack,
        payload=material,
        payload_schema="MaterialPackArtifact.v1",
    )
    narration_artifact = Artifact(
        id="art_narration",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_narration",
        kind=ArtifactKind.narration_units,
        payload=narration,
        payload_schema="NarrationUnitsArtifact.v1",
    )
    # NarrationBoundaryPlanning runs upstream and hands TimelineWindowPlanning the
    # detected pauses; the compiler no longer detects them itself. Default is
    # semantic-only (no real pauses), matching the sandbox tone.
    boundary_artifact = Artifact(
        id="art_narration_boundary",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_narration_boundary",
        kind=ArtifactKind.plan_narration_boundary,
        payload={
            "pause_windows": pause_windows or [],
            "safe_cut_boundaries": safe_cut_boundaries or [],
            "broll_slots": broll_slots or [],
        },
        payload_schema="NarrationBoundaryPlan.v1",
    )
    return RunState(
        request=request,
        artifacts={
            ArtifactKind.plan_material_pack: material_artifact,
            ArtifactKind.narration_units: narration_artifact,
            ArtifactKind.plan_narration_boundary: boundary_artifact,
        },
    )


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_1",
        job_id="job_1",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run(node_id: str = "TimelineWindowPlanning") -> NodeRun:
    return NodeRun(
        id=f"nr_{node_id.lower()}",
        run_id="run_1",
        node_id=node_id,
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _run_node(adapter: LocalRuntimeAdapter, state: RunState):
    timeline_ctx = NodeContext(
        adapter=adapter,
        run=_run(),
        node_run=_node_run(),
        state=state,
    )
    return nodes.timeline_window_planning.run(timeline_ctx)


def _portrait_payload(output) -> dict:
    windows = _payload(output, ArtifactKind.plan_timeline_windows)
    return windows["default_assignment"]["portrait_plan_payload"]


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
        "broll_slots": [
            {
                "slot_id": window["window_id"],
                "start_frame": window["start_frame"],
                "end_frame": window["end_frame"],
                "length_frames": window["length_frames"],
                "source_length_frames": window.get("source_length_frames"),
                "pad_start": window.get("pad_start", 0.0),
                "pad_end": window.get("pad_end", 0.0),
                "unit_ids": list(window.get("host_unit_ids") or []),
                "boundary_source": window.get("boundary_source"),
                "text": window.get("text") or "",
            }
            for window in payload["broll_windows"]
        ],
    }


def test_timeline_windows_have_no_concrete_frames_invented(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
    )

    payload = _payload(_run_node(adapter, state), ArtifactKind.plan_timeline_windows)

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


def test_broll_windows_are_authoritative_optional_slots(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
        broll_slots=[
            {
                "start_frame": 75,
                "end_frame": 150,
                "unit_ids": ["unit_1"],
                "text": "展示施工细节。",
                "boundary_source": "narration_unit",
            }
        ],
    )

    payload = _payload(_run_node(adapter, state), ArtifactKind.plan_timeline_windows)

    [window] = payload["broll_windows"]
    assert window["window_id"] == "bwin_000"
    assert window["start_frame"] == 75
    assert window["end_frame"] == 150
    assert window["length_frames"] == 75
    assert window["host_unit_ids"] == ["unit_1"]
    assert payload["geometry_policy"]["broll_window_contract"] == {
        "authority": "TimelineWindowPlanning",
        "semantics": "authoritative_optional_placement_slot",
        "downstream_may_skip": True,
        "downstream_may_resize": False,
    }


def test_full_coverage_broll_windows_cover_entire_audio_and_skip_portrait(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state(
        adapter,
        candidate_ids=[],
        broll_mode="full_coverage",
        pause_windows=[{"start": 3.9, "end": 4.1, "duration": 0.2, "center": 4.0}],
        safe_cut_boundaries=[
            {"cut_id": "cut_004", "frame": 120, "source": "semantic_audio_pause"},
            {"cut_id": "cut_008", "frame": 240, "source": "semantic"},
        ],
    )

    output = _run_node(adapter, state)
    payload = _payload(output, ArtifactKind.plan_timeline_windows)
    portrait_payload = _portrait_payload(output)

    assert [artifact.kind for artifact in output.artifacts] == [
        ArtifactKind.plan_timeline_windows
    ]
    assert payload["portrait_windows"] == []
    assert portrait_payload["segments"] == []
    assert portrait_payload["diagnostics"]["track_mode"] == "broll_full_coverage"
    assert [
        (window["start_frame"], window["end_frame"])
        for window in payload["broll_windows"]
    ] == [(0, 120), (120, 240), (240, 360)]
    assert payload["broll_windows"][0] == {
        "window_id": "bwin_000",
        "start_frame": 0,
        "end_frame": 120,
        "length_frames": 120,
        "source_length_frames": 120,
        "host_unit_ids": ["unit_001"],
        "text": "先讲解打磨工艺的细节非常重要。",
        "text_assignment": "argmax_overlap",
        "scene_hint": "先讲解打磨工艺的细节非常重要。",
    }
    assert payload["geometry_policy"]["broll_window_contract"] == {
        "authority": "TimelineWindowPlanning",
        "semantics": "authoritative_full_coverage_main_visual_track",
        "downstream_may_skip": False,
        "downstream_may_resize": False,
        "downstream_may_stitch": False,
    }
    assert payload["compile_diagnostics"]["selected_cut_source_counts"]["audio_pause"] == 1
    assert payload["compile_diagnostics"]["selected_cut_source_counts"]["safe_cut"] == 1
    assert payload["compile_diagnostics"]["raw_pause_window_count"] == 1
    assert payload["compile_diagnostics"]["used_audio_pauses"] is True


def test_full_coverage_ignores_short_fragments_and_uses_safe_cut(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state(
        adapter,
        candidate_ids=[],
        duration=7.0,
        broll_mode="full_coverage",
        pause_windows=[{"start": 0.95, "end": 1.05, "duration": 0.1, "center": 1.0}],
        safe_cut_boundaries=[{"cut_id": "cut_004", "frame": 120, "source": "semantic"}],
    )

    payload = _payload(_run_node(adapter, state), ArtifactKind.plan_timeline_windows)

    assert [
        (window["start_frame"], window["end_frame"])
        for window in payload["broll_windows"]
    ] == [(0, 120), (120, 210)]
    assert 30 not in payload["compile_diagnostics"]["cut_frames"]
    assert payload["compile_diagnostics"]["used_audio_pauses"] is False


def test_full_coverage_splits_overlong_windows_with_deterministic_fallback(
    monkeypatch,
    tmp_path,
):
    from packages.production.pipeline.nodes import timeline_window_planning as twp

    windows, diagnostics = twp.compile_full_coverage_broll_windows(
        narration_units=[],
        pause_windows=[],
        safe_cut_boundaries=[],
        total_frames=300,
        min_segment_duration=3.0,
    )
    assert [
        (window["start_frame"], window["end_frame"])
        for window in windows
    ] == [(0, 165), (165, 300)]
    assert diagnostics["max_segment_frames"] == 165
    assert diagnostics["fallback_cut_count"] == 1


def test_full_coverage_window_compiler_caps_segments_to_broll_candidate_capacity():
    from packages.production.pipeline.nodes import timeline_window_planning as twp

    windows, diagnostics = twp.compile_full_coverage_broll_windows(
        narration_units=[],
        pause_windows=[],
        safe_cut_boundaries=[],
        total_frames=360,
        min_segment_duration=3.0,
        max_source_frames_available=120,
    )

    assert [(window["start_frame"], window["end_frame"]) for window in windows] == [
        (0, 120),
        (120, 240),
        (240, 360),
    ]
    assert diagnostics["max_segment_frames"] == 120
    assert diagnostics["candidate_max_source_frames"] == 120


def test_full_coverage_cut_selection_prefers_midpoint_over_longest_candidate():
    from packages.production.pipeline.nodes import timeline_window_planning as twp

    windows, diagnostics = twp.compile_full_coverage_broll_windows(
        narration_units=[],
        pause_windows=[],
        safe_cut_boundaries=[
            {"cut_id": "cut_near_midpoint", "frame": 105, "source": "semantic"},
            {"cut_id": "cut_longest", "frame": 165, "source": "semantic"},
        ],
        total_frames=270,
        min_segment_duration=3.0,
    )

    assert [(window["start_frame"], window["end_frame"]) for window in windows] == [
        (0, 105),
        (105, 270),
    ]
    assert diagnostics["fallback_cut_count"] == 0


def test_full_coverage_prefers_semantic_group_boundary_over_nearer_unit_boundary():
    from packages.production.pipeline.nodes import timeline_window_planning as twp

    units = [
        SimpleNamespace(
            unit_id="unit_low",
            text="普通过渡。",
            start=0.0,
            end=3.0,
            pause_after_ms=0,
            hard_end=False,
            boundary_score=0.2,
        ),
        SimpleNamespace(
            unit_id="unit_group",
            text="语义句组在这里收束。",
            start=3.0,
            end=5.0,
            pause_after_ms=180,
            hard_end=False,
            boundary_score=0.68,
        ),
        SimpleNamespace(
            unit_id="unit_tail",
            text="后续展开。",
            start=5.0,
            end=10.0,
            pause_after_ms=0,
            hard_end=True,
            boundary_score=0.7,
        ),
    ]

    windows, diagnostics = twp.compile_full_coverage_broll_windows(
        narration_units=units,
        pause_windows=[],
        safe_cut_boundaries=[],
        total_frames=300,
        min_segment_duration=2.0,
    )

    assert [(window["start_frame"], window["end_frame"]) for window in windows] == [
        (0, 150),
        (150, 300),
    ]
    assert diagnostics["selected_cut_source_counts"]["semantic_group"] == 1
    assert diagnostics["semantic_group_count"] == 2
    assert diagnostics["semantic_group_boundary_count"] == 1


def test_full_coverage_window_text_uses_largest_overlap_owner():
    from packages.production.pipeline.nodes import timeline_window_planning as twp

    units = [
        SimpleNamespace(unit_id="unit_split", text="跨窗句子。", start=0.0, end=5.0),
        SimpleNamespace(unit_id="unit_second", text="第二句。", start=3.0, end=8.0),
    ]

    windows, diagnostics = twp.compile_full_coverage_broll_windows(
        narration_units=units,
        pause_windows=[],
        safe_cut_boundaries=[
            {"cut_id": "cut_003", "frame": 90, "source": "semantic"},
        ],
        total_frames=240,
        min_segment_duration=2.0,
    )

    assert [(window["start_frame"], window["end_frame"]) for window in windows] == [
        (0, 90),
        (90, 240),
    ]
    assert windows[0]["host_unit_ids"] == ["unit_split"]
    assert windows[0]["text"] == "跨窗句子。"
    assert windows[1]["host_unit_ids"] == ["unit_split", "unit_second"]
    assert windows[1]["text"] == "第二句。"
    assert diagnostics["split_unit_count"] == 1


def test_broll_windows_reject_unsnappable_short_aroll_gaps(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
        broll_slots=[
            {
                "start_frame": 7,
                "end_frame": 70,
                "unit_ids": ["unit_1"],
                "text": "0.23 秒开头残缝必须丢弃。",
                "boundary_source": "narration_unit",
            },
            {
                "start_frame": 4,
                "end_frame": 140,
                "unit_ids": ["unit_2"],
                "text": "0.13 秒开头残缝可以吸附。",
                "boundary_source": "narration_unit",
            },
            {
                "start_frame": 60,
                "end_frame": 150,
                "unit_ids": ["unit_3"],
                "text": "正好 2 秒可读 A-roll 后的画面。",
                "boundary_source": "narration_unit",
            },
        ],
    )

    payload = _payload(_run_node(adapter, state), ArtifactKind.plan_timeline_windows)

    assert len(payload["broll_windows"]) == 2
    first, second = payload["broll_windows"]
    assert first["window_id"] == "bwin_001"
    assert first["start_frame"] == 0
    assert first["end_frame"] == 140
    assert first["length_frames"] == 140
    assert first["source_length_frames"] == 136
    assert round(first["pad_start"], 3) == 0.133
    assert round(first["pad_end"], 3) == 0.0
    assert second["window_id"] == "bwin_002"
    assert second["start_frame"] == 60
    assert second["end_frame"] == 150
    assert second["length_frames"] == 90


def test_broll_windows_snap_short_tail_gap_to_boundary(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
        broll_slots=[
            {
                "start_frame": 75,
                "end_frame": 350,
                "unit_ids": ["unit_1"],
                "text": "0.33 秒尾部残缝必须丢弃。",
                "boundary_source": "narration_unit",
            },
            {
                "start_frame": 75,
                "end_frame": 356,
                "unit_ids": ["unit_2"],
                "text": "0.13 秒尾部残缝可以吸附。",
                "boundary_source": "narration_unit",
            },
        ],
    )

    payload = _payload(_run_node(adapter, state), ArtifactKind.plan_timeline_windows)

    [window] = payload["broll_windows"]
    assert window["window_id"] == "bwin_001"
    assert window["start_frame"] == 75
    assert window["end_frame"] == payload["total_frames"]
    assert window["length_frames"] == payload["total_frames"] - 75
    assert window["source_length_frames"] == 281
    assert round(window["pad_start"], 3) == 0.0
    assert round(window["pad_end"], 3) == 0.133


def test_default_portrait_plan_is_nested_without_publishing_final_artifact(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
    )

    output = _run_node(adapter, state)
    windows_payload = _payload(output, ArtifactKind.plan_timeline_windows)
    portrait_payload = _portrait_payload(output)

    assert portrait_payload == windows_payload["default_assignment"]["portrait_plan_payload"]
    assert [artifact.kind for artifact in output.artifacts] == [
        ArtifactKind.plan_timeline_windows
    ]


def test_agent_windows_always_feasible(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
    )
    payload = _payload(_run_node(adapter, state), ArtifactKind.plan_timeline_windows)
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


def test_semantic_only_when_no_real_pauses(monkeypatch, tmp_path):
    # Sandbox-shape: detection returns no pauses -> semantic-only boundaries.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    # Asset-level uniqueness (issue #102): a multi-chunk 12s timeline needs several
    # DISTINCT portrait assets — one asset can no longer be reused across chunks.
    output = _run_node(
        adapter,
        _state(adapter, candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"]),
    )

    payload = _portrait_payload(output)
    assert payload["diagnostics"]["used_audio_pauses"] is False
    assert payload["segments"], "real planner must emit a frame-contiguous plan"
    sources = {seg["boundary_source"] for seg in payload["segments"]}
    assert "semantic_audio_pause" not in sources


def test_boundaries_land_on_detected_pauses(monkeypatch, tmp_path):
    # NarrationBoundaryPlanning already detected the real pauses upstream and handed them
    # to TimelineWindowPlanning via plan.narration_boundary. Given pauses sitting right
    # at each sentence end, the compiler snaps cuts into the pause windows.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    units = _units()
    pauses = [
        {
            "start": u["end"] - 0.02,
            "end": u["end"] + 0.16,
            "duration": 0.18,
            "center": u["end"] + 0.07,
        }
        for u in units[:-1]
    ]
    adapter = _adapter(object_store)
    # Several distinct assets so the multi-chunk timeline is coverable under
    # asset-level uniqueness (issue #102); the cuts still snap to the detected pauses.
    state = _state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
        pause_windows=pauses,
    )
    output = _run_node(adapter, state)

    payload = _portrait_payload(output)
    assert payload["diagnostics"]["used_audio_pauses"] is True
    assert payload["diagnostics"]["audio_pause_count"] == len(pauses)
    sources = {seg["boundary_source"] for seg in payload["segments"]}
    assert "semantic_audio_pause" in sources


def test_asr_narration_units_are_rehydrated_with_pause_boundaries():
    from packages.production.pipeline._narration_units import build_planner_narration_units

    units = build_planner_narration_units(
        raw_units=[
            {
                "unit_id": "unit_1",
                "text": "第一句介绍痛点。",
                "start": 0.0,
                "end": 2.0,
                "confidence": 0.8,
            },
            {
                "unit_id": "unit_2",
                "text": "第二句说明方案。",
                "start": 2.26,
                "end": 4.0,
                "confidence": 0.8,
            },
        ],
        source="asr",
        script="第一句介绍痛点。第二句说明方案。",
        duration=4.0,
    )

    assert units[0].pause_after_ms == 260
    assert units[0].portrait_cut_allowed is True
    assert units[0].boundary_score > 0


def test_asset_uniqueness_hard_fails_when_only_window_reuse_could_cover():
    # Issue #102: real-world fixture that USED to cover a 34.74s timeline by reusing a
    # few uploaded videos across their many lip-sync windows. There are only 3 DISTINCT
    # portrait assets (template_id); under asset-level uniqueness each is used at most
    # once, so 3 assets cover at most ~27s. No escalation round (full pool ->
    # capacity-controlled split -> pause-capacity split) can cover, so the planner
    # returns no plan and the node hard-fails (material_insufficient_portrait) — it
    # never silently reuses an asset to pad coverage. This is the intended behavior
    # change (lower unified-video yield, honest hard-fail on thin material).
    from packages.planning.editing import SpokenSegment, build_narration_units_from_asr
    from packages.production.pipeline.nodes import timeline_window_planning as twp

    spoken = [
        SpokenSegment(start=0.20, end=4.07, text="你还在超市门口犹豫要不要进去，别纠结了。"),
        SpokenSegment(start=4.41, end=10.77, text="就在邻水海丰小镇旭通超市，一家真接地气的小超市，"),
        SpokenSegment(start=10.77, end=16.79, text="搞花里胡哨就卖你每天要用的日用品，价格实在到让你"),
        SpokenSegment(start=16.79, end=18.46, text="纸巾都舍不得放回去。"),
        SpokenSegment(start=18.85, end=24.89, text="不是连锁大店，但东西全价儿低，老板熟，买啥都"),
        SpokenSegment(start=24.89, end=26.97, text="像回自己家楼下那家店。"),
        SpokenSegment(start=27.37, end=33.44, text="现在路过海丰小镇，认准旭通超市，进店看看，顺手买"),
        SpokenSegment(start=33.82, end=34.74, text="真的不贵。"),
    ]
    units = build_narration_units_from_asr(spoken, 34.74)
    pauses = [
        {"start": 3.898, "end": 4.504, "duration": 0.606, "center": 4.201},
        {"start": 10.069, "end": 10.731, "duration": 0.663, "center": 10.4},
        {"start": 11.745, "end": 12.345, "duration": 0.6, "center": 12.045},
        {"start": 14.284, "end": 14.849, "duration": 0.565, "center": 14.566},
        {"start": 18.325, "end": 18.945, "duration": 0.62, "center": 18.635},
        {"start": 23.721, "end": 24.352, "duration": 0.632, "center": 24.037},
        {"start": 26.847, "end": 27.361, "duration": 0.514, "center": 27.104},
        {"start": 33.324, "end": 33.897, "duration": 0.573, "center": 33.61},
    ]
    windows = [
        ("asset_1dec3fdcf42c", "w10.000_20.000_seg0", 9.92),
        ("asset_a73194405891", "w10.000_20.000_seg0", 9.92),
        ("asset_1fc8ae367f8a", "w0.000_10.000_seg2", 7.184),
        ("asset_a73194405891", "w0.000_10.000_seg2", 6.704),
        ("asset_a73194405891", "w20.000_30.064_seg0", 6.688),
        ("asset_a73194405891", "w30.064_36.733_seg0", 6.589),
        ("asset_1dec3fdcf42c", "w0.000_10.000_seg2", 6.576),
        ("asset_1fc8ae367f8a", "w10.000_16.400_seg0", 6.32),
        ("asset_a73194405891", "w20.000_30.064_seg1", 3.216),
        ("asset_1dec3fdcf42c", "w0.000_10.000_seg1", 2.448),
        ("asset_a73194405891", "w0.000_10.000_seg1", 2.032),
        ("asset_1fc8ae367f8a", "w0.000_10.000_seg1", 1.648),
        ("asset_1dec3fdcf42c", "w20.000_21.467_seg0", 1.387),
        ("asset_a73194405891", "w0.000_10.000_seg0", 1.024),
        ("asset_1fc8ae367f8a", "w0.000_10.000_seg0", 0.928),
        ("asset_1dec3fdcf42c", "w0.000_10.000_seg0", 0.736),
    ]
    candidates = [
        {
            "window_id": f"{asset_id}:{clip_id}",
            "template_id": asset_id,
            "template_name": asset_id,
            "start": 0.0,
            "end": duration,
            "duration": duration,
            "role": "main",
            "confidence": 0.9,
            "source_mode_hint": "lipsynced",
            "recent_usage": {},
            "recency_penalty": 0.0,
        }
        for asset_id, clip_id, duration in windows
    ]

    assert len({c["template_id"] for c in candidates}) == 3  # only 3 distinct assets

    plan, escalation = twp._plan_with_escalation(
        narration_units=units,
        candidates=candidates,
        duration=34.74,
        audio_pauses=pauses,
    )

    # Reuse can no longer rescue coverage: every escalation round is exhausted.
    assert not plan.ok
    assert plan.segments == []
    assert escalation["stage"] == "exhausted"
    assert escalation["capacity_controlled_split"] is False


def test_plan_is_frame_contiguous_and_covers_full_audio(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    output = _run_node(
        adapter,
        _state(
            adapter,
            candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
            duration=12.0,
        ),
    )

    payload = _portrait_payload(output)
    segments = payload["segments"]
    assert segments
    # contiguous on the frame grid: each segment starts where the previous ended.
    assert segments[0]["timeline_start_frame"] == 0
    for prev, nxt in zip(segments, segments[1:]):
        assert prev["timeline_end_frame"] == nxt["timeline_start_frame"]
    # source slice length == timeline window length (frame-exact, no over-extension).
    for seg in segments:
        timeline_len = seg["timeline_end_frame"] - seg["timeline_start_frame"]
        source_len = seg["source_end_frame"] - seg["source_start_frame"]
        assert source_len == timeline_len
    # total covers the full audio (15s demo source covers a 12s timeline).
    last_frame = segments[-1]["timeline_end_frame"]
    assert last_frame == round(payload["duration_sec"] * payload["fps"])


def test_insufficient_material_hard_fails(monkeypatch, tmp_path):
    # No portrait candidates at all -> honest hard-fail with the material code.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, _state(adapter, candidate_ids=[]))
    assert exc.value.error.code == ErrorCode.material_insufficient_portrait


def test_portrait_candidate_without_clip_metadata_is_rejected(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state(
        adapter,
        candidate_ids=["asset_portrait_demo"],
        duration=12.0,
        with_clip_metadata=False,
    )
    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, state)
    assert exc.value.error.code == ErrorCode.material_insufficient_portrait


def test_candidate_too_short_to_cover_returns_no_fabricated_plan(monkeypatch, tmp_path):
    # The only candidate (15s demo source) cannot cover a 40s timeline without
    # over-extension -> the planner returns no plan even after the escalation ladder
    # (full pool + capacity-controlled split retry) -> honest hard-fail.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, _state(adapter, candidate_ids=["asset_portrait_demo"], duration=40.0))
    assert exc.value.error.code == ErrorCode.material_insufficient_portrait


def test_escalation_ladder_diagnostics_on_success(monkeypatch, tmp_path):
    # A coverable timeline: the single full-pool pass succeeds; diagnostics expose the
    # escalation stage + that no capacity-controlled split was needed.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    output = _run_node(
        adapter,
        _state(
            adapter,
            candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
            duration=12.0,
        ),
    )
    diag = _portrait_payload(output)["diagnostics"]
    assert diag["recovery_stage"] == "full_pool"
    assert diag["capacity_controlled_split"] is False
    assert diag["longest_usable_source_window"] > 0
    assert any(a["stage"] == "full_pool" and a["ok"] for a in diag["recovery_attempts"])


def test_capacity_controlled_split_retry_drives_recovery(monkeypatch, tmp_path):
    # Force the single (default) pass to fail and the capacity-controlled split retry to
    # succeed, proving the node DRIVES max_chunk_duration on escalation (gap 1).
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    from packages.planning.editing import BoundaryConstraints as _BC
    from packages.production.pipeline.nodes import timeline_window_planning as twp

    real_plan = twp.plan_boundary_timeline
    calls: list[dict] = []

    class _Empty:
        ok = False
        segments: list = []
        total_frames = 0
        used_audio_pauses = False

    def fake_plan(*, narration_units, portrait_candidates, constraints, audio_pauses=None, fps=30):
        calls.append({"max_chunk_duration": constraints.max_chunk_duration})
        # The full-pool, no-cap pass cannot cover, forcing escalation.
        if constraints.max_chunk_duration is None:
            return _Empty()
        # Capacity-controlled split pass: defer to the real planner (it succeeds).
        return real_plan(
            narration_units=narration_units,
            portrait_candidates=portrait_candidates,
            constraints=constraints,
            audio_pauses=audio_pauses,
            fps=fps,
        )

    monkeypatch.setattr(twp, "plan_boundary_timeline", fake_plan)
    adapter = _adapter(object_store)
    output = _run_node(
        adapter,
        _state(
            adapter,
            candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
            duration=12.0,
        ),
    )

    diag = _portrait_payload(output)["diagnostics"]
    assert diag["recovery_stage"] == "capacity_controlled_split"
    assert diag["capacity_controlled_split"] is True
    assert calls[0]["max_chunk_duration"] is None
    assert calls[1]["max_chunk_duration"] is not None
    assert isinstance(_BC, type)


def test_recency_context_demotes_recently_used_template_and_records_opening(monkeypatch, tmp_path):
    # A prior run used asset_portrait_demo as its opening. MaterialPackPlanning (the
    # single ledger reader) stamps that recency/opening context onto the candidate
    # metadata; TimelineWindowPlanning consumes it WITHOUT reading the ledger, yet the
    # new plan still demotes the recently-used template and records its opening segment
    # distinctly so the guard has data for the run after this one. Behaviour is
    # equivalent to the old self-read path — only the ledger read moved upstream.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    from packages.core.contracts import SelectionLedgerEntry
    from packages.planning.selection.recency_context import (
        build_portrait_recency_context_from_ledger,
    )

    # The exact recency context MaterialPackPlanning would compute from the case's
    # recent portrait ledger for this template (same function, same ledger row).
    recent_usage = build_portrait_recency_context_from_ledger(
        entries=[
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_prev",
                medium="portrait",
                asset_id="asset_portrait_demo",
                slot_phase="portrait_opening",
            )
        ],
        template_id="asset_portrait_demo",
        diversity_key=None,
    )
    assert recent_usage["is_recently_used"] is True  # guard: the fixture really is "recent"

    # Spy: TimelineWindowPlanning must never touch the selection ledger now.
    ledger_calls: list = []
    real_recent_selections = adapter.repository.recent_selections

    def _spy_recent_selections(*args, **kwargs):
        ledger_calls.append((args, kwargs))
        return real_recent_selections(*args, **kwargs)

    monkeypatch.setattr(adapter.repository, "recent_selections", _spy_recent_selections)

    # A single-chunk (8s) timeline so the lone recently-used asset can still cover it
    # under asset-level uniqueness (issue #102) — it is demoted but, as the only
    # candidate, still used exactly once (no fabricated reuse to pad coverage).
    output = _run_node(
        adapter,
        _state(
            adapter,
            candidate_ids=["asset_portrait_demo"],
            duration=8.0,
            recent_usage=recent_usage,
        ),
    )
    payload = _portrait_payload(output)
    # Zero ledger reads from the compiler (single-point ledger read moved upstream).
    assert ledger_calls == []
    # The only template is recently used -> diagnostics surface a non-zero recent count.
    assert payload["diagnostics"]["recently_used_segment_count"] >= 1
    # Opening segment recorded with the distinct slot_phase (drives the next-run guard).
    assert payload["segments"][0]["slot_phase"] == "portrait_opening"
    assert all(
        seg["slot_phase"] in {"portrait_opening", "portrait_main"} for seg in payload["segments"]
    )


def test_segment_payload_derives_clip_id_from_window_id():
    segment = SimpleNamespace(
        template_id="asset_portrait_demo",
        window_id="asset_portrait_demo:talk:take_1",
        timeline_start_frame=0,
        timeline_end_frame=90,
        source_start_frame=30,
        source_end_frame=120,
        role="main",
        phase="body",
        source_mode="lipsynced",
        boundary_source="semantic",
        boundary_reason="beat",
        unit_ids=["unit_1"],
    )

    payload = nodes.timeline_window_planning._segment_payload(
        1,
        segment,
        recent_template_ids=set(),
    )

    assert payload["asset_id"] == "asset_portrait_demo"
    assert payload["clip_id"] == "talk:take_1"
    assert payload["slot_phase"] == "portrait_main"


# Issue #102 PR-B acceptance: asset-level portrait uniqueness (max_uses=1) + hard-fail.


def test_single_asset_cannot_cover_multiple_chunks_hard_fails_no_reuse(monkeypatch, tmp_path):
    """Acceptance #1: one asset, narration needs >1 chunk, only this asset -> hard-fail.

    A 12s timeline splits into 2 boundary chunks; with only ONE distinct portrait asset
    and asset-level uniqueness (issue #102) the asset cannot be reused to fill the
    second chunk. The node returns material_insufficient_portrait — never a reuse-padded
    plan — and the error details carry the diagnosis (distinct asset count == 1) so the
    run report shows the failure is portrait coverage, not some other fault.
    """
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, _state(adapter, candidate_ids=["asset_portrait_demo"], duration=12.0))

    assert exc.value.error.code == ErrorCode.material_insufficient_portrait
    details = exc.value.error.details
    assert details["reason"] == "portrait_coverage_insufficient_under_asset_uniqueness"
    assert details["distinct_portrait_asset_count"] == 1
    assert details["recovery_stage"] == "exhausted"


def test_multi_asset_capacity_split_covers_each_asset_at_most_once(monkeypatch, tmp_path):
    """Acceptance #2: multiple short assets, rhythm pass fails, split recovers, each once.

    Four DISTINCT portrait assets each expose a short 5s source window. The default
    full-pool (rhythm) pass builds chunks longer than 5s, so no 5s window can cover them
    -> it fails. The capacity-controlled split shortens chunks below the longest usable
    window, letting the short windows from DISTINCT assets cover the timeline. Every
    asset is used at most once (no reuse), proving asset-level uniqueness holds through
    the split path (issue #102 requirement #4).
    """
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    output = _run_node(
        adapter,
        _state(
            adapter,
            candidate_ids=[
                "asset_portrait_demo",
                "asset_portrait_b",
                "asset_portrait_c",
                "asset_portrait_d",
            ],
            duration=12.0,
            source_window=(0.0, 5.0),
        ),
    )

    payload = _portrait_payload(output)
    diag = payload["diagnostics"]
    assert diag["recovery_stage"] == "capacity_controlled_split"
    assert diag["capacity_controlled_split"] is True
    # Each portrait asset appears at most once across the whole main track.
    asset_ids = [seg["asset_id"] for seg in payload["segments"]]
    assert len(asset_ids) == len(set(asset_ids)), f"asset reused: {asset_ids}"


def test_sufficient_distinct_material_uses_fresh_unique_each_once(monkeypatch, tmp_path):
    """Acceptance #3: plenty of distinct fresh assets -> fresh-first, each asset once.

    With enough DISTINCT fresh portrait assets the single full-pool pass covers the
    timeline directly (no capacity split, no reuse). Behaviour is unchanged from before
    issue #102 for the material-rich case: every chunk gets a distinct fresh asset.
    """
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    output = _run_node(
        adapter,
        _state(
            adapter,
            candidate_ids=[
                "asset_portrait_demo",
                "asset_portrait_b",
                "asset_portrait_c",
                "asset_portrait_d",
            ],
            duration=12.0,
        ),
    )

    payload = _portrait_payload(output)
    diag = payload["diagnostics"]
    assert diag["recovery_stage"] == "full_pool"
    assert diag["capacity_controlled_split"] is False
    asset_ids = [seg["asset_id"] for seg in payload["segments"]]
    assert len(asset_ids) >= 2
    assert len(asset_ids) == len(set(asset_ids)), f"asset reused: {asset_ids}"


def _fc_windows(*, start_frame: int, end_frame: int, window_id: str = "w1", fps: int = 30) -> dict:
    return {
        "fps": fps,
        "broll_windows": [
            {"window_id": window_id, "start_frame": start_frame, "end_frame": end_frame}
        ],
    }


def _fc_candidate(candidate_id: str, *, source_start: float, source_end: float) -> dict:
    return {
        "asset_id": f"asset_{candidate_id}",
        "metadata": {
            "clip_id": f"clip_{candidate_id}",
            "source_start": source_start,
            "source_end": source_end,
            "diversity_key": candidate_id,
            "scene_name": "scene",
        },
    }


def test_full_coverage_requires_single_candidate_to_cover_window():
    # Window 0-100; the first candidate has only 97 source frames and is rejected.
    # The second candidate can cover the whole window and is used by itself.
    windows = _fc_windows(start_frame=0, end_frame=100)
    assignment = {
        "broll": [
            {"window_id": "w1", "candidate_id": "short"},
            {"window_id": "w1", "candidate_id": "long"},
        ]
    }
    candidates = {
        "broll_by_id": {
            "short": _fc_candidate("short", source_start=0.0, source_end=97 / 30),
            "long": _fc_candidate("long", source_start=1.0, source_end=130 / 30),
        }
    }

    plan, drops = materialize_full_coverage_broll_from_assignment(
        windows=windows,
        assignment=assignment,
        candidates=candidates,
        enabled=True,
        max_inserts=8,
    )

    overlays = plan["overlays"]
    assert len(overlays) == 1
    assert overlays[0]["asset_id"] == "asset_long"
    assert overlays[0]["timeline_start_frame"] == 0
    assert overlays[0]["timeline_end_frame"] == 100
    assert overlays[0]["source_start_frame"] == 30
    assert overlays[0]["source_end_frame"] == 130
    assert drops == []


def test_full_coverage_drops_when_source_cannot_cover_window():
    # Window 0-100; the only candidate has just 50 source frames, so it cannot
    # cover the authoritative window and no partial overlay is emitted.
    windows = _fc_windows(start_frame=0, end_frame=100)
    assignment = {"broll": [{"window_id": "w1", "candidate_id": "c1"}]}
    candidates = {"broll_by_id": {"c1": _fc_candidate("c1", source_start=0.0, source_end=50 / 30)}}

    plan, drops = materialize_full_coverage_broll_from_assignment(
        windows=windows,
        assignment=assignment,
        candidates=candidates,
        enabled=True,
        max_inserts=8,
    )

    overlays = plan["overlays"]
    assert overlays == []
    assert len(drops) == 1
    assert drops[0]["reason"] == "insufficient_window_coverage"
    assert drops[0]["covered_frames"] == 0
    assert drops[0]["missing_frames"] == 100


def test_full_coverage_ignores_extra_choices_after_window_is_filled():
    windows = _fc_windows(start_frame=0, end_frame=100)
    assignment = {
        "broll": [
            {"window_id": "w1", "candidate_id": "c_first"},
            {"window_id": "w1", "candidate_id": "c_extra"},
        ]
    }
    candidates = {
        "broll_by_id": {
            "c_first": _fc_candidate("c_first", source_start=0.0, source_end=100 / 30),
            "c_extra": _fc_candidate("c_extra", source_start=1.0, source_end=130 / 30),
        }
    }

    plan, drops = materialize_full_coverage_broll_from_assignment(
        windows=windows,
        assignment=assignment,
        candidates=candidates,
        enabled=True,
        max_inserts=8,
    )

    overlays = plan["overlays"]
    assert len(overlays) == 1
    assert overlays[0]["asset_id"] == "asset_c_first"
    assert overlays[0]["timeline_start_frame"] == 0
    assert overlays[0]["timeline_end_frame"] == 100
    assert overlays[0]["source_start_frame"] == 0
    assert overlays[0]["source_end_frame"] == 100
    assert drops == []
