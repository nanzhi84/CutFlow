"""P1: the unified ``video`` material class runs the full sensor suite + a superset
VLM prompt, so one mixed clip yields both lip-sync-usable (A-roll) and cover-usable
(B-roll) segments — no human portrait/b-roll pre-classification at upload.

All dependencies are injected stubs (no real ffmpeg / VLM / silero).
"""

from __future__ import annotations

import json

from packages.core.contracts import AnnotationStatus, UsageRole
from packages.media.annotation._material import (
    is_video,
    material_class,
    runs_speech_and_face,
)
from packages.media.annotation.pipeline import V4Config, V4Deps, run_annotation_v4
from packages.media.annotation.vlm import build_window_prompt


def _frames_stub(video_path, sample_times, *, temp_dir, max_long_side=1024):
    return [(round(float(t), 3), f"{temp_dir}/f{i}.jpg") for i, t in enumerate(sample_times)]


def _base_deps(vlm_call) -> V4Deps:
    return V4Deps(
        detect_shot_cuts=lambda _vp: [],
        detect_speech_islands=lambda _vp: [],
        detect_quality_events=lambda _vp: [],
        extract_frames=_frames_stub,
        vlm_call=vlm_call,
        resolve_asr_text=lambda _vp: "全片台本",
    )


def _portrait_segment(start: float, end: float) -> dict:
    return {
        "start": start,
        "end": end,
        "semantics": {
            "subject_type": "person",
            "gaze_to_camera": True,
            "mouth_visible": True,
            "mouth_moving": True,
            "speaker_intent": "explain",
            "speech_action_alignment": "match",
            "retake_cue": "none",
        },
        "visual": {"shot_scale": "medium", "camera_motion": "static", "composition": "centered"},
        "usage": {
            "recommended_for_lip_sync": True,
            "recommended_for_voiceover": True,
            "voiceover_only": False,
            "role": "main",
        },
        "retrieval": {"summary": "talking head", "keywords": ["talk"], "retrieval_sentence": "talk"},
        "confidence": 0.9,
    }


def _broll_segment(start: float, end: float) -> dict:
    """A dedicated cutaway/scenery (B-roll) segment: no lip-sync, voiceover-only, cover."""
    return {
        "start": start,
        "end": end,
        "semantics": {"subject_type": "product", "scene_type": "studio"},
        "visual": {"shot_scale": "wide", "camera_motion": "static", "composition": "centered"},
        "usage": {
            "recommended_for_lip_sync": False,
            "recommended_for_voiceover": True,
            "voiceover_only": True,
            "role": "cover",
        },
        "retrieval": {"summary": "product b-roll", "keywords": ["product"], "retrieval_sentence": "product"},
        "confidence": 0.9,
    }


def _annotate_video(vlm_json: str, *, duration: float, faces: int = 1):
    deps = _base_deps(lambda _p, _f: vlm_json)
    deps.detect_speech_islands = lambda _vp: []
    deps.detect_max_faces = lambda _paths: faces
    return run_annotation_v4(
        asset_id="a",
        case_id="c",
        material_type="video",  # the unified bucket — no human A/B-roll pre-classification
        video_path="/fake.mp4",
        duration=duration,
        deps=deps,
        cfg=V4Config(),
    )


def test_material_class_routes_three_ways():
    assert material_class("portrait") == "portrait"
    assert material_class("broll") == "broll"
    assert material_class("video") == "video"
    # asset kinds that never reach the visual pipeline still classify deterministically
    assert material_class("bgm") == "broll"
    assert is_video("video") and not is_video("portrait")
    # video + portrait run the speech/face sensors; dedicated b-roll does not
    assert runs_speech_and_face("video")
    assert runs_speech_and_face("portrait")
    assert not runs_speech_and_face("broll")


def test_video_runs_speech_and_face_and_merges_report():
    speech_called = {"n": 0}
    face_called = {"n": 0}

    def _speech(_vp):
        speech_called["n"] += 1
        return []

    def _faces(_paths):
        face_called["n"] += 1
        return 1

    deps = _base_deps(lambda _p, _f: json.dumps({"segments": [_portrait_segment(0.0, 4.0)]}))
    deps.detect_speech_islands = _speech
    deps.detect_max_faces = _faces

    ann = run_annotation_v4(
        asset_id="a",
        case_id="c",
        material_type="video",
        video_path="/fake.mp4",
        duration=4.0,
        deps=deps,
        cfg=V4Config(),
    )

    assert ann.meta.annotation_status == AnnotationStatus.completed
    # VAD speech islands + multi-face sensor BOTH run for the unified bucket
    # (a dedicated b-roll asset skips both — see test_pipeline_broll_*).
    assert speech_called["n"] >= 1
    assert face_called["n"] >= 1
    # Authoritative CV face count is recorded so P2 lip-sync gating can use it.
    assert ann.clips[0].semantics.face_count_max == 1
    # The merged quality report carries BOTH portrait and b-roll whole-clip metrics.
    assert "lip_sync_suitability_score" in ann.quality_report  # portrait metric
    assert "usable_ratio" in ann.quality_report  # b-roll metric


def test_video_prompt_is_superset_of_portrait_and_broll():
    video_prompt = build_window_prompt(
        material_type="video",
        window_start=0.0,
        window_end=4.0,
        sensor_signals={},
        full_asr_text="",
    )
    # Superset: it carries BOTH a portrait-only field (gaze_to_camera) and a
    # b-roll-only field (narrative_role), and asks the VLM to set lip-sync usage.
    assert "gaze_to_camera" in video_prompt
    assert "narrative_role" in video_prompt
    assert "recommended_for_lip_sync" in video_prompt

    # The dedicated portrait prompt does NOT carry the b-roll narrative_role guide.
    portrait_prompt = build_window_prompt(
        material_type="portrait",
        window_start=0.0,
        window_end=4.0,
        sensor_signals={},
        full_asr_text="",
    )
    assert "narrative_role" not in portrait_prompt


# --- issue #99 regression: the unified ``video`` class must annotate portrait-like,
# broll-like, AND mixed clips without any human A/B-roll pre-classification. ---


def test_video_class_preserves_portrait_like_clip():
    """A talking-head clip uploaded into the ``video`` bucket keeps its lip-sync
    usability (A-roll) — same outcome as a dedicated ``portrait`` asset."""
    ann = _annotate_video(
        json.dumps({"segments": [_portrait_segment(0.0, 4.0)]}), duration=4.0
    )
    assert ann.meta.annotation_status == AnnotationStatus.completed
    lip_sync_clips = [c for c in ann.clips if c.usage.recommended_for_lip_sync]
    assert lip_sync_clips, "portrait-like clip must stay lip-sync usable under the video class"
    assert any(c.usage.role == UsageRole.main for c in lip_sync_clips)
    # The CV multi-face sensor still records an authoritative single-face count.
    assert ann.clips[0].semantics.face_count_max == 1


def test_video_class_preserves_broll_like_clip():
    """A pure cutaway/scenery clip in the ``video`` bucket is classified as cover
    (B-roll), voiceover-only, and NOT lip-sync usable — no quality regression vs a
    dedicated ``broll`` asset."""
    ann = _annotate_video(
        json.dumps({"segments": [_broll_segment(0.0, 4.0)]}), duration=4.0, faces=0
    )
    assert ann.meta.annotation_status == AnnotationStatus.completed
    assert ann.clips, "broll-like clip must still be annotated under the video class"
    clip = ann.clips[0]
    assert clip.usage.role == UsageRole.cover
    assert clip.usage.recommended_for_lip_sync is False
    assert clip.usage.voiceover_only is True


def test_video_class_mixed_clip_yields_both_aroll_and_broll():
    """The headline case: ONE mixed clip (talking head + cutaway) annotated as
    ``video`` yields BOTH a lip-sync-usable (A-roll) segment and a cover (B-roll)
    segment — the per-clip classification the unified bucket is built for."""
    mixed = json.dumps(
        {"segments": [_portrait_segment(0.0, 2.0), _broll_segment(2.0, 4.0)]}
    )
    ann = _annotate_video(mixed, duration=4.0)
    assert ann.meta.annotation_status == AnnotationStatus.completed
    assert len(ann.clips) >= 2
    assert any(c.usage.recommended_for_lip_sync for c in ann.clips), "missing A-roll segment"
    assert any(c.usage.role == UsageRole.cover for c in ann.clips), "missing B-roll segment"
    # Both whole-clip metric families land in the merged report (portrait + b-roll).
    assert "lip_sync_suitability_score" in ann.quality_report
    assert "usable_ratio" in ann.quality_report
