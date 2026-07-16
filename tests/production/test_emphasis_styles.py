from __future__ import annotations

import pytest

from packages.core.contracts.artifacts import (
    CaptionBand,
    CaptionCompositionPlanArtifact,
    CaptionCue,
    CaptionFrameSpan,
    CaptionLine,
    CaptionRun,
    EmphasisHint,
    NarrationUnit,
    StylePlanArtifact,
)
from packages.production.pipeline._caption_composition import build_caption_composition
from packages.production.pipeline._emphasis_styles import (
    EMPHASIS_STYLES,
    emphasis_style_horizontal_padding,
    select_emphasis_styles,
)
from packages.media.video.ffmpeg import FfmpegRunner, ffmpeg_bin
from packages.production.pipeline._sfx_events import plan_caption_sfx_events
from packages.production.pipeline._speech_timing import proportional_tokens
from packages.production.pipeline._subtitles import write_ass_subtitles
from packages.production.pipeline.nodes.caption_composition_planning import (
    _style_runs_fit_canvas,
)
from tests.fixtures.media import ffmpeg_has_filter


def test_style_registry_contains_the_eight_product_templates() -> None:
    assert list(EMPHASIS_STYLES) == [
        "classic_yellow",
        "blue_burst",
        "red_alert",
        "brand_stamp",
        "marker_orange",
        "ink_hand",
        "gold_serif",
        "highlight_box",
    ]
    assert EMPHASIS_STYLES["blue_burst"].backing == "burst_star"
    assert EMPHASIS_STYLES["marker_orange"].backing == "underline_swipe"
    assert EMPHASIS_STYLES["highlight_box"].sfx_class == "click"
    assert emphasis_style_horizontal_padding(list(EMPHASIS_STYLES.values())) == 10.0
    assert {
        style_id: (
            style.font_asset_id,
            style.fill,
            style.outline,
            style.outline_width,
            style.size_ratio,
            style.backing,
            style.backing_color,
            style.effect_id,
            style.sfx_class,
        )
        for style_id, style in EMPHASIS_STYLES.items()
    } == {
        "classic_yellow": (
            "asset_font_noto_sans_cjk_sc_bold",
            "#FFE14D",
            "#000000",
            4.0,
            1.40,
            None,
            None,
            "pop",
            "pop",
        ),
        "blue_burst": (
            "asset_font_zcool_qingke_huangyou",
            "#FFFFFF",
            "#1E6FD9",
            5.0,
            1.50,
            "burst_star",
            "#3FA7F5",
            "pop_rotate",
            "impact",
        ),
        "red_alert": (
            "asset_font_noto_sans_cjk_sc_bold",
            "#FF4D4F",
            "#FFFFFF",
            4.0,
            1.45,
            None,
            None,
            "drop_in",
            "impact",
        ),
        "brand_stamp": (
            "asset_font_smiley_sans",
            "#A8D8F0",
            "#1B4F8A",
            5.0,
            1.50,
            None,
            None,
            "zoom_settle",
            "ding",
        ),
        "marker_orange": (
            "asset_font_lxgw_marker",
            "#FF8A00",
            "#FFFFFF",
            3.5,
            1.40,
            "underline_swipe",
            "#FFD34D",
            "slide_up_in",
            "whoosh",
        ),
        "ink_hand": (
            "asset_font_mashanzheng",
            "#FFFFFF",
            "#000000",
            3.0,
            1.55,
            None,
            None,
            "soft_in",
            None,
        ),
        "gold_serif": (
            "asset_font_noto_serif_cjk_sc_bold",
            "#E8C97A",
            "#4A3418",
            3.0,
            1.35,
            None,
            None,
            "soft_in",
            None,
        ),
        "highlight_box": (
            "asset_font_noto_sans_cjk_sc_bold",
            "#111111",
            "#000000",
            0.0,
            1.30,
            "highlight_rect",
            "#FFE14D",
            "soft_in",
            "click",
        ),
    }


def test_semantic_style_selection_is_deterministic_and_respects_overrides() -> None:
    hints = [
        EmphasisHint(phrase="甲", intensity="normal"),
        EmphasisHint(phrase="乙", intensity="strong"),
        EmphasisHint(phrase="丙", intensity="hero"),
        EmphasisHint(phrase="丁", display_mode="whole_cue"),
    ]

    selected = select_emphasis_styles(hints, tone="轻快", bgm_mood="高级")

    assert [item.style_id for item in selected] == [
        "blue_burst",
        "marker_orange",
        "blue_burst",
        "brand_stamp",
    ]
    assert [
        item.style_id for item in select_emphasis_styles(hints, requested_style_id="red_alert")
    ] == ["red_alert"] * 4


def test_composition_carries_style_font_size_and_effect_to_runs() -> None:
    script = "甲乙"
    hints = [EmphasisHint(phrase="甲"), EmphasisHint(phrase="乙")]
    plan = build_caption_composition(
        script=script,
        units=[NarrationUnit(unit_id="unit", text=script, start=0, end=1, confidence=1)],
        tokens=proportional_tokens(script, start=0, end=1),
        hints=hints,
        fps=30,
        total_frames=30,
        width=1080,
        height=1920,
        band=CaptionBand(),
        normal_enabled=True,
        emphasis_enabled=True,
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id="font_emphasis",
        normal_font_size=64,
        emphasis_font_size=96,
        normal_measure=lambda text: len(text) * 20,
        emphasis_measure=lambda text: len(text) * 30,
        normal_baseline_offset=48,
        emphasis_baseline_offset=70,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
        emphasis_font_asset_ids=["font_a", "font_b"],
        emphasis_requested_font_asset_ids=["requested_a", "requested_b"],
        emphasis_font_sizes=[90, 84],
        emphasis_style_ids=["blue_burst", "marker_orange"],
        emphasis_effect_ids=["pop_rotate", "slide_up_in"],
    )

    runs = [run for cue in plan.cues for line in cue.lines for run in line.runs]
    assert [
        (run.style_id, run.font_size, run.effect_id, run.requested_font_asset_id)
        for run in runs
    ] == [
        ("blue_burst", 90, "pop_rotate", "requested_a"),
        ("marker_orange", 84, "slide_up_in", "requested_b"),
    ]


def _styled_composition() -> CaptionCompositionPlanArtifact:
    style_ids = list(EMPHASIS_STYLES)
    cues = []
    for index, style_id in enumerate(style_ids):
        start = index * 30
        font_size = round(64 * EMPHASIS_STYLES[style_id].size_ratio)
        run = CaptionRun(
            run_id=f"run_{style_id}",
            text="花字",
            role="emphasis",
            hint_id=f"hint_{index}",
            style_id=style_id,
            font_asset_id=EMPHASIS_STYLES[style_id].font_asset_id,
            font_size=font_size,
            char_span=(0, 2),
            enter_frame=start,
            exit_frame=start + 30,
            effect_id=EMPHASIS_STYLES[style_id].effect_id,
            advance_px=80,
            baseline_offset_px=68,
            char_enter_frames=[start, start + 2],
            char_advances_px=[40, 40],
        )
        cues.append(
            CaptionCue(
                cue_id=f"cue_{index}",
                text="花字",
                start_frame=start,
                end_frame=start + 30,
                spoken_span=CaptionFrameSpan(start_frame=start, end_frame=start + 30),
                display_span=CaptionFrameSpan(start_frame=start, end_frame=start + 30),
                source_unit_ids=[f"unit_{index}"],
                lines=[CaptionLine(runs=[run], advance_px=80)],
            )
        )
    return CaptionCompositionPlanArtifact(
        fps=30,
        width=1080,
        height=1920,
        normal_enabled=True,
        emphasis_enabled=True,
        band=CaptionBand(),
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id=EMPHASIS_STYLES["blue_burst"].font_asset_id,
        normal_font_size=64,
        emphasis_font_size=max(round(64 * style.size_ratio) for style in EMPHASIS_STYLES.values()),
        cues=cues,
    )


def test_ass_emits_dynamic_styles_backing_layers_and_text_layer_one(tmp_path) -> None:
    output = tmp_path / "styles.ass"
    composition = _styled_composition().model_copy(
        update={"emphasis_primary_color_override": "#123456"}
    )
    write_ass_subtitles(
        output,
        style=StylePlanArtifact.model_validate({"subtitle": {"emphasis_primary_color": "#123456"}}),
        caption_composition=composition,
        font_name="Normal Face",
        emphasis_font_name="Burst Face",
        font_overrides={
            spec.font_asset_id: (f"Face {spec.font_asset_id}", 400)
            for spec in EMPHASIS_STYLES.values()
            if spec.font_asset_id != EMPHASIS_STYLES["blue_burst"].font_asset_id
        },
    )

    content = output.read_text(encoding="utf-8")
    assert "Style: Emph_blue_burst,Burst Face,96,&H00563412" in content
    assert (
        "Style: Emph_marker_orange,Face asset_font_lxgw_marker,90,&H00563412"
        in content
    )
    assert sum(line.startswith("Style: Emph_") for line in content.splitlines()) == 8
    assert content.count("Dialogue: 0,") == 3
    assert "\\p1" in content
    assert "\\t(0,160,\\clip(" in content
    assert all(
        line.startswith("Dialogue: 1,")
        for line in content.splitlines()
        if "Emph_" in line and line.startswith("Dialogue:")
    )


def test_style_vertical_bounds_fail_closed_at_canvas_edge() -> None:
    composition = _styled_composition()

    assert _style_runs_fit_canvas(composition)
    assert not _style_runs_fit_canvas(
        composition.model_copy(
            update={"band": composition.band.model_copy(update={"baseline_y": 1.0})}
        )
    )


def test_style_sfx_overrides_effect_default() -> None:
    events = plan_caption_sfx_events(
        caption_composition=_styled_composition(),
        sfx_asset_ids_by_class={
            "pop": "pop",
            "impact": "impact",
            "ding": "ding",
            "whoosh": "whoosh",
            "click": "click",
        },
    )

    assert [(event.sfx_class, event.asset_id) for event in events] == [
        ("pop", "pop"),
        ("impact", "impact"),
        ("impact", "impact"),
        ("ding", "ding"),
        ("whoosh", "whoosh"),
        ("click", "click"),
    ]


@pytest.mark.skipif(
    not ffmpeg_has_filter("subtitles"),
    reason="ffmpeg build does not provide the subtitles filter",
)
def test_all_eight_styles_render_visible_frames_through_libass(tmp_path) -> None:
    subtitle_path = tmp_path / "style-backings.ass"
    write_ass_subtitles(
        subtitle_path,
        style=StylePlanArtifact.model_validate({"subtitle": {}}),
        caption_composition=_styled_composition(),
        font_name="Arial",
        emphasis_font_name="Arial",
        font_overrides={spec.font_asset_id: ("Arial", 400) for spec in EMPHASIS_STYLES.values()},
    )
    result = FfmpegRunner(timeout_sec=30).run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:size=320x568:rate=30:duration=8",
            "-vf",
            f"subtitles=filename='{subtitle_path}',select='eq(mod(n,30),15)',format=gray",
            "-fps_mode",
            "passthrough",
            "-frames:v",
            "8",
            "-f",
            "rawvideo",
            "-",
        ],
        text=False,
    )
    frame_size = 320 * 568
    frames = bytes(result.stdout)
    assert len(frames) == frame_size * 8
    for index in range(8):
        frame = frames[index * frame_size : (index + 1) * frame_size]
        assert max(frame) - min(frame) > 10, f"style frame {index} is blank"
