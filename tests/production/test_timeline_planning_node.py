"""TimelinePlanning is verify-only for B-roll frames (#105).

After B-roll frame authority moved upstream into TimelineWindowPlanning plus the
shared materializer, TimelinePlanning no longer snaps B-roll to portrait cuts or
re-derives frames from seconds: it reads the authoritative ``*_frame`` boundaries off
each overlay verbatim, validates, and assembles the timeline + render plan. A missing
frame is an upstream contract defect -> fail fast.
"""

from __future__ import annotations

import inspect

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
import packages.production.pipeline.nodes.timeline_planning as timeline_planning


def test_timeline_planning_stays_verify_only():
    source = inspect.getsource(timeline_planning)
    assert "place_insertion_safely" not in source
    assert "plan_insertions" not in source
    assert "align_insertions_to_portrait_cuts" not in source
    assert "slice_source_window" not in source


def _artifact(kind: ArtifactKind, payload: dict, *, schema: str | None = None) -> Artifact:
    return Artifact(
        id=f"art_{kind.value.replace('.', '_')}",
        case_id="case_demo",
        run_id="run_tl",
        node_run_id="nr_input",
        kind=kind,
        payload=payload,
        payload_schema=schema or f"{kind.value}.v1",
    )


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_tl",
        job_id="job_tl",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_timeline",
        run_id="run_tl",
        node_id="TimelinePlanning",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _portrait_payload() -> dict:
    # Two contiguous portrait shots on the 30fps grid: a cut at frame 150 (5.0s).
    def _seg(i, start, end):
        return {
            "segment_id": f"portrait_{i}",
            "asset_id": "asset_portrait_demo",
            "clip_id": None,
            "start_sec": start / 30,
            "end_sec": end / 30,
            "source_start": 0.0,
            "source_end": (end - start) / 30,
            "role": "main",
            "source_mode": "lipsynced",
            "boundary_source": "semantic",
            "boundary_reason": "beat",
            "unit_ids": [],
            "slot_phase": "portrait_main",
            "recently_used_material": False,
            "timeline_start_frame": start,
            "timeline_end_frame": end,
            "source_start_frame": 0,
            "source_end_frame": end - start,
        }

    return {
        "fps": 30,
        "duration_sec": 6.0,
        "segments": [_seg(1, 0, 150), _seg(2, 150, 180)],
    }


def _broll_overlay(*, with_frames: bool) -> dict:
    overlay = {
        "overlay_id": "broll_1",
        "window_id": "bwin_000",
        "asset_id": "asset_broll_demo",
        "clip_id": "cover_a",
        "timeline_start": 3.0,
        "timeline_end": 4.9,
        "source_start": 3.0,
        "source_end": 4.9,
        "reason": "matched",
        "confidence": 0.8,
    }
    if with_frames:
        # Deliberately NOT on a portrait cut: tail at frame 147, the cut is at 150.
        overlay.update(
            {
                "timeline_start_frame": 90,
                "timeline_end_frame": 147,
                "source_start_frame": 90,
                "source_end_frame": 147,
                "pad_start": 0.0,
                "pad_end": 0.0,
            }
        )
    return overlay


def _timeline_windows_payload(*, start_frame: int = 90, end_frame: int = 147) -> dict:
    return {
        "fps": 30,
        "total_frames": 180,
        "geometry_policy": {},
        "portrait_windows": [],
        "broll_windows": [
            {
                "window_id": "bwin_000",
                "start_frame": start_frame,
                "end_frame": end_frame,
                "length_frames": end_frame - start_frame,
            }
        ],
    }


def _state(
    *, with_frames: bool, window_start_frame: int = 90, window_end_frame: int = 147
) -> tuple[LocalRuntimeAdapter, RunState]:
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = Repository()
    portrait = _artifact(ArtifactKind.plan_portrait, _portrait_payload())
    broll = _artifact(
        ArtifactKind.plan_broll,
        {"enabled": True, "overlays": [_broll_overlay(with_frames=with_frames)]},
        schema="BrollPlanArtifact.v1",
    )
    timeline_windows = _artifact(
        ArtifactKind.plan_timeline_windows,
        _timeline_windows_payload(start_frame=window_start_frame, end_frame=window_end_frame),
        schema="TimelineWindowsPlan.v1",
    )
    adapter.repository.artifacts[portrait.id] = portrait
    adapter.repository.artifacts[broll.id] = broll
    adapter.repository.artifacts[timeline_windows.id] = timeline_windows
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
            output={"width": 160, "height": 90, "fps": 30},
        ),
        artifacts={
            ArtifactKind.plan_portrait: portrait,
            ArtifactKind.plan_broll: broll,
            ArtifactKind.plan_timeline_windows: timeline_windows,
        },
    )
    return adapter, state


def _full_coverage_state() -> tuple[LocalRuntimeAdapter, RunState]:
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = Repository()
    portrait = _artifact(
        ArtifactKind.plan_portrait,
        {
            "fps": 30,
            "total_duration": 6.0,
            "asset_id": None,
            "duration_sec": 6.0,
            "segments": [],
            "diagnostics": {"track_mode": "broll_full_coverage"},
        },
    )
    broll = _artifact(
        ArtifactKind.plan_broll,
        {"enabled": True, "overlays": [_broll_overlay(with_frames=True)]},
        schema="BrollPlanArtifact.v1",
    )
    timeline_windows = _artifact(
        ArtifactKind.plan_timeline_windows,
        _timeline_windows_payload(),
        schema="TimelineWindowsPlan.v1",
    )
    adapter.repository.artifacts[portrait.id] = portrait
    adapter.repository.artifacts[broll.id] = broll
    adapter.repository.artifacts[timeline_windows.id] = timeline_windows
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
            broll={"enabled": True, "mode": "full_coverage"},
            output={"width": 160, "height": 90, "fps": 30},
        ),
        artifacts={
            ArtifactKind.plan_portrait: portrait,
            ArtifactKind.plan_broll: broll,
            ArtifactKind.plan_timeline_windows: timeline_windows,
        },
    )
    return adapter, state


def test_timeline_planning_passes_broll_frames_through_verbatim_without_snapping():
    # The overlay's tail (frame 147) sits 3 frames short of the portrait cut at 150.
    # The OLD timeline node would have snapped it to 150; the verify-only node must
    # leave every B-roll frame exactly as upstream materialization authored it.
    adapter, state = _state(with_frames=True)
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    output = timeline_planning.run(ctx)

    timeline = next(
        a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_timeline
    )
    broll_track = next(t for t in timeline["tracks"] if t["track_id"] == "broll")
    assert broll_track["timeline_start_frame"] == 90
    assert broll_track["timeline_end_frame"] == 147  # NOT snapped to 150
    assert broll_track["source_start_frame"] == 90
    assert broll_track["source_end_frame"] == 147
    assert timeline["validation"]["valid"] is True


def test_timeline_planning_accepts_broll_full_coverage_without_portrait_track():
    adapter, state = _full_coverage_state()
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    output = timeline_planning.run(ctx)

    timeline = next(
        a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_timeline
    )
    assert [track["track_id"] for track in timeline["tracks"]] == ["broll"]
    assert timeline["total_frames"] == 180
    assert timeline["validation"]["valid"] is True


def test_timeline_planning_fail_fasts_on_overlay_missing_authoritative_frames():
    # A legacy / seconds-only overlay (no frame fields) is an upstream contract defect
    # now that window materialization is authoritative -> fail fast naming missing frames.
    adapter, state = _state(with_frames=False)
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    with pytest.raises(NodeExecutionError) as exc:
        timeline_planning.run(ctx)
    assert exc.value.error.code == ErrorCode.render_invalid_timeline
    assert "timeline_start_frame" in exc.value.error.message


def test_timeline_planning_rejects_broll_frame_drift_from_authoritative_window():
    adapter, state = _state(with_frames=True, window_start_frame=90, window_end_frame=150)
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    with pytest.raises(NodeExecutionError) as exc:
        timeline_planning.run(ctx)
    assert exc.value.error.code == ErrorCode.render_invalid_timeline
    assert "drifts from authoritative window" in exc.value.error.message
