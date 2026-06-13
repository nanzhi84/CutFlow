"""ExportFinishedVideo node: persist finished video, cover, and publish package."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.core.contracts import (
    ArtifactKind,
    FinishedVideo,
    NodeStatus,
    ScriptVersion,
    VideoVersion,
)
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.video.ffmpeg import FfmpegCommandError, extract_thumbnails
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    repository = ctx.repository
    final = state.require(ArtifactKind.video_final)
    timeline = state.require(ArtifactKind.plan_timeline)
    style = state.require(ArtifactKind.plan_style)
    script = ScriptVersion(
        id=state.request.script_version_id or new_id("script"),
        case_id=state.request.case_id,
        title=state.request.title or "Untitled script",
        script=state.request.script,
        creative_intent_artifact_id=state.artifacts.get(ArtifactKind.creative_intent).id
        if ArtifactKind.creative_intent in state.artifacts
        else None,
    )
    repository.scripts[script.id] = script
    video_artifact = ctx.artifact(
        ArtifactKind.video_finished,
        None,
        "uri-only",
        uri=final.uri,
        sha256=final.sha256,
        media_info=final.media_info,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="cutagent-cover-") as directory:
            thumbnails = extract_thumbnails(
                ctx.artifact_path(final),
                Path(directory),
                labels=("first", "mid"),
            )
            selected = thumbnails[-1]
            cover_stored = store_file(ctx.object_store(), selected.path, purpose="covers")
    except FfmpegCommandError as exc:
        raise NodeExecutionError(exc.error_code, "Finished video cover extraction failed.") from exc
    cover_artifact = ctx.artifact(
        ArtifactKind.cover_image,
        None,
        "uri-only",
        uri=cover_stored.ref.uri,
        sha256=cover_stored.sha256,
        media_info=selected.media_info,
    )
    finished = FinishedVideo(
        id=new_id("fv"),
        case_id=state.request.case_id,
        run_id=run.id,
        title=state.request.title or script.title,
        video_artifact=repository.artifact_ref(video_artifact.id),
        cover_artifact=repository.artifact_ref(cover_artifact.id),
        subtitle_artifact=(
            repository.artifact_ref(state.artifacts[ArtifactKind.subtitle_ass].id)
            if ArtifactKind.subtitle_ass in state.artifacts
            else None
        ),
        duration_sec=float(final.media_info.duration_sec if final.media_info and final.media_info.duration_sec else 0),
    )
    repository.finished_videos[finished.id] = finished
    video_version = VideoVersion(
        id=new_id("vv"),
        case_id=state.request.case_id,
        script_version_id=script.id,
        finished_video_id=finished.id,
        timeline_plan_artifact_id=timeline.id,
        style_plan_artifact_id=style.id,
    )
    repository.video_versions[video_version.id] = video_version
    package = repository.create_publish_package_from_finished_video(
        finished,
        title=finished.title,
        description=state.request.publish_content,
    )
    repository.create_event(
        "workflow.finished_video.created",
        "run",
        run.id,
        {"finished_video_id": finished.id, "publish_package_id": package.id},
        dedupe_key=f"finished_video:{finished.id}",
        event_type="artifact_created",
        node_id=node_run.node_id,
        status=NodeStatus.running.value,
        message=f"Finished video {finished.id} created.",
    )
    repository.record_yield_funnel_event(
        job_id=run.job_id,
        run_id=run.id,
        finished_video_id=finished.id,
        publish_package_id=package.id,
        event_type="finished_video_created",
        dedupe_key=f"{finished.id}:finished_video_created",
        event_time=finished.created_at,
    )
    package_artifact = ctx.artifact(
        ArtifactKind.publish_package,
        package.model_dump(mode="json"),
        "PublishPackageArtifact.v1",
    )
    return NodeOutput(artifacts=[video_artifact, cover_artifact, package_artifact])
