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
from packages.media.annotation.sensors import FaceDetection
from packages.production.pipeline import _caption_visual_safety as visual
from packages.production.pipeline._font_metrics import FontMetrics, make_text_measurer
from packages.production.pipeline._fonts import ResolvedFont
from packages.production.pipeline._caption_window_planner import (
    build_caption_option_candidates,
    build_emphasis_windows,
    compile_normal_windows,
    finalize_safe_caption_options,
)
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.nodes import caption_window_planning


def _measure(text: str) -> float:
    return len(text) * 40.0


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
            "lines": ["正常字幕内容"],
            "line_start_frames": [3],
            "source_unit_ids": ["unit-real-id"],
            "normalized_text": "正常字幕内容",
            "visual_preset_id": "normal",
            "effect_id": "soft_in",
        }
    ]
    assert diagnostics["font_metrics_source"] == "hmtx"


def test_phrase_window_clamps_to_cut_segment_instead_of_dropping_whole_unit():
    units = [
        {
            "unit_id": "u1",
            "text": "开头铺垫然后出现限时五折",
            "start": 0.0,
            "end": 4.0,
        }
    ]
    windows, candidate_count, dropped, token_matched, char_fallback = build_emphasis_windows(
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
    assert len(windows) == 1
    assert 60 <= windows[0]["start_frame"] < windows[0]["end_frame"] <= 120
    assert windows[0]["source_unit_ids"] == ["u1"]


def test_visual_safety_fails_closed_when_face_detector_is_unavailable(monkeypatch):
    monkeypatch.setattr(visual, "face_detector_available", lambda: False)
    result = visual.evaluate_anchor_safety(
        images=[np.zeros((100, 100, 3), dtype=np.uint8) for _ in range(3)],
        sample_frames=[0, 10, 20],
        anchors=[
            {
                "anchor_id": "a1",
                "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1},
            }
        ],
    )
    assert result.anchors == []
    assert result.unavailable_detector == "face"


def test_visual_safety_materializes_only_safe_anchor_and_complete_options(monkeypatch):
    monkeypatch.setattr(visual, "face_detector_available", lambda: True)
    monkeypatch.setattr(visual, "detect_faces_strict", lambda _image: [])
    monkeypatch.setattr(visual, "scene_text_detector_available", lambda: True)
    monkeypatch.setattr(visual, "detect_scene_text_strict", lambda _image: [])
    images = [np.zeros((100, 100, 3), dtype=np.uint8) for _ in range(3)]
    result = visual.evaluate_anchor_safety(
        images=images,
        sample_frames=[1, 10, 19],
        anchors=[
            {
                "anchor_id": "hz_001__top_left",
                "rect": {"x": 0.05, "y": 0.05, "w": 0.3, "h": 0.1},
                "text_align": "left",
                "max_lines": 1,
                "text_capacity": 8,
                "allowed_enter_directions": ["left", "up"],
                "region_tags": ["top", "left"],
            }
        ],
    )
    assert len(result.anchors) == 1
    options = build_caption_option_candidates(
        event_id="hz_001",
        text="限时五折",
        anchors=result.anchors,
        width=1080,
        height=1920,
        measure=lambda text: len(text) * 50.0,
        font_size=100.0,
        outline=5.0,
        shadow=1.0,
        normal_safe_rect=None,
    )
    assert options
    assert all(option["caption_option_id"].startswith("hz_001__") for option in options)


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


@pytest.mark.parametrize(
    ("normal_metrics_available", "expected_width"),
    [(True, 80.0), (False, 60.0)],
)
def test_failed_emphasis_font_reuses_resolved_normal_font_metrics(
    monkeypatch,
    tmp_path,
    normal_metrics_available,
    expected_width,
):
    metrics = FontMetrics(
        upem=1000,
        ascender=800,
        descender=-200,
        cmap={ord("A"): "A"},
        advances={"A": 500},
    )
    normal_font = ResolvedFont(
        family_name="Normal",
        fonts_dir=tmp_path,
        source_path=tmp_path / "normal.ttf",
    )
    resolved_ids = []

    def _resolve(*, font_asset_id, **_kwargs):
        resolved_ids.append(font_asset_id)
        if font_asset_id == "normal_font":
            return normal_font, None
        return None, font_asset_id

    captured = {}

    def _capture_candidates(**kwargs):
        captured["measured_width"] = kwargs["measure"]("AAAA")
        captured["font_size"] = kwargs["font_size"]
        return []

    monkeypatch.setattr(caption_window_planning, "resolve_font_asset", _resolve)
    monkeypatch.setattr(
        caption_window_planning,
        "load_font_metrics",
        lambda _path: metrics if normal_metrics_available else None,
    )
    monkeypatch.setattr(
        caption_window_planning,
        "build_caption_option_candidates",
        _capture_candidates,
    )
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="AAAA 限时五折",
        voice={"voice_id": "voice_demo"},
        subtitle={
            "font_id": "normal_font",
            "emphasis_font_id": "missing_emphasis_font",
            "font_size": 40,
            "emphasis_font_size": 80,
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

    output = caption_window_planning.run(_Context(state))

    assert resolved_ids == ["normal_font"]
    assert captured == {"measured_width": expected_width, "font_size": 40.0}
    assert WarningCode.font_resolution_failed not in output.warnings
    assert (WarningCode.font_metrics_fallback in output.warnings) is (
        not normal_metrics_available
    )


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


def test_visual_safety_filters_hero_envelope_without_rejecting_emphasis(monkeypatch):
    monkeypatch.setattr(visual, "face_detector_available", lambda: True)
    monkeypatch.setattr(
        visual,
        "detect_faces_strict",
        lambda _image: [
            FaceDetection(
                bbox=(600.0, 400.0, 200.0, 200.0),
                score=0.9,
                landmarks=(),
            )
        ],
    )
    monkeypatch.setattr(visual, "scene_text_detector_available", lambda: True)
    monkeypatch.setattr(visual, "detect_scene_text_strict", lambda _image: [])
    candidates = build_caption_option_candidates(
        event_id="e1",
        text="促销",
        anchors=[
            {
                "anchor_id": "a1",
                "rect": {"x": 0.3, "y": 0.45, "w": 0.4, "h": 0.1},
                "text_align": "center",
                "allowed_enter_directions": ["left"],
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
    result = visual.evaluate_option_safety(
        images=[np.zeros((1000, 1000, 3), dtype=np.uint8) for _ in range(3)],
        sample_frames=[1, 10, 19],
        option_candidates=candidates,
    )

    safe_animations = {item["animation_id"] for item in result.options}
    assert "pop" in safe_animations
    assert "slam_scale" not in safe_animations
    assert result.rejected_face >= 1


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


def test_visual_safety_fails_closed_when_face_detection_raises(monkeypatch):
    monkeypatch.setattr(visual, "face_detector_available", lambda: True)

    def _raise_detector_error(_image):
        raise RuntimeError("YuNet runtime failure")

    monkeypatch.setattr(visual, "detect_faces_strict", _raise_detector_error)
    result = visual.evaluate_anchor_safety(
        images=[np.zeros((100, 100, 3), dtype=np.uint8) for _ in range(3)],
        sample_frames=[0, 10, 20],
        anchors=[
            {
                "anchor_id": "a1",
                "rect": {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1},
            }
        ],
    )
    assert result.anchors == []
    assert result.unavailable_detector == "face"


def test_visual_safety_rejects_face_text_and_busy_anchors_independently(monkeypatch):
    monkeypatch.setattr(visual, "face_detector_available", lambda: True)
    monkeypatch.setattr(
        visual,
        "detect_faces_strict",
        lambda _image: [
            FaceDetection(
                bbox=(0.0, 0.0, 120.0, 120.0),
                score=0.9,
                landmarks=(),
            )
        ],
    )
    monkeypatch.setattr(visual, "scene_text_detector_available", lambda: True)
    monkeypatch.setattr(
        visual,
        "detect_scene_text_strict",
        lambda _image: [(0.4, 0.4, 0.2, 0.2)],
    )
    monkeypatch.setattr(
        visual,
        "_busy_score",
        lambda _cv2, _image, rect: 0.9 if rect[0] > 0.7 else 0.0,
    )
    anchors = [
        {"anchor_id": "face", "rect": {"x": 0.0, "y": 0.0, "w": 0.15, "h": 0.15}},
        {"anchor_id": "text", "rect": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2}},
        {"anchor_id": "busy", "rect": {"x": 0.8, "y": 0.2, "w": 0.1, "h": 0.1}},
        {
            "anchor_id": "safe",
            "rect": {"x": 0.2, "y": 0.7, "w": 0.1, "h": 0.1},
            "text_align": "center",
        },
    ]

    result = visual.evaluate_anchor_safety(
        images=[np.zeros((1000, 1000, 3), dtype=np.uint8) for _ in range(3)],
        sample_frames=[1, 10, 19],
        anchors=anchors,
    )

    assert [anchor["anchor_id"] for anchor in result.anchors] == ["safe"]
    assert result.rejected_face == 1
    assert result.rejected_scene_text == 1
    assert result.rejected_busy == 1
    assert result.rejected_total == 3


def test_option_safety_fails_closed_for_invalid_or_unmeasurable_envelopes(monkeypatch):
    monkeypatch.setattr(visual, "face_detector_available", lambda: True)
    monkeypatch.setattr(visual, "detect_faces_strict", lambda _image: [])
    monkeypatch.setattr(visual, "scene_text_detector_available", lambda: True)
    monkeypatch.setattr(visual, "detect_scene_text_strict", lambda _image: [])
    images = [np.zeros((100, 100, 3), dtype=np.uint8) for _ in range(3)]

    invalid = visual.evaluate_option_safety(
        images=images,
        sample_frames=[1, 2, 3],
        option_candidates=[
            {"caption_option_id": "bad", "safety_envelope": {"x": 0, "y": 0, "w": 0, "h": 1}}
        ],
    )
    assert invalid.options == []
    assert invalid.rejected_total == 1

    monkeypatch.setattr(visual, "_busy_score", lambda *_args: None)
    unavailable = visual.evaluate_option_safety(
        images=images,
        sample_frames=[1, 2, 3],
        option_candidates=[
            {
                "caption_option_id": "unmeasurable",
                "safety_envelope": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            }
        ],
    )
    assert unavailable.options == []
    assert unavailable.unavailable_detector == "busy"


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

        def __init__(self, run_state: RunState):
            self.state = run_state

        def artifact_path(self, _artifact_value):
            return Path("/tmp/final-video.mp4")

        def artifact(self, kind, payload, payload_schema):
            return _artifact(kind, payload, payload_schema)

    monkeypatch.setattr(
        caption_window_planning,
        "extract_frames_for_times",
        lambda _video, times, **_kwargs: [
            (time, f"frame-{index}.jpg") for index, time in enumerate(times)
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


def _artifact(kind: ArtifactKind, payload: dict, payload_schema: str) -> Artifact:
    return Artifact(
        id=f"art_{kind.value}",
        kind=kind,
        payload=payload,
        payload_schema=payload_schema,
    )
