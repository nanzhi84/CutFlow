"""CaptionCompositionPlanning: fixed caption band plus inline emphasis runs."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
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
    CaptionCompositionPlanArtifact,
    EmphasisHint,
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
    FontMetrics,
    FontTextSafetyReport,
    font_text_safety_report,
    load_font_metrics,
    make_text_measurer,
)
from packages.production.pipeline._emphasis_styles import (
    emphasis_run_vertical_margins,
    emphasis_style_horizontal_padding,
    select_emphasis_styles,
)
from packages.production.pipeline._fonts import (
    DEFAULT_EMPHASIS_FONT_ASSET_ID,
    DEFAULT_FONT_SENTINEL,
    ResolvedFont,
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


@dataclass(frozen=True)
class _HintFontPlan:
    asset_ids: list[str]
    measures_by_hint_id: dict[str, Callable[[str], float]]
    baselines_by_hint_id: dict[str, float]
    overhang_sides_by_asset: dict[str, tuple[float, float]]
    fallbacks_by_hint_id: dict[str, dict[str, object]]


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

    hints = list(intent.emphasis)
    raw_intent = intent.intent or {}
    selected_styles = select_emphasis_styles(
        hints,
        tone=raw_intent.get("tone"),
        bgm_mood=raw_intent.get("bgm_mood"),
        requested_style_id=state.request.subtitle.emphasis_style_id,
    )
    emphasis_style_ids = [style.style_id for style in selected_styles]
    emphasis_effect_ids = [style.effect_id for style in selected_styles]
    explicit_emphasis_font = bool(
        requested_emphasis and requested_emphasis != DEFAULT_FONT_SENTINEL
    )
    hint_requested_font_ids = [
        emphasis_font_id if explicit_emphasis_font else style.font_asset_id
        for style in selected_styles
    ]
    if hint_requested_font_ids:
        emphasis_font_id = hint_requested_font_ids[0]
    if state.request.subtitle.emphasis_font_size is None and selected_styles:
        hint_font_sizes = [
            max(12, int(round(normal_font_size * style.size_ratio)))
            for style in selected_styles
        ]
        emphasis_font_size = max(hint_font_sizes)
    else:
        hint_font_sizes = [emphasis_font_size] * len(hints)
    with tempfile.TemporaryDirectory(prefix="cutagent-caption-composition-") as directory:
        runtime_dir = Path(directory) / "fonts"
        normal_font = None
        emphasis_font = None
        normal_metrics = None
        emphasis_metrics = None
        overhang_sides_by_asset: dict[str, tuple[float, float]] = {}
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
            report = font_text_safety_report(normal_font.source_path, [state.request.script])
            issue = report.blocking_issue()
            if issue:
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    f"字幕字体（{normal_font_id}）无法安全覆盖当前文本（{issue}）。",
                    retryable=False,
                )
            overhang_sides_by_asset[normal_font_id] = _horizontal_overhang_px(
                report,
                normal_font_size,
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
        emphasis_measures_by_asset = {emphasis_font_id: emphasis_measure}
        emphasis_baselines_by_asset = {
            emphasis_font_id: _baseline_offset(
                emphasis_metrics or normal_metrics,
                emphasis_font_size,
            )
        }
        preview_plan = build_caption_composition(
            script=state.request.script,
            units=units,
            tokens=tokens,
            hints=hints,
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
            normal_measure=lambda _text: 0.0,
            emphasis_measure=lambda _text: 0.0,
            normal_baseline_offset=_baseline_offset(normal_metrics, normal_font_size),
            emphasis_baseline_offset=_baseline_offset(
                emphasis_metrics or normal_metrics,
                emphasis_font_size,
            ),
            timing_source=_timing_source(alignment),
            normal_metrics_source=normal_source,
            emphasis_metrics_source=emphasis_source,
            emphasis_font_asset_ids=(
                hint_requested_font_ids if emphasis_enabled else None
            ),
            emphasis_effect_ids=emphasis_effect_ids if emphasis_enabled else None,
            emphasis_style_ids=emphasis_style_ids if emphasis_enabled else None,
            emphasis_font_sizes=hint_font_sizes if emphasis_enabled else None,
            emphasis_requested_font_asset_ids=(
                hint_requested_font_ids if emphasis_enabled else None
            ),
            emphasis_primary_color_override=state.request.subtitle.emphasis_primary_color,
        )
        if emphasis_enabled:
            assert normal_font is not None and normal_metrics is not None
            assert emphasis_font is not None and emphasis_metrics is not None
            hint_font_plan = _plan_hint_fonts(
                ctx,
                hints=hints,
                preview_plan=preview_plan,
                normal_font_id=normal_font_id,
                normal_font=normal_font,
                normal_metrics=normal_metrics,
                emphasis_font_id=emphasis_font_id,
                emphasis_font=emphasis_font,
                emphasis_metrics=emphasis_metrics,
                requested_font_ids=hint_requested_font_ids,
                font_sizes=hint_font_sizes,
                runtime_dir=runtime_dir,
            )
            hint_font_asset_ids: list[str] | None = hint_font_plan.asset_ids
            emphasis_measures_by_hint_id = hint_font_plan.measures_by_hint_id
            emphasis_baselines_by_hint_id = hint_font_plan.baselines_by_hint_id
            font_fallback_candidates = hint_font_plan.fallbacks_by_hint_id
            for asset_id, sides in hint_font_plan.overhang_sides_by_asset.items():
                overhang_sides_by_asset[asset_id] = _merge_overhang_sides(
                    overhang_sides_by_asset.get(asset_id),
                    sides,
                )
        else:
            hint_font_asset_ids = None
            emphasis_measures_by_hint_id = None
            emphasis_baselines_by_hint_id = None
            font_fallback_candidates = {}
        overhang_by_asset = {
            asset_id: round(left + right, 3)
            for asset_id, (left, right) in overhang_sides_by_asset.items()
        }
        left_overhang_by_asset = {
            asset_id: round(left, 3)
            for asset_id, (left, _) in overhang_sides_by_asset.items()
        }
        right_overhang_by_asset = {
            asset_id: round(right, 3)
            for asset_id, (_, right) in overhang_sides_by_asset.items()
        }
        layout_overhang = max(
            (left for left, _ in overhang_sides_by_asset.values()),
            default=0.0,
        ) + max(
            (right for _, right in overhang_sides_by_asset.values()),
            default=0.0,
        )
        layout_horizontal_padding = layout_overhang + emphasis_style_horizontal_padding(
            selected_styles if emphasis_enabled else []
        )
        plan = build_caption_composition(
            script=state.request.script,
            units=units,
            tokens=tokens,
            hints=hints,
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
            emphasis_font_asset_ids=hint_font_asset_ids,
            emphasis_effect_ids=emphasis_effect_ids if emphasis_enabled else None,
            emphasis_style_ids=emphasis_style_ids if emphasis_enabled else None,
            emphasis_font_sizes=hint_font_sizes if emphasis_enabled else None,
            emphasis_requested_font_asset_ids=(
                hint_requested_font_ids if emphasis_enabled else None
            ),
            emphasis_measures_by_asset=emphasis_measures_by_asset,
            emphasis_measures_by_hint_id=emphasis_measures_by_hint_id,
            emphasis_baseline_offsets_by_asset=emphasis_baselines_by_asset,
            emphasis_baseline_offsets_by_hint_id=emphasis_baselines_by_hint_id,
            emphasis_primary_color_override=state.request.subtitle.emphasis_primary_color,
            font_horizontal_overhang_px=overhang_by_asset,
            font_horizontal_left_overhang_px=left_overhang_by_asset,
            font_horizontal_right_overhang_px=right_overhang_by_asset,
            layout_horizontal_overhang_px=layout_horizontal_padding,
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
        max_ink_width = width * band.max_width_ratio
        if any(
            line.advance_px + line.animation_headroom_px + layout_horizontal_padding
            > max_ink_width + 1e-6
            for cue in plan.cues
            for line in cue.lines
        ):
            raise NodeExecutionError(
                ErrorCode.render_subtitle_failed,
                "字幕字形墨迹宽度超过固定字幕带，已停止渲染以避免水平裁切。",
                retryable=False,
            )
        if not _style_runs_fit_canvas(plan):
            raise NodeExecutionError(
                ErrorCode.render_subtitle_failed,
                "花字模板的描边、底衬或入场动画超出画布垂直边界，已停止渲染以避免裁切。",
                retryable=False,
            )
        applied_hint_ids = {
            run.hint_id
            for cue in plan.cues
            for line in cue.lines
            for run in line.runs
            if run.hint_id
        }
        font_fallbacks = [
            details
            for hint_id, details in font_fallback_candidates.items()
            if hint_id in applied_hint_ids
        ]

    degradations = []
    warnings = []
    if font_fallbacks:
        warnings.append(WarningCode.font_glyph_fallback)
        degradations.append(
            degradation_notice(
                WarningCode.font_glyph_fallback,
                "部分强调短语缺少所选字体字形，已逐条回退到默认强调字体。",
                node_id=ctx.node_run.node_id,
                affects_true_yield=False,
            ).model_copy(update={"details": {"fallbacks": font_fallbacks}})
        )
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


def _plan_hint_fonts(
    ctx: NodeContext,
    *,
    hints: list[EmphasisHint],
    preview_plan: CaptionCompositionPlanArtifact,
    normal_font_id: str,
    normal_font: ResolvedFont,
    normal_metrics: FontMetrics,
    emphasis_font_id: str,
    emphasis_font: ResolvedFont,
    emphasis_metrics: FontMetrics,
    requested_font_ids: list[str],
    font_sizes: list[int],
    runtime_dir: Path,
) -> _HintFontPlan:
    if len(requested_font_ids) != len(hints) or len(font_sizes) != len(hints):
        raise ValueError("hint font requests must align with emphasis hints")
    rendered_text_by_hint_id: dict[str, str] = {}
    for cue in preview_plan.cues:
        for line in cue.lines:
            for run in line.runs:
                if run.role == "emphasis" and run.hint_id:
                    rendered_text_by_hint_id[run.hint_id] = (
                        rendered_text_by_hint_id.get(run.hint_id, "") + run.text
                    )

    asset_ids: list[str] = []
    measures_by_hint_id: dict[str, Callable[[str], float]] = {}
    baselines_by_hint_id: dict[str, float] = {}
    overhang_sides_by_asset: dict[str, tuple[float, float]] = {}
    fallbacks_by_hint_id: dict[str, dict[str, object]] = {}
    resolved_fonts = {emphasis_font_id: emphasis_font}
    metrics_by_asset = {emphasis_font_id: emphasis_metrics}
    for hint_index, hint in enumerate(hints):
        hint_id = f"hint_{hint_index + 1:04d}"
        requested_font_id = requested_font_ids[hint_index]
        font_size = font_sizes[hint_index]
        selected_font = resolved_fonts.get(requested_font_id)
        selected_metrics = metrics_by_asset.get(requested_font_id)
        if selected_font is None:
            selected_font = _resolve_required_font(
                ctx,
                font_asset_id=requested_font_id,
                runtime_dir=runtime_dir,
                label="花字模板",
                defaulted=False,
            )
            selected_metrics = load_font_metrics(selected_font.source_path)
            if selected_metrics is None:
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    f"无法读取花字模板字体（{requested_font_id}）的字形度量。",
                    retryable=False,
                )
            if distinct_font_assets_have_ambiguous_ass_style(
                normal_font_id,
                normal_font,
                requested_font_id,
                selected_font,
            ):
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    "普通字幕与花字模板选择了同一字体家族中无法由 ASS 区分的两个字重。",
                    retryable=False,
                )
            resolved_fonts[requested_font_id] = selected_font
            metrics_by_asset[requested_font_id] = selected_metrics
        assert selected_metrics is not None
        safety_text = rendered_text_by_hint_id.get(hint_id)
        if not safety_text:
            asset_ids.append(requested_font_id)
            measure, _ = make_text_measurer(selected_metrics, font_size)
            measures_by_hint_id[hint_id] = measure
            baselines_by_hint_id[hint_id] = _baseline_offset(selected_metrics, font_size)
            continue
        selected_report = font_text_safety_report(selected_font.source_path, [safety_text])
        issue = selected_report.blocking_issue(allow_missing_glyphs=True)
        if issue:
            raise NodeExecutionError(
                ErrorCode.render_subtitle_failed,
                f"强调字幕字体（{requested_font_id}）无法安全覆盖强调文本（{issue}）。",
                retryable=False,
            )
        chosen_font_id = requested_font_id
        chosen_metrics = selected_metrics
        chosen_report = selected_report
        if selected_report.missing_codepoints:
            if requested_font_id == DEFAULT_EMPHASIS_FONT_ASSET_ID:
                missing = selected_report.missing_codepoints[0]
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    f"默认强调字幕字体（{emphasis_font_id}）缺少字形 U+{missing:04X}。",
                    retryable=False,
                )
            fallback_font = resolved_fonts.get(DEFAULT_EMPHASIS_FONT_ASSET_ID)
            fallback_metrics = metrics_by_asset.get(DEFAULT_EMPHASIS_FONT_ASSET_ID)
            if fallback_font is None:
                fallback_font = (
                    normal_font
                    if DEFAULT_EMPHASIS_FONT_ASSET_ID == normal_font_id
                    else _resolve_required_font(
                        ctx,
                        font_asset_id=DEFAULT_EMPHASIS_FONT_ASSET_ID,
                        runtime_dir=runtime_dir,
                        label="缺字回退强调字幕",
                        defaulted=True,
                    )
                )
                fallback_metrics = (
                    normal_metrics
                    if DEFAULT_EMPHASIS_FONT_ASSET_ID == normal_font_id
                    else load_font_metrics(fallback_font.source_path)
                )
                if fallback_metrics is None:
                    raise NodeExecutionError(
                        ErrorCode.render_subtitle_failed,
                        f"无法读取缺字回退字体（{DEFAULT_EMPHASIS_FONT_ASSET_ID}）的字形度量。",
                        retryable=False,
                    )
                _validate_fallback_ass_style(
                    normal_font_id=normal_font_id,
                    normal_font=normal_font,
                    emphasis_font_id=requested_font_id,
                    emphasis_font=selected_font,
                    fallback_font=fallback_font,
                )
                resolved_fonts[DEFAULT_EMPHASIS_FONT_ASSET_ID] = fallback_font
                metrics_by_asset[DEFAULT_EMPHASIS_FONT_ASSET_ID] = fallback_metrics
            fallback_report = font_text_safety_report(fallback_font.source_path, [safety_text])
            fallback_issue = fallback_report.blocking_issue()
            if fallback_issue:
                raise NodeExecutionError(
                    ErrorCode.render_subtitle_failed,
                    "强调字幕缺字且默认回退字体无法安全覆盖该文本"
                    f"（{fallback_issue}）。",
                    retryable=False,
                )
            chosen_font_id = DEFAULT_EMPHASIS_FONT_ASSET_ID
            chosen_metrics = fallback_metrics
            chosen_report = fallback_report
            fallbacks_by_hint_id[hint_id] = {
                "hint_index": hint_index,
                "phrase": hint.phrase.strip(),
                "requested_font_asset_id": requested_font_id,
                "fallback_font_asset_id": DEFAULT_EMPHASIS_FONT_ASSET_ID,
                "missing_codepoints": [
                    f"U+{codepoint:04X}" for codepoint in selected_report.missing_codepoints
                ],
            }
        asset_ids.append(chosen_font_id)
        measure, _ = make_text_measurer(chosen_metrics, font_size)
        measures_by_hint_id[hint_id] = measure
        baselines_by_hint_id[hint_id] = _baseline_offset(chosen_metrics, font_size)
        overhang_sides_by_asset[chosen_font_id] = _merge_overhang_sides(
            overhang_sides_by_asset.get(chosen_font_id),
            _horizontal_overhang_px(chosen_report, font_size),
        )
    return _HintFontPlan(
        asset_ids=asset_ids,
        measures_by_hint_id=measures_by_hint_id,
        baselines_by_hint_id=baselines_by_hint_id,
        overhang_sides_by_asset=overhang_sides_by_asset,
        fallbacks_by_hint_id=fallbacks_by_hint_id,
    )


def _validate_fallback_ass_style(
    *,
    normal_font_id: str,
    normal_font: ResolvedFont,
    emphasis_font_id: str,
    emphasis_font: ResolvedFont,
    fallback_font: ResolvedFont,
) -> None:
    if distinct_font_assets_have_ambiguous_ass_style(
        normal_font_id,
        normal_font,
        DEFAULT_EMPHASIS_FONT_ASSET_ID,
        fallback_font,
    ):
        raise NodeExecutionError(
            ErrorCode.render_subtitle_failed,
            "普通字幕与缺字回退字体属于 ASS 无法区分的同家族同字重。",
            retryable=False,
        )
    if distinct_font_assets_have_ambiguous_ass_style(
        emphasis_font_id,
        emphasis_font,
        DEFAULT_EMPHASIS_FONT_ASSET_ID,
        fallback_font,
    ):
        raise NodeExecutionError(
            ErrorCode.render_subtitle_failed,
            "所选强调字幕与缺字回退字体属于 ASS 无法区分的同家族同字重。",
            retryable=False,
        )


def _horizontal_overhang_px(
    report: FontTextSafetyReport,
    font_size: int,
) -> tuple[float, float]:
    if report.cell_height_units <= 0:
        return 0.0, 0.0
    scale = font_size / report.cell_height_units
    return (
        report.horizontal_left_overhang_units * scale,
        report.horizontal_right_overhang_units * scale,
    )


def _merge_overhang_sides(
    current: tuple[float, float] | None,
    candidate: tuple[float, float],
) -> tuple[float, float]:
    if current is None:
        return candidate
    return max(current[0], candidate[0]), max(current[1], candidate[1])


def _timing_source(alignment: AlignmentArtifact) -> str:
    if int(alignment.diagnostics.get("char_fallback") or 0) > 0:
        return "interpolated"
    source = alignment.source
    if source == "tts":
        return "native"
    if source in {"asr", "forced_alignment", "tts_subtitle"}:
        return "asr_anchored"
    return "interpolated"


def _style_runs_fit_canvas(plan: CaptionCompositionPlanArtifact) -> bool:
    line_height = max(plan.normal_font_size, plan.emphasis_font_size) * plan.band.line_height_ratio
    baseline_y = plan.band.baseline_y * plan.height
    for cue in plan.cues:
        for line_index, line in enumerate(cue.lines):
            baseline = baseline_y - (len(cue.lines) - line_index - 1) * line_height
            for run in line.runs:
                if run.role != "emphasis" or not run.style_id:
                    continue
                font_size = run.font_size or plan.emphasis_font_size
                above, below = emphasis_run_vertical_margins(
                    style_id=run.style_id,
                    effect_id=run.effect_id,
                    font_size=font_size,
                    advance_px=run.advance_px,
                )
                top_y = baseline - run.baseline_offset_px
                if top_y - above < 0 or top_y + font_size + below > plan.height:
                    return False
    return True
