"""LipSync node: drive the lipsync provider (or pass the portrait through)."""

from __future__ import annotations

from packages.ai.gateway import ProviderCall
from packages.core.contracts import ArtifactKind, ErrorCode, NodeStatus
from packages.core.contracts.artifacts import LipSyncReportArtifact
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._node_context import NodeContext


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
    invocation, result = ctx.provider_gateway.invoke(
        ProviderCall(
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id,
            provider_profile_id=state.request.lipsync.provider_profile_id,
            capability_id="lipsync.video",
            input={"portrait_uri": portrait.uri or "", "audio_uri": audio.uri or "", "duration_sec": duration},
        )
    )
    if result is None or invocation.error:
        raise NodeExecutionError(
            invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
            invocation.error.message if invocation.error else "LipSync provider failed.",
            retryable=True,
        )
    provider_artifact_id = result.output.get("video_artifact_id")
    if isinstance(provider_artifact_id, str) and provider_artifact_id in ctx.repository.artifacts:
        artifact = ctx.repository.artifacts[provider_artifact_id]
        report = ctx.artifact(
            ArtifactKind.lipsync_report,
            LipSyncReportArtifact(
                provider_invocation_id=invocation.id,
                provider_profile_id=state.request.lipsync.provider_profile_id,
                input_video_artifact_id=portrait.id,
                input_audio_artifact_id=audio.id,
                output_video_artifact_id=artifact.id,
            ).model_dump(mode="json"),
            "LipSyncReportArtifact.v1",
        )
        return NodeOutput(artifacts=[artifact, report], provider_invocation_ids=[invocation.id])
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
