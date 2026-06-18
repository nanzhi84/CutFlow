"""B-roll must exclude person-centric clips (presenter / talking-head / 出镜人物).

A "B-roll only" video should carry clean scene/product footage. A unified-video
clip that features a real person as its subject (presenter, salesperson, multi-
face frame, talking-head cues) is NOT lip-sync-usable AND is not clean b-roll —
it must fall into neither pool, never surfacing as a b-roll insert. A scene clip
that merely catches one incidental face still qualifies as b-roll.
"""

from __future__ import annotations

from packages.core.contracts import (
    AnnotationMetaV4,
    AnnotationV4,
    ClipRetrievalV4,
    ClipSemanticsV4,
    ClipUsageV4,
    ClipV4,
    UsageRole,
)
from packages.planning.material import clip_shows_person, rank_broll_candidates
from packages.planning.material.keywords import ScriptSegment


def _clip(
    segment_id,
    start,
    end,
    *,
    role=UsageRole.cover,
    lip_sync=False,
    subject_type="furniture_showroom",
    face_count_max=None,
    contains_face=None,
    mouth_moving=None,
    gaze_to_camera=None,
    keywords=("定制",),
):
    return ClipV4(
        segment_id=segment_id,
        start=start,
        end=end,
        duration=end - start,
        semantics=ClipSemanticsV4(
            scene_type="场景",
            subject_type=subject_type,
            face_count_max=face_count_max,
            contains_face=contains_face,
            mouth_moving=mouth_moving,
            gaze_to_camera=gaze_to_camera,
        ),
        usage=ClipUsageV4(role=role, recommended_for_lip_sync=lip_sync),
        retrieval=ClipRetrievalV4(
            summary=" ".join(keywords),
            keywords=list(keywords),
            retrieval_sentence=" ".join(keywords),
        ),
        confidence=0.9,
    )


def _video_annotation(asset_id, clips):
    return AnnotationV4(
        meta=AnnotationMetaV4(
            asset_id=asset_id, case_id="case_demo", material_type="video", duration=20.0
        ),
        clips=clips,
        quality_report={"usable_ratio": 0.9},
    )


_SEGMENTS = [ScriptSegment(text="深度非标定制", start=0.0, end=5.0, keywords=("定制",))]


def test_clip_shows_person_helper():
    # presenter / salesperson subject -> person
    assert clip_shows_person(_clip("p", 0.0, 5.0, subject_type="female_presenter"))
    assert clip_shows_person(_clip("s", 0.0, 5.0, subject_type="female_salesperson"))
    # explicit face flag / multi-face / talking-head visual cues -> person
    assert clip_shows_person(_clip("f", 0.0, 5.0, contains_face=True))
    assert clip_shows_person(_clip("m", 0.0, 5.0, face_count_max=2))
    assert clip_shows_person(_clip("mouth", 0.0, 5.0, mouth_moving=True))
    assert clip_shows_person(_clip("g", 0.0, 5.0, gaze_to_camera=True))
    # the recommended_for_lip_sync *usage* flag alone is NOT a person signal here
    # (lip-sync-usable clips are routed to A-roll upstream; the flag is noisy on
    # legacy b-roll) — a scene clip stays b-roll even if that flag is set.
    assert not clip_shows_person(
        _clip("lip", 0.0, 5.0, subject_type="furniture_showroom", lip_sync=True)
    )
    # clean scene / single incidental face -> NOT a person clip
    assert not clip_shows_person(_clip("scene", 0.0, 5.0, subject_type="interior_living_room"))
    assert not clip_shows_person(
        _clip("one_face", 0.0, 5.0, subject_type="furniture_showroom", face_count_max=1)
    )
    assert not clip_shows_person(
        _clip(
            "product_detector_noise",
            0.0,
            5.0,
            subject_type="product",
            contains_face=False,
            face_count_max=20,
        )
    )


def test_presenter_clip_excluded_from_broll_pool():
    # A female-presenter cover clip whose keywords match the script must NOT surface
    # as a b-roll candidate; only the person-free scene clip should.
    annotation = _video_annotation(
        "vid_template",
        [
            _clip("presenter", 0.0, 6.0, subject_type="female_presenter", face_count_max=1),
            _clip("scenery", 6.0, 12.0, subject_type="furniture_showroom", face_count_max=None),
        ],
    )
    broll = rank_broll_candidates(annotations={"vid_template": annotation}, segments=_SEGMENTS)
    clip_ids = {c.clip_id for c in broll}
    assert "presenter" not in clip_ids
    assert "scenery" in clip_ids


def test_multi_face_clip_excluded_from_broll_pool():
    annotation = _video_annotation(
        "vid_crowd",
        [
            _clip("crowd", 0.0, 6.0, subject_type="luxury_store", face_count_max=2),
            _clip("empty_room", 6.0, 12.0, subject_type="interior_space", face_count_max=None),
        ],
    )
    broll = rank_broll_candidates(annotations={"vid_crowd": annotation}, segments=_SEGMENTS)
    clip_ids = {c.clip_id for c in broll}
    assert "crowd" not in clip_ids
    assert "empty_room" in clip_ids


def test_scene_clip_with_single_incidental_face_still_qualifies():
    # A clean scene clip that merely catches one face in frame (no person subject)
    # remains a valid b-roll candidate — we must not over-exclude legitimate cover.
    annotation = _video_annotation(
        "vid_scene",
        [_clip("hands_on_craft", 0.0, 6.0, subject_type="detail_showcase", face_count_max=1)],
    )
    broll = rank_broll_candidates(annotations={"vid_scene": annotation}, segments=_SEGMENTS)
    assert {c.clip_id for c in broll} == {"hands_on_craft"}
