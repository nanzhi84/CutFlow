"""ExportFinishedVideo node: persist finished video, cover, and publish package.

The cover is frame-based by default (extract a representative thumbnail — no
fabrication, no spend). When the request opts into ``cover.mode == "ai"`` AND a
real ``image.generate`` ProviderProfile + active secret exist, the PAID AI cover
is generated through the gateway instead. Without that configuration the AI path
is never reached and the existing frame-based cover runs unchanged (emitting a
``cover_frame_fallback`` degradation only when AI was requested but unavailable).
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from packages.ai.gateway import ProviderCall
from packages.ai.prompts.registry import PromptRegistry
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
from packages.core.observability import record_funnel_event
from packages.production.finished_video_numbering import next_finished_video_number
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.degradation_policies import COVER_FALLBACK_POLICY

COVER_PROMPT_VERSION_ID = "prompt_cover_ai_cover_v1"
# Spec §10.1: the AI-cover prompt must resolve through the Prompt Registry binding
# (PublishCover.ai_cover), not be looked up by a hardcoded version id. We still
# tolerate a missing binding (degraded environments / bare test adapters) by
# falling back to the canonical seeded version so the cover never hard-fails on
# prompt resolution alone.
AI_COVER_NODE_ID = "PublishCover.ai_cover"


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    repository = ctx.repository
    final = state.require(ArtifactKind.video_final)
    timeline = state.require(ArtifactKind.plan_timeline)
    style = state.require(ArtifactKind.plan_style)
    script = _resolve_script_version(state, repository)
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
    lipsync_provider_id, lipsync_fallback_used, lipsync_fallback_reason = _resolve_lipsync_attribution(ctx)
    finished = FinishedVideo(
        id=new_id("fv"),
        case_id=state.request.case_id,
        run_id=run.id,
        title=state.request.title or script.title,
        video_number=_next_video_number(repository, state.request.case_id),
        video_artifact=repository.artifact_ref(video_artifact.id),
        cover_artifact=repository.artifact_ref(cover_artifact.id),
        subtitle_artifact=(
            repository.artifact_ref(state.artifacts[ArtifactKind.subtitle_ass].id)
            if ArtifactKind.subtitle_ass in state.artifacts
            else None
        ),
        duration_sec=float(final.media_info.duration_sec if final.media_info and final.media_info.duration_sec else 0),
        lipsync_provider_id=lipsync_provider_id,
        lipsync_fallback_used=lipsync_fallback_used,
        lipsync_fallback_reason=lipsync_fallback_reason,
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
    record_funnel_event(
        repository,
        event_type="finished_video_created",
        job_id=run.job_id,
        run_id=run.id,
        finished_video_id=finished.id,
        publish_package_id=package.id,
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


def _next_video_number(repository, case_id: str) -> str:
    return next_finished_video_number(
        video.video_number for video in repository.finished_videos.values() if video.case_id == case_id
    )


def _resolve_script_version(state, repository) -> ScriptVersion:
    """Link the adopted ScriptVersion for this run.

    When the request references an existing adopted ScriptVersion (e.g. one created
    by adopting a Case Agent draft) we REUSE that row so its provenance
    (``adopted_from_draft_id``, original creative-intent link) survives and it is no
    longer orphaned. Only when no matching ScriptVersion is loaded do we fabricate a
    fresh one under the requested (or a new) id.
    """
    requested_id = state.request.script_version_id
    if requested_id:
        existing = repository.scripts.get(requested_id)
        if existing is not None and existing.case_id == state.request.case_id:
            return existing
    creative_intent_artifact_id = (
        state.artifacts.get(ArtifactKind.creative_intent).id
        if ArtifactKind.creative_intent in state.artifacts
        else None
    )
    return ScriptVersion(
        id=requested_id or new_id("script"),
        case_id=state.request.case_id,
        title=state.request.title or "未命名成片",
        script=state.request.script,
        creative_intent_artifact_id=creative_intent_artifact_id,
    )


def _resolve_lipsync_attribution(ctx: NodeContext) -> tuple[str | None, bool, str | None]:
    """Resolve which lipsync provider produced this finished video from the run's
    LipSyncReportArtifact (HeyGem-primary → VideoReTalk-fallback).

    Returns ``(provider_id, fallback_used, fallback_reason)``. Falls back to
    ``(None, False, None)`` whenever attribution cannot be established honestly:
    no report (lipsync absent in this template), the report is ``skipped`` (request
    disabled / sandbox pass-through), no ``provider_profile_id`` recorded, or the
    profile is no longer resolvable in the repository. ``provider_id`` is the
    ProviderProfile.provider_id (e.g. ``runninghub.heygem`` / ``dashscope.videoretalk``),
    NOT the profile id, so the UI can map it to a stable, profile-agnostic label."""
    report = ctx.state.artifacts.get(ArtifactKind.lipsync_report)
    payload = report.payload if report is not None else None
    if not isinstance(payload, dict):
        return None, False, None
    # A skipped report means no real lipsync provider produced the video (request
    # disabled / sandbox pass-through) -> no attribution to surface.
    if payload.get("skipped"):
        return None, False, None
    profile_id = payload.get("provider_profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        return None, False, None
    profile = ctx.repository.provider_profiles.get(profile_id)
    if profile is None:
        return None, False, None
    fallback_used = bool(payload.get("fallback_from"))
    fallback_reason = payload.get("fallback_reason") if fallback_used else None
    return profile.provider_id, fallback_used, fallback_reason


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
            DegradationNotice(
                code=WarningCode.cover_frame_fallback,
                message="AI cover unavailable; used frame-based cover.",
                node_id=ctx.node_run.node_id,
                policy_id=COVER_FALLBACK_POLICY.id,
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


def _resolve_cover_prompt_version_id(ctx: NodeContext) -> str | None:
    """Resolve the AI-cover prompt version through the registry binding (Spec §10.1).

    Returns the bound, published version id when ``PublishCover.ai_cover`` is bound;
    otherwise falls back to the canonical seeded version id if that version exists,
    else ``None`` (no usable prompt -> caller renders with the in-code default).

    Resolution is driven off the repository's bindings (via PromptRegistry) so the
    node no longer looks the version up by a hardcoded id, satisfying the
    no-hardcoded-prod-prompt rule regardless of how the runtime adapter is wired."""
    registry = getattr(ctx.adapter, "prompt_registry", None) or PromptRegistry(ctx.repository)
    try:
        _binding, version = registry.resolve_published_version(
            node_id=AI_COVER_NODE_ID,
            case_id=ctx.run.case_id,
        )
        return version.id
    except NodeExecutionError:
        pass
    if COVER_PROMPT_VERSION_ID in ctx.repository.prompt_versions:
        return COVER_PROMPT_VERSION_ID
    return None


def _generate_ai_cover(ctx: NodeContext, profile_id: str) -> tuple[Artifact | None, str | None]:
    """Generate the AI cover via the gateway. Returns ``(None, None)`` on any
    provider failure so the caller can fall back to the frame cover.

    When the request references an uploaded ``cover_template`` MediaAsset
    (``cover.reference_asset_id``), its image conditions the cover: ``has_template``
    flips the prompt to follow the reference style/layout and the template bytes are
    forwarded to the image-edit reference path so the uploaded style actually takes
    effect (mirrors the origin ``generate_publish_cover(template_id=...)``)."""
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    template = _resolve_cover_template(ctx)
    version_id = _resolve_cover_prompt_version_id(ctx)
    version = ctx.repository.prompt_versions.get(version_id) if version_id is not None else None
    prompt = build_cover_prompt(
        CoverPromptInputs(
            title=state.request.title or "",
            description=state.request.publish_content,
            case_name=state.request.case_id,
            has_template=template is not None,
        ),
        template=version.content if version is not None else None,
    )
    call_input: dict = {"prompt": prompt}
    if template is not None:
        call_input["template_image_b64"] = template[0]
        call_input["template_filename"] = template[1]
    invocation, result = ctx.provider_gateway.invoke(
        ProviderCall(
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id,
            provider_profile_id=profile_id,
            capability_id="image.generate",
            prompt_version_id=version.id if version is not None else None,
            input=call_input,
            idempotency_key=f"cover-{run.id}",
        )
    )
    if result is None or invocation.error:
        return None, None
    artifact_id = result.output.get("cover_artifact_id")
    if not isinstance(artifact_id, str) or artifact_id not in ctx.repository.artifacts:
        return None, None
    return ctx.repository.artifacts[artifact_id], invocation.id


def _resolve_cover_template(ctx: NodeContext) -> tuple[str, str] | None:
    """Return ``(base64_image, filename)`` for the requested cover-template asset, or
    ``None`` when no reference is set or it cannot be resolved (the cover then falls
    back to template-free generation rather than failing the whole node)."""
    asset_id = ctx.state.request.cover.reference_asset_id
    if not asset_id:
        return None
    try:
        artifact = ctx.source_artifact_for_asset(asset_id)
        path = ctx.artifact_path(artifact)
        data = path.read_bytes()
    except (NodeExecutionError, FileNotFoundError, OSError):
        return None
    if not data:
        return None
    filename = Path(path).name or "cover-template.jpg"
    return base64.b64encode(data).decode("ascii"), filename
