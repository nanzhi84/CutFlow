from __future__ import annotations

from packages.ai.gateway import ProviderCall
from packages.core.config.settings import sandbox_fallback_allowed
from packages.core.contracts import ArtifactKind, ErrorCode, NodeStatus, WarningCode
from packages.core.contracts.artifacts import LipSyncReportArtifact
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    portrait = state.require(ArtifactKind.video_portrait_track)
    audio = state.require(ArtifactKind.audio_tts)
    duration = float(audio.media_info.duration_sec if audio.media_info and audio.media_info.duration_sec else 0)

    if not state.request.lipsync.enabled:
        artifact = ctx.artifact(
            ArtifactKind.video_lipsync,
            None,
            "uri-only",
            uri=portrait.uri,
            sha256=portrait.sha256,
            media_info=portrait.media_info,
        )
        report = ctx.artifact(
            ArtifactKind.lipsync_report,
            LipSyncReportArtifact(
                skipped=True,
                skipped_reason="request.disabled",
                input_video_artifact_id=portrait.id,
                input_audio_artifact_id=audio.id,
                output_video_artifact_id=artifact.id,
            ).model_dump(mode="json"),
            "LipSyncReportArtifact.v1",
        )
        return NodeOutput(status=NodeStatus.skipped, artifacts=[artifact, report])

    profile, is_real = ctx.resolve_lipsync_profile(state.request)

    def invoke(profile_id: str):
        return ctx.provider_gateway.invoke(
            ProviderCall(
                case_id=run.case_id,
                run_id=run.id,
                node_run_id=node_run.id,
                provider_profile_id=profile_id,
                capability_id="lipsync.video",
                input={"portrait_uri": portrait.uri or "", "audio_uri": audio.uri or "", "duration_sec": duration},
                idempotency_key=f"{run.id}:{node_run.id}:lipsync:{profile_id}",
            )
        )

    def success_output(invocation, result, *, used_profile, fallback_from=None, fallback_reason=None) -> NodeOutput:
        artifact = ctx.repository.artifacts[result.output["video_artifact_id"]]
        report = ctx.artifact(
            ArtifactKind.lipsync_report,
            LipSyncReportArtifact(
                provider_invocation_id=invocation.id,
                provider_profile_id=used_profile.id,
                input_video_artifact_id=portrait.id,
                input_audio_artifact_id=audio.id,
                output_video_artifact_id=artifact.id,
                fallback_from=fallback_from,
                fallback_to=used_profile.id if fallback_from else None,
                fallback_reason=fallback_reason,
            ).model_dump(mode="json"),
            "LipSyncReportArtifact.v1",
        )
        # No silent fallback: when the primary lipsync provider failed and a
        # fallback produced the video, surface the primary failure reason as a
        # node-level degradation (shown inline in the run detail timeline), not
        # just on the finished-video badge tooltip.
        if fallback_from:
            return NodeOutput(
                status=NodeStatus.degraded,
                artifacts=[artifact, report],
                provider_invocation_ids=[invocation.id],
                warnings=[WarningCode.lipsync_fallback_used],
                degradations=[
                    degradation_notice(
                        WarningCode.lipsync_fallback_used,
                        f"主口型供应商（{fallback_from}）失败，已由兜底供应商（{used_profile.id}）生成。"
                        f"失败原因：{fallback_reason or '未知错误'}",
                        node_id=node_run.node_id,
                    )
                ],
            )
        return NodeOutput(artifacts=[artifact, report], provider_invocation_ids=[invocation.id])

    if not is_real:
        if not sandbox_fallback_allowed():
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                "未配置可用的真实唇形同步（LipSync）供应商。请在「设置」中配置并启用真实 LipSync 供应商及密钥。",
            )
        return _sandbox_passthrough(ctx, state, portrait, audio, invoke)

    invocation, result = invoke(profile.id)
    if result is not None and not invocation.error and _has_video_artifact(ctx, result):
        return success_output(invocation, result, used_profile=profile)

    primary_error = invocation.error.message if invocation.error else "LipSync provider failed."
    fallback_profile = ctx.select_lipsync_fallback_profile(profile, primary_error)
    if fallback_profile is None:
        raise NodeExecutionError(
            invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
            primary_error,
            retryable=True,
        )
    fb_invocation, fb_result = invoke(fallback_profile.id)
    if fb_result is not None and not fb_invocation.error and _has_video_artifact(ctx, fb_result):
        return success_output(
            fb_invocation,
            fb_result,
            used_profile=fallback_profile,
            fallback_from=profile.id,
            fallback_reason=primary_error,
        )
    raise NodeExecutionError(
        fb_invocation.error.code if fb_invocation.error else ErrorCode.provider_remote_failed,
        f"LipSync fallback to {fallback_profile.id} failed: "
        f"{fb_invocation.error.message if fb_invocation.error else 'unknown error'}",
        retryable=True,
    )


def _has_video_artifact(ctx: NodeContext, result) -> bool:
    artifact_id = result.output.get("video_artifact_id")
    return isinstance(artifact_id, str) and artifact_id in ctx.repository.artifacts


def _sandbox_passthrough(ctx: NodeContext, state, portrait, audio, invoke) -> NodeOutput:
    invocation, result = invoke(state.request.lipsync.provider_profile_id)
    if result is None or invocation.error:
        raise NodeExecutionError(
            invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
            invocation.error.message if invocation.error else "LipSync provider failed.",
            retryable=True,
        )
    artifact = ctx.artifact(
        ArtifactKind.video_lipsync,
        None,
        "uri-only",
        uri=portrait.uri,
        sha256=portrait.sha256,
        media_info=portrait.media_info,
    )
    report = ctx.artifact(
        ArtifactKind.lipsync_report,
        LipSyncReportArtifact(
            provider_invocation_id=invocation.id,
            provider_profile_id=state.request.lipsync.provider_profile_id,
            skipped=True,
            skipped_reason="sandbox.pass_through",
            input_video_artifact_id=portrait.id,
            input_audio_artifact_id=audio.id,
            output_video_artifact_id=artifact.id,
            warnings=["sandbox_lipsync_passthrough"],
        ).model_dump(mode="json"),
        "LipSyncReportArtifact.v1",
    )
    return NodeOutput(artifacts=[artifact, report], provider_invocation_ids=[invocation.id])
