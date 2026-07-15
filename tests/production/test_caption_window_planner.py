from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    WarningCode,
)
from packages.core.contracts.artifacts import EmphasisHint
from packages.production.pipeline import _caption_visual_safety as visual
from packages.production.pipeline._font_metrics import FontMetrics, make_text_measurer
from packages.production.pipeline._fonts import ResolvedFont
from packages.production.pipeline._caption_window_planner import (
    build_normal_caption_position_candidates,
    build_caption_option_candidates,
    build_emphasis_windows,
    compile_normal_windows,
    emphasis_conflict_graph,
    finalize_safe_caption_options,
    max_feasible_emphasis_count,
)
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.nodes import caption_window_planning
from packages.core.workflow import NodeExecutionError


def _measure(text: str) -> float:
    return len(text) * 40.0


def _patch_resolved_caption_fonts(monkeypatch) -> None:
    metrics = FontMetrics(
        upem=1000,
        ascender=800,
        descender=-200,
        cmap={},
        advances={},
    )
    monkeypatch.setattr(
        caption_window_planning,
        "resolve_font_asset",
        lambda *, font_asset_id, runtime_dir, **_kwargs: (
            ResolvedFont(
                family_name=(
                    "Noto Serif CJK SC" if "serif" in font_asset_id else "Noto Sans CJK SC"
                ),
                fonts_dir=runtime_dir,
                source_path=runtime_dir / f"{font_asset_id}.otf",
            ),
            None,
        ),
    )
    monkeypatch.setattr(caption_window_planning, "load_font_metrics", lambda _path: metrics)
    monkeypatch.setattr(caption_window_planning, "font_text_safety_issue", lambda *_args: None)


def test_normal_windows_are_frame_authoritative_and_keep_unit_ids():
    windows, diagnostics = compile_normal_windows(
        units=[
            {
                "unit_id": "unit-real-id",
                "text": "正常字幕内容",
                "start": 0.1,
                "end": 1.1,
            }
        ],
        resolution=(1080, 1920),
        fps=30,
        total_frames=60,
        margin_l=80,
        margin_r=80,
        measure=_measure,
        metrics_source="hmtx",
        enabled=True,
    )
    assert windows == [
        {
            "window_id": "caption_001",
            "start_frame": 3,
            "end_frame": 33,
            "spoken_span": {"start_frame": 3, "end_frame": 33},
            "display_span": {"start_frame": 3, "end_frame": 33},
            "token_ids": [],
            "char_span": None,
            "lines": ["正常字幕内容"],
            "line_start_frames": [3],
            "source_unit_ids": ["unit-real-id"],
            "normalized_text": "正常字幕内容",
            "visual_preset_id": "normal",
            "effect_id": "soft_in",
        }
    ]
    assert diagnostics["font_metrics_source"] == "hmtx"


def test_three_line_windows_get_token_authoritative_progressive_starts():
    text = "一二三四五六七八九"
    tokens = [
        {
            "token_id": f"t{index}",
            "text": char,
            "start": (index - 1) * 0.4,
            "end": index * 0.4,
            "source_unit_id": "unit_001",
        }
        for index, char in enumerate(text, start=1)
    ]
    windows, diagnostics = compile_normal_windows(
        units=[{"unit_id": "unit_001", "text": text, "start": 0.0, "end": 3.6}],
        resolution=(360, 640),
        fps=30,
        total_frames=108,
        margin_l=20,
        margin_r=20,
        measure=lambda value: len(value) * 40.0,
        metrics_source="hmtx",
        enabled=True,
        tokens=tokens,
        max_lines=3,
        max_line_width_px=140,
    )

    assert len(windows) == 1
    assert windows[0]["lines"] == ["一二三", "四五六", "七八九"]
    assert windows[0]["line_start_frames"] == [0, 36, 72]
    assert diagnostics["token_matched"] == 9


def test_normal_position_candidates_are_measured_and_cover_reference_bands():
    candidates = build_normal_caption_position_candidates(
        window_id="caption_001",
        lines=["你拿着", "这张图纸", "去做交付"],
        width=1080,
        height=1920,
        measure=lambda value: len(value) * 54.0,
        font_size=84.0,
        outline=1.0,
        shadow_x=6.0,
        shadow_y=3.0,
        requested_position_y=0.87,
        vertical_shift_px=14.0,
    )

    assert len(candidates) == 10
    assert {item["text_align"] for item in candidates} == {"left", "center", "right"}
    assert {item["anchor_id"] for item in candidates} >= {
        "left_upper",
        "right_middle",
        "center_lower",
        "requested_bottom",
    }
    assert all(0.0 <= item["rect"]["x"] <= 1.0 for item in candidates)
    assert all(item["rect"]["x"] + item["rect"]["w"] <= 1.0 for item in candidates)
    assert all(item["rect"]["y"] + item["rect"]["h"] <= 1.0 for item in candidates)
    assert all(item["safety_envelope"]["h"] > item["rect"]["h"] for item in candidates)


def test_normal_position_candidates_reject_geometry_that_cannot_fit_without_clamping():
    candidates = build_normal_caption_position_candidates(
        window_id="caption_too_wide",
        lines=["UNBREAKABLE-PRODUCT-CODE"],
        width=1080,
        height=1920,
        measure=lambda _value: 1400.0,
        font_size=84.0,
        outline=1.0,
        shadow_x=6.0,
        shadow_y=3.0,
        requested_position_y=0.87,
        vertical_shift_px=14.0,
    )

    assert candidates == []


def _normal_position_diagnostics() -> dict:
    return {
        "sampled_frames": 0,
        "visual_analysis_failed": False,
        "unavailable_detectors": [],
        "normal_generated_candidates": 0,
        "normal_dynamic_positioned": 0,
        "normal_relaxed_safety": 0,
        "normal_rejected_face": 0,
        "normal_rejected_scene_text": 0,
        "normal_rejected_busy": 0,
    }


def _normal_measurement(option_candidates, *, face_clear_anchor, scene_text=0.0, busy=0.0):
    return visual.OptionMeasurementResult(
        measurements=[
            visual.OptionMeasurement(
                option=option,
                face_overlap=0.0 if option["anchor_id"] == face_clear_anchor else 0.5,
                scene_text_overlap=scene_text,
                busy_score=busy,
            )
            for option in option_candidates
        ],
        sample_frames=[0, 44, 89],
    )


def test_normal_window_uses_face_clear_negative_space_on_three_final_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(
        caption_window_planning,
        "extract_frames_for_times",
        lambda _video, times, **_kwargs: [
            (time, f"frame-{index}.jpg") for index, time in enumerate(times)
        ],
    )
    monkeypatch.setattr(cv2, "imread", lambda _path: np.zeros((8, 8, 3), dtype=np.uint8))
    monkeypatch.setattr(
        caption_window_planning,
        "measure_option_candidates",
        lambda *, option_candidates, **_kwargs: _normal_measurement(
            option_candidates, face_clear_anchor="right_upper"
        ),
    )
    windows = [
        {
            "window_id": "caption_001",
            "start_frame": 0,
            "end_frame": 90,
            "lines": ["材料谁都能买"],
        }
    ]
    diagnostics = _normal_position_diagnostics()

    caption_window_planning._analyze_normal_windows(
        video_path="final.mp4",
        temp_dir=tmp_path,
        fps=30,
        windows=windows,
        diagnostics=diagnostics,
        width=1080,
        height=1920,
        measure=lambda value: len(value) * 54.0,
        font_size=84.0,
        outline=1.0,
        shadow_x=6.0,
        shadow_y=3.0,
        requested_position_y=0.87,
    )

    assert windows[0]["text_align"] == "right"
    assert windows[0]["rect"]["y"] == 0.3
    assert diagnostics["sampled_frames"] == 3
    assert diagnostics["normal_dynamic_positioned"] == 1


def test_normal_window_relaxes_scene_text_but_never_the_face_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        caption_window_planning,
        "extract_frames_for_times",
        lambda _video, times, **_kwargs: [
            (time, f"frame-{index}.jpg") for index, time in enumerate(times)
        ],
    )
    monkeypatch.setattr(cv2, "imread", lambda _path: np.zeros((8, 8, 3), dtype=np.uint8))
    monkeypatch.setattr(
        caption_window_planning,
        "measure_option_candidates",
        lambda *, option_candidates, **_kwargs: _normal_measurement(
            option_candidates,
            face_clear_anchor="left_middle",
            scene_text=0.5,
            busy=0.95,
        ),
    )
    windows = [
        {
            "window_id": "caption_001",
            "start_frame": 0,
            "end_frame": 90,
            "lines": ["你拿着这张图纸"],
        }
    ]
    diagnostics = _normal_position_diagnostics()

    caption_window_planning._analyze_normal_windows(
        video_path="final.mp4",
        temp_dir=tmp_path,
        fps=30,
        windows=windows,
        diagnostics=diagnostics,
        width=1080,
        height=1920,
        measure=lambda value: len(value) * 54.0,
        font_size=84.0,
        outline=1.0,
        shadow_x=6.0,
        shadow_y=3.0,
        requested_position_y=0.87,
    )

    assert windows[0]["text_align"] == "left"
    assert windows[0]["rect"]["y"] == 0.42
    assert diagnostics["normal_relaxed_safety"] == 1
    assert diagnostics["normal_dynamic_positioned"] == 1


def test_normal_window_fails_closed_when_every_position_crosses_face_red_line(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        caption_window_planning,
        "extract_frames_for_times",
        lambda _video, times, **_kwargs: [
            (time, f"frame-{index}.jpg") for index, time in enumerate(times)
        ],
    )
    monkeypatch.setattr(cv2, "imread", lambda _path: np.zeros((8, 8, 3), dtype=np.uint8))
    monkeypatch.setattr(
        caption_window_planning,
        "measure_option_candidates",
        lambda *, option_candidates, sample_frames, **_kwargs: visual.OptionMeasurementResult(
            measurements=[
                visual.OptionMeasurement(
                    option=option,
                    face_overlap=0.5,
                    scene_text_overlap=0.0,
                    busy_score=0.0,
                )
                for option in option_candidates
            ],
            sample_frames=list(sample_frames),
        ),
    )
    windows = [
        {
            "window_id": "caption_face_blocked",
            "start_frame": 0,
            "end_frame": 90,
            "lines": ["不要盖住人脸"],
            "effect_id": "soft_in",
        }
    ]
    diagnostics = _normal_position_diagnostics()

    with pytest.raises(NodeExecutionError, match="没有任何通过人脸安全红线的位置"):
        caption_window_planning._analyze_normal_windows(
            video_path="final.mp4",
            temp_dir=tmp_path,
            fps=30,
            windows=windows,
            diagnostics=diagnostics,
            width=1080,
            height=1920,
            measure=lambda value: len(value) * 54.0,
            font_size=84.0,
            outline=1.0,
            shadow_x=6.0,
            shadow_y=3.0,
            requested_position_y=0.87,
        )

    assert diagnostics["visual_analysis_failed"] is False


def test_normal_window_fails_closed_when_visual_analysis_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        caption_window_planning,
        "measure_option_candidates",
        lambda *, sample_frames, **_kwargs: visual.OptionMeasurementResult(
            measurements=[],
            sample_frames=list(sample_frames),
            unavailable_detector="face",
        ),
    )
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    windows = [
        {
            "window_id": "caption_detector_failed",
            "start_frame": 0,
            "end_frame": 90,
            "lines": ["不能拿未知检测结果冒充安全"],
        }
    ]
    diagnostics = _normal_position_diagnostics()

    with pytest.raises(NodeExecutionError, match="拒绝未经人脸红线验证"):
        caption_window_planning._analyze_normal_windows(
            video_path="final.mp4",
            temp_dir=tmp_path,
            fps=30,
            windows=windows,
            diagnostics=diagnostics,
            width=1080,
            height=1920,
            measure=lambda value: len(value) * 54.0,
            font_size=84.0,
            outline=1.0,
            shadow_x=6.0,
            shadow_y=3.0,
            requested_position_y=0.87,
            frame_images={0: image, 44: image, 89: image},
        )

    assert diagnostics["visual_analysis_failed"] is True
    assert diagnostics["unavailable_detectors"] == ["face"]


def test_normal_window_fails_closed_when_true_geometry_has_no_candidate(tmp_path):
    windows = [
        {
            "window_id": "caption_too_wide",
            "start_frame": 0,
            "end_frame": 90,
            "lines": ["UNBREAKABLE-PRODUCT-CODE"],
        }
    ]
    diagnostics = _normal_position_diagnostics()

    with pytest.raises(NodeExecutionError, match="normal_no_position_candidates"):
        caption_window_planning._analyze_normal_windows(
            video_path="final.mp4",
            temp_dir=tmp_path,
            fps=30,
            windows=windows,
            diagnostics=diagnostics,
            width=1080,
            height=1920,
            measure=lambda _value: 1400.0,
            font_size=84.0,
            outline=1.0,
            shadow_x=6.0,
            shadow_y=3.0,
            requested_position_y=0.87,
            frame_images={},
        )



def test_window_frame_cache_batches_and_deduplicates_layer_observations(
    monkeypatch, tmp_path
):
    calls = []

    def _extract(_video, indices, **_kwargs):
        calls.append(list(indices))
        return [(frame, f"frame-{frame}.jpg") for frame in indices]

    monkeypatch.setattr(caption_window_planning, "extract_frames_for_indices", _extract)
    monkeypatch.setattr(cv2, "imread", lambda path: path)

    images = caption_window_planning._extract_window_frame_images(
        video_path="final.mp4",
        temp_dir=tmp_path,
        windows=[
            {"start_frame": 0, "end_frame": 90},
            {"start_frame": 0, "end_frame": 90},
            {"start_frame": 90, "end_frame": 180},
        ],
    )

    assert calls == [[0, 44, 89, 90, 134, 179]]
    assert sorted(images) == calls[0]


def test_phrase_window_clamps_to_cut_segment_instead_of_dropping_whole_unit():
    units = [
        {
            "unit_id": "u1",
            "text": "开头铺垫然后出现限时五折",
            "start": 0.0,
            "end": 4.0,
        }
    ]
    (
        windows,
        candidate_count,
        dropped,
        token_matched,
        char_fallback,
        hold_extended,
        hold_below_min,
    ) = build_emphasis_windows(
        emphasis=[EmphasisHint(phrase="限时五折")],
        units=units,
        fps=30,
        total_frames=120,
        cut_frames={60},
        resolution=(1080, 1920),
        normal_caption_top_y=0.75,
    )
    assert candidate_count == 1
    assert dropped == 0
    assert token_matched == 0
    assert char_fallback == 1
    assert (hold_extended, hold_below_min) == (0, 0)
    assert len(windows) == 1
    assert 60 <= windows[0]["start_frame"] < windows[0]["end_frame"] <= 120
    assert windows[0]["source_unit_ids"] == ["u1"]


def _emphasis_windows(*, units, tokens, emphasis, total_frames, cut_frames=None):
    """Build token-driven emphasis windows through the full planner path."""
    return build_emphasis_windows(
        emphasis=[EmphasisHint(phrase=phrase) for phrase in emphasis],
        units=units,
        fps=30,
        total_frames=total_frames,
        cut_frames=cut_frames or set(),
        resolution=(1080, 1920),
        normal_caption_top_y=0.75,
        tokens=tokens,
    )


def test_emphasis_hold_extends_short_window_to_minimum_without_moving_start():
    units = [{"unit_id": "u1", "text": "限时五折", "start": 0.0, "end": 3.0}]
    tokens = [
        {"text": "限时", "start": 0.0, "end": 2.0},
        {"text": "五折", "start": 2.0, "end": 2.3},  # 0.3s phrase span -> frames 60..69
    ]
    windows, _count, _dropped, matched, _fallback, hold_extended, hold_below_min = (
        _emphasis_windows(units=units, tokens=tokens, emphasis=["五折"], total_frames=120)
    )
    assert matched == 1
    # start (the cut-in beat) stays exact; the tail extends to exactly 1.2s = 36 frames.
    assert (windows[0]["start_frame"], windows[0]["end_frame"]) == (60, 96)
    assert (hold_extended, hold_below_min) == (1, 0)


def test_emphasis_hold_is_clamped_to_the_segment_cut():
    units = [{"unit_id": "u1", "text": "限时五折", "start": 0.0, "end": 4.0}]
    tokens = [
        {"text": "限时", "start": 0.0, "end": 2.0},
        {"text": "五折", "start": 2.0, "end": 2.3},  # frames 60..69
    ]
    # Cut at frame 78 = start + 0.6s: the hold can only reach the segment end.
    windows, *_rest, hold_extended, hold_below_min = _emphasis_windows(
        units=units, tokens=tokens, emphasis=["五折"], total_frames=180, cut_frames={78}
    )
    assert (windows[0]["start_frame"], windows[0]["end_frame"]) == (60, 78)
    # 0.6s < 1.2s, so the window is extended yet still below the minimum hold.
    assert (hold_extended, hold_below_min) == (1, 1)


def test_emphasis_hold_is_independent_of_unselected_neighbor_and_reports_conflict():
    units = [
        {"unit_id": "u1", "text": "限时五折", "start": 0.0, "end": 3.0},
        {"unit_id": "u2", "text": "包邮", "start": 3.0, "end": 6.0},
    ]
    tokens = [
        {"text": "限时", "start": 0.0, "end": 2.0},
        {"text": "五折", "start": 2.0, "end": 2.3},  # window A -> frames 60..69
        {"text": "包邮", "start": 3.5, "end": 4.0},  # window B start = frame 105
    ]
    windows, *_rest, hold_extended, hold_below_min = _emphasis_windows(
        units=units, tokens=tokens, emphasis=["五折", "包邮"], total_frames=180
    )
    # Each candidate receives its own legal minimum hold. Candidate-to-candidate
    # density is solved later, so an unselected neighbour cannot shorten A.
    assert (windows[0]["start_frame"], windows[0]["end_frame"]) == (60, 96)
    assert (windows[1]["start_frame"], windows[1]["end_frame"]) == (105, 141)
    assert (hold_extended, hold_below_min) == (2, 0)
    for window in windows:
        window["caption_options"] = [{"caption_option_id": f"{window['event_id']}__safe"}]
    assert emphasis_conflict_graph(windows, fps=30) == [
        {
            "first_event_id": windows[0]["event_id"],
            "second_event_id": windows[1]["event_id"],
            "actual_gap_frames": 9,
            "required_gap_frames": 24,
            "reason": "insufficient_gap",
        }
    ]
    assert max_feasible_emphasis_count(windows, fps=30) == 1


def test_emphasis_hold_leaves_already_long_window_untouched():
    units = [{"unit_id": "u1", "text": "限时五折", "start": 0.0, "end": 4.0}]
    tokens = [
        {"text": "限时", "start": 0.0, "end": 2.0},
        {"text": "五折", "start": 2.0, "end": 3.5},  # 1.5s span -> frames 60..105
    ]
    windows, *_rest, hold_extended, hold_below_min = _emphasis_windows(
        units=units, tokens=tokens, emphasis=["五折"], total_frames=180
    )
    assert (windows[0]["start_frame"], windows[0]["end_frame"]) == (60, 105)
    assert (hold_extended, hold_below_min) == (0, 0)


def test_emphasis_hold_never_shortens_a_candidate_to_make_neighbor_fit():
    units = [
        {"unit_id": "u1", "text": "限时五折", "start": 0.0, "end": 3.0},
        {"unit_id": "u2", "text": "包邮", "start": 3.0, "end": 6.0},
    ]
    tokens = [
        {"text": "限时", "start": 0.0, "end": 2.0},
        {"text": "五折", "start": 2.0, "end": 2.9},  # window A -> frames 60..87
        {"text": "包邮", "start": 3.0, "end": 4.0},  # window B start = frame 90
    ]
    windows, *_rest = _emphasis_windows(
        units=units, tokens=tokens, emphasis=["五折", "包邮"], total_frames=180
    )
    # A is extended to its own minimum hold even though that makes the candidates
    # overlap. The downstream local solver will keep the stronger legal subset.
    assert windows[0]["end_frame"] == 96


def test_run_32495477be9a_conflict_graph_has_maximum_feasible_five():
    spans = [
        ("hz_001", 42, 71),
        ("hz_002", 174, 202),
        ("hz_003", 298, 334),
        ("hz_004", 465, 509),
        ("hz_005", 572, 602),
        ("hz_006", 610, 647),
    ]
    windows = [
        {
            "event_id": event_id,
            "start_frame": start,
            "end_frame": end,
            "caption_options": [{"caption_option_id": f"{event_id}__safe"}],
        }
        for event_id, start, end in spans
    ]

    assert emphasis_conflict_graph(windows, fps=30) == [
        {
            "first_event_id": "hz_005",
            "second_event_id": "hz_006",
            "actual_gap_frames": 8,
            "required_gap_frames": 24,
            "reason": "insufficient_gap",
        }
    ]
    assert max_feasible_emphasis_count(windows, fps=30) == 5


def test_hmtx_font_and_final_ass_size_drive_option_envelope():
    metrics = FontMetrics(
        upem=1000,
        ascender=800,
        descender=-200,
        cmap={ord("A"): "A"},
        advances={"A": 500},
    )
    anchor = {
        "anchor_id": "a1",
        "rect": {"x": 0.2, "y": 0.2, "w": 0.6, "h": 0.2},
        "text_align": "center",
        "allowed_enter_directions": [],
    }
    measure_small, source = make_text_measurer(metrics, 40.0)
    measure_large, _source = make_text_measurer(metrics, 80.0)
    small = build_caption_option_candidates(
        event_id="e1",
        text="AAAA",
        anchors=[anchor],
        width=1000,
        height=1000,
        measure=measure_small,
        font_size=40.0,
        outline=4.0,
        shadow=1.0,
        normal_safe_rect=None,
    )
    large = build_caption_option_candidates(
        event_id="e1",
        text="AAAA",
        anchors=[anchor],
        width=1000,
        height=1000,
        measure=measure_large,
        font_size=80.0,
        outline=4.0,
        shadow=1.0,
        normal_safe_rect=None,
    )

    assert source == "hmtx"
    small_pop = next(item for item in small if item["animation_id"] == "pop")
    large_pop = next(item for item in large if item["animation_id"] == "pop")
    assert large_pop["safety_envelope"]["w"] > small_pop["safety_envelope"]["w"]
    assert large_pop["safety_envelope"]["h"] > small_pop["safety_envelope"]["h"]


def _font_safety_context(*, normal_font_id="normal_font", emphasis_font_id="emphasis_font"):
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="AAAA 限时五折",
        voice={"voice_id": "voice_demo"},
        subtitle={
            "font_id": normal_font_id,
            "emphasis_font_id": emphasis_font_id,
            "font_size": 40,
            "emphasis_font_size": 80,
            "normal_enabled": True,
            "emphasis_enabled": True,
        },
        bgm={"enabled": False},
        output={"width": 1080, "height": 1080, "fps": 30},
    )
    state = RunState(
        request=request,
        artifacts={
            ArtifactKind.video_rendered: _artifact(
                ArtifactKind.video_rendered, {}, "RenderedVideoArtifact.v1"
            ),
            ArtifactKind.plan_timeline: _artifact(
                ArtifactKind.plan_timeline,
                {"fps": 30, "total_frames": 120, "tracks": []},
                "TimelineArtifact.v1",
            ),
            ArtifactKind.narration_units: _artifact(
                ArtifactKind.narration_units,
                {
                    "units": [
                        {
                            "unit_id": "u1",
                            "text": "AAAA 限时五折",
                            "start": 0.0,
                            "end": 4.0,
                        }
                    ]
                },
                "NarrationUnitsArtifact.v1",
            ),
            ArtifactKind.creative_intent: _artifact(
                ArtifactKind.creative_intent,
                {"emphasis": [{"phrase": "限时五折"}]},
                "CreativeIntentArtifact.v1",
            ),
        },
    )

    class _Context:
        node_run = SimpleNamespace(node_id="CaptionWindowPlanning")
        repository = SimpleNamespace(media_assets={})

        def __init__(self, run_state):
            self.state = run_state

        def source_artifact_for_asset(self, _asset_id):
            raise AssertionError("resolver is patched")

        def artifact_path(self, _artifact_value):
            return Path("/tmp/final-video.mp4")

        def artifact(self, kind, payload, payload_schema):
            return _artifact(kind, payload, payload_schema)

    return _Context(state)


def _test_font_metrics() -> FontMetrics:
    return FontMetrics(
        upem=1000,
        ascender=800,
        descender=-200,
        cmap={ord("A"): "A"},
        advances={"A": 500},
    )


def test_missing_emphasis_font_fails_closed(monkeypatch, tmp_path):
    normal_font = ResolvedFont(
        family_name="Normal",
        fonts_dir=tmp_path,
        source_path=tmp_path / "normal.ttf",
    )

    def _resolve(*, font_asset_id, **_kwargs):
        if font_asset_id == "normal_font":
            return normal_font, None
        return None, font_asset_id

    monkeypatch.setattr(caption_window_planning, "resolve_font_asset", _resolve)
    monkeypatch.setattr(
        caption_window_planning,
        "load_font_metrics",
        lambda _path: _test_font_metrics(),
    )
    monkeypatch.setattr(caption_window_planning, "font_text_safety_issue", lambda *_args: None)

    with pytest.raises(NodeExecutionError, match="无法证明花字像素安全"):
        caption_window_planning.run(
            _font_safety_context(emphasis_font_id="missing_emphasis_font")
        )


@pytest.mark.parametrize(
    ("unreadable_name", "message"),
    [("normal.ttf", "无法读取字幕字体"), ("emphasis.ttf", "无法读取花字字体")],
)
def test_unreadable_font_metrics_fail_closed(monkeypatch, tmp_path, unreadable_name, message):
    fonts = {
        "normal_font": ResolvedFont(
            family_name="Normal",
            fonts_dir=tmp_path,
            source_path=tmp_path / "normal.ttf",
        ),
        "emphasis_font": ResolvedFont(
            family_name="Emphasis",
            fonts_dir=tmp_path,
            source_path=tmp_path / "emphasis.ttf",
        ),
    }
    monkeypatch.setattr(
        caption_window_planning,
        "resolve_font_asset",
        lambda *, font_asset_id, **_kwargs: (fonts[font_asset_id], None),
    )
    monkeypatch.setattr(
        caption_window_planning,
        "load_font_metrics",
        lambda path: None if path.name == unreadable_name else _test_font_metrics(),
    )
    monkeypatch.setattr(caption_window_planning, "font_text_safety_issue", lambda *_args: None)

    with pytest.raises(NodeExecutionError, match=message):
        caption_window_planning.run(_font_safety_context())


def test_distinct_font_assets_with_same_family_fail_closed(monkeypatch, tmp_path):
    fonts = {
        asset_id: ResolvedFont(
            family_name="Shared Family",
            fonts_dir=tmp_path,
            source_path=tmp_path / f"{asset_id}.ttf",
        )
        for asset_id in ("normal_font", "emphasis_font")
    }
    monkeypatch.setattr(
        caption_window_planning,
        "resolve_font_asset",
        lambda *, font_asset_id, **_kwargs: (fonts[font_asset_id], None),
    )
    monkeypatch.setattr(
        caption_window_planning,
        "load_font_metrics",
        lambda _path: _test_font_metrics(),
    )
    monkeypatch.setattr(caption_window_planning, "font_text_safety_issue", lambda *_args: None)

    with pytest.raises(NodeExecutionError, match="同一字体家族"):
        caption_window_planning.run(_font_safety_context())


@pytest.mark.parametrize("collection_asset_id", ["normal_font", "emphasis_font"])
def test_ttc_font_collection_fails_closed(monkeypatch, tmp_path, collection_asset_id):
    fonts = {
        asset_id: ResolvedFont(
            family_name=asset_id,
            fonts_dir=tmp_path,
            source_path=tmp_path
            / f"{asset_id}{'.ttc' if asset_id == collection_asset_id else '.ttf'}",
        )
        for asset_id in ("normal_font", "emphasis_font")
    }
    monkeypatch.setattr(
        caption_window_planning,
        "resolve_font_asset",
        lambda *, font_asset_id, **_kwargs: (fonts[font_asset_id], None),
    )
    monkeypatch.setattr(
        caption_window_planning,
        "load_font_metrics",
        lambda _path: _test_font_metrics(),
    )
    monkeypatch.setattr(caption_window_planning, "font_text_safety_issue", lambda *_args: None)

    with pytest.raises(NodeExecutionError, match="TTC"):
        caption_window_planning.run(_font_safety_context())


def test_three_level_envelopes_reject_canvas_and_normal_caption_collisions():
    edge_anchor = {
        "anchor_id": "edge",
        "rect": {"x": 0.88, "y": 0.2, "w": 0.08, "h": 0.12},
        "text_align": "left",
        "allowed_enter_directions": ["left", "right", "up"],
    }
    edge_options = build_caption_option_candidates(
        event_id="e1",
        text="促销",
        anchors=[edge_anchor],
        width=1000,
        height=1000,
        measure=lambda _text: 100.0,
        font_size=100.0,
        outline=5.0,
        shadow=1.0,
        normal_safe_rect=None,
    )
    assert edge_options == []

    centered_options = build_caption_option_candidates(
        event_id="e1",
        text="促销",
        anchors=[
            {
                **edge_anchor,
                "anchor_id": "center",
                "rect": {"x": 0.2, "y": 0.2, "w": 0.6, "h": 0.2},
                "text_align": "center",
            }
        ],
        width=1000,
        height=1000,
        measure=lambda _text: 100.0,
        font_size=100.0,
        outline=5.0,
        shadow=1.0,
        normal_safe_rect=None,
        hero_eligible=True,
    )
    assert {item["animation_id"] for item in centered_options} == {"pop", "slam_scale"}

    normal_collision = build_caption_option_candidates(
        event_id="e2",
        text="促销",
        anchors=[
            {
                **edge_anchor,
                "anchor_id": "lower",
                "rect": {"x": 0.2, "y": 0.65, "w": 0.4, "h": 0.1},
            }
        ],
        width=1000,
        height=1000,
        measure=lambda _text: 100.0,
        font_size=100.0,
        outline=5.0,
        shadow=1.0,
        normal_safe_rect={"x": 0.0, "y": 0.74, "w": 1.0, "h": 0.2},
    )
    assert normal_collision == []


def test_safe_anchor_and_option_caps_are_diagnosed():
    anchors = [
        {
            "anchor_id": f"a{index}",
            "rect": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.1},
            "text_align": "left",
            "region_tags": [],
        }
        for index in range(7)
    ]
    safe_options = [
        {
            "caption_option_id": f"option_{anchor['anchor_id']}_{animation}",
            "anchor_id": anchor["anchor_id"],
            "typography_variant_id": "emphasis_default_v1",
            "animation_id": animation,
            "face_overlap": 0.0,
            "scene_text_overlap": 0.0,
            "busy_score": 0.0,
            "sample_frames": [1, 2, 3],
        }
        for anchor in anchors
        for animation in (
            "none",
            "fade_in",
            "pop_in",
            "slide_up",
            "slide_left",
            "slide_right",
            "punch",
        )
    ]
    persisted_anchors, options, diagnostics = finalize_safe_caption_options(
        anchors=anchors,
        safe_options=safe_options,
    )

    assert len(persisted_anchors) <= 6
    assert len(options) == 24
    assert diagnostics == {
        "safe_anchor_candidates": 7,
        "anchors_pruned_by_cap": 1,
        "options_pruned_by_cap": 25,
    }


def test_visual_geometry_helpers_cover_real_opencv_paths():
    smooth = np.zeros((240, 320, 3), dtype=np.uint8)
    checker = (np.indices((240, 320)).sum(axis=0) % 2 * 255).astype(np.uint8)
    textured = np.repeat(checker[:, :, None], 3, axis=2)
    smooth_score = visual._busy_score(cv2, smooth, (0.0, 0.0, 1.0, 1.0))
    textured_score = visual._busy_score(cv2, textured, (0.0, 0.0, 1.0, 1.0))
    assert smooth_score is not None and textured_score is not None
    assert textured_score > smooth_score
    assert visual._busy_score(cv2, None, (0.0, 0.0, 0.1, 0.1)) is None
    assert visual._rect_tuple({"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}) == (
        0.1,
        0.1,
        0.2,
        0.2,
    )
    assert visual._rect_tuple({"x": -1, "y": 0, "w": 1, "h": 1}) is None
    assert visual._rect_tuple({"x": "bad", "y": 0, "w": 1, "h": 1}) is None
    assert visual._overlap_fraction((0, 0, 1, 1), (0.5, 0.5, 1, 1)) == 0.25
    padded = visual._normalize_padded_bbox((10, 10, 20, 20), 100, 100)
    assert padded[0] < 0.1 and padded[1] < 0.1


def test_short_caption_window_cannot_fake_three_safety_samples():
    assert visual.sample_frame_indices(5, 7) == []
    assert visual.sample_frame_indices(5, 8) == [5, 6, 7]


def test_caption_window_emits_visual_degradation_on_face_runtime_failure(monkeypatch):
    cv2 = pytest.importorskip("cv2")
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="开头铺垫然后出现限时五折",
        voice={"voice_id": "voice_demo"},
        subtitle={"normal_enabled": False, "emphasis_enabled": True},
        bgm={"enabled": False},
    )
    state = RunState(
        request=request,
        artifacts={
            ArtifactKind.video_rendered: _artifact(
                ArtifactKind.video_rendered, {}, "RenderedVideoArtifact.v1"
            ),
            ArtifactKind.plan_timeline: _artifact(
                ArtifactKind.plan_timeline,
                {"fps": 30, "total_frames": 120, "tracks": []},
                "TimelineArtifact.v1",
            ),
            ArtifactKind.narration_units: _artifact(
                ArtifactKind.narration_units,
                {
                    "units": [
                        {
                            "unit_id": "u1",
                            "text": "开头铺垫然后出现限时五折",
                            "start": 0.0,
                            "end": 4.0,
                        }
                    ]
                },
                "NarrationUnitsArtifact.v1",
            ),
            ArtifactKind.creative_intent: _artifact(
                ArtifactKind.creative_intent,
                {"emphasis": [{"phrase": "限时五折"}]},
                "CreativeIntentArtifact.v1",
            ),
        },
    )

    class _Context:
        node_run = SimpleNamespace(node_id="CaptionWindowPlanning")
        repository = SimpleNamespace(media_assets={})

        def __init__(self, run_state: RunState):
            self.state = run_state

        def artifact_path(self, _artifact_value):
            return Path("/tmp/final-video.mp4")

        def source_artifact_for_asset(self, _asset_id):
            raise AssertionError("font resolver is patched")

        def artifact(self, kind, payload, payload_schema):
            return _artifact(kind, payload, payload_schema)

    _patch_resolved_caption_fonts(monkeypatch)
    monkeypatch.setattr(
        caption_window_planning,
        "extract_frames_for_indices",
        lambda _video, indices, **_kwargs: [
            (frame, f"frame-{index}.jpg") for index, frame in enumerate(indices)
        ],
    )
    monkeypatch.setattr(
        cv2,
        "imread",
        lambda _path: np.zeros((100, 100, 3), dtype=np.uint8),
    )
    monkeypatch.setattr(visual, "face_detector_available", lambda: True)

    def _raise_detector_error(_image):
        raise RuntimeError("YuNet runtime failure")

    monkeypatch.setattr(visual, "detect_faces_strict", _raise_detector_error)

    output = caption_window_planning.run(_Context(state))

    assert WarningCode.caption_visual_analysis_failed in output.warnings
    plan = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_caption_windows
    )
    assert plan["diagnostics"]["visual_analysis_failed"] is True
    assert plan["diagnostics"]["unavailable_detectors"] == ["face"]
    assert plan["emphasis_windows"][0]["caption_options"] == []


def test_caption_window_reports_emphasis_hold_extension_in_diagnostics(monkeypatch):
    cv2 = pytest.importorskip("cv2")
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="限时五折",
        voice={"voice_id": "voice_demo"},
        subtitle={"normal_enabled": False, "emphasis_enabled": True},
        bgm={"enabled": False},
    )
    state = RunState(
        request=request,
        artifacts={
            ArtifactKind.video_rendered: _artifact(
                ArtifactKind.video_rendered, {}, "RenderedVideoArtifact.v1"
            ),
            ArtifactKind.plan_timeline: _artifact(
                ArtifactKind.plan_timeline,
                {"fps": 30, "total_frames": 120, "tracks": []},
                "TimelineArtifact.v1",
            ),
            ArtifactKind.narration_units: _artifact(
                ArtifactKind.narration_units,
                {"units": [{"unit_id": "u1", "text": "限时五折", "start": 0.0, "end": 3.0}]},
                "NarrationUnitsArtifact.v1",
            ),
            ArtifactKind.audio_alignment: _artifact(
                ArtifactKind.audio_alignment,
                {
                    "tokens": [
                        {"text": "限时", "start": 0.0, "end": 2.0},
                        {"text": "五折", "start": 2.0, "end": 2.3},
                    ]
                },
                "AudioAlignmentArtifact.v1",
            ),
            ArtifactKind.creative_intent: _artifact(
                ArtifactKind.creative_intent,
                {"emphasis": [{"phrase": "五折"}]},
                "CreativeIntentArtifact.v1",
            ),
        },
    )

    class _Context:
        node_run = SimpleNamespace(node_id="CaptionWindowPlanning")
        repository = SimpleNamespace(media_assets={})

        def __init__(self, run_state: RunState):
            self.state = run_state

        def artifact_path(self, _artifact_value):
            return Path("/tmp/final-video.mp4")

        def source_artifact_for_asset(self, _asset_id):
            raise AssertionError("font resolver is patched")

        def artifact(self, kind, payload, payload_schema):
            return _artifact(kind, payload, payload_schema)

    _patch_resolved_caption_fonts(monkeypatch)
    monkeypatch.setattr(
        caption_window_planning,
        "extract_frames_for_indices",
        lambda _video, indices, **_kwargs: [
            (frame, f"frame-{index}.jpg") for index, frame in enumerate(indices)
        ],
    )
    monkeypatch.setattr(cv2, "imread", lambda _path: np.zeros((100, 100, 3), dtype=np.uint8))
    monkeypatch.setattr(visual, "face_detector_available", lambda: True)
    monkeypatch.setattr(visual, "detect_faces_strict", lambda _image: [])
    monkeypatch.setattr(visual, "scene_text_detector_available", lambda: True)
    monkeypatch.setattr(visual, "detect_scene_text_strict", lambda _image: [])

    output = caption_window_planning.run(_Context(state))
    plan = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_caption_windows
    )
    # 0.3s spoken phrase (frames 60..69) held out to the 1.2s (36-frame) minimum.
    window = plan["emphasis_windows"][0]
    assert (window["start_frame"], window["end_frame"]) == (60, 96)
    assert plan["diagnostics"]["emphasis_hold_extended"] == 1
    assert plan["diagnostics"]["emphasis_hold_below_min"] == 0


def test_caption_window_v4_emits_v2_dynamic_three_line_plan(monkeypatch):
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="一二三四五六七八九",
        voice={"voice_id": "voice_demo"},
        subtitle={
            "normal_enabled": True,
            "emphasis_enabled": False,
            "font_size": 64,
        },
        bgm={"enabled": False},
        output={"width": 480, "height": 640, "fps": 30},
    )
    state = RunState(
        request=request,
        artifacts={
            ArtifactKind.video_rendered: _artifact(
                ArtifactKind.video_rendered, {}, "RenderedVideoArtifact.v1"
            ),
            ArtifactKind.plan_timeline: _artifact(
                ArtifactKind.plan_timeline,
                {"fps": 30, "total_frames": 108, "tracks": []},
                "TimelineArtifact.v1",
            ),
            ArtifactKind.narration_units: _artifact(
                ArtifactKind.narration_units,
                {
                    "units": [
                        {
                            "unit_id": "unit_001",
                            "text": "一二三四五六七八九",
                            "start": 0.0,
                            "end": 3.6,
                        }
                    ]
                },
                "NarrationUnitsArtifact.v1",
            ),
        },
    )

    class _Context:
        node_run = SimpleNamespace(node_id="CaptionWindowPlanning")
        repository = SimpleNamespace(media_assets={})

        def __init__(self, run_state: RunState):
            self.state = run_state

        def artifact_path(self, _artifact_value):
            return Path("/tmp/final-video.mp4")

        def source_artifact_for_asset(self, _asset_id):
            raise AssertionError("font resolver is patched")

        def artifact(self, kind, payload, payload_schema):
            return _artifact(kind, payload, payload_schema)

    _patch_resolved_caption_fonts(monkeypatch)
    monkeypatch.setattr(
        caption_window_planning,
        "extract_frames_for_indices",
        lambda _video, indices, **_kwargs: [
            (frame, f"frame-{index}.jpg") for index, frame in enumerate(indices)
        ],
    )
    monkeypatch.setattr(cv2, "imread", lambda _path: np.zeros((8, 8, 3), dtype=np.uint8))
    monkeypatch.setattr(
        caption_window_planning,
        "measure_option_candidates",
        lambda *, option_candidates, sample_frames, **_kwargs: visual.OptionMeasurementResult(
            measurements=[
                visual.OptionMeasurement(
                    option=option,
                    face_overlap=0.0,
                    scene_text_overlap=0.0,
                    busy_score=0.0,
                )
                for option in option_candidates
            ],
            sample_frames=list(sample_frames),
        ),
    )

    output = caption_window_planning.run(_Context(state))
    artifact = next(
        item for item in output.artifacts if item.kind == ArtifactKind.plan_caption_windows
    )
    window = artifact.payload["normal_windows"][0]

    assert artifact.payload_schema == "CaptionWindowsPlan.v2"
    assert artifact.payload["policy_version"] == "caption_windows_v2"
    assert artifact.payload["normal_font_asset_id"] == "asset_font_noto_serif_cjk_sc_regular"
    assert artifact.payload["emphasis_font_asset_id"] is None
    assert window["lines"] == ["一二三", "四五六", "七八九"]
    assert window["rect"] is not None
    assert window["safety_envelope"]["h"] >= window["rect"]["h"]
    assert window["text_align"] in {"left", "center", "right"}
    assert artifact.payload["diagnostics"]["normal_dynamic_positioned"] == 1


def _artifact(kind: ArtifactKind, payload: dict, payload_schema: str) -> Artifact:
    return Artifact(
        id=f"art_{kind.value}",
        kind=kind,
        payload=payload,
        payload_schema=payload_schema,
    )
