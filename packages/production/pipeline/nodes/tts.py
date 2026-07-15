"""TTS node: synthesize narration audio (provider or sandbox fallback)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from packages.ai.gateway import ProviderCall
from packages.core.contracts import ArtifactKind, ErrorCode, TtsSpeechOutput
from packages.core.contracts.artifacts import RawSpeechAlignmentArtifact
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
    # ``tts_provider_profile_id`` only returns a real profile id when an enabled
    # real ProviderProfile + active secret exist; otherwise it returns
    # ``sandbox.tts.default``. So a non-sandbox id here means the real path is
    # active — request TTS-native subtitles to feed precise forced alignment.
    is_real_profile = provider_profile_id != "sandbox.tts.default"
    call_input: dict = {"text": state.request.script, "voice_id": voice_id}
    if is_real_profile:
        call_input.update(
            {
                "speed": state.request.voice.speed,
                "volume": state.request.voice.volume,
                "emotion": state.request.voice.emotion,
                "subtitle": True,
            }
        )
    idempotency = ctx.provider_call_idempotency(
        # Delivery semantics changed again from legacy synchronous v1 to an async
        # provider-authored full MP3 with native timing. Keep a distinct durable
        # identity so a resumed run cannot replay the superseded paid result.
        logical_call_slot="tts:full-script-single-file:v2",
        provider_profile_id=provider_profile_id,
    )
    invocation, result = ctx.provider_gateway.invoke(
        ProviderCall(
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id,
            provider_profile_id=provider_profile_id,
            capability_id="tts.speech",
            input=call_input,
            idempotency_key=idempotency.key,
            fallback_idempotency_keys=idempotency.fallback_keys,
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
        speech_output = TtsSpeechOutput.model_validate(result.output)
        artifacts = [ctx.repository.artifacts[provider_artifact_id]]
        if speech_output.timing is not None:
            raw_alignment = RawSpeechAlignmentArtifact(
                audio_artifact_id=provider_artifact_id,
                timing=speech_output.timing,
                provider_invocation_id=invocation.id,
            )
            artifacts.append(
                ctx.artifact(
                    ArtifactKind.audio_alignment_raw,
                    raw_alignment.model_dump(mode="json"),
                    "RawSpeechAlignmentArtifact.v1",
                )
            )
        return NodeOutput(
            artifacts=artifacts,
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
