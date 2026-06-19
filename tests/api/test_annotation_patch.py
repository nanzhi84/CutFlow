"""Unit tests for the annotation PatchService + replace-source re-clip (Spec §12.2)."""

from __future__ import annotations

import pytest

from apps.api.services.annotation_patch import apply_patch, build_projection
from packages.core import contracts as c
from packages.core.workflow import NodeExecutionError
from packages.media.annotation.reclip import (
    reclip_canonical_to_duration,
    reclipped_or_validated,
)


def _asset(duration_meta: float = 0.0) -> c.MediaAssetRecord:
    return c.MediaAssetRecord(
        id="asset1", case_id="case1", title="T", kind="broll", source_artifact_id="art1", tags=["x"]
    )


def _segment(start: float, end: float, role: str = "cover") -> dict:
    return {
        "segment_id": f"seg_{start}_{end}",
        "start": start,
        "end": end,
        "duration": round(end - start, 3),
        "usage": {"role": role, "recommended_for_voiceover": True},
        "retrieval": {"summary": "s"},
    }


def test_patch_merges_segments_into_canonical_clips():
    canonical = {"labels": ["x"], "kind": "broll"}
    new_canonical, new_projection = apply_patch(
        canonical=canonical,
        projection={"title": "T", "usable": True},
        asset=_asset(),
        operations=[{"op": "replace", "path": "/canonical/segments", "value": [_segment(0, 2)]}],
    )
    # Canonical owns the edited clip; it is a real AnnotationV4 (re-validates).
    assert [clip["segment_id"] for clip in new_canonical["clips"]] == ["seg_0_2"]
    ann = c.AnnotationV4.model_validate(new_canonical)
    assert ann.clips[0].usage.role == c.UsageRole.cover
    # Projection is rebuilt from canonical (segments mirrored), labels stay in projection.
    assert new_projection["segments"][0]["segment_id"] == "seg_0_2"
    assert "labels" not in new_canonical


def test_patch_rejects_invalid_segment_schema():
    with pytest.raises(NodeExecutionError) as exc:
        apply_patch(
            canonical={"labels": [], "kind": "broll"},
            projection={},
            asset=_asset(),
            operations=[{"op": "replace", "path": "/canonical/segments", "value": [{"label": "keep"}]}],
        )
    assert exc.value.error.code == c.ErrorCode.artifact_schema_mismatch


def test_patch_rejects_inverted_time():
    with pytest.raises(NodeExecutionError) as exc:
        apply_patch(
            canonical={"labels": [], "kind": "broll"},
            projection={},
            asset=_asset(),
            operations=[{"op": "replace", "path": "/canonical/segments", "value": [_segment(2.0, 1.0)]}],
        )
    assert exc.value.error.code == c.ErrorCode.artifact_schema_mismatch


def test_patch_merges_quality_events_into_canonical():
    new_canonical, _ = apply_patch(
        canonical={"labels": [], "kind": "broll"},
        projection={},
        asset=_asset(),
        operations=[
            {
                "op": "replace",
                "path": "/canonical/quality_events",
                "value": [{"event_type": "manual_note", "start": 0.0, "end": 1.0, "risk_tier": "soft"}],
            }
        ],
    )
    ann = c.AnnotationV4.model_validate(new_canonical)
    assert ann.quality_events[0].event_type == c.QualityEventType.manual_note


def test_patch_labels_and_title_land_in_projection():
    _, projection = apply_patch(
        canonical={"labels": [], "kind": "broll"},
        projection={"title": "old", "usable": True},
        asset=_asset(),
        operations=[
            {"op": "replace", "path": "/labels", "value": ["a", "b"]},
            {"op": "replace", "path": "/title", "value": "new-title"},
        ],
    )
    assert projection["labels"] == ["a", "b"]
    assert projection["title"] == "new-title"


def _v4_canonical(duration: float, clip_end: float) -> dict:
    ann = c.AnnotationV4(
        meta=c.AnnotationMetaV4(asset_id="a", case_id="c", material_type="broll", duration=duration),
        clips=[c.ClipV4.model_validate(_segment(0.0, clip_end))],
        usage_windows=[c.UsageWindowV4(start=0.0, end=clip_end, role=c.UsageRole.cover)],
        evidence_frames=[clip_end],
    )
    return ann.model_dump(mode="json")


def test_reclip_clamps_clips_to_new_duration():
    canonical = _v4_canonical(duration=4.0, clip_end=4.0)
    reclipped = reclip_canonical_to_duration(canonical, old_duration=4.0, new_duration=2.0)
    assert reclipped is not None
    # End-of-video clip snaps to the new end then clamps within [0, 2].
    assert reclipped["clips"][0]["end"] <= 2.0 + 1e-6
    assert reclipped["meta"]["duration"] == pytest.approx(2.0)
    # The re-clipped canonical re-validates against the new duration.
    c.AnnotationV4.model_validate(reclipped)


def test_reclipped_or_validated_returns_none_for_stub():
    assert reclipped_or_validated({"labels": [], "kind": "broll"}, old_duration=2.0, new_duration=1.0) is None


def test_reclipped_or_validated_invalidates_bgm_segments_on_source_drift():
    canonical = c.AnnotationV4(
        meta=c.AnnotationMetaV4(asset_id="bgm1", case_id="case1", material_type="bgm", duration=120.0),
        bgm_segments=[
            c.BgmSegmentV4(
                segment_id="bgm_segment_1",
                start=0.0,
                end=60.0,
                duration=60.0,
                role=c.BgmSegmentRole.hook,
            ),
            c.BgmSegmentV4(
                segment_id="bgm_segment_2",
                start=60.0,
                end=120.0,
                duration=60.0,
                role=c.BgmSegmentRole.climax,
            ),
        ],
        quality_report={"bgm": {"annotated_coverage_ratio": 1.0, "segment_count": 2}},
    ).model_dump(mode="json")

    assert reclipped_or_validated(canonical, old_duration=120.0, new_duration=180.0) is None


def test_reclip_drops_segment_entirely_past_new_duration():
    # A mid-clip far past the new (much shorter) duration collapses and is dropped.
    canonical = _v4_canonical(duration=10.0, clip_end=9.0)
    # Add a clip that lives entirely in [8, 9] (past a 2s new duration, not an endpoint).
    extra = _segment(8.0, 8.5)
    canonical["clips"].append(extra)
    reclipped = reclip_canonical_to_duration(canonical, old_duration=10.0, new_duration=2.0)
    assert reclipped is not None
    seg_ids = {clip["segment_id"] for clip in reclipped["clips"]}
    assert "seg_8.0_8.5" not in seg_ids
    c.AnnotationV4.model_validate(reclipped)


def test_build_projection_mirrors_canonical_segments():
    asset = _asset()
    ann = c.AnnotationV4.model_validate(_v4_canonical(duration=4.0, clip_end=4.0))
    projection = build_projection(ann, asset, vlm_configured=True)
    assert projection["usable"] is True  # has usage_windows
    assert projection["segments"][0]["segment_id"] == "seg_0.0_4.0"
    assert projection["vlm_configured"] is True
