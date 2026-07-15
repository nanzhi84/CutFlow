"""NarrationAlignment node: ASR-aligned (or estimated) narration units."""

from __future__ import annotations

from packages.ai.gateway import ProviderCall
from packages.core.contracts import (
    ArtifactKind,
    DegradationNotice,
    ErrorCode,
    SpeechSegmentTiming,
    SpeechTiming,
    SpeechTokenTiming,
    WarningCode,
)
from packages.core.contracts.artifacts import (
    AlignmentArtifact,
    AlignmentSegment,
    NarrationUnit,
    NarrationUnitsArtifact,
    RawSpeechAlignmentArtifact,
)
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.planning.editing import build_narration_units
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._provider_recovery import reject_unrecoverable_provider_error
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline._speech_timing import (
    assign_token_ownership,
    estimated_timing_for_script,
    normalize_timing_for_script,
)
from packages.production.pipeline.degradation_policies import ASR_ESTIMATED_FALLBACK_POLICY


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    tts = state.require(ArtifactKind.audio_tts)
    duration = float(
        tts.media_info.duration_sec if tts.media_info and tts.media_info.duration_sec else 1
    )

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
        estimated_tokens = assign_token_ownership(
            estimated_timing_for_script(
                state.request.script,
                duration=duration,
            ).tokens,
            script=state.request.script,
            units=list(units),
        )
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
            tokens=estimated_tokens,
            source="estimated",
            diagnostics={"token_matched": 0, "char_fallback": len(state.request.script)},
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
        tokens: list[SpeechTokenTiming] | None = None,
        diagnostics: dict | None = None,
        provider_invocation_ids: list[str] | None = None,
        warnings: list[WarningCode] | None = None,
        degradations: list[DegradationNotice] | None = None,
    ) -> NodeOutput:
        owned_tokens = assign_token_ownership(
            list(tokens or []),
            script=state.request.script,
            units=list(units),
        )
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
            tokens=owned_tokens,
            source=source,
            diagnostics=diagnostics or {},
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
            warnings=warnings or [],
            degradations=degradations or [],
            provider_invocation_ids=provider_invocation_ids or [],
        )

    def tts_native_timing_degradations() -> list[DegradationNotice]:
        """ASR fallback ran because native TTS timing was absent. Report it only
        when a real TTS provider produced the audio — sandbox TTS never emits
        native timing, so it must not raise a spurious degradation."""
        try:
            tts_profile_id = ctx.tts_provider_profile_id(state.request)
        except NodeExecutionError:
            return []
        if tts_profile_id.startswith("sandbox"):
            return []
        return [
            degradation_notice(
                WarningCode.tts_timing_unavailable,
                "TTS 未返回原生时间戳，已回退 ASR 对齐。",
                node_id=node_run.node_id,
                affects_true_yield=False,
            )
        ]

    # PRIMARY source: durable, provider-neutral TTS timing. Temporal activities
    # rehydrate artifacts, not RunState.scratch, so this is the only cross-node
    # native-timing fact source.
    raw_alignment_artifact = state.artifacts.get(ArtifactKind.audio_alignment_raw)
    raw_alignment = None
    if raw_alignment_artifact is not None:
        try:
            raw_alignment = RawSpeechAlignmentArtifact.model_validate(
                raw_alignment_artifact.payload
            )
        except Exception:
            raw_alignment = None
    if raw_alignment is not None and raw_alignment.audio_artifact_id == tts.id:
        segments, tokens, diagnostics = normalize_timing_for_script(
            raw_alignment.timing,
            script=state.request.script,
            duration=duration,
        )
    else:
        segments, tokens, diagnostics = [], [], {}
    if segments:
        units = ctx.narration_units_from_segments(
            [{"text": item.text, "start": item.start, "end": item.end} for item in segments],
            duration,
            script=state.request.script,
        )
        return alignment_output(
            units,
            source="tts",
            strict=True,
            tokens=tokens,
            diagnostics=diagnostics,
            provider_invocation_ids=(
                [raw_alignment.provider_invocation_id]
                if raw_alignment.provider_invocation_id
                else None
            ),
        )

    asr_profile = ctx.first_available_provider_profile("asr.transcribe")
    if asr_profile is not None and tts.uri:
        audio_url = ctx.object_store().signed_url(tts.uri).url
        idempotency = ctx.provider_call_idempotency(
            logical_call_slot="asr", provider_profile_id=asr_profile.id
        )
        invocation, result = ctx.provider_gateway.invoke(
            ProviderCall(
                case_id=run.case_id,
                run_id=run.id,
                node_run_id=node_run.id,
                provider_profile_id=asr_profile.id,
                capability_id="asr.transcribe",
                input={"audio_uri": audio_url, "language_hints": ["zh"]},
                idempotency_key=idempotency.key,
                fallback_idempotency_keys=idempotency.fallback_keys,
            )
        )
        if result is None or invocation.error:
            reject_unrecoverable_provider_error(invocation)
            if not state.request.strictness.strict_timestamps:
                error_code = (
                    invocation.error.code.value
                    if invocation.error and hasattr(invocation.error.code, "value")
                    else str(
                        invocation.error.code
                        if invocation.error
                        else ErrorCode.provider_remote_failed.value
                    )
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
        timing = _timing_from_provider_output(result.output)
        segments, tokens, diagnostics = normalize_timing_for_script(
            timing,
            script=state.request.script,
            duration=duration,
        )
        if not segments:
            if state.request.strictness.strict_timestamps:
                raise NodeExecutionError(
                    ErrorCode.render_invalid_timeline,
                    "ASR returned no valid timestamp segments.",
                )
            return estimated_output(
                provider_invocation_ids=[invocation.id],
                warnings=[WarningCode.timestamp_estimated],
                degradations=[_estimated_degradation(ctx, "asr_timing_invalid", invocation.id)],
            )
        units = ctx.narration_units_from_segments(
            [{"text": item.text, "start": item.start, "end": item.end} for item in segments],
            duration,
            script=state.request.script,
        )
        degradations = tts_native_timing_degradations()
        return alignment_output(
            units,
            source="asr",
            strict=True,
            tokens=tokens,
            diagnostics=diagnostics,
            provider_invocation_ids=[invocation.id],
            warnings=[WarningCode.tts_timing_unavailable] if degradations else None,
            degradations=degradations or None,
        )
    if state.request.strictness.strict_timestamps:
        raise NodeExecutionError(
            ErrorCode.render_invalid_timeline,
            "Estimated narration timestamps are not allowed in strict alignment mode.",
        )
    return estimated_output(
        warnings=[WarningCode.timestamp_estimated],
        degradations=[_estimated_degradation(ctx, "asr_unavailable", None)],
    )


def _timing_from_provider_output(output: dict) -> SpeechTiming:
    raw_timing = output.get("timing")
    if isinstance(raw_timing, dict):
        try:
            return SpeechTiming.model_validate(raw_timing)
        except Exception:
            pass
    segments: list[SpeechSegmentTiming] = []
    for item in output.get("segments") or []:
        if not isinstance(item, dict):
            continue
        try:
            segments.append(
                SpeechSegmentTiming(
                    text=str(item.get("text") or ""),
                    start=float(item.get("start") or 0.0),
                    end=float(item.get("end") or 0.0),
                )
            )
        except Exception:
            continue
    return SpeechTiming(segments=segments, granularity="segment", text_basis="normalized")


def _estimated_degradation(
    ctx: NodeContext,
    reason: str,
    provider_invocation_id: str | None,
) -> DegradationNotice:
    details = {"reason": reason}
    if provider_invocation_id:
        details["provider_invocation_id"] = provider_invocation_id
    return DegradationNotice(
        code=WarningCode.timestamp_estimated,
        message="Precise speech timing unavailable; estimated timestamps used.",
        node_id=ctx.node_run.node_id,
        policy_id=ASR_ESTIMATED_FALLBACK_POLICY.id,
        details=details,
    )
