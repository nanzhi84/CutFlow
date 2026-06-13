"""Tests for the V4 VLM prompt builder + response parser (failure classification).

Pure-function tests: no network, no gateway. They exercise build_window_prompt's
material-type branching and parse_window_response's Schema/Semantic taxonomy.
"""

from __future__ import annotations

import json

import pytest

from packages.core.contracts import UsageRole
from packages.media.annotation.errors import SchemaError, SemanticError
from packages.media.annotation.vlm import build_window_prompt, parse_window_response


def _segment(
    *,
    start: float,
    end: float,
    role: str = "cover",
    lip_sync: bool = False,
    confidence: float = 0.9,
) -> dict:
    return {
        "start": start,
        "end": end,
        "semantics": {"subject_type": "product", "scene_type": "studio"},
        "visual": {"shot_scale": "medium", "camera_motion": "static", "composition": "centered"},
        "usage": {
            "recommended_for_lip_sync": lip_sync,
            "recommended_for_voiceover": True,
            "voiceover_only": True,
            "role": role,
        },
        "retrieval": {"summary": "a clip", "keywords": ["k1", "k2"], "retrieval_sentence": "a clip"},
        "confidence": confidence,
    }


def _full_window_response(window_start: float, window_end: float) -> str:
    return json.dumps({"segments": [_segment(start=window_start, end=window_end)]})


def test_prompt_branches_on_material_type():
    broll = build_window_prompt(
        material_type="broll",
        window_start=0.0,
        window_end=4.0,
        sensor_signals={"shot_cuts": [1.0]},
        full_asr_text="hello",
    )
    portrait = build_window_prompt(
        material_type="portrait",
        window_start=0.0,
        window_end=4.0,
        sensor_signals={},
        full_asr_text="",
    )
    assert "narrative_role" in broll
    assert "cover(盖旁白的空镜)" in broll
    assert "shot_cuts" in broll  # sensor signals are injected
    assert "gaze_to_camera" in portrait
    assert "hook(开场钩子)" in portrait


def test_prompt_appends_retry_hint():
    prompt = build_window_prompt(
        material_type="broll",
        window_start=0.0,
        window_end=4.0,
        sensor_signals={},
        full_asr_text="",
        retry_hint="please fix format",
    )
    assert "please fix format" in prompt


def test_parse_valid_response_yields_clips():
    clips = parse_window_response(
        _full_window_response(0.0, 4.0),
        material_type="broll",
        window_start=0.0,
        window_end=4.0,
        duration=4.0,
    )
    assert len(clips) == 1
    clip = clips[0]
    assert clip.usage.role == UsageRole.cover
    assert clip.retrieval.keywords == ["k1", "k2"]
    assert clip.segment_id.startswith("w0.000_4.000_seg")


def test_parse_strips_markdown_fence():
    raw = "```json\n" + _full_window_response(0.0, 4.0) + "\n```"
    clips = parse_window_response(
        raw, material_type="broll", window_start=0.0, window_end=4.0, duration=4.0
    )
    assert len(clips) == 1


def test_parse_non_json_raises_schema_error():
    with pytest.raises(SchemaError):
        parse_window_response(
            "not json at all", material_type="broll", window_start=0.0, window_end=4.0, duration=4.0
        )


def test_parse_missing_segments_raises_schema_error():
    with pytest.raises(SchemaError):
        parse_window_response(
            json.dumps({"clips": []}),
            material_type="broll",
            window_start=0.0,
            window_end=4.0,
            duration=4.0,
        )


def test_parse_illegal_role_raises_schema_error():
    raw = json.dumps({"segments": [_segment(start=0.0, end=4.0, role="bogus")]})
    with pytest.raises(SchemaError):
        parse_window_response(
            raw, material_type="broll", window_start=0.0, window_end=4.0, duration=4.0
        )


def test_parse_empty_segments_raises_semantic_error():
    with pytest.raises(SemanticError):
        parse_window_response(
            json.dumps({"segments": []}),
            material_type="broll",
            window_start=0.0,
            window_end=4.0,
            duration=4.0,
        )


def test_parse_out_of_window_raises_semantic_error():
    raw = json.dumps({"segments": [_segment(start=10.0, end=14.0)]})
    with pytest.raises(SemanticError):
        parse_window_response(
            raw, material_type="broll", window_start=0.0, window_end=4.0, duration=20.0
        )


def test_parse_low_confidence_raises_semantic_error():
    raw = json.dumps({"segments": [_segment(start=0.0, end=4.0, confidence=0.05)]})
    with pytest.raises(SemanticError):
        parse_window_response(
            raw, material_type="broll", window_start=0.0, window_end=4.0, duration=4.0
        )


def test_parse_role_lipsync_contradiction_raises_semantic_error():
    raw = json.dumps({"segments": [_segment(start=0.0, end=4.0, role="main", lip_sync=False)]})
    with pytest.raises(SemanticError):
        parse_window_response(
            raw, material_type="portrait", window_start=0.0, window_end=4.0, duration=4.0
        )


def test_parse_coverage_gap_raises_semantic_error():
    raw = json.dumps(
        {
            "segments": [
                _segment(start=0.0, end=1.0),
                _segment(start=3.5, end=4.0),  # large internal gap [1.0, 3.5]
            ]
        }
    )
    with pytest.raises(SemanticError):
        parse_window_response(
            raw, material_type="broll", window_start=0.0, window_end=4.0, duration=4.0
        )
