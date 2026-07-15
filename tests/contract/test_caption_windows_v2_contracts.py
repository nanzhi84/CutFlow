from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.core.contracts import ArtifactKind, DegradationCode, WarningCode
from packages.core.contracts.artifacts import (
    CaptionCue,
    CaptionWindowDiagnostics,
    CaptionWindowsPlanArtifact,
)


def _caption_windows_payload() -> dict:
    return {
        "policy_version": "caption_windows_v1",
        "source_video_artifact_id": "video_1",
        "source_timeline_artifact_id": "timeline_1",
        "fps": 30,
        "width": 1080,
        "height": 1920,
        "normal_enabled": True,
        "emphasis_enabled": True,
        "normal_safe_rect": {"x": 0.08, "y": 0.74, "w": 0.84, "h": 0.18},
        "normal_windows": [
            {
                "window_id": "caption_001",
                "normalized_text": "中文智能断行",
                "start_frame": 0,
                "end_frame": 60,
                "lines": ["中文智能", "断行"],
                "source_unit_ids": ["unit_001"],
            }
        ],
        "emphasis_windows": [
            {
                "event_id": "emphasis_001",
                "text": "智能断行",
                "normalized_text": "智能断行",
                "start_frame": 8,
                "end_frame": 36,
                "source_unit_ids": ["unit_001"],
                "anchor_candidates": [
                    {
                        "anchor_id": "anchor_upper",
                        "rect": {"x": 0.1, "y": 0.08, "w": 0.8, "h": 0.2},
                        "text_align": "center",
                        "allowed_animation_ids": ["pop_in"],
                        "region_tags": ["upper"],
                        "face_overlap": 0.0,
                        "scene_text_overlap": 0.0,
                        "busy_score": 0.1,
                        "sample_frames": [8, 22, 35],
                    }
                ],
                "caption_options": [
                    {
                        "caption_option_id": "caption_option_001",
                        "anchor_id": "anchor_upper",
                        "typography_variant_id": "emphasis_primary",
                        "animation_id": "pop_in",
                    }
                ],
            }
        ],
        "diagnostics": {
            "merged_units": 0,
            "split_cues": 1,
            "font_metrics_source": "hmtx",
            "sampled_frames": 3,
            "generated_anchor_candidates": 1,
            "rejected_anchor_candidates": 0,
            "visual_analysis_failed": False,
            "emphasis_candidates": 1,
            "events_crossing_cuts_dropped": 0,
            "events_without_options": 0,
            "rejected_face": 0,
            "rejected_scene_text": 0,
            "rejected_busy": 0,
            "unavailable_detectors": [],
        },
    }


def test_caption_windows_plan_accepts_frame_authoritative_complete_options():
    plan = CaptionWindowsPlanArtifact.model_validate(_caption_windows_payload())

    assert plan.normal_windows[0].normalized_text == "中文智能断行"
    assert plan.emphasis_windows[0].source_unit_ids == ["unit_001"]
    assert plan.emphasis_windows[0].caption_options[0].anchor_id == "anchor_upper"
    assert set(plan.diagnostics.model_dump()) == set(CaptionWindowDiagnostics.model_fields)


def test_caption_windows_v2_preserves_dynamic_three_line_normal_geometry():
    payload = _caption_windows_payload()
    payload["policy_version"] = "caption_windows_v2"
    payload["normal_font_asset_id"] = "asset_font_noto_serif_cjk_sc_regular"
    payload["emphasis_font_asset_id"] = "asset_font_noto_sans_cjk_sc_bold"
    payload["normal_windows"][0].update(
        {
            "lines": ["你拿着", "这张图纸", "去做交付"],
            "line_start_frames": [0, 12, 24],
            "rect": {"x": 0.11, "y": 0.34, "w": 0.48, "h": 0.15},
            "safety_envelope": {"x": 0.11, "y": 0.34, "w": 0.48, "h": 0.16},
            "text_align": "left",
        }
    )

    plan = CaptionWindowsPlanArtifact.model_validate(payload)

    assert plan.policy_version == "caption_windows_v2"
    assert plan.normal_font_asset_id == "asset_font_noto_serif_cjk_sc_regular"
    assert plan.emphasis_font_asset_id == "asset_font_noto_sans_cjk_sc_bold"
    assert plan.normal_windows[0].lines == ["你拿着", "这张图纸", "去做交付"]
    assert plan.normal_windows[0].rect is not None
    assert plan.normal_windows[0].rect.x == 0.11
    assert plan.normal_windows[0].safety_envelope is not None
    assert plan.normal_windows[0].safety_envelope.h == 0.16
    assert plan.normal_windows[0].text_align == "left"


def test_caption_windows_v2_rejects_rectless_or_fontless_normal_windows():
    payload = _caption_windows_payload()
    payload["policy_version"] = "caption_windows_v2"
    payload["normal_font_asset_id"] = "font_normal"
    payload["emphasis_font_asset_id"] = "font_emphasis"
    with pytest.raises(ValidationError, match="rect and safety_envelope"):
        CaptionWindowsPlanArtifact.model_validate(payload)

    payload["normal_windows"][0]["rect"] = {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.1}
    payload["normal_windows"][0]["safety_envelope"] = {
        "x": 0.1,
        "y": 0.2,
        "w": 0.4,
        "h": 0.1,
    }
    with pytest.raises(ValidationError, match="must contain"):
        CaptionWindowsPlanArtifact.model_validate(payload)

    payload["normal_windows"][0]["safety_envelope"]["w"] = 0.5
    payload["normal_font_asset_id"] = None
    with pytest.raises(ValidationError, match="normal_font_asset_id"):
        CaptionWindowsPlanArtifact.model_validate(payload)


def test_caption_windows_plan_rejects_invalid_geometry_timing_and_option_references():
    payload = _caption_windows_payload()
    payload["normal_safe_rect"] = {"x": 0.8, "y": 0.7, "w": 0.3, "h": 0.2}
    with pytest.raises(ValidationError, match="exceeds canvas width"):
        CaptionWindowsPlanArtifact.model_validate(payload)

    payload = _caption_windows_payload()
    payload["normal_windows"][0]["end_frame"] = 0
    with pytest.raises(ValidationError):
        CaptionWindowsPlanArtifact.model_validate(payload)

    payload = _caption_windows_payload()
    payload["emphasis_windows"][0]["caption_options"][0]["anchor_id"] = "unknown"
    with pytest.raises(ValidationError, match="unknown anchors"):
        CaptionWindowsPlanArtifact.model_validate(payload)

    payload = _caption_windows_payload()
    payload["emphasis_windows"][0]["caption_options"][0]["animation_id"] = "punch"
    with pytest.raises(ValidationError, match="not allowed"):
        CaptionWindowsPlanArtifact.model_validate(payload)


def test_caption_windows_plan_rejects_duplicate_tokens_overlapping_spans_and_short_gap():
    first = {
        "window_id": "caption_001",
        "normalized_text": "第一句",
        "start_frame": 0,
        "end_frame": 20,
        "spoken_span": {"start_frame": 0, "end_frame": 22},
        "display_span": {"start_frame": 0, "end_frame": 20},
        "token_ids": ["token_001"],
        "char_span": [0, 3],
        "lines": ["第一句"],
        "source_unit_ids": ["unit_001"],
    }
    second = {
        "window_id": "caption_002",
        "normalized_text": "第二句",
        "start_frame": 22,
        "end_frame": 42,
        "spoken_span": {"start_frame": 20, "end_frame": 42},
        "display_span": {"start_frame": 22, "end_frame": 42},
        "token_ids": ["token_002"],
        "char_span": [3, 6],
        "lines": ["第二句"],
        "source_unit_ids": ["unit_002"],
    }
    payload = _caption_windows_payload()
    payload["normal_windows"] = [first, second]
    CaptionWindowsPlanArtifact.model_validate(payload)

    payload = _caption_windows_payload()
    payload["normal_windows"] = [first, {**second, "token_ids": ["token_001"]}]
    with pytest.raises(ValidationError, match="globally unique"):
        CaptionWindowsPlanArtifact.model_validate(payload)

    payload = _caption_windows_payload()
    payload["normal_windows"] = [first, {**second, "char_span": [2, 6]}]
    with pytest.raises(ValidationError, match="monotonic"):
        CaptionWindowsPlanArtifact.model_validate(payload)

    short_gap = {
        **second,
        "start_frame": 21,
        "display_span": {"start_frame": 21, "end_frame": 42},
    }
    payload = _caption_windows_payload()
    payload["normal_windows"] = [first, short_gap]
    with pytest.raises(ValidationError, match="two-frame gap"):
        CaptionWindowsPlanArtifact.model_validate(payload)


def test_legacy_caption_cue_accepts_old_indices_and_v2_stable_unit_ids():
    old = CaptionCue(start=0, end=1, lines=["旧产物"], source_unit_ids=[0])
    current = CaptionCue(start=0, end=1, lines=["新产物"], source_unit_ids=["unit_001"])

    assert old.source_unit_ids == [0]
    assert current.source_unit_ids == ["unit_001"]


def test_caption_display_v2_kinds_and_degradation_codes_are_stable():
    assert ArtifactKind.plan_caption_windows.value == "plan.caption_windows"
    assert ArtifactKind.plan_media_selection_diagnostics.value == "plan.media_selection_diagnostics"
    assert ArtifactKind.plan_postprocess_diagnostics.value == "plan.postprocess_diagnostics"
    assert WarningCode.caption_visual_analysis_failed.value == "caption.visual_analysis_failed"
    assert WarningCode.caption_normal_relaxed_safety.value == "caption.normal_relaxed_safety"
    assert DegradationCode.postprocess_planning_failed.value == "postprocess.planning_failed"
