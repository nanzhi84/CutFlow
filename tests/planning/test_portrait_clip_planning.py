"""P2: clip-level portrait (lip-sync A-roll) gating from a unified video.

In a single mixed ``video`` annotation only talking-head clips (single-face,
recommended_for_lip_sync, long enough) are lip-sync usable; its cover clips go to
the b-roll pool instead, and its talking-head clips are kept OUT of the b-roll
pool.
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
from packages.planning.material import clip_is_lip_sync_usable, rank_broll_candidates
from packages.planning.material.keywords import ScriptSegment


def _clip(
    segment_id,
    start,
    end,
    *,
    role=UsageRole.main,
    lip_sync=True,
    voiceover_only=False,
    face_count_max=1,
    keywords=("内容",),
    semantics: dict | None = None,
):
    semantic_payload = {"scene_type": "场景", "face_count_max": face_count_max}
    if semantics:
        semantic_payload.update(semantics)
    return ClipV4(
        segment_id=segment_id,
        start=start,
        end=end,
        duration=end - start,
        semantics=ClipSemanticsV4(**semantic_payload),
        usage=ClipUsageV4(
            role=role,
            recommended_for_lip_sync=lip_sync,
            voiceover_only=voiceover_only,
        ),
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
        quality_report={"usable_ratio": 0.9, "lip_sync_suitability_score": 80},
    )


def test_clip_is_lip_sync_usable_gates():
    # usable talking head
    assert clip_is_lip_sync_usable(_clip("ok", 2.0, 9.0))
    # not recommended for lip sync (a cover clip)
    assert not clip_is_lip_sync_usable(
        _clip("cover", 0.0, 5.0, role=UsageRole.cover, lip_sync=False)
    )
    # multi-face frame -> cannot lip-sync
    assert not clip_is_lip_sync_usable(_clip("multi", 0.0, 5.0, face_count_max=2))
    # too short to anchor a narration chunk
    assert not clip_is_lip_sync_usable(_clip("tiny", 0.0, 0.4))
    # avoid role
    assert not clip_is_lip_sync_usable(_clip("avoid", 0.0, 5.0, role=UsageRole.avoid))
    # voiceover-only portrait clips must not enter the lip-sync A-roll pool
    assert not clip_is_lip_sync_usable(_clip("voiceonly", 0.0, 5.0, voiceover_only=True))
    # face_count_max None (CV unavailable) is fail-open -> still usable
    assert clip_is_lip_sync_usable(_clip("nofcm", 2.0, 9.0, face_count_max=None))


def test_static_single_face_person_clip_can_feed_lipsync_source():
    static_person = _clip(
        "static_person",
        0.0,
        7.0,
        role=UsageRole.cover,
        lip_sync=False,
        semantics={
            "subject_type": "person",
            "gaze_to_camera": True,
            "mouth_visible": True,
            "mouth_moving": False,
            "body_orientation": "frontal",
        },
    )

    assert clip_is_lip_sync_usable(static_person)


def test_video_talking_head_clips_stay_out_of_broll_pool():
    # A main-role lip-sync clip whose keywords match the script must NOT surface as a
    # b-roll candidate (it is A-roll); only the cover clip should.
    annotation = _video_annotation(
        "vid_mixed",
        [
            _clip("talk", 0.0, 6.0, keywords=("打磨", "工艺")),  # role=main -> excluded from b-roll
            _clip(
                "cover",
                6.0,
                10.0,
                role=UsageRole.cover,
                lip_sync=False,
                keywords=("打磨", "工艺"),
            ),
        ],
    )
    segments = [ScriptSegment(text="打磨工艺细节", start=0.0, end=4.0, keywords=("打磨", "工艺"))]
    broll = rank_broll_candidates(annotations={"vid_mixed": annotation}, segments=segments)
    clip_ids = {c.clip_id for c in broll}
    assert "talk" not in clip_ids
    assert "cover" in clip_ids


def test_broll_generic_cover_backfill_is_opt_in():
    annotation = _video_annotation(
        "vid_shelf",
        [
            _clip(
                "shelf_cover",
                0.0,
                5.0,
                role=UsageRole.cover,
                lip_sync=False,
                keywords=("货架", "商品"),
                semantics={"subject_type": "product"},
            )
        ],
    )
    segments = [
        ScriptSegment(text="顺手买点啥，真的不贵。", start=0.0, end=5.0, keywords=("顺手", "不贵"))
    ]

    assert rank_broll_candidates(annotations={"vid_shelf": annotation}, segments=segments) == []
    coverage = rank_broll_candidates(
        annotations={"vid_shelf": annotation},
        segments=segments,
        include_generic_coverage=True,
    )

    assert [candidate.clip_id for candidate in coverage] == ["shelf_cover"]
    assert coverage[0].matched_keywords == ()
    assert coverage[0].score < 50


def test_video_backup_lipsync_clip_stays_out_of_broll_pool():
    annotation = _video_annotation(
        "vid_mixed",
        [
            _clip(
                "backup_talk",
                0.0,
                6.0,
                role=UsageRole.backup,
                lip_sync=True,
                keywords=("打磨", "工艺"),
            ),
            _clip(
                "cover",
                6.0,
                10.0,
                role=UsageRole.cover,
                lip_sync=False,
                keywords=("打磨", "工艺"),
            ),
        ],
    )
    segments = [ScriptSegment(text="打磨工艺细节", start=0.0, end=4.0, keywords=("打磨", "工艺"))]
    broll = rank_broll_candidates(annotations={"vid_mixed": annotation}, segments=segments)
    clip_ids = {c.clip_id for c in broll}
    assert "backup_talk" not in clip_ids
    assert "cover" in clip_ids
