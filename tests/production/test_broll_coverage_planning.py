from __future__ import annotations

import pytest

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    MediaInfo,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.planning.material import BrollCandidate
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
import packages.production.pipeline.nodes.broll_coverage_planning as broll_coverage_planning


def _artifact(
    kind: ArtifactKind,
    payload: dict | None = None,
    *,
    payload_schema: str | None = None,
    media_info: MediaInfo | None = None,
) -> Artifact:
    return Artifact(
        id=f"art_{kind.value.replace('.', '_')}",
        case_id="case_demo",
        run_id="run_broll_only",
        node_run_id="nr_input",
        kind=kind,
        payload=payload,
        payload_schema=payload_schema or f"{kind.value}.v1",
        media_info=media_info,
    )


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_broll_only",
        job_id="job_broll_only",
        case_id="case_demo",
        workflow_template_id="broll_only_v1",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_broll_coverage",
        run_id="run_broll_only",
        node_id="BrollCoveragePlanning",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _ctx(*, duration: float = 5.0, min_segment_duration: float = 1.0) -> NodeContext:
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = Repository()
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="先展示维修过程。再展示完工效果。",
            voice={"voice_id": "voice_sandbox"},
            workflow_template_id="broll_only_v1",
            broll={"enabled": True, "min_segment_duration": min_segment_duration},
        ),
        artifacts={
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack,
                {"broll_candidates": [{"asset_id": "asset_broll_demo"}]},
                payload_schema="MaterialPackArtifact.v1",
            ),
            ArtifactKind.narration_units: _artifact(
                ArtifactKind.narration_units,
                {
                    "source": "estimated",
                    "strict": False,
                    "units": [
                        {
                            "unit_id": "unit_1",
                            "text": "先展示维修过程。",
                            "start": 0.0,
                            "end": 2.5,
                            "confidence": 0.9,
                        },
                        {
                            "unit_id": "unit_2",
                            "text": "再展示完工效果。",
                            "start": 2.5,
                            "end": duration,
                            "confidence": 0.9,
                        },
                    ],
                },
                payload_schema="NarrationUnitsArtifact.v1",
            ),
            ArtifactKind.audio_tts: _artifact(
                ArtifactKind.audio_tts,
                payload_schema="uri-only",
                media_info=MediaInfo(
                    media_type="audio",
                    codec="pcm_s16le",
                    format="wav",
                    duration_sec=duration,
                ),
            ),
        },
    )
    return NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)


def _candidate(
    *,
    asset_id: str,
    clip_id: str,
    source_start: float,
    source_end: float,
) -> BrollCandidate:
    return BrollCandidate(
        asset_id=asset_id,
        clip_id=clip_id,
        score=100.0,
        base_score=100.0,
        recency_penalty=0.0,
        matched_keywords=("展示",),
        scene_name="展示素材",
        source_start=source_start,
        source_end=source_end,
        diversity_key="demo",
    )


def test_broll_coverage_planning_outputs_full_duration_plan(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        broll_coverage_planning,
        "rank_broll_candidates",
        lambda **_: [
            _candidate(
                asset_id="asset_broll_demo",
                clip_id="cover_a",
                source_start=0.0,
                source_end=3.0,
            ),
            _candidate(
                asset_id="asset_broll_demo",
                clip_id="cover_b",
                source_start=0.0,
                source_end=3.0,
            ),
        ],
    )

    output = broll_coverage_planning.run(_ctx(duration=5.0))
    payload = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_broll
    )

    assert payload["enabled"] is True
    # overlays is the single canonical structure; segments is no longer written.
    assert "segments" not in payload
    assert payload["overlays"][0]["timeline_start"] == 0.0
    assert payload["overlays"][-1]["timeline_end"] == 5.0


def test_broll_coverage_planning_never_reads_selection_ledger(monkeypatch: pytest.MonkeyPatch):
    # The selection ledger is read once, in MaterialPackPlanning. BrollCoveragePlanning
    # must cover the narration WITHOUT reading the ledger.
    monkeypatch.setattr(
        broll_coverage_planning,
        "rank_broll_candidates",
        lambda **_: [
            _candidate(
                asset_id="asset_broll_demo", clip_id="cover_a", source_start=0.0, source_end=3.0
            ),
            _candidate(
                asset_id="asset_broll_demo", clip_id="cover_b", source_start=0.0, source_end=3.0
            ),
        ],
    )
    ctx = _ctx(duration=5.0)
    ledger_calls: list = []
    real_recent_selections = ctx.repository.recent_selections

    def _spy_recent_selections(*args, **kwargs):
        ledger_calls.append((args, kwargs))
        return real_recent_selections(*args, **kwargs)

    monkeypatch.setattr(ctx.repository, "recent_selections", _spy_recent_selections)

    output = broll_coverage_planning.run(ctx)
    payload = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_broll
    )

    assert ledger_calls == []
    assert payload["overlays"][0]["timeline_start"] == 0.0
    assert payload["overlays"][-1]["timeline_end"] == 5.0


def test_broll_coverage_planning_hard_fails_when_material_is_insufficient(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        broll_coverage_planning,
        "rank_broll_candidates",
        lambda **_: [
            _candidate(
                asset_id="asset_broll_demo",
                clip_id="short_cover",
                source_start=0.0,
                source_end=2.0,
            )
        ],
    )

    with pytest.raises(NodeExecutionError) as exc:
        broll_coverage_planning.run(_ctx(duration=5.0))

    assert exc.value.error.code == ErrorCode.material_insufficient_broll
