from __future__ import annotations

import pytest

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.media.assets import store_file
from packages.media.video.ffmpeg import probe_media, probe_video_frame_count
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
import packages.production.pipeline.nodes.broll_render_base as broll_render_base


def _artifact(
    kind: ArtifactKind,
    payload: dict | None = None,
    *,
    payload_schema: str | None = None,
) -> Artifact:
    return Artifact(
        id=f"art_{kind.value.replace('.', '_')}",
        case_id="case_demo",
        run_id="run_broll_only",
        node_run_id="nr_input",
        kind=kind,
        payload=payload,
        payload_schema=payload_schema or f"{kind.value}.v1",
    )


def test_broll_render_base_outputs_rendered_video_with_exact_frame_count(
    tmp_path,
    media_fixture_factory,
    monkeypatch: pytest.MonkeyPatch,
):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    repository = Repository()
    source_path = media_fixture_factory.video(
        duration_sec=2.0,
        width=320,
        height=180,
        fps=24,
        filename="broll_render_base_source.mp4",
    )
    stored = store_file(object_store, source_path, purpose="seed-media")
    source_artifact = repository.create_artifact(
        kind=ArtifactKind.uploaded_file,
        payload_schema="UploadedFileArtifact.v1",
        payload={
            "upload_session_id": None,
            "filename": source_path.name,
            "content_type": "video/mp4",
            "size_bytes": source_path.stat().st_size,
            "object_uri": stored.ref.uri,
            "sha256": stored.sha256,
            "metadata": {"asset_id": "asset_broll_demo"},
        },
        case_id="case_demo",
        uri=stored.ref.uri,
        sha256=stored.sha256,
        media_info=probe_media(source_path),
    )
    repository.media_assets["asset_broll_demo"] = repository.media_assets[
        "asset_broll_demo"
    ].model_copy(update={"source_artifact_id": source_artifact.id})

    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    total_frames = 48
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
                        }
                    ],
                },
                payload_schema="BrollPlanArtifact.v1",
            ),
            ArtifactKind.plan_timeline: _artifact(
                ArtifactKind.plan_timeline,
                {"total_frames": total_frames, "tracks": [], "validation": {"valid": True}},
                payload_schema="TimelinePlanArtifact.v1",
            ),
            ArtifactKind.plan_render: _artifact(
                ArtifactKind.plan_render,
                {
                    "timeline_artifact_id": "art_plan_timeline",
                    "render_size": [160, 90],
                    "fps": fps,
                    "tracks": [],
                },
                payload_schema="RenderPlanArtifact.v1",
            ),
        },
    )
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
            id="nr_broll_render",
            run_id="run_broll_only",
            node_id="BrollRenderBase",
            node_version="v1",
            status=NodeStatus.running,
            input_manifest_hash="sha256:test",
        ),
        state=state,
    )

    output = broll_render_base.run(ctx)
    artifact = next(a for a in output.artifacts if a.kind == ArtifactKind.video_rendered)

    assert artifact.uri is not None
    assert artifact.sha256 is not None
    assert artifact.media_info is not None
    assert artifact.media_info.width == 160
    assert artifact.media_info.height == 90
    assert probe_video_frame_count(ctx.artifact_path(artifact)) == total_frames
