"""Shot-cut detection sensor (the first of four cut-precision defences).

Uses PySceneDetect's ContentDetector for frame-accurate real shot cuts. Cuts are
the scene starts (excluding 0 and the tail), ascending and deduplicated. The
detector's native ``min_scene_len`` (in frames = round(fps * min_scene_len_sec))
merges ultra-short shots, so no hand-rolled merge. A video that won't open or
has no cuts (single take) returns [] for the window planner to handle.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

logger = logging.getLogger(__name__)

# Cut dedup / rounding precision: 3 decimals (~ms) is enough and absorbs jitter.
_CUT_TIME_DECIMALS = 3
# Dedup threshold (sec): adjacent cuts closer than this are the same point.
_DEDUP_EPS = 0.001


def _compute_min_scene_len_frames(fps: float, min_scene_len_sec: float) -> int:
    """Convert "shortest shot seconds" to ContentDetector frames = round(fps*sec), min 1.

    Any illegal input (<=0) or a zero result degrades to 1 frame (the minimum).
    """
    try:
        f = float(fps)
        s = float(min_scene_len_sec)
    except (TypeError, ValueError):
        return 1
    if f <= 0.0 or s <= 0.0:
        return 1
    frames = round(f * s)
    return max(1, int(frames))


def _scene_starts_to_cut_times(starts: Iterable[float]) -> list[float]:
    """Convert scene start seconds into cuts: drop 0 (head), ascending, deduped, rounded.

    PySceneDetect scenes look like [(start, end), ...] with the first starting at
    0; a cut is the boundary between shots, i.e. every scene start except the
    head. Dropping the ~0 head start yields the cut list (the tail isn't a start).
    """
    cuts: list[float] = []
    for raw in starts:
        try:
            t = round(float(raw), _CUT_TIME_DECIMALS)
        except (TypeError, ValueError):
            continue
        if t <= 0.0:
            continue  # head (0) is not a cut
        cuts.append(t)

    cuts.sort()
    deduped: list[float] = []
    for t in cuts:
        if not deduped or (t - deduped[-1]) >= _DEDUP_EPS:
            deduped.append(t)
    return deduped


def detect_shot_cuts(
    video_path: str,
    *,
    min_scene_len_sec: float = 3.0,
    threshold: float = 27.0,
) -> list[float]:
    """Detect frame-accurate shot cuts.

    Returns an ascending list of cut timestamps (seconds), excluding 0 and the
    tail. A video that won't open or a single take returns [].
    """
    if not video_path or not os.path.exists(video_path):
        return []

    try:
        # Lazy import so a missing scenedetect does not break the import chain.
        from scenedetect import ContentDetector, open_video
        from scenedetect.scene_manager import SceneManager
    except Exception as exc:  # pragma: no cover - dependency missing
        logger.warning("[shots] scenedetect unavailable: %s", exc)
        return []

    try:
        video = open_video(video_path)
    except Exception as exc:
        logger.warning("[shots] failed to open video %s: %s", video_path, exc)
        return []

    fps = float(getattr(video, "frame_rate", 0.0) or 0.0)
    min_scene_len_frames = _compute_min_scene_len_frames(fps, min_scene_len_sec)

    try:
        manager = SceneManager()
        manager.add_detector(
            ContentDetector(
                threshold=float(threshold),
                min_scene_len=min_scene_len_frames,
            )
        )
        manager.detect_scenes(video=video, show_progress=False)
        scene_list = manager.get_scene_list()
    except Exception as exc:
        logger.warning("[shots] scene detection failed %s: %s", video_path, exc)
        return []

    if not scene_list:
        return []

    starts: list[float] = []
    for scene in scene_list:
        try:
            tc = scene[0]
            # scenedetect 0.7: prefer .seconds (get_seconds() deprecated).
            starts.append(
                float(tc.seconds if hasattr(tc, "seconds") else tc.get_seconds())
            )
        except Exception:
            continue

    return _scene_starts_to_cut_times(starts)
