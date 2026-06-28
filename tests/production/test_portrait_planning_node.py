"""PortraitPlanning node wires the PURE editing planner into the pipeline.

These tests prove the node consumes narration units + material portrait candidates +
detected audio pauses and emits the real frame-contiguous portrait plan — no seeded /
placeholder timeline — and soft-degrades honestly when material is insufficient.
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
from packages.media.assets import store_file
from packages.production.pipeline import nodes
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
from tests.fixtures.media import generate_test_audio


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


def _state(
    adapter: LocalRuntimeAdapter,
    *,
    candidate_ids: list[str],
    duration: float = 12.0,
    with_clip_metadata: bool = True,
) -> RunState:
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script=SCRIPT,
        voice={"voice_id": "voice_sandbox"},
        portrait={"template_mode": "agent"},
        strictness={"strict_timestamps": False},
    )
    material = {
        "portrait_candidates": [
            {
                "asset_id": cid,
                "score": 1.0,
                "metadata": (
                    {"clip_id": f"{cid}_talk", "source_start": 0.0, "source_end": 15.0}
                    if with_clip_metadata
                    else {}
                ),
            }
            for cid in candidate_ids
        ]
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
    return RunState(
        request=request,
        artifacts={
            ArtifactKind.plan_material_pack: material_artifact,
            ArtifactKind.narration_units: narration_artifact,
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


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_portrait",
        run_id="run_1",
        node_id="PortraitPlanning",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _run_node(adapter: LocalRuntimeAdapter, state: RunState):
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    return nodes.portrait_planning.run(ctx)


def _portrait_payload(output) -> dict:
    return next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_portrait)


def test_semantic_only_when_no_real_pauses(monkeypatch, tmp_path):
    # Sandbox-shape: detection returns no pauses -> semantic-only boundaries.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.portrait_planning.detect_silence_windows",
        lambda *a, **k: [],
    )
    adapter = _adapter(object_store)
    output = _run_node(adapter, _state(adapter, candidate_ids=["asset_portrait_demo"]))

    payload = _portrait_payload(output)
    assert payload["diagnostics"]["used_audio_pauses"] is False
    assert payload["segments"], "real planner must emit a frame-contiguous plan"
    sources = {seg["boundary_source"] for seg in payload["segments"]}
    assert "semantic_audio_pause" not in sources


def test_boundaries_land_on_detected_pauses(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    units = _units()
    # Real silences sitting right at each sentence end -> the planner snaps cuts into
    # the pause windows (semantic_audio_pause boundary source).
    pauses = [
        {
            "start": u["end"] - 0.02,
            "end": u["end"] + 0.16,
            "duration": 0.18,
            "center": u["end"] + 0.07,
        }
        for u in units[:-1]
    ]
    seen: dict[str, object] = {}

    def fake_detect(audio_path, *a, **k):
        seen["audio_path"] = audio_path
        return pauses

    monkeypatch.setattr(
        "packages.production.pipeline.nodes.portrait_planning.detect_silence_windows",
        fake_detect,
    )
    # A real, resolvable TTS audio artifact must exist for detection to be attempted
    # (the node resolves its local path before running detection on it).
    adapter = _adapter(object_store)
    state = _state(adapter, candidate_ids=["asset_portrait_demo"])
    audio_path = generate_test_audio(tmp_path, duration_sec=12, frequency=440)
    stored = store_file(object_store, audio_path, purpose="generated-audio")
    state.artifacts[ArtifactKind.audio_tts] = Artifact(
        id="art_tts",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_tts",
        kind=ArtifactKind.audio_tts,
        uri=stored.ref.uri,
        sha256=stored.sha256,
        payload_schema="uri-only",
    )
    output = _run_node(adapter, state)

    assert seen.get("audio_path") is not None, "detection must run on the resolved TTS path"

    payload = _portrait_payload(output)
    assert payload["diagnostics"]["used_audio_pauses"] is True
    assert payload["diagnostics"]["audio_pause_count"] == len(pauses)
    sources = {seg["boundary_source"] for seg in payload["segments"]}
    assert "semantic_audio_pause" in sources


def test_unreadable_tts_audio_does_not_silently_disable_pause_detection(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state(adapter, candidate_ids=["asset_portrait_demo"])
    state.artifacts[ArtifactKind.audio_tts] = Artifact(
        id="art_tts",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_tts",
        kind=ArtifactKind.audio_tts,
        uri="s3://foreign-bucket/generated-audio/missing.mp3",
        payload_schema="uri-only",
    )

    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, state)

    assert exc.value.error.code == ErrorCode.artifact_missing


def test_asr_narration_units_are_rehydrated_with_pause_boundaries():
    from packages.production.pipeline.nodes import portrait_planning as pp

    units = pp._planner_narration_units(
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


def test_escalation_uses_real_pause_capacity_split_below_longest_window():
    from packages.planning.editing import SpokenSegment, build_narration_units_from_asr
    from packages.production.pipeline.nodes import portrait_planning as pp

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

    plan, escalation = pp._plan_with_escalation(
        narration_units=units,
        candidates=candidates,
        duration=34.74,
        audio_pauses=pauses,
    )

    assert plan.ok
    assert plan.used_audio_pauses is True
    assert escalation["stage"] == "capacity_controlled_split"
    assert escalation["capacity_controlled_split"] is True
    assert escalation["audio_pause_capacity_cap"] is None


def test_plan_is_frame_contiguous_and_covers_full_audio(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.portrait_planning.detect_silence_windows",
        lambda *a, **k: [],
    )
    adapter = _adapter(object_store)
    output = _run_node(
        adapter, _state(adapter, candidate_ids=["asset_portrait_demo"], duration=12.0)
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


def test_insufficient_material_soft_degrades(monkeypatch, tmp_path):
    # No portrait candidates at all -> honest hard-fail with the material code.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.portrait_planning.detect_silence_windows",
        lambda *a, **k: [],
    )
    adapter = _adapter(object_store)
    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, _state(adapter, candidate_ids=[]))
    assert exc.value.error.code == ErrorCode.material_insufficient_portrait


def test_portrait_candidate_without_clip_metadata_is_rejected(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.portrait_planning.detect_silence_windows",
        lambda *a, **k: [],
    )
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
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.portrait_planning.detect_silence_windows",
        lambda *a, **k: [],
    )
    adapter = _adapter(object_store)
    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, _state(adapter, candidate_ids=["asset_portrait_demo"], duration=40.0))
    assert exc.value.error.code == ErrorCode.material_insufficient_portrait


def test_escalation_ladder_diagnostics_on_success(monkeypatch, tmp_path):
    # A coverable timeline: the single full-pool pass succeeds; diagnostics expose the
    # escalation stage + that no capacity-controlled split was needed.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.portrait_planning.detect_silence_windows",
        lambda *a, **k: [],
    )
    adapter = _adapter(object_store)
    output = _run_node(
        adapter, _state(adapter, candidate_ids=["asset_portrait_demo"], duration=12.0)
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
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.portrait_planning.detect_silence_windows",
        lambda *a, **k: [],
    )
    from packages.planning.editing import BoundaryConstraints as _BC
    from packages.production.pipeline.nodes import portrait_planning as pp

    real_plan = pp.plan_boundary_timeline
    calls: list[dict] = []

    class _Empty:
        ok = False
        segments: list = []
        total_frames = 0
        used_audio_pauses = False

    def fake_plan(*, narration_units, portrait_candidates, constraints, audio_pauses=None, fps=30):
        calls.append(
            {
                "max_chunk_duration": constraints.max_chunk_duration,
                "include_unlimited_reuse_scope": constraints.include_unlimited_reuse_scope,
            }
        )
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

    monkeypatch.setattr(pp, "plan_boundary_timeline", fake_plan)
    adapter = _adapter(object_store)
    output = _run_node(
        adapter, _state(adapter, candidate_ids=["asset_portrait_demo"], duration=12.0)
    )

    diag = _portrait_payload(output)["diagnostics"]
    assert diag["recovery_stage"] == "capacity_controlled_split"
    assert diag["capacity_controlled_split"] is True
    assert calls[0]["max_chunk_duration"] is None
    assert calls[1]["max_chunk_duration"] is not None
    assert calls[1]["include_unlimited_reuse_scope"] is True
    assert isinstance(_BC, type)


def test_recency_context_demotes_recently_used_template_and_records_opening(monkeypatch, tmp_path):
    # A prior run used asset_portrait_demo as its opening -> the next run's candidate
    # carries a live recency/opening context (previously dead), AND the new plan records
    # its opening segment distinctly so the guard has data for the run after this one.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.portrait_planning.detect_silence_windows",
        lambda *a, **k: [],
    )
    adapter = _adapter(object_store)
    from packages.core.contracts import SelectionLedgerEntry

    adapter.repository.record_selection_ledger_entries(
        [
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_prev",
                medium="portrait",
                asset_id="asset_portrait_demo",
                slot_phase="portrait_opening",
            )
        ]
    )
    output = _run_node(
        adapter, _state(adapter, candidate_ids=["asset_portrait_demo"], duration=12.0)
    )
    payload = _portrait_payload(output)
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

    payload = nodes.portrait_planning._segment_payload(
        1,
        segment,
        recent_template_ids=set(),
    )

    assert payload["asset_id"] == "asset_portrait_demo"
    assert payload["clip_id"] == "talk:take_1"
    assert payload["slot_phase"] == "portrait_main"
