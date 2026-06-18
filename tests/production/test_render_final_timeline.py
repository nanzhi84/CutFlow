from __future__ import annotations

import pytest

from packages.production.pipeline.nodes.render_final_timeline import _broll_segments_from_timeline


def test_render_broll_segments_use_timeline_frames_over_original_plan_seconds():
    timeline = {
        "tracks": [
            {
                "track_id": "broll",
                "segment_id": "broll_1",
                "timeline_start_frame": 90,
                "timeline_end_frame": 150,
                "source_start_frame": 3,
                "source_end_frame": 63,
            }
        ]
    }
    broll_plan = {
        "segments": [
            {
                "asset_id": "asset_demo",
                "clip_id": "clip_demo",
                "start_sec": 3.0,
                "end_sec": 4.9,
                "source_start": 0.1,
                "source_end": 2.0,
                "reason": "matched",
            }
        ]
    }

    segments = _broll_segments_from_timeline(timeline, broll_plan, fps=30)

    assert len(segments) == 1
    segment = segments[0]
    assert segment["asset_id"] == "asset_demo"
    assert segment["clip_id"] == "clip_demo"
    assert segment["start_sec"] == pytest.approx(3.0)
    assert segment["end_sec"] == pytest.approx(5.0)
    assert segment["source_start"] == pytest.approx(0.1)
    assert segment["source_end"] == pytest.approx(2.1)
    assert segment["reason"] == "matched"
