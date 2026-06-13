"""Mechanical ffmpeg command/filtergraph builders for the digital-human pipeline.

These free functions were extracted from ``digital_human.py`` so that the
self-contained ffmpeg argument and ASS subtitle construction lives separately
from node orchestration. They take explicit arguments only (no adapter state)
and are called by ``LocalRuntimeAdapter`` render methods. The ffmpeg command
strings, filtergraphs and validation are byte-for-byte identical to the
original implementation.
"""

from __future__ import annotations

from pathlib import Path

from packages.core.contracts import ErrorCode
from packages.core.workflow import NodeExecutionError
from packages.media.video.ffmpeg import FfmpegRunner, ffmpeg_bin


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


def render_video_timeline(
    *,
    main_path: Path,
    output_path: Path,
    overlay_inputs: list[tuple[dict, Path]],
    total_frames: int,
    width: int,
    height: int,
    fps: int,
) -> None:
    """Build the overlay filtergraph and run ffmpeg.

    ``overlay_inputs`` is the list of ``(segment, source_path)`` pairs already
    resolved and source-window-validated by the caller. This function performs
    the mechanical timeline-window validation and filtergraph assembly.
    """
    args = [
        ffmpeg_bin(),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(main_path),
    ]
    for _, source_path in overlay_inputs:
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


def write_ass_subtitles(
    output_path: Path,
    *,
    narration: dict,
    style: dict,
    width: int,
    height: int,
) -> None:
    subtitle = style.get("subtitle", {}) if isinstance(style.get("subtitle"), dict) else {}
    font_size = int(subtitle.get("font_size") or 64)
    margin_v = int(height * 0.12)
    position = subtitle.get("position")
    if isinstance(position, dict) and "y" in position:
        margin_v = max(20, int(height * (1 - float(position["y"]))))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
            f"1,0,0,0,100,100,0,0,1,4,1,2,80,80,{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for unit in narration.get("units", []):
        text = ass_escape(str(unit.get("text", "")))
        if not text:
            continue
        lines.append(
            "Dialogue: 0,"
            f"{ass_time(float(unit.get('start', 0) or 0))},"
            f"{ass_time(float(unit.get('end', 0) or 0))},"
            f"Default,,0,0,0,,{text}"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def ass_time(seconds: float) -> str:
    centiseconds = round(max(seconds, 0) * 100)
    hours, remainder = divmod(centiseconds, 3600 * 100)
    minutes, remainder = divmod(remainder, 60 * 100)
    secs, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("{", "").replace("}", "").replace("\n", r"\N")
