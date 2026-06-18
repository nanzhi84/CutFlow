"""NarrationAlignment node: ASR-aligned (or estimated) narration units."""

from __future__ import annotations

from packages.ai.gateway import ProviderCall
from packages.core.contracts import (
    ArtifactKind,
    DegradationNotice,
    ErrorCode,
    WarningCode,
)
from packages.core.contracts.artifacts import (
    AlignmentArtifact,
    AlignmentSegment,
    NarrationUnit,
    NarrationUnitsArtifact,
)
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.planning.editing import build_narration_units
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.degradation_policies import ASR_ESTIMATED_FALLBACK_POLICY


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    tts = state.require(ArtifactKind.audio_tts)
    duration = float(tts.media_info.duration_sec if tts.media_info and tts.media_info.duration_sec else 1)

    def estimated_output(
        *,
        provider_invocation_ids: list[str] | None = None,
        warnings: list[WarningCode] | None = None,
        degradations: list[DegradationNotice] | None = None,
    ) -> NodeOutput:
        units = build_narration_units(
            script=state.request.script,
            asr_segments=None,
            video_duration=duration,
        )
        if not units:
            units = [
                NarrationUnit(
                    unit_id="unit_001",
                    text=state.request.script,
                    start=0,
                    end=round(duration, 3),
                    confidence=0.5,
                )
            ]
        alignment = AlignmentArtifact(
            audio_artifact_id=tts.id,
            segments=[
                AlignmentSegment(
                    text=unit.text,
                    start_sec=unit.start,
                    end_sec=unit.end,
                    word_confidence=unit.confidence,
                )
                for unit in units
            ],
        )
        narration = NarrationUnitsArtifact(
            source="estimated",
            units=units,
            strict=False,
            warnings=[WarningCode.timestamp_estimated.value],
        )
        return NodeOutput(
            artifacts=[
                ctx.artifact(
                    ArtifactKind.audio_alignment,
                    alignment.model_dump(mode="json"),
                    "AlignmentArtifact.v1",
                ),
                ctx.artifact(
                    ArtifactKind.narration_units,
                    narration.model_dump(mode="json"),
                    "NarrationUnitsArtifact.v1",
                ),
            ],
            warnings=warnings or [],
            degradations=degradations or [],
            provider_invocation_ids=provider_invocation_ids or [],
        )

    def alignment_output(
        units: list[NarrationUnit],
        *,
        source: str,
        strict: bool,
        provider_invocation_ids: list[str] | None = None,
    ) -> NodeOutput:
        alignment = AlignmentArtifact(
            audio_artifact_id=tts.id,
            segments=[
                AlignmentSegment(
                    text=unit.text,
                    start_sec=unit.start,
                    end_sec=unit.end,
                    word_confidence=unit.confidence,
                )
                for unit in units
            ],
        )
        narration = NarrationUnitsArtifact(source=source, units=units, strict=strict, warnings=[])
        return NodeOutput(
            artifacts=[
                ctx.artifact(
                    ArtifactKind.audio_alignment,
                    alignment.model_dump(mode="json"),
                    "AlignmentArtifact.v1",
                ),
                ctx.artifact(
                    ArtifactKind.narration_units,
                    narration.model_dump(mode="json"),
                    "NarrationUnitsArtifact.v1",
                ),
            ],
            provider_invocation_ids=provider_invocation_ids or [],
        )

    # PRIMARY source: MiniMax TTS-native subtitle segments (precise per-sentence
    # timing produced alongside the real TTS audio). Only present when the real
    # TTS path ran; with no secret the scratch is empty and we fall through.
    subtitle_segments = state.scratch.get("tts_subtitle_segments")
    if isinstance(subtitle_segments, list) and subtitle_segments:
        units = ctx.narration_units_from_segments(
            subtitle_segments,
            duration,
            script=state.request.script,
        )
        invocation_id = state.scratch.get("tts_subtitle_invocation_id")
        return alignment_output(
            units,
            source="tts_subtitle",
            strict=True,
            provider_invocation_ids=[invocation_id] if isinstance(invocation_id, str) else None,
        )

    asr_profile = ctx.first_available_provider_profile("asr.transcribe")
    if asr_profile is not None and tts.uri:
        audio_url = ctx.object_store().signed_url(tts.uri).url
        invocation, result = ctx.provider_gateway.invoke(
            ProviderCall(
                case_id=run.case_id,
                run_id=run.id,
                node_run_id=node_run.id,
                provider_profile_id=asr_profile.id,
                capability_id="asr.transcribe",
                input={"audio_uri": audio_url, "language_hints": ["zh"]},
            )
        )
        if result is None or invocation.error:
            if not state.request.strictness.strict_timestamps:
                error_code = (
                    invocation.error.code.value
                    if invocation.error and hasattr(invocation.error.code, "value")
                    else str(invocation.error.code if invocation.error else ErrorCode.provider_remote_failed.value)
                )
                degradation = DegradationNotice(
                    code=WarningCode.timestamp_estimated,
                    message="ASR unavailable; estimated narration timestamps used.",
                    node_id=node_run.node_id,
                    policy_id=ASR_ESTIMATED_FALLBACK_POLICY.id,
                    details={
                        "reason": "asr_unavailable_estimated_fallback",
                        "provider_invocation_id": invocation.id,
                        "provider_error_code": error_code,
                    },
                )
                return estimated_output(
                    provider_invocation_ids=[invocation.id],
                    warnings=[WarningCode.timestamp_estimated],
                    degradations=[degradation],
                )
            raise NodeExecutionError(
                invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
                invocation.error.message if invocation.error else "ASR provider failed.",
                retryable=True,
            )
        units = ctx.narration_units_from_segments(
            result.output.get("segments", []),
            duration,
            script=state.request.script,
        )
        return alignment_output(
            units,
            source="asr",
            strict=True,
            provider_invocation_ids=[invocation.id],
        )
    if state.request.strictness.strict_timestamps:
        raise NodeExecutionError(
            ErrorCode.render_invalid_timeline,
            "Estimated narration timestamps are not allowed in strict alignment mode.",
        )
    return estimated_output()
