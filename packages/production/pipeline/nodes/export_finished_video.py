"""ExportFinishedVideo node: persist finished video, cover, and publish package.

The cover is frame-based by default (extract a representative thumbnail — no
fabrication, no spend). When the request opts into ``cover.mode == "ai"`` AND a
real ``image.generate`` ProviderProfile + active secret exist, the PAID AI cover
is generated through the gateway instead. Without that configuration the AI path
is never reached and the existing frame-based cover runs unchanged (emitting a
``cover_frame_fallback`` degradation only when AI was requested but unavailable).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.ai.gateway import ProviderCall
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DegradationNotice,
    FinishedVideo,
    NodeStatus,
    ScriptVersion,
    VideoVersion,
    WarningCode,
)
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.cover import CoverPromptInputs, build_cover_prompt
from packages.media.video.ffmpeg import FfmpegCommandError, extract_thumbnails
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice

COVER_PROMPT_VERSION_ID = "prompt_cover_ai_cover_v1"


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
    cover_artifact, cover_degradations, cover_invocation_ids = _build_cover(ctx, final)
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
    return NodeOutput(
        status=NodeStatus.degraded if cover_degradations else NodeStatus.succeeded,
        artifacts=[video_artifact, cover_artifact, package_artifact],
        degradations=cover_degradations,
        provider_invocation_ids=cover_invocation_ids,
    )


def _build_cover(
    ctx: NodeContext, final: Artifact
) -> tuple[Artifact, list[DegradationNotice], list[str]]:
    """Resolve the cover artifact, gating the PAID AI cover behind a real
    ``image.generate`` profile + active secret. Falls back to the frame-based
    cover (current behavior) whenever AI is unavailable or fails."""
    request = ctx.state.request
    wants_ai = request.cover.mode == "ai"
    profile_id = ctx.image_cover_profile_id(request) if wants_ai else None
    if profile_id is not None:
        ai_cover, invocation_id = _generate_ai_cover(ctx, profile_id)
        if ai_cover is not None:
            return ai_cover, [], [invocation_id] if invocation_id else []
    cover_artifact = _frame_cover(ctx, final)
    degradations: list[DegradationNotice] = []
    if wants_ai:
        # AI cover requested but unavailable/failed -> honest frame fallback.
        degradations.append(
            degradation_notice(
                WarningCode.cover_frame_fallback,
                "AI cover unavailable; used frame-based cover.",
                node_id=ctx.node_run.node_id,
            )
        )
    return cover_artifact, degradations, []


def _frame_cover(ctx: NodeContext, final: Artifact) -> Artifact:
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
    return ctx.artifact(
        ArtifactKind.cover_image,
        None,
        "uri-only",
        uri=cover_stored.ref.uri,
        sha256=cover_stored.sha256,
        media_info=selected.media_info,
    )


def _generate_ai_cover(ctx: NodeContext, profile_id: str) -> tuple[Artifact | None, str | None]:
    """Generate the AI cover via the gateway. Returns ``(None, None)`` on any
    provider failure so the caller can fall back to the frame cover."""
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    version = ctx.repository.prompt_versions.get(COVER_PROMPT_VERSION_ID)
    prompt = build_cover_prompt(
        CoverPromptInputs(
            title=state.request.title or "",
            description=state.request.publish_content,
            case_name=state.request.case_id,
        ),
        template=version.content if version is not None else None,
    )
    invocation, result = ctx.provider_gateway.invoke(
        ProviderCall(
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id,
            provider_profile_id=profile_id,
            capability_id="image.generate",
            prompt_version_id=COVER_PROMPT_VERSION_ID if version is not None else None,
            input={"prompt": prompt},
            idempotency_key=f"cover-{run.id}",
        )
    )
    if result is None or invocation.error:
        return None, None
    artifact_id = result.output.get("cover_artifact_id")
    if not isinstance(artifact_id, str) or artifact_id not in ctx.repository.artifacts:
        return None, None
    return ctx.repository.artifacts[artifact_id], invocation.id
