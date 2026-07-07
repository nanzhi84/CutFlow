"""Read-boundary helper for BrollPlanArtifact: overlays are canonical (#104).

``broll_overlays_from_plan`` is the single place that knows how to read a
(possibly legacy) ``BrollPlanArtifact`` payload. New plans only write
``overlays``; the helper still derives overlays from the pre-#104 dict
``segments`` shape when a legacy persisted plan has no ``overlays``.
"""

from __future__ import annotations

from packages.core.contracts.artifacts import BrollOverlay
from packages.production._broll_overlays import broll_overlays_from_plan


def test_prefers_typed_overlays_when_present():
    plan = {
        "enabled": True,
        "overlays": [
            {
                "overlay_id": "broll_1",
                "asset_id": "asset_demo",
                "clip_id": "cover_a",
                "timeline_start": 1.0,
                "timeline_end": 3.0,
                "source_start": 0.5,
                "source_end": 2.5,
                "reason": "matched",
                "confidence": 0.8,
                "matched_keywords": ["x"],
                "scene_name": "demo",
                "diversity_key": "scene:demo",
                "fade_frames": 8,
                "placement": "pip_fixed",
            }
        ],
    }

    overlays = broll_overlays_from_plan(plan)

    assert len(overlays) == 1
    overlay = overlays[0]
    assert isinstance(overlay, BrollOverlay)
    assert overlay.overlay_id == "broll_1"
    assert overlay.asset_id == "asset_demo"
    assert overlay.clip_id == "cover_a"
    assert overlay.timeline_start == 1.0
    assert overlay.timeline_end == 3.0
    assert overlay.source_start == 0.5
    assert overlay.source_end == 2.5
    assert overlay.diversity_key == "scene:demo"
    assert overlay.fade_frames == 8
    assert overlay.placement == "pip_fixed"


def test_derives_overlays_from_legacy_segments_when_no_overlays():
    # A pre-#104 persisted BrollPlanArtifact: only the legacy dict ``segments``
    # (start_sec/end_sec), no ``overlays`` key at all. The helper must still
    # yield typed overlays so every downstream consumer keeps working.
    legacy_plan = {
        "enabled": True,
        "segments": [
            {
                "asset_id": "asset_legacy",
                "clip_id": "cover_legacy",
                "start_sec": 2.0,
                "end_sec": 4.0,
                "source_start": 1.0,
                "source_end": 3.0,
                "reason": "legacy-matched",
                "confidence": 0.7,
                "matched_keywords": ["legacy"],
                "scene_name": "old",
                "diversity_key": "scene:old",
            }
        ],
    }

    overlays = broll_overlays_from_plan(legacy_plan)

    assert len(overlays) == 1
    overlay = overlays[0]
    assert isinstance(overlay, BrollOverlay)
    # A positional overlay id is synthesised for legacy segments.
    assert overlay.overlay_id == "broll_1"
    assert overlay.asset_id == "asset_legacy"
    assert overlay.clip_id == "cover_legacy"
    # legacy start_sec/end_sec map onto canonical timeline_start/timeline_end.
    assert overlay.timeline_start == 2.0
    assert overlay.timeline_end == 4.0
    assert overlay.source_start == 1.0
    assert overlay.source_end == 3.0
    assert overlay.reason == "legacy-matched"
    assert overlay.confidence == 0.7
    assert overlay.matched_keywords == ["legacy"]
    assert overlay.scene_name == "old"
    assert overlay.diversity_key == "scene:old"


def test_overlays_win_over_any_stale_legacy_segments():
    # If both are present (only old data should be), overlays are canonical.
    plan = {
        "enabled": True,
        "segments": [{"asset_id": "stale", "start_sec": 0.0, "end_sec": 1.0}],
        "overlays": [
            {
                "overlay_id": "broll_1",
                "asset_id": "fresh",
                "timeline_start": 5.0,
                "timeline_end": 6.0,
                "source_start": 0.0,
                "source_end": 1.0,
                "reason": "matched",
                "confidence": 0.9,
            }
        ],
    }

    overlays = broll_overlays_from_plan(plan)

    assert [o.asset_id for o in overlays] == ["fresh"]
    assert overlays[0].timeline_start == 5.0


def test_empty_overlays_still_win_over_stale_legacy_segments():
    # overlays being present is the schema signal. An intentionally empty canonical
    # plan must not resurrect stale legacy segments from a mixed payload.
    plan = {
        "enabled": True,
        "overlays": [],
        "segments": [{"asset_id": "stale", "start_sec": 0.0, "end_sec": 1.0}],
    }

    assert broll_overlays_from_plan(plan) == []


def test_empty_or_disabled_plan_yields_no_overlays():
    assert broll_overlays_from_plan(None) == []
    assert broll_overlays_from_plan({}) == []
    assert broll_overlays_from_plan({"enabled": False}) == []
    assert broll_overlays_from_plan({"enabled": True, "overlays": [], "segments": []}) == []
