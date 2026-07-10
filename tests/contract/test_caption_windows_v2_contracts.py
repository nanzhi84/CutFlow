from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.core.contracts import ArtifactKind, DegradationCode, WarningCode
from packages.core.contracts.artifacts import (
    CaptionCue,
    CaptionWindowDiagnostics,
    CaptionWindowsPlanArtifact,
    PostProcessAgentOutput,
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


def test_postprocess_output_requires_all_three_fields_and_rejects_layout_overreach():
    with pytest.raises(ValidationError):
        PostProcessAgentOutput.model_validate({})

    output = PostProcessAgentOutput.model_validate(
        {"bgm_id": None, "caption_choices": [], "analysis": "没有合适候选"}
    )
    assert output.bgm_id is None
    assert output.caption_choices == []

    with pytest.raises(ValidationError):
        PostProcessAgentOutput.model_validate(
            {
                "bgm_id": None,
                "caption_choices": [],
                "analysis": "非法越权",
                "font_size": 72,
            }
        )


def test_legacy_caption_cue_accepts_old_indices_and_v2_stable_unit_ids():
    old = CaptionCue(start=0, end=1, lines=["旧产物"], source_unit_ids=[0])
    current = CaptionCue(start=0, end=1, lines=["新产物"], source_unit_ids=["unit_001"])

    assert old.source_unit_ids == [0]
    assert current.source_unit_ids == ["unit_001"]


def test_caption_display_v2_kinds_and_degradation_codes_are_stable():
    assert ArtifactKind.plan_caption_windows.value == "plan.caption_windows"
    assert (
        ArtifactKind.plan_media_selection_diagnostics.value
        == "plan.media_selection_diagnostics"
    )
    assert ArtifactKind.plan_postprocess_diagnostics.value == "plan.postprocess_diagnostics"
    assert WarningCode.caption_visual_analysis_failed.value == "caption.visual_analysis_failed"
    assert DegradationCode.postprocess_planning_failed.value == "postprocess.planning_failed"
