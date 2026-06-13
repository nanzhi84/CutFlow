"""NarrationAlignment node: ASR-aligned (or estimated) narration units."""

from __future__ import annotations

import re

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
from packages.production.pipeline._node_context import NodeContext


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
        parts = [part.strip() for part in re.split(r"[。！？.!?；;]+", state.request.script) if part.strip()]
        if not parts:
            parts = [state.request.script]
        weights = [max(1, len([char for char in part if not char.isspace()])) for part in parts]
        total_weight = sum(weights)
        units: list[NarrationUnit] = []
        cursor = 0.0
        for index, (text, weight) in enumerate(zip(parts, weights, strict=True)):
            if index == len(parts) - 1:
                end = duration
            else:
                end = cursor + duration * (weight / total_weight)
            units.append(
                NarrationUnit(
                    unit_id=f"unit_{index + 1}",
                    text=text,
                    start=round(cursor, 3),
                    end=round(end, 3),
                    confidence=0.5,
                )
            )
            cursor = end
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
        units = ctx.narration_units_from_segments(result.output.get("segments", []), duration)
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
            source="asr",
            units=units,
            strict=True,
            warnings=[],
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
            provider_invocation_ids=[invocation.id],
        )
    if state.request.strictness.strict_timestamps:
        raise NodeExecutionError(
            ErrorCode.render_invalid_timeline,
            "Estimated narration timestamps are not allowed in strict alignment mode.",
        )
    return estimated_output()
