"""ExportSeedanceVideo node: persist the Seedance output as a finished video.

A deliberately slim alternative to ``ExportFinishedVideo`` for the one-shot
Seedance chain. ``ExportFinishedVideo`` hard-requires ``video.final`` +
``plan.timeline`` + ``plan.style`` and writes a ``VideoVersion`` with two
non-null plan FKs — none of which a text/image-to-video run produces. This node
takes the ``video.rendered`` artifact straight to a ``FinishedVideo`` +
``PublishPackage`` (the publish package builder has no timeline/style deps),
skipping ``VideoVersion`` entirely.
"""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, ErrorCode, FinishedVideo
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.finished_video_numbering import next_finished_video_number
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes.export_finished_video import (
    _create_publish_package_artifact,
    _frame_cover,
    _resolve_owner_user_id,
)

_DEFAULT_DURATION_SEC = 15.0


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    repository = ctx.repository
    video = state.require(ArtifactKind.video_rendered)

    video_artifact = ctx.artifact(
        ArtifactKind.video_finished,
        None,
        "uri-only",
        uri=video.uri,
        size_bytes=video.size_bytes,
        sha256=video.sha256,
        media_info=video.media_info,
    )
    cover_artifact = _safe_frame_cover(ctx, video)

    duration = (
        float(video.media_info.duration_sec)
        if video.media_info and video.media_info.duration_sec
        else _DEFAULT_DURATION_SEC
    )
    finished = FinishedVideo(
        id=new_id("fv"),
        case_id=state.request.case_id,
        run_id=run.id,
        owner_user_id=_resolve_owner_user_id(run, repository),
        title=state.request.title or "Seedance 短片",
        video_number=next_finished_video_number(
            v.video_number
            for v in repository.finished_videos.values()
            if v.case_id == state.request.case_id
        ),
        video_artifact=repository.artifact_ref(video_artifact.id),
        cover_artifact=(repository.artifact_ref(cover_artifact.id) if cover_artifact else None),
        duration_sec=duration,
        lipsync_provider_id=None,
        lipsync_fallback_used=False,
    )
    repository.finished_videos[finished.id] = finished
    package_artifact = _create_publish_package_artifact(
        ctx,
        finished,
        description=state.request.publish_content,
    )
    artifacts = [video_artifact]
    if cover_artifact is not None:
        artifacts.append(cover_artifact)
    artifacts.append(package_artifact)
    return NodeOutput(artifacts=artifacts)


def _safe_frame_cover(ctx: NodeContext, video):
    """Best-effort frame cover for a one-shot clip.

    Swallow the cover ONLY when the source video is not locally readable — the
    sandbox path stores a uri-only ``sandbox://`` placeholder, and a remote-only
    object store has no local bytes; both surface as ``artifact_missing``. A real
    ffmpeg extraction failure on a readable file carries a render error code and
    must NOT be hidden (no silent degrade), so it propagates and fails the node."""
    try:
        return _frame_cover(ctx, video)
    except NodeExecutionError as exc:
        if exc.error.code == ErrorCode.artifact_missing:
            return None
        raise
    except (FileNotFoundError, ValueError, OSError):
        return None
