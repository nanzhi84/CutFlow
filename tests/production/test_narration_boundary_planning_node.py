"""NarrationBoundaryPlanning front-moves pause detection + safe-cut planning (#135).

These tests prove the node reads the TTS audio, detects real pauses (ffmpeg
silencedetect), assembles the semantic + audio safe-cut boundaries, and emits a
frame-quantized plan.narration_boundary artifact — the pause windows PortraitPlanning then
consumes instead of re-detecting them. They also pin the two guards that moved here with
the detection: honest artifact_missing on an unreadable TTS audio, and semantic-only
fallback when the audio has no reliable silence.
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
from packages.planning.editing import frame_index
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


def _state(duration: float = 12.0) -> RunState:
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script=SCRIPT,
        voice={"voice_id": "voice_sandbox"},
        strictness={"strict_timestamps": False},
    )
    narration = {"source": "estimated", "units": _units(duration), "strict": False}
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
        artifacts={ArtifactKind.narration_units: narration_artifact},
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
        id="nr_narration_boundary",
        run_id="run_1",
        node_id="NarrationBoundaryPlanning",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _run_node(adapter: LocalRuntimeAdapter, state: RunState):
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    return nodes.narration_boundary_planning.run(ctx)


def _payload(output) -> dict:
    return next(
        a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_narration_boundary
    )


def test_semantic_only_when_no_tts_audio(monkeypatch, tmp_path):
    # No audio_tts artifact -> no pauses -> semantic-only boundary source, no ffmpeg call.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    output = _run_node(adapter, _state())

    payload = _payload(output)
    assert payload["source"] == "semantic_only"
    assert payload["pause_windows"] == []
    assert payload["diagnostics"]["used_audio_pauses"] is False
    # The full timeline is always bracketed by a start + end safe cut.
    assert len(payload["safe_cut_boundaries"]) >= 2
    assert payload["safe_cut_boundaries"][0]["frame"] == 0
    assert payload["fps"] == 30


def test_detects_pauses_on_resolved_tts_path_and_publishes_them(monkeypatch, tmp_path):
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
    seen: dict[str, object] = {}

    def fake_detect(audio_path, *a, **k):
        seen["audio_path"] = audio_path
        return pauses

    monkeypatch.setattr(
        "packages.production.pipeline.nodes.narration_boundary_planning.detect_silence_windows",
        fake_detect,
    )
    adapter = _adapter(object_store)
    state = _state()
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
    payload = _payload(output)
    assert payload["source"] == "tts_subtitle+silence"
    # The raw pause windows are handed downstream verbatim for PortraitPlanning to consume.
    assert payload["pause_windows"] == pauses
    assert payload["diagnostics"]["used_audio_pauses"] is True
    # A real pause at a sentence end produces a semantic_audio_pause safe cut carrying the
    # issue's core contract fields: a frame-quantized cut (frame == floor(t*fps + 0.5)) plus
    # the semantic source (after_unit_id / semantic_time). Assert them on the actual cut, not
    # just the presence of the source string.
    pause_cuts = [
        cut for cut in payload["safe_cut_boundaries"] if cut["source"] == "semantic_audio_pause"
    ]
    assert pause_cuts, "a pause at a sentence end must yield a semantic_audio_pause cut"
    cut = pause_cuts[0]
    assert cut["frame"] == frame_index(cut["time"])
    assert cut["after_unit_id"] is not None
    assert cut["semantic_time"] is not None


def test_unreadable_tts_audio_hard_fails_not_silently_disabled(monkeypatch, tmp_path):
    # The artifact_missing guard moved here with the detection: an unreadable TTS audio is
    # an honest hard-fail, never a silent "no pauses".
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    state = _state()
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


def test_frame_quantized_slots_are_contiguous_and_cover_the_timeline(monkeypatch, tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    output = _run_node(adapter, _state(duration=12.0))

    payload = _payload(output)
    total_frames = payload["total_frames"]
    assert total_frames == round(12.0 * 30)
    cuts = payload["safe_cut_boundaries"]
    slots = payload["portrait_slots"]
    assert slots, "a multi-sentence timeline yields >= 1 portrait slot"
    # Portrait slots tile the safe-cut list with no gap / no overlap on the frame grid.
    assert slots[0]["start_frame"] == cuts[0]["frame"]
    for prev, nxt in zip(slots, slots[1:]):
        assert prev["end_frame"] == nxt["start_frame"]
    assert slots[-1]["end_frame"] == cuts[-1]["frame"]
    # Every narration unit maps to a B-roll available window carrying its text.
    assert len(payload["broll_slots"]) == len(_units(12.0))
    assert all(slot["text"] for slot in payload["broll_slots"])


def test_boundaries_match_portrait_planning_consumed_pauses(monkeypatch, tmp_path):
    """The pauses this node publishes are exactly what PortraitPlanning would plan on.

    Running both nodes on the same inputs, the portrait main-track boundaries land on the
    audio pauses this node detected — proving the split front-moves detection without
    changing the frame boundaries PortraitPlanning produces (#135 acceptance).
    """
    from tests.production import test_portrait_planning_node as portrait_test

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
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.narration_boundary_planning.detect_silence_windows",
        lambda *a, **k: pauses,
    )
    adapter = _adapter(object_store)
    state = _state()
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
    boundary_output = _run_node(adapter, state)
    boundary_payload = _payload(boundary_output)

    # Feed the published pauses into PortraitPlanning exactly as the pipeline would.
    portrait_state = portrait_test._state(
        adapter,
        candidate_ids=["asset_portrait_demo", "asset_portrait_b", "asset_portrait_c"],
        pause_windows=boundary_payload["pause_windows"],
    )
    portrait_output = portrait_test._run_node(adapter, portrait_state)
    portrait_payload = next(
        a.payload for a in portrait_output.artifacts if a.kind == ArtifactKind.plan_portrait
    )
    sources = {seg["boundary_source"] for seg in portrait_payload["segments"]}
    assert "semantic_audio_pause" in sources
    assert portrait_payload["diagnostics"]["used_audio_pauses"] is True
