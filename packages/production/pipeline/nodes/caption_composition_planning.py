"""CaptionCompositionPlanning: fixed caption band plus inline emphasis runs."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import ValidationError

from packages.core.contracts import (
    ArtifactKind,
    ErrorCode,
    NodeStatus,
    WarningCode,
)
from packages.core.contracts.artifacts import (
    AlignmentArtifact,
    CaptionBand,
    NarrationUnitsArtifact,
    TimelinePlanArtifact,
)
from packages.core.contracts.caption_policy import (
    CAPTION_ANCHOR_X,
    CAPTION_BASELINE_Y,
    CAPTION_LINE_HEIGHT_RATIO,
    CAPTION_MAX_WIDTH_RATIO,
)
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._caption_composition import build_caption_composition
from packages.production.pipeline._font_metrics import (
    font_text_safety_issue,
    load_font_metrics,
    make_text_measurer,
)
from packages.production.pipeline._fonts import (
    DEFAULT_FONT_SENTINEL,
    caption_font_asset_ids,
    distinct_font_assets_have_ambiguous_ass_style,
    is_font_collection,
    resolve_font_asset,
)
from packages.production.pipeline._materialize import (
    _subtitle_emphasis_font_size,
    _subtitle_font_size,
    _subtitle_position,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import degradation_notice
from packages.production.pipeline._speech_timing import assign_token_ownership
from packages.production.pipeline._subtitles import ass_font_size
from packages.production.pipeline.nodes._creative_intent import load_creative_intent


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    try:
        timeline = TimelinePlanArtifact.model_validate(
            state.require(ArtifactKind.plan_timeline).payload
        )
        narration = NarrationUnitsArtifact.model_validate(
            state.require(ArtifactKind.narration_units).payload
        )
        alignment = AlignmentArtifact.model_validate(
            state.require(ArtifactKind.audio_alignment).payload
        )
    except ValidationError as exc:
        raise NodeExecutionError(
            ErrorCode.artifact_schema_mismatch,
            f"Caption planning input artifact is invalid: {exc}",
            retryable=False,
        ) from exc
    units = narration.units
    tokens = assign_token_ownership(
        alignment.tokens,
        script=state.request.script,
        units=units,
    )
    intent = load_creative_intent(state)
    width = int(state.request.output.width)
    height = int(state.request.output.height)
    fps = max(1, timeline.fps)
    total_frames = max(1, timeline.total_frames)
    normal_enabled = bool(state.request.subtitle.enabled and state.request.subtitle.normal_enabled)
    emphasis_enabled = bool(normal_enabled and state.request.subtitle.emphasis_enabled)

    requested_normal = state.request.subtitle.font_id
    requested_emphasis = state.request.subtitle.emphasis_font_id
    normal_font_id, emphasis_font_id = caption_font_asset_ids(
        requested_normal,
        requested_emphasis,
    )
    requested_size = _subtitle_font_size(
        state.request.subtitle.style_preset,
        state.request.subtitle.font_size,
    )
    requested_emphasis_size = _subtitle_emphasis_font_size(
        state.request.subtitle.emphasis_font_size,
        requested_size,
    )
    normal_font_size = ass_font_size(requested_size, height=height)
    emphasis_font_size = ass_font_size(requested_emphasis_size, height=height)
    position = _subtitle_position(
        state.request.subtitle.style_preset,
        state.request.subtitle.position,
    )
    band = CaptionBand(
        anchor_x=float(position.get("x", CAPTION_ANCHOR_X)),
        baseline_y=float(position.get("y", CAPTION_BASELINE_Y)),
        line_height_ratio=CAPTION_LINE_HEIGHT_RATIO,
        max_width_ratio=CAPTION_MAX_WIDTH_RATIO,
    )

    with tempfile.TemporaryDirectory(prefix="cutagent-caption-composition-") as directory:
        runtime_dir = Path(directory) / "fonts"
        normal_font = None
        emphasis_font = None
        normal_metrics = None
        emphasis_metrics = None
        if normal_enabled:
            normal_font = _resolve_required_font(
                ctx,
                font_asset_id=normal_font_id,
                runtime_dir=runtime_dir,
                label="普通字幕",
                defaulted=not requested_normal or requested_normal == DEFAULT_FONT_SENTINEL,
            )
            normal_metrics = load_font_metrics(normal_font.source_path)
            if normal_metrics is None:
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    f"无法读取字幕字体（{normal_font_id}）的字形度量。",
                    retryable=False,
                )
            issue = font_text_safety_issue(normal_font.source_path, [state.request.script])
            if issue:
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    f"字幕字体（{normal_font_id}）无法安全覆盖当前文本（{issue}）。",
                    retryable=False,
                )
        if emphasis_enabled:
            if emphasis_font_id == normal_font_id:
                emphasis_font = normal_font
                emphasis_metrics = normal_metrics
            else:
                emphasis_font = _resolve_required_font(
                    ctx,
                    font_asset_id=emphasis_font_id,
                    runtime_dir=runtime_dir,
                    label="强调字幕",
                    defaulted=not requested_emphasis
                    or requested_emphasis == DEFAULT_FONT_SENTINEL,
                )
                emphasis_metrics = load_font_metrics(emphasis_font.source_path)
                if emphasis_metrics is None:
                    raise NodeExecutionError(
                        ErrorCode.render_subtitle_failed,
                        f"无法读取强调字幕字体（{emphasis_font_id}）的字形度量。",
                        retryable=False,
                    )
            if distinct_font_assets_have_ambiguous_ass_style(
                normal_font_id,
                normal_font,
                emphasis_font_id,
                emphasis_font,
            ):
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    "普通字幕与强调字幕选择了同一字体家族中无法由 ASS 区分的两个字重；"
                    "请选择常规与粗体组合，或使用同一个字体资产。",
                    retryable=False,
                )
        normal_measure, normal_source = make_text_measurer(normal_metrics, normal_font_size)
        emphasis_measure, emphasis_source = make_text_measurer(
            emphasis_metrics or normal_metrics,
            emphasis_font_size,
        )
        plan = build_caption_composition(
            script=state.request.script,
            units=units,
            tokens=tokens,
            hints=list(intent.emphasis),
            fps=fps,
            total_frames=total_frames,
            width=width,
            height=height,
            band=band,
            normal_enabled=normal_enabled,
            emphasis_enabled=emphasis_enabled,
            normal_font_asset_id=normal_font_id if normal_enabled else None,
            emphasis_font_asset_id=emphasis_font_id if emphasis_enabled else None,
            normal_font_size=normal_font_size,
            emphasis_font_size=emphasis_font_size,
            normal_measure=normal_measure,
            emphasis_measure=emphasis_measure,
            normal_baseline_offset=_baseline_offset(normal_metrics, normal_font_size),
            emphasis_baseline_offset=_baseline_offset(
                emphasis_metrics or normal_metrics,
                emphasis_font_size,
            ),
            timing_source=_timing_source(alignment),
            normal_metrics_source=normal_source,
            emphasis_metrics_source=emphasis_source,
        )
        if plan.diagnostics.units_unmatched:
            fallback_payloads = [
                item.model_dump(mode="json") for item in plan.diagnostics.fallbacks
            ]
            raise NodeExecutionError(
                ErrorCode.render_subtitle_failed,
                "旁白单元无法单调映射回原始脚本，已停止字幕生成以避免静默漏字。",
                retryable=False,
                details={"fallbacks": fallback_payloads},
            )
        if emphasis_font is not None:
            rendered_emphasis_text = [
                run.text
                for cue in plan.cues
                for line in cue.lines
                for run in line.runs
                if run.role == "emphasis"
            ]
            issue = font_text_safety_issue(
                emphasis_font.source_path,
                rendered_emphasis_text,
            )
            if issue:
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    f"强调字幕字体（{emphasis_font_id}）无法安全覆盖实际强调文本（{issue}）。",
                    retryable=False,
                )

    degradations = []
    warnings = []
    if plan.diagnostics.fallbacks:
        fallback_payloads = [
            item.model_dump(mode="json") for item in plan.diagnostics.fallbacks
        ]
        warnings.append(WarningCode.caption_composition_fallback)
        degradations.append(
            degradation_notice(
                WarningCode.caption_composition_fallback,
                "部分强调短语无法可靠映射或在固定字幕带内完整排版，"
                "已确定性降级为普通字幕。",
                node_id=ctx.node_run.node_id,
                affects_true_yield=False,
            ).model_copy(update={"details": {"fallbacks": fallback_payloads}})
        )
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_caption_composition,
                plan.model_dump(mode="json"),
                "CaptionCompositionPlan.v1",
            )
        ],
        warnings=warnings,
        degradations=degradations,
    )


def _resolve_required_font(
    ctx: NodeContext,
    *,
    font_asset_id: str | None,
    runtime_dir: Path,
    label: str,
    defaulted: bool,
):
    resolved, unresolved = resolve_font_asset(
        font_asset_id=font_asset_id,
        runtime_dir=runtime_dir,
        source_artifact_for_asset=ctx.source_artifact_for_asset,
        artifact_path=ctx.artifact_path,
    )
    if unresolved or resolved is None:
        detail = (
            f"默认{label}字体资产缺失；请先执行 python scripts/import_font_assets.py。"
            if defaulted
            else f"指定{label}字体（{unresolved or font_asset_id}）文件无法加载。"
        )
        raise NodeExecutionError(ErrorCode.render_subtitle_failed, detail, retryable=False)
    if is_font_collection(resolved):
        raise NodeExecutionError(
            ErrorCode.render_subtitle_failed,
            f"{label}字体（{font_asset_id}）是多字形 TTC 集合，无法保证字形度量一致。",
            retryable=False,
        )
    return resolved


def _baseline_offset(metrics, font_size: int) -> float:
    if metrics is None or metrics.cell_height <= 0:
        return font_size * 0.8
    return metrics.ascender * font_size / metrics.cell_height


def _timing_source(alignment: AlignmentArtifact) -> str:
    if int(alignment.diagnostics.get("char_fallback") or 0) > 0:
        return "interpolated"
    source = alignment.source
    if source == "tts":
        return "native"
    if source in {"asr", "forced_alignment", "tts_subtitle"}:
        return "asr_anchored"
    return "interpolated"
