from __future__ import annotations

from packages.core.contracts import (
    DegradationCode,
    MediaAssetRecord,
    SpeechTiming,
    TtsSpeechOutput,
    WarningCode,
)
from packages.core.contracts.artifacts import AlignmentArtifact, EmphasisHint, OverlayEvent
from packages.core.contracts.artifacts import RawSpeechAlignmentArtifact
from packages.media.rendering import validate_rendered_output
from packages.media.video.ffmpeg import probe_audio_channels
from packages.production.pipeline._caption_visual_presets import CAPTION_VISUAL_PRESETS
from packages.production.pipeline._caption_effects import effect_envelope, overlay_effect_tags
from packages.production.pipeline._caption_window_planner import (
    build_caption_option_candidates,
    build_emphasis_windows,
    compile_normal_windows,
)
from packages.production.pipeline._ffmpeg import (
    SfxMixEvent,
    _build_audio_filters,
    render_final_media,
)
from packages.production.pipeline._sfx_events import plan_caption_sfx_events
from packages.production.pipeline._speech_timing import normalize_timing_for_script
from packages.production.pipeline._subtitles import write_ass_subtitles


def test_provider_neutral_tts_outputs_normalize_identically_for_two_fake_adapters():
    timing = SpeechTiming.model_validate(
        {
            "segments": [{"text": "限时五折", "start": 0.0, "end": 1.0}],
            "tokens": [
                {"text": "限时", "start": 0.0, "end": 0.4},
                {"text": "五折", "start": 0.4, "end": 1.0},
            ],
            "granularity": "token",
            "text_basis": "original",
        }
    )

    def fake_adapter(audio_id: str) -> TtsSpeechOutput:
        return TtsSpeechOutput(
            audio_artifact_id=audio_id,
            audio_uri=f"s3://audio/{audio_id}.wav",
            duration_sec=1.0,
            timing=timing,
        )

    outputs = [fake_adapter("fake_a"), fake_adapter("fake_b")]
    raw_artifacts = [
        RawSpeechAlignmentArtifact(
            audio_artifact_id="audio",
            timing=output.timing,
        ).model_dump(mode="json")
        for output in outputs
        if output.timing is not None
    ]
    downstream = [
        normalize_timing_for_script(
            output.timing,
            script="限时五折",
            duration=output.duration_sec,
        )
        for output in outputs
        if output.timing is not None
    ]
    assert raw_artifacts[0] == raw_artifacts[1]
    assert downstream[0] == downstream[1]


def test_normalized_tts_text_uses_sentence_local_display_fallback_with_diagnostics():
    timing = SpeechTiming.model_validate(
        {
            "segments": [{"text": "只要两千元", "start": 0.0, "end": 1.0}],
            "tokens": [
                {"text": "只要", "start": 0.0, "end": 0.3},
                {"text": "两千元", "start": 0.3, "end": 1.0},
            ],
            "granularity": "token",
            "text_basis": "normalized",
        }
    )
    segments, tokens, diagnostics = normalize_timing_for_script(
        timing,
        script="只要2000元",
        duration=1.0,
    )
    assert segments[0].text == "只要2000元"
    assert "".join(token.text for token in tokens) == "只要2000元"
    assert diagnostics["token_matched"] == 0
    assert diagnostics["char_fallback"] == len(tokens)


def test_timing_normalizer_handles_token_only_empty_and_multi_segment_mismatch():
    token_only = SpeechTiming.model_validate(
        {
            "tokens": [{"text": "你好", "start": 0.1, "end": 0.8}],
            "granularity": "token",
            "text_basis": "original",
        }
    )
    segments, tokens, diagnostics = normalize_timing_for_script(
        token_only,
        script="你好",
        duration=1.0,
    )
    assert [(item.start, item.end) for item in segments] == [(0.1, 0.8)]
    assert tokens[0].text == "你好"
    assert diagnostics["token_matched"] == 1

    assert normalize_timing_for_script(
        SpeechTiming(granularity="segment"),
        script="",
        duration=0,
    )[:2] == ([], [])

    mismatched = SpeechTiming.model_validate(
        {
            "segments": [
                {"text": "第一段", "start": 0.0, "end": 1.0},
                {"text": "第二段", "start": 1.0, "end": 2.0},
            ],
            "granularity": "segment",
            "text_basis": "normalized",
        }
    )
    repaired, repaired_tokens, _diagnostics = normalize_timing_for_script(
        mismatched,
        script="总共2000元",
        duration=2.0,
    )
    assert len(repaired) == 2
    assert "".join(item.text for item in repaired_tokens) == "总共2000元"


def test_tokens_drive_phrase_boundaries_and_hero_only_exists_at_a_cut():
    units = [{"unit_id": "u1", "text": "开场限时五折", "start": 0.0, "end": 3.0}]
    tokens = [
        {"text": "开场", "start": 0.0, "end": 1.0},
        {"text": "限时", "start": 1.0, "end": 1.4},
        {"text": "五折", "start": 1.4, "end": 2.0},
    ]
    windows, total, dropped, matched, fallback = build_emphasis_windows(
        emphasis=[EmphasisHint(phrase="限时五折")],
        units=units,
        fps=30,
        total_frames=90,
        cut_frames={30},
        resolution=(1080, 1920),
        normal_caption_top_y=0.75,
        tokens=tokens,
    )
    assert (total, dropped, matched, fallback) == (1, 0, 1, 0)
    assert (windows[0]["start_frame"], windows[0]["end_frame"]) == (30, 60)
    assert windows[0]["hero_eligible"] is True

    anchor = {
        "anchor_id": "center",
        "rect": {"x": 0.2, "y": 0.15, "w": 0.6, "h": 0.3},
        "text_align": "center",
    }
    at_cut = build_caption_option_candidates(
        event_id="e1",
        text="限时五折",
        anchors=[anchor],
        width=1080,
        height=1920,
        measure=lambda text: len(text) * 35.0,
        font_size=64.0,
        outline=5.0,
        shadow=1.0,
        normal_safe_rect=None,
        hero_eligible=True,
    )
    away_from_cut = build_caption_option_candidates(
        event_id="e1",
        text="限时五折",
        anchors=[anchor],
        width=1080,
        height=1920,
        measure=lambda text: len(text) * 35.0,
        font_size=64.0,
        outline=5.0,
        shadow=1.0,
        normal_safe_rect=None,
        hero_eligible=False,
    )
    assert {item["visual_preset_id"] for item in at_cut} == {"emphasis", "hero"}
    assert {item["visual_preset_id"] for item in away_from_cut} == {"emphasis"}
    by_preset = {item["visual_preset_id"]: item for item in at_cut}
    assert by_preset["hero"]["safety_envelope"]["w"] > by_preset["emphasis"]["safety_envelope"]["w"]
    assert CAPTION_VISUAL_PRESETS["normal"].size_ratio == 1.0
    assert CAPTION_VISUAL_PRESETS["emphasis"].size_ratio == 1.25
    assert CAPTION_VISUAL_PRESETS["hero"].size_ratio == 2.2


def test_normal_cue_uses_token_bounds_and_delays_second_line():
    text = "甲乙丙丁戊己"
    tokens = [
        {"text": char, "start": index * 0.25, "end": (index + 1) * 0.25}
        for index, char in enumerate(text)
    ]
    windows, diagnostics = compile_normal_windows(
        units=[{"unit_id": "u1", "text": text, "start": 0.0, "end": 1.5}],
        resolution=(400, 800),
        fps=30,
        total_frames=45,
        margin_l=40,
        margin_r=40,
        measure=lambda value: len(value) * 80.0,
        metrics_source="hmtx",
        enabled=True,
        tokens=tokens,
        cut_frames=set(),
    )
    window = windows[0]
    assert (window["start_frame"], window["end_frame"]) == (0, 45)
    assert len(window["lines"]) == 2
    assert window["line_start_frames"][1] > window["line_start_frames"][0]
    assert diagnostics["token_matched"] == len(tokens)


def test_ass_has_three_sizes_three_effects_and_inline_dual_color(tmp_path):
    output = tmp_path / "caption-v3.ass"
    write_ass_subtitles(
        output,
        style={
            "subtitle": {
                "font_size": 40,
                "primary_color": "#FFFFFF",
                "emphasis_primary_color": "#FFFF00",
            }
        },
        width=1080,
        height=1080,
        caption_cues=[
            {
                "start": 0.0,
                "end": 1.0,
                "lines": ["普通字幕"],
                "effect_id": "soft_in",
            }
        ],
        font_name="Noto Serif CJK SC",
        emphasis_font_name="Noto Serif CJK SC",
        overlay_events=[
            {
                "event_id": "e1",
                "text": "限时2000元",
                "start": 0.2,
                "end": 0.8,
                "visual_preset_id": "emphasis",
                "animation_id": "pop",
                "rect": {"x": 0.2, "y": 0.2, "w": 0.6, "h": 0.2},
                "text_align": "center",
            },
            {
                "event_id": "e2",
                "text": "核心卖点",
                "start": 1.0,
                "end": 1.5,
                "visual_preset_id": "hero",
                "animation_id": "slam_scale",
                "rect": {"x": 0.1, "y": 0.35, "w": 0.8, "h": 0.3},
                "text_align": "center",
            },
        ],
    )
    ass = output.read_text(encoding="utf-8")
    assert "Style: Default,Noto Serif CJK SC,40," in ass
    assert "Style: Emphasis,Noto Serif CJK SC,50,&H00FFFFFF" in ass
    assert "Style: Hero,Noto Serif CJK SC,88,&H00FFFFFF" in ass
    assert "\\move(540," in ass and "\\fad(120,0)" in ass
    assert "\\fscx85\\fscy85" in ass
    assert "\\fscx220\\fscy220" in ass
    assert "限时{\\1c&H0000FFFF}2000元{\\1c&H00FFFFFF}" in ass


def test_sfx_events_are_frame_synced_density_limited_and_prioritized():
    events = plan_caption_sfx_events(
        normal_cues=[
            {"window_id": "n1", "start": 0.5, "effect_id": "soft_in"},
            {"window_id": "n2", "start": 0.6, "effect_id": "soft_in"},
            {"window_id": "n3", "start": 1.1, "effect_id": "soft_in"},
        ],
        overlay_events=[
            {
                "event_id": "hero",
                "start": 0.5,
                "sfx_id": "asset_sfx_impact",
                "priority": 90,
                "visual_preset_id": "hero",
            }
        ],
        duration=2.0,
    )
    assert events[0]["asset_id"] == "asset_sfx_impact"
    assert abs(events[0]["start_ms"] - 500) <= 50
    assert [event["start_ms"] for event in events].count(500) == 1
    assert not any(event["start_ms"] == 600 for event in events)


def test_no_sfx_keeps_legacy_mix_shape_except_for_stereo_pinning():
    graph = _build_audio_filters(
        duration=2.0,
        bgm_volume=0.2,
        auto_mix=False,
        fade_in=0.0,
        fade_out=0.0,
        bgm_source_start=0.0,
        bgm_source_end=2.0,
        sfx_events=[],
    )
    assert "amix=inputs=2" in graph
    assert "aformat=channel_layouts=stereo" in graph
    assert "alimiter" not in graph


def test_bgm_and_sfx_share_one_limited_mix_and_effect_fallbacks_are_bounded(tmp_path):
    graph = _build_audio_filters(
        duration=2.0,
        bgm_volume=0.2,
        auto_mix=False,
        fade_in=0.0,
        fade_out=0.0,
        bgm_source_start=0.0,
        bgm_source_end=2.0,
        sfx_events=[SfxMixEvent(path=tmp_path / "ding.wav", start_ms=-10, volume=-1.0)],
    )
    assert "[voice][bgm][sfx0]amix=inputs=3" in graph
    assert "adelay=0|0" in graph
    assert "volume=0.000" in graph
    assert graph.count("alimiter=limit=0.97") == 1
    assert effect_envelope("soft_in") == (1.0, 14.0)
    assert effect_envelope("unknown") == (1.0, 0.0)
    assert overlay_effect_tags("unknown", x=10, y=20) == ["\\pos(10,20)"]


def test_real_ffmpeg_sfx_mix_keeps_stereo_and_exact_frame_count(
    tmp_path,
    media_fixture_factory,
):
    video = media_fixture_factory.video(
        duration_sec=1.0,
        width=320,
        height=568,
        fps=30,
        filename="caption-v3-video.mp4",
    )
    voice = media_fixture_factory.audio(
        duration_sec=1.0,
        sample_rate=48000,
        filename="caption-v3-voice.wav",
    )
    sfx = media_fixture_factory.audio(
        duration_sec=0.15,
        sample_rate=48000,
        frequency=880,
        filename="caption-v3-ding.wav",
    )
    output = tmp_path / "caption-v3-final.mp4"
    render_final_media(
        rendered_path=video,
        audio_path=voice,
        output_path=output,
        subtitle_path=None,
        bgm_path=None,
        bgm_volume=0.0,
        duration=1.0,
        fps=30,
        sfx_events=[SfxMixEvent(path=sfx, start_ms=333, volume=0.4, asset_id="ding")],
    )
    validate_rendered_output(output, expected_frames=30)
    assert probe_audio_channels(output) == 2


def test_legacy_payload_defaults_and_sfx_contract_are_backward_compatible():
    alignment = AlignmentArtifact.model_validate({"audio_artifact_id": "audio", "segments": []})
    overlay = OverlayEvent.model_validate({"start": 0.0, "end": 1.0, "text": "旧花字"})
    sfx = MediaAssetRecord(id="sfx", title="Click", kind="sfx")
    assert alignment.tokens == []
    assert overlay.visual_preset_id is None
    assert sfx.kind == "sfx"
    assert WarningCode.sfx_asset_missing.value == "sfx.asset_missing"
    assert DegradationCode.sfx_mix_failed.value == "sfx.mix_failed"
    assert set(CAPTION_VISUAL_PRESETS) == {"normal", "emphasis", "hero"}
