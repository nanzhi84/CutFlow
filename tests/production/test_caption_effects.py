from __future__ import annotations

import re
from typing import get_args

import pytest
from pydantic import ValidationError

from packages.core.contracts import SpeechTokenTiming
from packages.core.contracts.artifacts import (
    CaptionBand,
    CaptionCompositionPlanArtifact,
    CaptionCue,
    CaptionEffectId,
    CaptionFrameSpan,
    CaptionLine,
    CaptionRun,
    EmphasisHint,
    NarrationUnit,
    StylePlanArtifact,
)
from packages.core.contracts.caption_effects_policy import CAPTION_EFFECT_IDS
from packages.media.video.ffmpeg import FfmpegRunner, ffmpeg_bin, probe_video_frame_count
from packages.production.pipeline._caption_composition import build_caption_composition
from packages.production.pipeline._caption_effects import (
    ANIMATED_CAPTION_EFFECT_IDS,
    CAPTION_EFFECTS,
    caption_effect,
)
from packages.production.pipeline._ffmpeg import render_final_media
from packages.production.pipeline._speech_timing import proportional_tokens
from packages.production.pipeline._subtitles import write_ass_subtitles
from tests.fixtures.media import ffmpeg_has_filter

_GOLDEN_DIALOGUE_TEXT = {
    "soft_in": ["{\\an7\\pos(520,1558)\\fad(120,0)}甲乙"],
    "fade_through": ["{\\an7\\pos(520,1558)\\fad(120,100)}甲乙"],
    "wipe_reveal": [
        "{\\an7\\fad(60,0)\\move(513,1558,527,1558,0,90)}甲",
        "{\\an7\\fad(60,0)\\move(533,1558,547,1558,0,90)}乙",
    ],
    "slide_up_in": ["{\\an7\\move(520,1580,520,1558,0,160)\\fad(90,0)}甲乙"],
    "pop": [
        "{\\an7\\pos(519,1558)\\fscx85\\fscy85"
        "\\t(0,120,\\fscx105\\fscy105)\\t(120,240,\\fscx100\\fscy100)}甲乙"
    ],
    "pop_rotate": [
        "{\\an7\\pos(518,1558)\\frz-6\\fscx80\\fscy80"
        "\\t(0,140,\\frz0\\fscx108\\fscy108)"
        "\\t(140,260,\\fscx100\\fscy100)}甲乙"
    ],
    "jelly_pop": [
        "{\\an7\\pos(519,1558)\\fscx85\\fscy85"
        "\\t(0,120,\\fscx105\\fscy105)\\t(120,240,\\fscx100\\fscy100)"
        "\\t(240,420,\\fscx97\\fscy103)\\t(420,600,\\fscx101\\fscy99)"
        "\\t(600,780,\\fscx100\\fscy100)}甲乙"
    ],
    "drop_in": [
        "{\\an7\\move(520,1532,520,1558,0,140)\\fad(60,0)"
        "\\t(140,200,\\fscy94)\\t(200,260,\\fscy100)}甲乙"
    ],
    "zoom_settle": [
        "{\\an7\\pos(514,1558)\\fscx130\\fscy130\\fad(80,0)"
        "\\t(0,200,\\fscx100\\fscy100)}甲乙"
    ],
}


def _effect_composition(effect_id: str) -> CaptionCompositionPlanArtifact:
    role = "normal" if "normal" in caption_effect(effect_id).roles else "emphasis"
    run = CaptionRun(
        run_id="run_effect",
        text="甲乙",
        role=role,
        hint_id="hint_0001" if role == "emphasis" else None,
        char_span=(0, 2),
        enter_frame=0,
        exit_frame=30,
        effect_id=effect_id,
        advance_px=40,
        baseline_offset_px=55,
        char_enter_frames=[0, 2],
        char_advances_px=[20, 20],
    )
    return CaptionCompositionPlanArtifact(
        fps=30,
        width=1080,
        height=1920,
        normal_enabled=True,
        emphasis_enabled=role == "emphasis",
        band=CaptionBand(),
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id="font_emphasis" if role == "emphasis" else None,
        normal_font_size=64,
        emphasis_font_size=72,
        cues=[
            CaptionCue(
                cue_id="cue_0001",
                text="甲乙",
                start_frame=0,
                end_frame=30,
                spoken_span=CaptionFrameSpan(start_frame=0, end_frame=30),
                display_span=CaptionFrameSpan(start_frame=0, end_frame=30),
                source_unit_ids=["unit_0001"],
                lines=[
                    CaptionLine(
                        runs=[run],
                        advance_px=40,
                        animation_headroom_px=caption_effect(effect_id).headroom_px(40),
                    )
                ],
            )
        ],
    )


def _all_effects_composition() -> CaptionCompositionPlanArtifact:
    cues: list[CaptionCue] = []
    for index, effect_id in enumerate(ANIMATED_CAPTION_EFFECT_IDS):
        start_frame = index * 30
        end_frame = start_frame + 30
        role = "normal" if "normal" in caption_effect(effect_id).roles else "emphasis"
        run = CaptionRun(
            run_id=f"run_{effect_id}",
            text="AB",
            role=role,
            hint_id=f"hint_{index:04d}" if role == "emphasis" else None,
            char_span=(0, 2),
            enter_frame=start_frame,
            exit_frame=end_frame,
            effect_id=effect_id,
            advance_px=40,
            baseline_offset_px=55,
            char_enter_frames=[start_frame, start_frame + 2],
            char_advances_px=[20, 20],
        )
        cues.append(
            CaptionCue(
                cue_id=f"cue_{index:04d}",
                text="AB",
                start_frame=start_frame,
                end_frame=end_frame,
                spoken_span=CaptionFrameSpan(
                    start_frame=start_frame,
                    end_frame=end_frame,
                ),
                display_span=CaptionFrameSpan(
                    start_frame=start_frame,
                    end_frame=end_frame,
                ),
                source_unit_ids=[f"unit_{index:04d}"],
                lines=[
                    CaptionLine(
                        runs=[run],
                        advance_px=40,
                        animation_headroom_px=caption_effect(effect_id).headroom_px(40),
                    )
                ],
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
        emphasis_font_asset_id="font_emphasis",
        normal_font_size=64,
        emphasis_font_size=72,
        cues=cues,
    )


def test_registry_and_shared_contract_policy_have_exactly_nine_animations() -> None:
    assert set(CAPTION_EFFECTS) == CAPTION_EFFECT_IDS
    assert set(get_args(CaptionEffectId)) == CAPTION_EFFECT_IDS
    assert set(ANIMATED_CAPTION_EFFECT_IDS) == set(_GOLDEN_DIALOGUE_TEXT)
    assert len(ANIMATED_CAPTION_EFFECT_IDS) == 9


@pytest.mark.parametrize("effect_id", ANIMATED_CAPTION_EFFECT_IDS)
def test_caption_effect_golden_ass(effect_id: str, tmp_path) -> None:
    output = tmp_path / f"{effect_id}.ass"
    write_ass_subtitles(
        output,
        style=StylePlanArtifact.model_validate({"subtitle": {}}),
        caption_composition=_effect_composition(effect_id),
        font_name="Normal Face",
        emphasis_font_name="Emphasis Face",
    )

    content = output.read_text(encoding="utf-8")
    dialogue_text = [
        line.rsplit(",,", maxsplit=1)[1]
        for line in content.splitlines()
        if line.startswith("Dialogue:")
    ]
    assert dialogue_text == _GOLDEN_DIALOGUE_TEXT[effect_id]


@pytest.mark.skipif(
    not ffmpeg_has_filter("subtitles"),
    reason="ffmpeg build does not provide the subtitles filter",
)
def test_all_registered_effects_render_through_libass(
    tmp_path,
    media_fixture_factory,
) -> None:
    subtitle_path = tmp_path / "caption-effects.ass"
    write_ass_subtitles(
        subtitle_path,
        style=StylePlanArtifact.model_validate({"subtitle": {}}),
        caption_composition=_all_effects_composition(),
        font_name="Arial",
        emphasis_font_name="Arial",
    )
    duration = float(len(ANIMATED_CAPTION_EFFECT_IDS))
    rendered = media_fixture_factory.video(
        duration_sec=duration,
        width=320,
        height=568,
        fps=30,
        filename="caption-effects-video.mp4",
    )
    voice = media_fixture_factory.audio(
        duration_sec=duration,
        filename="caption-effects-voice.wav",
    )
    output = tmp_path / "caption-effects-final.mp4"

    render_final_media(
        rendered_path=rendered,
        audio_path=voice,
        output_path=output,
        subtitle_path=subtitle_path,
        bgm_path=None,
        bgm_volume=0.0,
        duration=duration,
        fps=30,
        auto_mix=False,
    )

    assert probe_video_frame_count(output) == len(ANIMATED_CAPTION_EFFECT_IDS) * 30


@pytest.mark.skipif(
    not ffmpeg_has_filter("subtitles"),
    reason="ffmpeg build does not provide the subtitles filter",
)
def test_late_wipe_character_has_a_visible_libass_frame(tmp_path) -> None:
    base = _effect_composition("wipe_reveal")
    base_run = base.cues[0].lines[0].runs[0]
    run = base_run.model_copy(
        update={
            "text": "A",
            "char_span": (0, 1),
            "char_enter_frames": [29],
            "char_advances_px": [40],
        }
    )
    line = base.cues[0].lines[0].model_copy(update={"runs": [run]})
    cue = base.cues[0].model_copy(update={"text": "A", "lines": [line]})
    composition = base.model_copy(update={"cues": [cue]})
    subtitle_path = tmp_path / "late-wipe.ass"
    write_ass_subtitles(
        subtitle_path,
        style=StylePlanArtifact.model_validate({"subtitle": {}}),
        caption_composition=composition,
        font_name="Arial",
        emphasis_font_name="Arial",
    )

    assert "Dialogue: 0,0:00:00.95,0:00:01.00" in subtitle_path.read_text(
        encoding="utf-8"
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
            "color=black:size=320x568:rate=30:duration=1",
            "-vf",
            f"subtitles=filename='{subtitle_path}',select='eq(n,29)',format=gray",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        text=False,
    )
    frame = bytes(result.stdout)
    assert frame
    assert max(frame) - min(frame) > 10


def test_scaled_run_headroom_advances_the_following_run_cursor(tmp_path) -> None:
    pop_run = CaptionRun(
        run_id="run_pop",
        text="A",
        role="emphasis",
        hint_id="hint_0001",
        char_span=(0, 1),
        enter_frame=0,
        exit_frame=30,
        effect_id="pop",
        advance_px=40,
        baseline_offset_px=55,
    )
    normal_run = CaptionRun(
        run_id="run_normal",
        text="B",
        role="normal",
        char_span=(1, 2),
        enter_frame=0,
        exit_frame=30,
        effect_id="none",
        advance_px=40,
        baseline_offset_px=55,
    )
    base = _effect_composition("pop")
    composition = base.model_copy(
        update={
            "cues": [
                base.cues[0].model_copy(
                    update={
                        "text": "AB",
                        "lines": [
                            CaptionLine(
                                runs=[pop_run, normal_run],
                                advance_px=80,
                                animation_headroom_px=2,
                            )
                        ],
                    }
                )
            ]
        }
    )
    output = tmp_path / "mixed-run-headroom.ass"
    write_ass_subtitles(
        output,
        style=StylePlanArtifact.model_validate({"subtitle": {}}),
        caption_composition=composition,
        font_name="Arial",
        emphasis_font_name="Arial",
    )

    positions = [
        tuple(int(value) for value in match.groups())
        for line in output.read_text(encoding="utf-8").splitlines()
        if line.startswith("Dialogue:")
        and (match := re.search(r"\\pos\((\d+),(\d+)\)", line))
    ]
    assert positions == [(499, 1558), (541, 1558)]


def test_wipe_reveal_character_timing_staggers_inside_the_source_token() -> None:
    script = "AB"
    token = SpeechTokenTiming(
        token_id="token_0001",
        text=script,
        start=0.1,
        end=0.4,
        char_span=(0, 2),
    )
    plan = build_caption_composition(
        script=script,
        units=[
            NarrationUnit(
                unit_id="unit_0001",
                text=script,
                start=0.0,
                end=1.0,
                confidence=1.0,
            )
        ],
        tokens=[token],
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
        emphasis_measure=lambda text: len(text) * 20,
        normal_baseline_offset=48,
        emphasis_baseline_offset=55,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
        normal_effect_id="wipe_reveal",
    )

    run = plan.cues[0].lines[0].runs[0]
    assert run.char_enter_frames == [round(token.start * 30), round(token.start * 30) + 1]
    assert run.char_enter_frames[-1] < round(token.end * 30)
    assert run.char_advances_px == [20.0, 20.0]


@pytest.mark.parametrize(
    ("effect_id", "width"),
    [("pop_rotate", 104), ("jelly_pop", 101)],
)
def test_scaled_effect_headroom_never_crosses_the_max_width_boundary(
    effect_id: str,
    width: int,
) -> None:
    script = "重点"
    plan = build_caption_composition(
        script=script,
        units=[
            NarrationUnit(
                unit_id="unit_0001",
                text=script,
                start=0.0,
                end=1.0,
                confidence=1.0,
            )
        ],
        tokens=proportional_tokens(script, start=0.0, end=1.0),
        hints=[EmphasisHint(phrase=script)],
        fps=30,
        total_frames=30,
        width=width,
        height=1920,
        band=CaptionBand(max_width_ratio=1.0),
        normal_enabled=True,
        emphasis_enabled=True,
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id="font_emphasis",
        normal_font_size=64,
        emphasis_font_size=72,
        normal_measure=lambda text: len(text) * 40,
        emphasis_measure=lambda text: len(text) * 48,
        normal_baseline_offset=48,
        emphasis_baseline_offset=55,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
        emphasis_effect_ids=[effect_id],
    )

    assert plan.diagnostics.hints_applied == 1
    assert [
        run.effect_id
        for run in plan.cues[0].lines[0].runs
        if run.role == "emphasis"
    ] == [effect_id]
    assert all(
        line.advance_px + line.animation_headroom_px <= width
        for cue in plan.cues
        for line in cue.lines
    )


def test_empty_normal_segments_do_not_add_fixed_wipe_headroom() -> None:
    script = "重点"
    plan = build_caption_composition(
        script=script,
        units=[
            NarrationUnit(
                unit_id="unit_0001",
                text=script,
                start=0.0,
                end=1.0,
                confidence=1.0,
            )
        ],
        tokens=proportional_tokens(script, start=0.0, end=1.0),
        hints=[EmphasisHint(phrase=script)],
        fps=30,
        total_frames=30,
        width=140,
        height=1920,
        band=CaptionBand(max_width_ratio=1.0),
        normal_enabled=True,
        emphasis_enabled=True,
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id="font_emphasis",
        normal_font_size=64,
        emphasis_font_size=72,
        normal_measure=lambda text: len(text) * 20,
        emphasis_measure=lambda text: len(text) * 62.4,
        normal_baseline_offset=48,
        emphasis_baseline_offset=55,
        timing_source="native",
        normal_metrics_source="hmtx",
        emphasis_metrics_source="hmtx",
        normal_effect_id="wipe_reveal",
        emphasis_effect_ids=["pop_rotate"],
    )

    assert plan.diagnostics.hints_applied == 1
    assert plan.diagnostics.hints_unbreakable == 0
    line = plan.cues[0].lines[0]
    assert line.advance_px + line.animation_headroom_px <= 140


def test_contract_rejects_effects_that_are_not_allowed_for_the_run_role() -> None:
    base = {
        "run_id": "run_invalid",
        "text": "字",
        "char_span": (0, 1),
        "enter_frame": 0,
        "exit_frame": 1,
        "advance_px": 10,
        "baseline_offset_px": 0,
    }
    with pytest.raises(ValidationError, match="normal caption run cannot use pop_rotate"):
        CaptionRun.model_validate({**base, "role": "normal", "effect_id": "pop_rotate"})
    with pytest.raises(ValidationError, match="emphasis caption run cannot use fade_through"):
        CaptionRun.model_validate(
            {
                **base,
                "role": "emphasis",
                "hint_id": "hint_0001",
                "effect_id": "fade_through",
            }
        )


def test_zoom_settle_requires_whole_cue_display_mode() -> None:
    script = "重点"
    with pytest.raises(ValueError, match="requires whole_cue"):
        build_caption_composition(
            script=script,
            units=[
                NarrationUnit(
                    unit_id="unit_0001",
                    text=script,
                    start=0.0,
                    end=1.0,
                    confidence=1.0,
                )
            ],
            tokens=proportional_tokens(script, start=0.0, end=1.0),
            hints=[EmphasisHint(phrase=script)],
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
            emphasis_font_size=72,
            normal_measure=lambda text: len(text) * 20,
            emphasis_measure=lambda text: len(text) * 20,
            normal_baseline_offset=48,
            emphasis_baseline_offset=55,
            timing_source="native",
            normal_metrics_source="hmtx",
            emphasis_metrics_source="hmtx",
            emphasis_effect_ids=["zoom_settle"],
        )
