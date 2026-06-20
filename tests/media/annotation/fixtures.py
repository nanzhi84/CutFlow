"""Synthetic, in-test fixtures for the annotation sensor suite.

Everything is generated offline with ffmpeg/numpy/cv2 - no network, no sample
assets checked in. Helpers build tiny mp4s (multi-cut clips, black clips) and
frame images so the sensor tests stay fast and deterministic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, capture_output=True, check=True, timeout=60)


def make_multi_cut_video(
    directory: Path,
    *,
    fps: int = 25,
    seg_dur: float = 2.0,
    width: int = 320,
    height: int = 240,
) -> Path:
    """A 3-segment clip (red -> green -> blue) = 2 hard cuts, via concat of solid colors.

    Solid-color segments give PySceneDetect's ContentDetector unambiguous content
    changes at the segment boundaries.
    """
    out = directory / f"multicut_{fps}fps_{seg_dur:g}s.mp4"
    if out.exists():
        return out
    segs: list[Path] = []
    for _idx, color in enumerate(("red", "green", "blue")):
        seg = directory / f"_seg_{color}.mp4"
        _run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s={width}x{height}:r={fps}:d={seg_dur:.3f}",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                str(seg),
            ]
        )
        segs.append(seg)
    concat_list = directory / "_concat.txt"
    concat_list.write_text("".join(f"file '{s}'\n" for s in segs))
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            str(out),
        ]
    )
    return out


def make_black_video(
    directory: Path,
    *,
    duration: float = 1.5,
    fps: int = 25,
    width: int = 320,
    height: int = 240,
) -> Path:
    """A fully black clip (triggers blackdetect)."""
    out = directory / f"black_{duration:g}s.mp4"
    if out.exists():
        return out
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}:r={fps}:d={duration:.3f}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(out),
        ]
    )
    return out


def make_tone_wav(
    directory: Path,
    *,
    duration: float = 2.0,
    sample_rate: int = 16000,
    frequency: int = 220,
) -> Path:
    """A pure sine-tone wav (Silero typically reads as non-speech, but exercises the path)."""
    out = directory / f"tone_{frequency}hz_{duration:g}s.wav"
    if out.exists():
        return out
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate={sample_rate}:duration={duration:.3f}",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(out),
        ]
    )
    return out


def make_silent_wav(
    directory: Path,
    *,
    duration: float = 2.0,
    sample_rate: int = 16000,
) -> Path:
    """A silent wav (VAD must return no speech islands)."""
    out = directory / f"silence_{duration:g}s.wav"
    if out.exists():
        return out
    _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl=mono",
            "-t",
            f"{duration:.3f}",
            "-c:a",
            "pcm_s16le",
            str(out),
        ]
    )
    return out


def make_face_image(directory: Path, *, name: str = "single_face.png"):
    """A synthetic frontal "face" drawn with cv2 (skin oval, eyes, mouth) on a plain bg.

    Not guaranteed to trip YuNet (it is a real CNN), so face-count tests assert on
    the deterministic >=0 contract rather than an exact positive count.
    """
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    img = np.full((480, 480, 3), 210, dtype=np.uint8)
    cv2.ellipse(img, (240, 250), (110, 140), 0, 0, 360, (180, 200, 230), -1)
    cv2.circle(img, (200, 220), 18, (255, 255, 255), -1)
    cv2.circle(img, (280, 220), 18, (255, 255, 255), -1)
    cv2.circle(img, (200, 220), 8, (40, 40, 40), -1)
    cv2.circle(img, (280, 220), 8, (40, 40, 40), -1)
    cv2.ellipse(img, (240, 320), (45, 20), 0, 0, 180, (60, 60, 120), 4)
    out = directory / name
    cv2.imwrite(str(out), img)
    return out, img


def make_blank_image(directory: Path, *, name: str = "blank.png"):
    """A flat gray image with no face (face count must be 0)."""
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    img = np.full((240, 320, 3), 128, dtype=np.uint8)
    out = directory / name
    cv2.imwrite(str(out), img)
    return out, img
