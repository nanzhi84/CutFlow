"""PortraitPlanning node wires the PURE editing planner into the pipeline.

These tests prove the node consumes narration units + material portrait candidates +
detected audio pauses and emits the real frame-contiguous portrait plan — no seeded /
placeholder timeline — and soft-degrades honestly when material is insufficient.
"""

from __future__ import annotations

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


def _state(adapter: LocalRuntimeAdapter, *, candidate_ids: list[str], duration: float = 12.0) -> RunState:
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script=SCRIPT,
        voice={"voice_id": "voice_sandbox"},
        portrait={"template_mode": "agent"},
        strictness={"strict_timestamps": False},
    )
    material = {"portrait_candidates": [{"asset_id": cid, "score": 1.0} for cid in candidate_ids]}
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
        {"start": u["end"] - 0.02, "end": u["end"] + 0.16, "duration": 0.18, "center": u["end"] + 0.07}
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


def test_plan_is_frame_contiguous_and_covers_full_audio(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.portrait_planning.detect_silence_windows",
        lambda *a, **k: [],
    )
    adapter = _adapter(object_store)
    output = _run_node(adapter, _state(adapter, candidate_ids=["asset_portrait_demo"], duration=12.0))

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


def test_candidate_too_short_to_cover_returns_no_fabricated_plan(monkeypatch, tmp_path):
    # The only candidate (15s demo source) cannot cover a 40s timeline without
    # over-extension -> the planner returns no plan -> honest soft-degrade.
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
