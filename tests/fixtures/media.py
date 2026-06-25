from __future__ import annotations

import json
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

from packages.media.video.ffmpeg import FfmpegRunner, ffmpeg_bin, ffprobe_bin


class MediaFixtureFactory:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def video(
        self,
        *,
        duration_sec: float = 2,
        width: int = 320,
        height: int = 568,
        fps: int = 30,
        filename: str | None = None,
    ) -> Path:
        return generate_test_video(
            self.directory,
            duration_sec=duration_sec,
            width=width,
            height=height,
            fps=fps,
            filename=filename,
        )

    def audio(
        self,
        *,
        duration_sec: float = 2,
        sample_rate: int = 16000,
        frequency: int = 440,
        filename: str | None = None,
    ) -> Path:
        return generate_test_audio(
            self.directory,
            duration_sec=duration_sec,
            sample_rate=sample_rate,
            frequency=frequency,
            filename=filename,
        )


def generate_test_video(
    directory: Path,
    *,
    duration_sec: float = 2,
    width: int = 320,
    height: int = 568,
    fps: int = 30,
    filename: str | None = None,
) -> Path:
    path = directory / (filename or f"testsrc2_{width}x{height}_{fps}fps_{duration_sec:g}s.mp4")
    if path.exists():
        return path
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate={fps}",
            "-t",
            f"{duration_sec:.3f}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-movflags",
            "+faststart",
            str(path),
        ]
    )
    return path


def generate_test_hdr_video(
    directory: Path,
    *,
    duration_sec: float = 1,
    width: int = 320,
    height: int = 568,
    fps: int = 15,
    filename: str | None = None,
) -> Path:
    """Generate a BT.2020 / PQ (smpte2084) HDR video for tonemap tests.

    Uses HEVC 10-bit with explicit HDR color tags so ``probe_media`` reports
    ``is_hdr=True``."""
    path = directory / (filename or f"hdr_{width}x{height}_{fps}fps_{duration_sec:g}s.mp4")
    if path.exists():
        return path
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate={fps}",
            "-t",
            f"{duration_sec:.3f}",
            "-pix_fmt",
            "yuv420p10le",
            "-c:v",
            "libx265",
            "-x265-params",
            "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
            "-color_primaries",
            "bt2020",
            "-color_trc",
            "smpte2084",
            "-colorspace",
            "bt2020nc",
            str(path),
        ]
    )
    return path


def generate_test_audio(
    directory: Path,
    *,
    duration_sec: float = 2,
    sample_rate: int = 16000,
    frequency: int = 440,
    filename: str | None = None,
) -> Path:
    path = directory / (filename or f"sine_{sample_rate}hz_{duration_sec:g}s.wav")
    if path.exists():
        return path
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate={sample_rate}:duration={duration_sec:.3f}",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )
    return path


@lru_cache
def ffmpeg_has_filter(name: str) -> bool:
    result = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    filters = f"{result.stdout}\n{result.stderr}"
    return re.search(rf"^\s*[.A-Z|]+\s+{re.escape(name)}\s", filters, re.MULTILINE) is not None


def require_ffmpeg_filters(*names: str) -> None:
    missing = [name for name in names if not ffmpeg_has_filter(name)]
    if missing:
        pytest.skip(f"ffmpeg missing required filter(s): {', '.join(missing)}")


@lru_cache
def ffmpeg_writes_strict_bt709_tags() -> bool:
    with tempfile.TemporaryDirectory(prefix="cutagent-ffmpeg-cap-") as directory:
        output = Path(directory) / "bt709.mp4"
        try:
            FfmpegRunner().run(
                [
                    ffmpeg_bin(),
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=64x64:rate=5",
                    "-t",
                    "0.200",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-color_range",
                    "tv",
                    "-colorspace",
                    "bt709",
                    "-color_primaries",
                    "bt709",
                    "-color_trc",
                    "bt709",
                    "-movflags",
                    "+faststart+write_colr",
                    str(output),
                ]
            )
            probe = FfmpegRunner().run(
                [
                    ffprobe_bin(),
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=color_space,color_transfer,color_primaries",
                    "-of",
                    "json",
                    str(output),
                ]
            )
            stream = (json.loads(probe.stdout).get("streams") or [{}])[0]
        except Exception:
            return False
    return (
        str(stream.get("color_space", "")).lower() == "bt709"
        and str(stream.get("color_transfer", "")).lower() == "bt709"
        and str(stream.get("color_primaries", "")).lower() == "bt709"
    )


def require_strict_bt709_tags() -> None:
    if not ffmpeg_writes_strict_bt709_tags():
        pytest.skip("ffmpeg does not preserve strict bt709 color tags")
