from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.core.contracts import SpeechTokenTiming
from packages.media.video.ffmpeg import probe_audio_channels, probe_video_frame_count
from packages.core.contracts.artifacts import (
    AlignmentArtifact,
    CaptionBand,
    CaptionCompositionDiagnostics,
    CaptionCompositionPlanArtifact,
    EmphasisHint,
    NarrationUnit,
    StylePlanArtifact,
)
from packages.production.pipeline._caption_composition import (
    _Cue,
    _bind_hints,
    _break_penalty,
    _layout_cue,
    _legal_break,
    _line_start_frame,
    _locate_units,
    _omitted_breaks,
    _split_cue,
    _tokens_cover_meaningful_span,
    build_caption_composition,
)
from packages.production.pipeline._ffmpeg import SfxMixEvent, render_final_media
from packages.production.pipeline.nodes.caption_composition_planning import _timing_source
from packages.production.pipeline._speech_timing import proportional_tokens
from packages.production.pipeline._subtitles import _parse_hex_rgb, write_ass_subtitles
from tests.fixtures.media import ffmpeg_has_filter


def _unit(unit_id: str, text: str, start: float, end: float) -> NarrationUnit:
    return NarrationUnit(
        unit_id=unit_id,
        text=text,
        start=start,
        end=end,
        confidence=1.0,
    )


def _units(script: str, *, duration: float = 4.0) -> list[NarrationUnit]:
    return [
        _unit("unit_0001", script, 0.0, duration)
    ]


def _tokens(script: str, *, duration: float = 4.0) -> list[SpeechTokenTiming]:
    return proportional_tokens(script, start=0.0, end=duration)


def _build(
    script: str,
    hints: list[EmphasisHint] | None = None,
    *,
    width: int = 1080,
    max_width_ratio: float = 0.85,
    normal_width: float = 20.0,
    emphasis_width: float = 28.0,
    timing_source: str = "native",
    normal_enabled: bool = True,
    emphasis_enabled: bool = True,
    tokens_override: list[SpeechTokenTiming] | None = None,
) -> CaptionCompositionPlanArtifact:
    return build_caption_composition(
        script=script,
        units=_units(script),
        tokens=_tokens(script) if tokens_override is None else tokens_override,
        hints=hints or [],
        fps=30,
        total_frames=120,
        width=width,
        height=1920,
        band=CaptionBand(baseline_y=0.84, max_width_ratio=max_width_ratio),
        normal_enabled=normal_enabled,
        emphasis_enabled=emphasis_enabled,
        normal_font_asset_id="font_normal" if normal_enabled else None,
        emphasis_font_asset_id=("font_emphasis" if normal_enabled and emphasis_enabled else None),
        normal_font_size=64,
        emphasis_font_size=72,
        normal_measure=lambda text: len(text) * normal_width,
        emphasis_measure=lambda text: len(text) * emphasis_width,
        normal_baseline_offset=48.0,
        emphasis_baseline_offset=55.0,
        timing_source=timing_source,
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
    )


def _runs(plan: CaptionCompositionPlanArtifact):
    return [run for cue in plan.cues for line in cue.lines for run in line.runs]


def _reconstructed(plan: CaptionCompositionPlanArtifact) -> str:
    return "".join(cue.text for cue in plan.cues)


def test_no_emphasis_builds_one_fixed_band_normal_plan() -> None:
    plan = _build("普通字幕稳定显示", emphasis_enabled=False)

    assert plan.band.baseline_y == 0.84
    assert plan.emphasis_enabled is False
    assert {run.role for run in _runs(plan)} == {"normal"}
    assert _reconstructed(plan) == "普通字幕稳定显示"
    assert plan.diagnostics.hints_total == 0


@pytest.mark.parametrize(
    ("script", "phrase"),
    [
        ("重点内容放在句首", "重点内容"),
        ("这是句中重点内容示例", "重点内容"),
        ("这句话结尾是重点内容", "重点内容"),
    ],
)
def test_inline_emphasis_can_appear_at_every_sentence_position(script: str, phrase: str) -> None:
    plan = _build(script, [EmphasisHint(phrase=phrase, priority=80)])
    runs = _runs(plan)

    assert "".join(run.text for run in runs) == script
    emphasized = [run for run in runs if run.role == "emphasis"]
    assert [run.text for run in emphasized] == [phrase]
    phrase_start = script.find(phrase)
    first_token = next(
        token for token in _tokens(script) if token.char_span and token.char_span[0] == phrase_start
    )
    assert emphasized[0].enter_frame == round(first_token.start * 30)


def test_repeated_phrase_claims_first_unoccupied_instances_in_token_order() -> None:
    plan = _build(
        "高端定制也能高端定制",
        [EmphasisHint(phrase="高端定制"), EmphasisHint(phrase="高端定制")],
    )

    emphasis = [run for run in _runs(plan) if run.role == "emphasis"]
    assert [run.text for run in emphasis] == ["高端定制", "高端定制"]
    assert [run.char_span for run in emphasis] == [(0, 4), (6, 10)]


def test_overlap_prefers_priority_then_length_and_keeps_text_once() -> None:
    plan = _build(
        "高端定制服务",
        [
            EmphasisHint(phrase="高端定制", priority=20),
            EmphasisHint(phrase="定制服务", priority=90),
        ],
    )

    assert "".join(run.text for run in _runs(plan)) == "高端定制服务"
    assert [run.text for run in _runs(plan) if run.role == "emphasis"] == ["定制服务"]
    assert plan.diagnostics.hints_overlapped == 1


def test_whole_cue_absorbs_inline_and_can_wrap_to_multiple_lines() -> None:
    script = "三只喜鹊仍然全部位于固定字幕带"
    plan = _build(
        script,
        [
            EmphasisHint(phrase="三只喜鹊", priority=60, display_mode="whole_cue"),
            EmphasisHint(phrase="喜鹊", priority=100, display_mode="inline"),
        ],
        max_width_ratio=0.22,
    )

    assert len(plan.cues[0].lines) > 1
    assert {run.role for run in _runs(plan)} == {"emphasis"}
    assert "".join(run.text for run in _runs(plan)) == script
    assert len({run.hint_id for run in _runs(plan)}) == 1
    assert plan.diagnostics.hints_overlapped == 1


def test_phrase_miss_is_dropped_without_minimum_or_synthetic_fill() -> None:
    plan = _build("脚本只有这一句", [EmphasisHint(phrase="原文不存在")])

    assert not [run for run in _runs(plan) if run.role == "emphasis"]
    assert plan.diagnostics.hints_total == 1
    assert plan.diagnostics.hints_unmatched == 1
    assert plan.diagnostics.hints_applied == 0


def test_token_miss_falls_back_to_normal_with_explicit_diagnostics() -> None:
    plan = _build(
        "脚本包含重点短语",
        [EmphasisHint(phrase="重点短语")],
        tokens_override=[],
    )

    assert {run.role for run in _runs(plan)} == {"normal"}
    assert plan.diagnostics.hints_token_unmatched == 1
    assert plan.diagnostics.hints_applied == 0
    assert [item.model_dump(mode="json") for item in plan.diagnostics.fallbacks] == [
        {
            "reason": "token_unmatched",
            "hint_ids": ["hint_0001"],
            "phrase": "重点短语",
        }
    ]


def test_partial_token_coverage_cannot_emphasize_a_whole_phrase() -> None:
    tokens = _tokens("脚本包含重点短语")
    phrase_start = "脚本包含重点短语".index("重点短语")
    partial = [
        token for token in tokens if token.char_span == (phrase_start, phrase_start + 1)
    ]
    plan = _build(
        "脚本包含重点短语",
        [EmphasisHint(phrase="重点短语")],
        tokens_override=partial,
    )

    assert {run.role for run in _runs(plan)} == {"normal"}
    assert plan.diagnostics.hints_token_unmatched == 1


def test_normalized_units_map_monotonically_back_to_original_whitespace() -> None:
    script = "第一段  有双空格。\n第二段。"
    units = [
        _unit("u1", "第一段 有双空格。", 0.0, 1.0),
        _unit("u2", "第二段。", 1.4, 2.0),
    ]
    plan = build_caption_composition(
        script=script,
        units=units,
        tokens=_tokens(script, duration=2.0),
        hints=[],
        fps=30,
        total_frames=60,
        width=1080,
        height=1920,
        band=CaptionBand(),
        normal_enabled=True,
        emphasis_enabled=False,
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id=None,
        normal_font_size=64,
        emphasis_font_size=72,
        normal_measure=lambda text: len(text) * 20,
        emphasis_measure=lambda text: len(text) * 28,
        normal_baseline_offset=48,
        emphasis_baseline_offset=55,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
    )

    assert [cue.text for cue in plan.cues] == ["第一段  有双空格。", "第二段。"]
    assert plan.diagnostics.units_unmatched == 0


def test_unmatched_unit_is_explicitly_diagnosed() -> None:
    plan = build_caption_composition(
        script="原始脚本",
        units=[_unit("u1", "另一个脚本", 0.0, 1.0)],
        tokens=[],
        hints=[],
        fps=30,
        total_frames=30,
        width=1080,
        height=1920,
        band=CaptionBand(),
        normal_enabled=True,
        emphasis_enabled=False,
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id=None,
        normal_font_size=64,
        emphasis_font_size=72,
        normal_measure=lambda text: len(text) * 20,
        emphasis_measure=lambda text: len(text) * 28,
        normal_baseline_offset=48,
        emphasis_baseline_offset=55,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
    )

    assert plan.cues == []
    assert plan.diagnostics.units_unmatched == 1
    assert plan.diagnostics.fallbacks[0].reason == "narration_unit_unmatched"


def test_first_tiny_cue_merges_forward_when_gap_is_short() -> None:
    script = "嗨！这是后续完整说明。"
    units = [
        _unit("u1", "嗨！", 0.0, 0.2),
        _unit("u2", "这是后续完整说明。", 0.21, 2.0),
    ]
    plan = build_caption_composition(
        script=script,
        units=units,
        tokens=_tokens(script, duration=2.0),
        hints=[],
        fps=30,
        total_frames=60,
        width=1080,
        height=1920,
        band=CaptionBand(),
        normal_enabled=True,
        emphasis_enabled=False,
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id=None,
        normal_font_size=64,
        emphasis_font_size=72,
        normal_measure=lambda text: len(text) * 20,
        emphasis_measure=lambda text: len(text) * 28,
        normal_baseline_offset=48,
        emphasis_baseline_offset=55,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
    )

    assert len(plan.cues) == 1
    assert plan.cues[0].text == script
    assert plan.diagnostics.merged_units == 1


def test_consecutive_leading_tiny_cues_keep_merging_forward() -> None:
    script = "嗨！啊！这是后续完整说明。"
    units = [
        _unit("u1", "嗨！", 0.0, 0.1),
        _unit("u2", "啊！", 0.11, 0.2),
        _unit("u3", "这是后续完整说明。", 0.21, 2.0),
    ]
    plan = build_caption_composition(
        script=script,
        units=units,
        tokens=_tokens(script, duration=2.0),
        hints=[],
        fps=30,
        total_frames=60,
        width=1080,
        height=1920,
        band=CaptionBand(),
        normal_enabled=True,
        emphasis_enabled=False,
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id=None,
        normal_font_size=64,
        emphasis_font_size=72,
        normal_measure=lambda text: len(text) * 20,
        emphasis_measure=lambda text: len(text) * 28,
        normal_baseline_offset=48,
        emphasis_baseline_offset=55,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
    )

    assert len(plan.cues) == 1
    assert plan.cues[0].text == script
    assert plan.diagnostics.merged_units == 2


def test_timing_source_downgrades_when_any_char_timing_is_interpolated() -> None:
    assert _timing_source(
        AlignmentArtifact(
            audio_artifact_id="audio", segments=[], source="tts", diagnostics={"char_fallback": 1}
        )
    ) == "interpolated"
    assert _timing_source(
        AlignmentArtifact(
            audio_artifact_id="audio", segments=[], source="asr", diagnostics={"char_fallback": 0}
        )
    ) == "asr_anchored"


def test_unbreakable_emphasis_falls_back_to_normal_without_overlay() -> None:
    phrase = "这是一个必须整体保留但宽度放不下的强调短语"
    plan = _build(
        phrase,
        [EmphasisHint(phrase=phrase)],
        max_width_ratio=0.16,
        emphasis_width=45.0,
    )

    assert {run.role for run in _runs(plan)} == {"normal"}
    assert "".join(run.text for run in _runs(plan)) == phrase
    assert plan.diagnostics.hints_unbreakable == 1
    assert plan.diagnostics.fallbacks[0].reason == "emphasis_unbreakable"
    assert all(
        line.advance_px <= plan.width * plan.band.max_width_ratio + 0.01
        for cue in plan.cues
        for line in cue.lines
    )


def test_mixed_font_advance_and_run_baselines_are_preserved() -> None:
    plan = _build("普通强调普通", [EmphasisHint(phrase="强调")])
    runs = _runs(plan)

    assert [run.advance_px for run in runs] == [40.0, 56.0, 40.0]
    assert [run.baseline_offset_px for run in runs] == [48.0, 55.0, 48.0]
    assert plan.cues[0].lines[0].advance_px == 136.0


def test_multiple_inline_hints_keep_order_without_duplicate_text() -> None:
    script = "普通重点连接第二重点收尾"
    plan = _build(
        script,
        [EmphasisHint(phrase="重点"), EmphasisHint(phrase="第二重点")],
    )

    assert "".join(run.text for run in _runs(plan)) == script
    assert [run.text for run in _runs(plan) if run.role == "emphasis"] == [
        "重点",
        "第二重点",
    ]


def test_inline_boundary_whitespace_and_punctuation_belong_to_preceding_run() -> None:
    script = "普通，重点！ 下一段"
    plan = _build(script, [EmphasisHint(phrase="重点")])
    runs = _runs(plan)

    assert [run.text for run in runs] == ["普通，", "重点！ ", "下一段"]
    assert [run.role for run in runs] == ["normal", "emphasis", "normal"]
    assert "".join(run.text for run in runs) == script


def test_sentence_end_emphasis_does_not_cross_its_narration_cue() -> None:
    script = "第一句重点。\n第二句继续"
    plan = build_caption_composition(
        script=script,
        units=[
            _unit("u1", "第一句重点", 0.0, 1.0),
            _unit("u2", "第二句继续", 1.2, 2.2),
        ],
        tokens=_tokens(script, duration=2.2),
        hints=[EmphasisHint(phrase="重点")],
        fps=30,
        total_frames=66,
        width=1080,
        height=1920,
        band=CaptionBand(),
        normal_enabled=True,
        emphasis_enabled=True,
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id="font_emphasis",
        normal_font_size=64,
        emphasis_font_size=72,
        normal_measure=lambda text: len(text) * 20,
        emphasis_measure=lambda text: len(text) * 28,
        normal_baseline_offset=48,
        emphasis_baseline_offset=55,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
    )

    emphasized = [run for run in _runs(plan) if run.role == "emphasis"]
    assert [run.text for run in emphasized] == ["重点"]
    assert plan.diagnostics.hints_unmatched == 0


def test_chinese_line_break_keeps_protected_numeric_token_whole() -> None:
    script = "限时套餐只要299元起现在预订"
    plan = _build(script, max_width_ratio=0.15)
    line_texts = ["".join(run.text for run in line.runs) for line in plan.cues[0].lines]

    assert len(line_texts) > 1
    assert sum("299元起" in text for text in line_texts) == 1
    assert all(text not in {"299", "元起"} for text in line_texts)


def test_line_break_never_splits_a_multi_character_speech_token() -> None:
    script = "前面超级词后面内容"
    tokens = [
        SpeechTokenTiming(
            text="超级词",
            start=0.8,
            end=1.4,
            token_id="token_phrase",
            char_span=(2, 5),
        )
    ]
    plan = _build(
        script,
        max_width_ratio=0.08,
        tokens_override=tokens,
    )
    line_texts = ["".join(run.text for run in line.runs) for cue in plan.cues for line in cue.lines]

    assert sum("超级词" in text for text in line_texts) == 1
    assert all(text not in {"超", "超级", "级词", "词"} for text in line_texts)


def test_emphasis_span_is_not_split_across_lines_or_time_cues() -> None:
    phrase = "高端定制方案"
    script = f"开头说明{phrase}后续内容继续展开"
    plan = _build(
        script,
        [EmphasisHint(phrase=phrase)],
        max_width_ratio=0.2,
    )
    emphasis = [run for run in _runs(plan) if run.role == "emphasis"]

    assert len(emphasis) == 1
    assert emphasis[0].text == phrase


@pytest.mark.parametrize("source", ["native", "asr_anchored", "interpolated"])
def test_timing_source_is_reported_without_fabricating_precision(source: str) -> None:
    plan = _build("时间分档", [EmphasisHint(phrase="分档")], timing_source=source)

    assert plan.diagnostics.timing_source == source
    emphasis = next(run for run in _runs(plan) if run.role == "emphasis")
    owned = [token for token in _tokens("时间分档") if token.token_id in emphasis.token_ids]
    assert emphasis.enter_frame == round(min(token.start for token in owned) * 30)


def test_disabled_normal_band_forces_empty_emphasis_plan() -> None:
    plan = _build(
        "不会显示",
        [EmphasisHint(phrase="显示")],
        normal_enabled=False,
        emphasis_enabled=True,
    )

    assert plan.normal_enabled is False
    assert plan.emphasis_enabled is False
    assert plan.cues == []


def test_contract_rejects_emphasis_only_payload() -> None:
    with pytest.raises(ValidationError, match="normal caption band"):
        CaptionCompositionPlanArtifact.model_validate(
            {
                "fps": 30,
                "width": 1080,
                "height": 1920,
                "normal_enabled": False,
                "emphasis_enabled": True,
                "band": {},
                "normal_font_size": 64,
                "emphasis_font_size": 72,
                "emphasis_font_asset_id": "font_emphasis",
                "cues": [],
            }
        )


def test_ass_golden_uses_one_dialogue_per_run_and_no_legacy_geometry(tmp_path) -> None:
    plan = _build("普通强调收尾", [EmphasisHint(phrase="强调")])
    output = tmp_path / "subtitle.ass"
    style = StylePlanArtifact.model_validate(
        {
            "subtitle": {
            "primary_color": "#FFFFFF",
            "outline_color": "#000000",
            "outline": 4,
            "emphasis_primary_color": "#FFE84A",
            "emphasis_outline_color": "#000000",
            "emphasis_outline": 4,
            }
        }
    )

    write_ass_subtitles(
        output,
        style=style,
        caption_composition=plan,
        font_name="Normal Face",
        emphasis_font_name="Emphasis Face",
        font_weight=400,
        emphasis_font_weight=700,
    )
    content = output.read_text(encoding="utf-8")
    dialogue = [line for line in content.splitlines() if line.startswith("Dialogue:")]

    assert len(dialogue) == 3
    assert "Style: Normal,Normal Face,64" in content
    assert "Style: Emphasis,Emphasis Face,72" in content
    assert re.search(r"Style: Normal,.*,&H00[0-9A-F]{6},.*?,0,0,0,0,100", content)
    assert re.search(r"Style: Emphasis,.*,&H00[0-9A-F]{6},.*?,-1,0,0,0,100", content)
    assert "\\fscx105\\fscy105" in content
    assert "\\fscx220" not in content
    assert "\\move(" not in content
    assert "rect" not in content.lower()
    assert "placement" not in content.lower()
    positions = [
        tuple(int(value) for value in match.groups())
        for line in dialogue
        if (match := re.search(r"\\pos\((\d+),(\d+)\)", line))
    ]
    assert positions == [(472, 1565), (512, 1558), (568, 1565)]
    rendered_text = "".join(re.sub(r"^.*?\}\s*", "", line) for line in dialogue)
    assert rendered_text == "普通强调收尾"


def test_per_hint_font_metrics_and_ass_override_are_kept_explicit(tmp_path) -> None:
    script = "甲乙丙丁"
    plan = build_caption_composition(
        script=script,
        units=_units(script),
        tokens=_tokens(script),
        hints=[EmphasisHint(phrase="乙"), EmphasisHint(phrase="丁")],
        fps=30,
        total_frames=120,
        width=1080,
        height=1920,
        band=CaptionBand(),
        normal_enabled=True,
        emphasis_enabled=True,
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id="font_decorative",
        normal_font_size=64,
        emphasis_font_size=72,
        normal_measure=lambda text: len(text) * 10,
        emphasis_measure=lambda text: len(text) * 20,
        normal_baseline_offset=48,
        emphasis_baseline_offset=55,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
        emphasis_font_asset_ids=["font_decorative", "font_fallback"],
        emphasis_measures_by_asset={
            "font_decorative": lambda text: len(text) * 20,
            "font_fallback": lambda text: len(text) * 12,
        },
        emphasis_baseline_offsets_by_asset={
            "font_decorative": 55,
            "font_fallback": 51,
        },
        font_horizontal_overhang_px={"font_decorative": 3.0, "font_fallback": 2.0},
        layout_horizontal_overhang_px=3.0,
    )

    emphasis_runs = [run for run in _runs(plan) if run.role == "emphasis"]
    assert [run.font_asset_id for run in emphasis_runs] == [
        "font_decorative",
        "font_fallback",
    ]
    assert [run.advance_px for run in emphasis_runs] == [20.0, 12.0]
    assert [run.baseline_offset_px for run in emphasis_runs] == [55.0, 51.0]
    assert plan.diagnostics.font_horizontal_overhang_px == {
        "font_decorative": 3.0,
        "font_fallback": 2.0,
    }

    output = tmp_path / "font-override.ass"
    write_ass_subtitles(
        output,
        style=StylePlanArtifact.model_validate({"subtitle": {}}),
        caption_composition=plan,
        font_name="Normal Face",
        emphasis_font_name="Decorative Face",
        font_overrides={"font_fallback": ("Fallback Face", 700)},
    )
    content = output.read_text(encoding="utf-8")
    assert "\\fnFallback Face\\b1" in content


def test_ass_centers_asymmetric_ink_bounds_inside_the_caption_band(tmp_path) -> None:
    plan = _build("普通", emphasis_enabled=False)
    plan.diagnostics.font_horizontal_left_overhang_px = {"font_normal": 8.0}
    plan.diagnostics.font_horizontal_right_overhang_px = {"font_normal": 0.0}
    output = tmp_path / "asymmetric-overhang.ass"

    write_ass_subtitles(
        output,
        style=StylePlanArtifact.model_validate({"subtitle": {}}),
        caption_composition=plan,
        font_name="Normal Face",
        emphasis_font_name="Normal Face",
    )

    dialogue = next(
        line
        for line in output.read_text(encoding="utf-8").splitlines()
        if line.startswith("Dialogue:")
    )
    assert "\\pos(524," in dialogue


def test_ass_writer_requires_resolved_fonts_and_rejects_invalid_hex() -> None:
    plan = _build("普通字幕")

    with pytest.raises(ValueError, match="font family names"):
        write_ass_subtitles(
            Path("unused.ass"),
            style=StylePlanArtifact.model_validate({"subtitle": {}}),
            caption_composition=plan,
            font_name=" ",
            emphasis_font_name="Emphasis Face",
        )

    assert _parse_hex_rgb("GGGGGG") is None


def test_all_cues_share_the_same_band_baseline() -> None:
    script = "第一句。第二句。"
    units = [
        _unit("u1", "第一句。", 0.0, 1.5),
        _unit("u2", "第二句。", 2.0, 3.5),
    ]
    plan = build_caption_composition(
        script=script,
        units=units,
        tokens=_tokens(script, duration=3.5),
        hints=[],
        fps=30,
        total_frames=105,
        width=1080,
        height=1920,
        band=CaptionBand(baseline_y=0.82),
        normal_enabled=True,
        emphasis_enabled=False,
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id=None,
        normal_font_size=64,
        emphasis_font_size=72,
        normal_measure=lambda text: len(text) * 20,
        emphasis_measure=lambda text: len(text) * 28,
        normal_baseline_offset=48,
        emphasis_baseline_offset=55,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
    )

    assert len(plan.cues) == 2
    assert plan.band.baseline_y == 0.82
    assert {len(cue.lines) for cue in plan.cues} == {1}


def test_caption_private_boundaries_fail_or_split_deterministically() -> None:
    diagnostics = CaptionCompositionDiagnostics()
    located = _locate_units(
        "abc",
        [
            _unit("empty", "", 0, 1),
            _unit("missing", "zzz", 0, 1),
            _unit("invalid-time", "abc", 1, 1),
        ],
        diagnostics,
    )
    assert located == []
    assert diagnostics.units_unmatched == 3
    assert {item.reason for item in diagnostics.fallbacks} == {
        "narration_unit_unmatched",
        "narration_unit_timing_invalid",
    }

    empty_hint = EmphasisHint.model_construct(phrase="", priority=0, display_mode="inline")
    assert _bind_hints("abc", [empty_hint], diagnostics) == []
    assert diagnostics.hints_unmatched == 1

    script = "甲乙。丙丁"
    cue = _Cue(0.0, 4.0, 0, len(script), ["u1"])
    laid_out, failed = _layout_cue(
        cue,
        script=script,
        hints=[],
        tokens=[],
        normal_measure=lambda text: len(text) * 100,
        emphasis_measure=lambda text: len(text) * 100,
        max_width=1,
        diagnostics=diagnostics,
    )
    assert failed is False
    assert diagnostics.split_cues >= 1
    assert "".join(script[item.cue.char_start : item.cue.char_end] for item in laid_out) == script

    left, right = _split_cue(_Cue(0.0, 4.0, 0, 7, ["u2"]), 3, "abc def")
    assert left.end == right.start
    assert left.char_end == 3
    assert right.char_start == 4

    assert _legal_break(" abc", _Cue(0, 1, 0, 4, ["u3"]), 1, []) is False
    assert _tokens_cover_meaningful_span("!!!", [], 0, 3) is False
    assert (
        _tokens_cover_meaningful_span(
            "ab",
            [SpeechTokenTiming(text="a", start=0, end=1, char_span=(0, 1))],
            0,
            2,
        )
        is False
    )
    assert (
        _tokens_cover_meaningful_span(
            "a",
            [SpeechTokenTiming(text="a", start=0, end=1, char_span=None)],
            0,
            1,
        )
        is False
    )
    assert _line_start_frame("   ", 0, 3, [], 30, 5, 10) == 5
    assert _omitted_breaks(
        _Cue(0, 1, 0, 5, ["u4"]),
        [(1, 2), (3, 4)],
        " a b ",
    ) == [(0, 1), (2, 3), (4, 5)]
    assert _break_penalty("。") == 0.0
    assert _break_penalty("，") == 0.2
    assert _break_penalty("字") == 1.0


@pytest.mark.skipif(
    not ffmpeg_has_filter("subtitles"),
    reason="ffmpeg build does not provide the subtitles filter",
)
def test_real_single_pass_voice_bgm_sfx_mix_preserves_frames_and_stereo(
    tmp_path,
    media_fixture_factory,
) -> None:
    plan = _build("普通强调收尾", [EmphasisHint(phrase="强调")])
    subtitle_path = tmp_path / "caption.ass"
    write_ass_subtitles(
        subtitle_path,
        style=StylePlanArtifact.model_validate({"subtitle": {}}),
        caption_composition=plan,
        font_name="Arial",
        emphasis_font_name="Arial",
    )
    rendered = media_fixture_factory.video(
        duration_sec=2,
        width=320,
        height=568,
        fps=30,
        filename="caption-real-mix-video.mp4",
    )
    voice = media_fixture_factory.audio(
        duration_sec=2,
        frequency=440,
        filename="caption-real-mix-voice.wav",
    )
    bgm = media_fixture_factory.audio(
        duration_sec=2,
        frequency=220,
        filename="caption-real-mix-bgm.wav",
    )
    sfx = media_fixture_factory.audio(
        duration_sec=0.2,
        frequency=880,
        filename="caption-real-mix-sfx.wav",
    )
    output = tmp_path / "caption-real-mix-final.mp4"

    render_final_media(
        rendered_path=rendered,
        audio_path=voice,
        output_path=output,
        subtitle_path=subtitle_path,
        bgm_path=bgm,
        bgm_volume=0.15,
        duration=2.0,
        fps=30,
        auto_mix=False,
        sfx_events=[SfxMixEvent(path=sfx, start_ms=450, volume=0.2, asset_id="sfx")],
    )

    assert probe_video_frame_count(output) == 60
    assert probe_audio_channels(output) == 2
