"""PortraitTrackBuild fail-fasts on a frame-less portrait segment (#105).

PortraitPlanning unconditionally emits frame-aligned segments on the 30fps grid and
its node uses ``reuse_policy="never"`` (resume always re-runs the planner), so there
is no live path that feeds PortraitTrackBuild a segment without ``source_*_frame``.
The seconds -> frame fallback was therefore removed: a missing source frame is an
upstream contract defect that must surface, not be silently re-derived.
"""

from __future__ import annotations

import pytest

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
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
import packages.production.pipeline.nodes.portrait_track_build as portrait_track_build


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_ptb",
        job_id="job_ptb",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_ptb",
        run_id="run_ptb",
        node_id="PortraitTrackBuild",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def test_portrait_track_build_fail_fasts_on_missing_source_frames():
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = Repository()
    # A portrait segment that carries seconds but NO source_*_frame fields.
    portrait = Artifact(
        id="art_plan_portrait",
        case_id="case_demo",
        run_id="run_ptb",
        node_run_id="nr_portrait",
        kind=ArtifactKind.plan_portrait,
        payload={
            "fps": 30,
            "duration_sec": 3.0,
            "segments": [
                {
                    "segment_id": "portrait_1",
                    "asset_id": "asset_portrait_demo",
                    "start_sec": 0.0,
                    "end_sec": 3.0,
                    "source_start": 0.0,
                    "source_end": 3.0,
                    "timeline_start_frame": 0,
                    "timeline_end_frame": 90,
                    "source_start_frame": None,
                    "source_end_frame": None,
                }
            ],
        },
        payload_schema="PortraitPlanArtifact.v1",
    )
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
            output={"width": 160, "height": 90, "fps": 30},
        ),
        artifacts={ArtifactKind.plan_portrait: portrait},
    )
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)

    with pytest.raises(NodeExecutionError) as exc:
        portrait_track_build.run(ctx)
    assert exc.value.error.code == ErrorCode.render_invalid_timeline
    assert "source_start_frame" in exc.value.error.message
