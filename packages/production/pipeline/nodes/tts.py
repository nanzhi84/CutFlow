"""TTS node: synthesize narration audio (provider or sandbox fallback)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.ai.gateway import ProviderCall
from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.media.assets import store_file
from packages.media.audio import synthesize_sandbox_tts
from packages.media.video.ffmpeg import FfmpegCommandError, probe_media
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    voice_id = state.request.voice.voice_id or "voice_sandbox"
    provider_profile_id = ctx.tts_provider_profile_id(state.request)
    invocation, result = ctx.provider_gateway.invoke(
        ProviderCall(
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id,
            provider_profile_id=provider_profile_id,
            capability_id="tts.speech",
            input={"text": state.request.script, "voice_id": voice_id},
        )
    )
    if result is None or invocation.error:
        raise NodeExecutionError(
            invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
            invocation.error.message if invocation.error else "TTS provider failed.",
            retryable=True,
        )
    provider_artifact_id = result.output.get("audio_artifact_id")
    if isinstance(provider_artifact_id, str) and provider_artifact_id in ctx.repository.artifacts:
        return NodeOutput(
            artifacts=[ctx.repository.artifacts[provider_artifact_id]],
            provider_invocation_ids=[invocation.id],
        )
    object_store = ctx.object_store()
    try:
        with tempfile.TemporaryDirectory(prefix="cutagent-tts-") as directory:
            wav_path = Path(directory) / f"{run.id}_tts.wav"
            synthesize_sandbox_tts(
                state.request.script,
                wav_path,
                speed=state.request.voice.speed,
                volume=state.request.voice.volume,
            )
            media_info = probe_media(wav_path)
            stored = store_file(object_store, wav_path, purpose="generated-audio")
    except FfmpegCommandError as exc:
        raise NodeExecutionError(exc.error_code, "Sandbox TTS audio generation failed.") from exc
    artifact = ctx.artifact(
        ArtifactKind.audio_tts,
        None,
        "uri-only",
        uri=stored.ref.uri,
        sha256=stored.sha256,
        media_info=media_info,
    )
    return NodeOutput(artifacts=[artifact], provider_invocation_ids=[invocation.id])
