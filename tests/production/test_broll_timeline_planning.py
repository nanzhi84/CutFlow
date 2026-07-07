from __future__ import annotations

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    MediaInfo,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.repository import Repository
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
import packages.production.pipeline.nodes.broll_timeline_planning as broll_timeline_planning


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


def test_broll_timeline_planning_builds_single_broll_track_and_render_plan():
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = Repository()
    duration = 4.2
    fps = 24
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="B-roll narration.",
            voice={"voice_id": "voice_sandbox"},
            workflow_template_id="broll_only_v1",
            output={"width": 160, "height": 90, "fps": fps},
        ),
        artifacts={
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
            ArtifactKind.plan_broll: _artifact(
                ArtifactKind.plan_broll,
                {
                    "enabled": True,
                    "overlays": [
                        {
                            "overlay_id": "broll_1",
                            "asset_id": "asset_broll_demo",
                            "clip_id": "cover_a",
                            "timeline_start": 0.0,
                            "timeline_end": 2.0,
                            "source_start": 0.0,
                            "source_end": 2.0,
                            "reason": "matched",
                            "confidence": 0.8,
                        },
                        {
                            "overlay_id": "broll_2",
                            "asset_id": "asset_broll_demo",
                            "clip_id": "cover_b",
                            "timeline_start": 2.0,
                            "timeline_end": duration,
                            "source_start": 0.0,
                            "source_end": 2.2,
                            "reason": "matched",
                            "confidence": 0.8,
                        },
                    ],
                },
                payload_schema="BrollPlanArtifact.v1",
            ),
        },
    )
    adapter.repository.artifacts[state.artifacts[ArtifactKind.plan_broll].id] = state.artifacts[
        ArtifactKind.plan_broll
    ]
    ctx = NodeContext(
        adapter=adapter,
        run=WorkflowRun(
            id="run_broll_only",
            job_id="job_broll_only",
            case_id="case_demo",
            workflow_template_id="broll_only_v1",
            workflow_version="v1",
            status=RunStatus.running,
        ),
        node_run=NodeRun(
            id="nr_broll_timeline",
            run_id="run_broll_only",
            node_id="BrollTimelinePlanning",
            node_version="v1",
            status=NodeStatus.running,
            input_manifest_hash="sha256:test",
        ),
        state=state,
    )

    output = broll_timeline_planning.run(ctx)
    timeline = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_timeline)
    render = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_render)

    assert timeline["total_frames"] == round(duration * fps)
    assert {track["track_id"] for track in timeline["tracks"]} == {"broll"}
    assert timeline["validation"]["valid"] is True
    assert render["render_size"] == [160, 90]
    assert render["fps"] == fps
    assert render["timeline_artifact_id"]
