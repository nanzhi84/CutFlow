from __future__ import annotations

import pytest

from apps.api.services.annotation_patch import apply_patch, build_projection
from packages.core import contracts as c
from packages.core.workflow import NodeExecutionError


def _asset() -> c.MediaAssetRecord:
    return c.MediaAssetRecord(
        id="asset_bgm",
        case_id="case_bgm",
        title="Track",
        kind="bgm",
        source_artifact_id="art_bgm",
    )


def _segment(start: float = 10.0, end: float = 22.0, *, role: str = "hook") -> dict:
    return {
        "segment_id": f"bgm_{start}_{end}",
        "start": start,
        "end": end,
        "duration": round(end - start, 3),
        "role": role,
        "drop_anchor_sec": start + 2.0,
        "energy": 0.82,
        "mood": "燃",
        "scene_fit": ["转场"],
        "avoid_scene": ["静态讲解"],
        "reason": "drop clear",
        "confidence": 0.9,
        "source": "sensor+audio",
    }


def _annotation(duration: float = 60.0) -> c.AnnotationV4:
    return c.AnnotationV4(
        meta=c.AnnotationMetaV4(
            asset_id="asset_bgm",
            case_id="case_bgm",
            material_type="bgm",
            duration=duration,
            annotation_status=c.AnnotationStatus.completed,
        ),
        bgm_segments=[c.BgmSegmentV4.model_validate(_segment())],
    )


def test_build_projection_exposes_bgm_segments():
    projection = build_projection(_annotation(), _asset())

    assert "bgm_usage_windows" not in projection
    assert projection["bgm_segments"][0]["segment_id"] == "bgm_10.0_22.0"
    assert projection["bgm_segments"][0]["mood"] == "燃"


def test_patch_merges_bgm_segments_into_canonical_and_projection():
    canonical = _annotation().model_dump(mode="json")

    new_canonical, new_projection = apply_patch(
        canonical=canonical,
        projection={"title": "Track", "usable": False},
        asset=_asset(),
        operations=[
            {
                "op": "replace",
                "path": "/canonical/bgm_segments",
                "value": [_segment(24.0, 36.0, role="climax")],
            }
        ],
    )

    assert "bgm_usage_windows" not in new_canonical
    assert "bgm_usage_windows" not in new_projection
    assert new_canonical["bgm_segments"][0]["role"] == "climax"
    assert new_projection["bgm_segments"][0]["mood"] == "燃"
    c.AnnotationV4.model_validate(new_canonical)


def test_patch_rejects_bgm_segments_outside_duration():
    canonical = _annotation(duration=30.0).model_dump(mode="json")

    with pytest.raises(NodeExecutionError) as exc:
        apply_patch(
            canonical=canonical,
            projection={},
            asset=_asset(),
            operations=[
                {
                    "op": "replace",
                    "path": "/canonical/bgm_segments",
                    "value": [_segment(20.0, 40.0)],
                }
            ],
        )

    assert exc.value.error.code == c.ErrorCode.render_invalid_timeline


@pytest.mark.parametrize(
    "path",
    [
        "/projection/bgm_usage_windows",
        "/canonical/bgm_usage_windows",
    ],
)
def test_patch_rejects_deprecated_bgm_usage_windows_paths(path: str):
    canonical = _annotation().model_dump(mode="json")

    with pytest.raises(NodeExecutionError) as exc:
        apply_patch(
            canonical=canonical,
            projection={"title": "Track", "usable": True},
            asset=_asset(),
            operations=[
                {
                    "op": "replace",
                    "path": path,
                    "value": [_segment()],
                }
            ],
        )

    assert exc.value.error.code == c.ErrorCode.artifact_schema_mismatch
