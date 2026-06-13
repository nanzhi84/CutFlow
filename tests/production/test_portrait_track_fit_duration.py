"""Real-ffmpeg coverage for ``fit_video_to_exact_duration`` (portrait exact-fit).

The portrait-track build (``nodes/portrait_track_build``) concatenates per-segment
transcodes whose ``-t`` ms-quantization + fps resampling + ``concat -c copy``
accumulate sub-frame drift that, for longer tracks, exceeds the one-frame
tolerance of the duration sanity check. ``fit_video_to_exact_duration`` re-encodes
the concatenated track to exactly the plan duration (clone-pad if short, trim if
long). The node-level wiring + relaxed tolerance is covered by
``tests/production/test_portrait_planning_node.py`` and the golden workflow; this
test drives the real ffmpeg pass directly to prove both pad-short and trim-long
branches land on the exact target.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from packages.media.video.ffmpeg import FfmpegRunner, ffmpeg_bin, probe_media
from packages.production.pipeline._ffmpeg import fit_video_to_exact_duration

PLAN_WIDTH = 720
PLAN_HEIGHT = 1280
PLAN_FPS = 30


@pytest.mark.skipif(shutil.which(ffmpeg_bin()) is None, reason="ffmpeg not available")
@pytest.mark.parametrize("source_dur,target", [(2.0, 5.0), (8.0, 5.0)])
def test_fit_video_to_exact_duration_real_ffmpeg(
    tmp_path: Path, source_dur: float, target: float
) -> None:
    """The real ffmpeg pass pads-short / trims-long to the exact target.

    Covers both branches: a 2s source padded up to 5s (clone) and an 8s source
    trimmed down to 5s. The output must be >= target (no end freeze for the
    already-long case) and not materially longer.
    """
    src = tmp_path / f"src_{source_dur:g}.mp4"
    FfmpegRunner().run(
        [
            ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={PLAN_WIDTH}x{PLAN_HEIGHT}:rate={PLAN_FPS}",
            "-t", f"{source_dur:.3f}", "-pix_fmt", "yuv420p", "-c:v", "libx264",
            "-preset", "ultrafast", str(src),
        ]
    )
    out = tmp_path / "fitted.mp4"
    fit_video_to_exact_duration(
        src, out, duration=target, width=PLAN_WIDTH, height=PLAN_HEIGHT, fps=PLAN_FPS
    )
    info = probe_media(out)
    actual = float(info.duration_sec or 0)
    # Exactly target within a couple frames; never short, never materially long.
    assert actual >= target - (1 / PLAN_FPS)
    assert abs(actual - target) <= max(2 / PLAN_FPS, 0.05)
