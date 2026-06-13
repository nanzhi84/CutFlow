"""ffmpeg/codec glue for the digital-human pipeline.

These are mechanical command builders extracted verbatim from the original
pipeline god-file. They contain no business logic beyond constructing and
running ffmpeg invocations; node handlers call them to build seed media,
transcode/concat portrait segments, composite the b-roll timeline, and mux the
final voice/BGM/subtitle output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from packages.core.contracts import Artifact, ErrorCode
from packages.core.workflow import NodeExecutionError
from packages.media.video.ffmpeg import FfmpegRunner, ffmpeg_bin, probe_media


def generate_seed_video(
    output_path: Path,
    *,
    duration_sec: float,
    width: int,
    height: int,
    fps: int,
) -> None:
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
            str(output_path),
        ]
    )


def generate_seed_audio(output_path: Path, *, duration_sec: float) -> None:
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
            f"sine=frequency=220:sample_rate=44100:duration={duration_sec:.3f}",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )


def transcode_video_segment(
    source_path: Path,
    output_path: Path,
    *,
    source_start: float,
    duration: float,
    width: int,
    height: int,
    fps: int,
) -> None:
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{source_start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source_path),
            "-an",
            "-vf",
            (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps={fps},setsar=1"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def concat_video_segments(segments: list[Path], output_path: Path) -> None:
    concat_list = output_path.with_suffix(".txt")
    concat_list.write_text(
        "\n".join(f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in segments),
        encoding="utf-8",
    )
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
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
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def fit_video_to_exact_duration(
    source_path: Path,
    output_path: Path,
    *,
    duration: float,
    width: int,
    height: int,
    fps: int,
) -> None:
    """Force a rendered track to be exactly ``duration`` seconds long.

    Per-segment ``-t`` ms-quantization, fps resampling and ``concat -c copy``
    accumulate sub-frame timing drift that, for longer tracks, exceeds the
    one-frame tolerance of the portrait-track sanity check. This re-encodes the
    concatenated track to a deterministic length so the check passes reliably:

    - ``tpad=stop_mode=clone`` clones the final frame to *pad* a short track
      past the target (the clone padding is generous: it always exceeds
      ``duration`` so the subsequent trim is what sets the exact length).
    - ``-t {duration:.3f}`` then *trims* to exactly ``duration``.

    The result is guaranteed ``>=`` the plan duration (no end freeze/black for a
    track that was already long enough) and never materially longer. One extra
    ffmpeg pass; the track is short, so re-encoding is cheap.
    """
    pad_duration = max(duration, 0.0) + 1.0
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source_path),
            "-an",
            "-vf",
            (
                f"tpad=stop_mode=clone:stop_duration={pad_duration:.3f},"
                f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1"
            ),
            "-t",
            f"{duration:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def render_video_timeline(
    *,
    main_path: Path,
    output_path: Path,
    broll_segments: list[dict],
    total_frames: int,
    width: int,
    height: int,
    fps: int,
    source_artifact_for_asset: Callable[[str | None], Artifact],
    artifact_path: Callable[[Artifact], Path],
) -> None:
    args = [
        ffmpeg_bin(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(main_path),
    ]
    overlay_inputs: list[tuple[dict, Path]] = []
    for segment in broll_segments:
        source_artifact = source_artifact_for_asset(segment.get("asset_id"))
        source_path = artifact_path(source_artifact)
        source_info = source_artifact.media_info or probe_media(source_path)
        source_duration = float(source_info.duration_sec or 0)
        source_start = float(segment.get("source_start", 0) or 0)
        source_end = float(segment.get("source_end", 0) or 0)
        if source_start < 0 or source_end <= source_start or source_end > source_duration + (1 / fps):
            raise NodeExecutionError(ErrorCode.render_invalid_timeline, "B-roll source window is out of bounds.")
        overlay_inputs.append((segment, source_path))
        args.extend(["-i", str(source_path)])

    filters = [
        (
            f"[0:v]fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},trim=start_frame=0:end_frame={total_frames},"
            "setpts=PTS-STARTPTS,setsar=1[base0]"
        )
    ]
    previous_label = "base0"
    total_duration = total_frames / fps
    for index, (segment, _) in enumerate(overlay_inputs, start=1):
        timeline_start = float(segment.get("start_sec", 0) or 0)
        timeline_end = float(segment.get("end_sec", 0) or 0)
        if timeline_start < 0 or timeline_end <= timeline_start or timeline_end > total_duration + (1 / fps):
            raise NodeExecutionError(ErrorCode.render_invalid_timeline, "B-roll timeline window is out of bounds.")
        source_start = float(segment.get("source_start", 0) or 0)
        source_end = float(segment.get("source_end", 0) or 0)
        overlay_label = f"ov{index}"
        next_label = f"base{index}"
        filters.append(
            (
                f"[{index}:v]trim=start={source_start:.3f}:end={source_end:.3f},"
                "setpts=PTS-STARTPTS,"
                f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,"
                f"setpts=PTS-STARTPTS+{timeline_start:.3f}/TB[{overlay_label}]"
            )
        )
        filters.append(
            (
                f"[{previous_label}][{overlay_label}]overlay="
                f"enable='between(t,{timeline_start:.3f},{timeline_end:.3f})':"
                f"x=0:y=0:eof_action=pass[{next_label}]"
            )
        )
        previous_label = next_label

    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            f"[{previous_label}]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    FfmpegRunner(timeout_sec=60).run(args)


def render_final_media(
    *,
    rendered_path: Path,
    audio_path: Path,
    output_path: Path,
    subtitle_path: Path | None,
    bgm_path: Path | None,
    bgm_volume: float,
    duration: float,
    fps: int,
) -> None:
    args = [
        ffmpeg_bin(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(rendered_path),
        "-i",
        str(audio_path),
    ]
    if bgm_path is not None:
        args.extend(["-stream_loop", "-1", "-i", str(bgm_path)])
    escaped_subtitle = str(subtitle_path).replace("\\", "\\\\").replace(":", "\\:") if subtitle_path else None
    video_filters = "[0:v]"
    if escaped_subtitle:
        video_filters += f"subtitles={escaped_subtitle},"
    video_filters += f"fps={fps},format=yuv420p[v]"
    if bgm_path is None:
        audio_filters = (
            f"[1:a]aresample=48000,apad=pad_dur=1,atrim=0:{duration:.3f},"
            "asetpts=PTS-STARTPTS[a]"
        )
    else:
        audio_filters = (
            f"[1:a]aresample=48000,volume=1.0,apad=pad_dur=1,atrim=0:{duration:.3f},"
            "asetpts=PTS-STARTPTS[voice];"
            f"[2:a]aresample=48000,volume={bgm_volume:.3f},atrim=0:{duration:.3f},"
            "asetpts=PTS-STARTPTS[bgm];"
            "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
    args.extend(
        [
            "-filter_complex",
            f"{video_filters};{audio_filters}",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    FfmpegRunner(timeout_sec=60).run(args)
