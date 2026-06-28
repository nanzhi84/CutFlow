from __future__ import annotations

import hashlib
import json
import re
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Sequence

from packages.core.config import build_settings
from packages.core.contracts import ErrorCode, MediaInfo


DEFAULT_TIMEOUT_SEC = 30
VIDEO_PROCESS_TIMEOUT_SEC = 300
# Headroom factor applied to the size budget when deriving a target bitrate, so a
# single-pass encode lands comfortably under the cap (leaving margin for the audio
# track + container overhead). Mirrors the origin video_processor 0.95 margin.
COMPRESS_BUDGET_MARGIN = 0.95
# Bitrate caps (kbps) for the resolution-reduction fallback strategies. Mirror the
# origin ladder: 720p capped at 2500k, 480p capped at 1500k.
COMPRESS_720P_MAX_KBPS = 2500
COMPRESS_480P_MAX_KBPS = 1500
STABILIZATION_SHAKINESS = 4
STABILIZATION_ACCURACY = 10
STABILIZATION_STEPSIZE = 6
STABILIZATION_MIN_CONTRAST = 0.2
STABILIZATION_SMOOTHING = 10
STABILIZATION_ZOOM = 2.0
STABILIZATION_MAX_SHIFT = 48
FFMPEG_QUIET_ARGS = ("-y", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error")
VIDEO_ENCODE_ARGS = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart")

# HDR -> SDR (BT.709) tonemap chain: PQ/HLG/BT.2020 sources are tonemapped to
# BT.709 before render/thumbnail to avoid washed-out / oversaturated color. The
# chain converts to linear light, tonemaps with Hable, then maps back to BT.709.
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67", "smpte428"})
HDR_PRIMARIES = frozenset({"bt2020"})
HDR_TONEMAP_VF = (
    "zscale=t=linear:npl=100,format=gbrpf32le,tonemap=tonemap=hable:desat=0,"
    "zscale=p=bt709:t=bt709:m=bt709:r=tv,format=yuv420p"
)
HDR_SDR_OUTPUT_ARGS = (
    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
)

# Strict delivery profile for normalized uploads (rotation-corrected, scaled to
# 1080p with letterbox padding, h264/yuv420p/bt709, post-encode validated).
UPLOAD_NORMALIZE_CRF = "20"
UPLOAD_NORMALIZE_PRESET = "medium"
UPLOAD_NORMALIZE_AUDIO_BITRATE = "192k"
UPLOAD_CROP_SAMPLE_COUNT = 4
UPLOAD_EMBEDDED_PORTRAIT_MAX_WIDTH_RATIO = 0.72
UPLOAD_EMBEDDED_PORTRAIT_MIN_HEIGHT_RATIO = 0.88
UPLOAD_EMBEDDED_PORTRAIT_MAX_ASPECT_RATIO = 0.75
UPLOAD_EMBEDDED_PORTRAIT_MIN_MARGIN_RATIO = 0.1
UPLOAD_EMBEDDED_PORTRAIT_MAX_VARIANCE_PX = 48


class FfmpegCommandError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: ErrorCode = ErrorCode.render_failed,
        command: Sequence[str] | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.command = list(command or [])
        self.stderr = stderr


@dataclass(frozen=True)
class ThumbnailResult:
    label: str
    path: Path
    sha256: str
    media_info: MediaInfo


@dataclass(frozen=True)
class CompressionStrategy:
    """One rung of the compress-to-budget ladder."""

    name: str
    video_kbps: int
    resolution: tuple[int, int] | None


@dataclass(frozen=True)
class CompressionResult:
    path: Path
    strategy: str
    size_bytes: int
    media_info: MediaInfo


def ffmpeg_bin() -> str:
    return _resolve_bin(build_settings().media.ffmpeg_bin, "ffmpeg")


def ffprobe_bin() -> str:
    return _resolve_bin(build_settings().media.ffprobe_bin, "ffprobe")


def _resolve_bin(configured: str | None, executable: str) -> str:
    if configured:
        return configured
    found = shutil.which(executable)
    if found:
        return found
    local = Path.home() / ".local" / "bin" / executable
    if local.exists():
        return str(local)
    return executable


class FfmpegRunner:
    def __init__(self, *, timeout_sec: int = DEFAULT_TIMEOUT_SEC) -> None:
        self.timeout_sec = timeout_sec

    def run(self, args: Sequence[str], *, timeout_sec: int | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(args),
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_sec or self.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise FfmpegCommandError(
                f"Media command timed out after {timeout_sec or self.timeout_sec}s.",
                error_code=ErrorCode.provider_timeout,
                command=args,
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            raise FfmpegCommandError(
                f"Media command failed with exit code {exc.returncode}.",
                error_code=ErrorCode.render_failed,
                command=args,
                stderr=stderr,
            ) from exc


def probe_media(path: str | Path) -> MediaInfo:
    media_path = Path(path)
    if not media_path.exists():
        raise FfmpegCommandError(
            f"Media file does not exist: {media_path}",
            error_code=ErrorCode.artifact_missing,
        )
    result = FfmpegRunner().run(
        [
            ffprobe_bin(), "-v", "error", "-show_entries",
            (
                "format=format_name,duration:stream="
                "codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,sample_rate,channels,duration,"
                "color_transfer,color_primaries,color_space:stream_tags=rotate:stream_side_data"
            ),
            "-of", "json", str(media_path),
        ]
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FfmpegCommandError("ffprobe returned invalid JSON.", command=[ffprobe_bin(), str(media_path)]) from exc
    streams = payload.get("streams") or []
    format_info = payload.get("format") or {}
    if not streams:
        raise FfmpegCommandError(f"No media streams found in {media_path}.")
    primary = _primary_stream(streams)
    codec_type = str(primary.get("codec_type") or "")
    codec = str(primary.get("codec_name") or "unknown")
    fmt = str(format_info.get("format_name") or media_path.suffix.lstrip(".") or "unknown")
    duration = _float_or_none(format_info.get("duration")) or _float_or_none(primary.get("duration"))
    if codec_type == "subtitle":
        return MediaInfo(
            media_type="subtitle",
            codec=codec,
            format=fmt,
            duration_sec=duration,
        )
    if codec_type == "audio":
        return MediaInfo(
            media_type="audio",
            codec=codec,
            format=fmt,
            duration_sec=duration,
            sample_rate=_int_or_none(primary.get("sample_rate")),
            channels=_int_or_none(primary.get("channels")),
        )
    media_type = "image" if _is_image(media_path, fmt, duration) else "video"
    color_transfer = _color_value(primary.get("color_transfer"))
    color_primaries = _color_value(primary.get("color_primaries"))
    return MediaInfo(
        media_type=media_type,
        codec=codec,
        format=fmt,
        duration_sec=None if media_type == "image" else duration,
        width=_int_or_none(primary.get("width")),
        height=_int_or_none(primary.get("height")),
        fps=None if media_type == "image" else _fps(primary),
        color_transfer=color_transfer,
        color_primaries=color_primaries,
        is_hdr=_is_hdr_color(color_transfer, color_primaries),
    )


def extract_thumbnails(
    video_path: str | Path,
    output_dir: str | Path,
    *,
    labels: tuple[str, str] = ("first", "mid"),
) -> list[ThumbnailResult]:
    source = Path(video_path)
    info = probe_media(source)
    if info.media_type != "video":
        raise FfmpegCommandError(f"Thumbnail source must be video: {source}")
    duration = float(info.duration_sec or 0)
    timestamps = [0.0, max(0.0, duration / 2.0)]
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    is_hdr = bool(info.is_hdr)
    results: list[ThumbnailResult] = []
    for label, timestamp in zip(labels, timestamps, strict=True):
        output = out_dir / f"{label}.png"
        # HDR sources are tonemapped to BT.709 SDR so the thumbnail/cover is not
        # washed out / oversaturated relative to the rendered video.
        hdr_args = ["-vf", HDR_TONEMAP_VF, *HDR_SDR_OUTPUT_ARGS] if is_hdr else []
        FfmpegRunner().run(
            [
                ffmpeg_bin(), *FFMPEG_QUIET_ARGS, "-ss", f"{timestamp:.3f}", "-i", str(source),
                *hdr_args, "-frames:v", "1", "-update", "1", str(output),
            ]
        )
        results.append(ThumbnailResult(label=label, path=output, sha256=sha256_file(output), media_info=probe_media(output)))
    return results


def extract_frame_at_time(
    video_path: str | Path,
    output_path: str | Path,
    *,
    time_sec: float = 0.0,
) -> ThumbnailResult:
    """Extract a single frame at ``time_sec`` (clamped into the video duration).

    Used by the publishing Cover Node for operator frame previews and the frame
    cover fallback (§2.2 cover.frame_fallback). Raises ``FfmpegCommandError`` on
    a non-video source or ffmpeg failure — never silently produces an empty frame.
    """
    source = Path(video_path)
    info = probe_media(source)
    if info.media_type != "video":
        raise FfmpegCommandError(f"Cover frame source must be video: {source}")
    duration = float(info.duration_sec or 0)
    requested = max(0.0, float(time_sec or 0.0))
    timestamp = min(requested, max(0.0, duration - 0.05)) if duration > 0 else requested
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    FfmpegRunner().run(
        [
            ffmpeg_bin(), *FFMPEG_QUIET_ARGS, "-ss", f"{timestamp:.3f}", "-i", str(source),
            "-frames:v", "1", "-update", "1", str(output),
        ]
    )
    if not output.exists() or output.stat().st_size <= 0:
        raise FfmpegCommandError(f"Cover frame extraction produced no output: {output}")
    return ThumbnailResult(
        label=f"frame_{timestamp:.3f}",
        path=output,
        sha256=sha256_file(output),
        media_info=probe_media(output),
    )


def needs_normalize_for_upload(path: str | Path) -> bool:
    """Whether the source must be transcoded to platform-safe upload codecs.

    True when the video stream is not H.264, the audio stream is not AAC, or the
    container is not MP4. Best-effort: a probe failure returns True so the caller
    transcodes rather than uploading an unknown-format file.
    """
    try:
        info = probe_media(path)
    except FfmpegCommandError:
        return True
    if info.media_type != "video":
        return False
    fmt = (info.format or "").lower()
    container_ok = "mp4" in fmt or "mov" in fmt or "m4a" in fmt
    codec_ok = (info.codec or "").lower() in {"h264", "avc1"}
    return not (container_ok and codec_ok)


def stabilize_video(
    video_path: str | Path,
    output_path: str | Path | None = None,
    *,
    timeout_sec: int = VIDEO_PROCESS_TIMEOUT_SEC,
) -> Path:
    source = Path(video_path)
    info = probe_media(source)
    if info.media_type != "video":
        raise FfmpegCommandError(f"Stabilization source must be video: {source}")
    output = Path(output_path) if output_path else source.with_name(f"{source.stem}_stabilized.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    runner = FfmpegRunner(timeout_sec=timeout_sec)
    base_args = [ffmpeg_bin(), *FFMPEG_QUIET_ARGS, "-i", str(source)]
    with tempfile.TemporaryDirectory(prefix="cutagent_vidstab_") as temp_dir:
        transforms = Path(temp_dir) / "transforms.trf"
        detect_vf = (
            f"format=yuv420p,vidstabdetect=result={_ffmpeg_filter_arg(transforms)}:"
            f"shakiness={STABILIZATION_SHAKINESS}:accuracy={STABILIZATION_ACCURACY}:"
            f"stepsize={STABILIZATION_STEPSIZE}:mincontrast={STABILIZATION_MIN_CONTRAST}"
        )
        runner.run(
            [
                *base_args, "-vf", detect_vf, "-an", "-f", "null", "-",
            ],
            timeout_sec=timeout_sec,
        )
        if not transforms.exists() or transforms.stat().st_size <= 0:
            raise FfmpegCommandError("Stabilization did not produce transform data.")
        # HDR sources are tonemapped to BT.709 SDR before the (linear-space)
        # stabilize transform so the stabilized asset that enters the pipeline is
        # not washed out / oversaturated.
        hdr_prefix = f"{HDR_TONEMAP_VF}," if info.is_hdr else ""
        hdr_output_args = list(HDR_SDR_OUTPUT_ARGS) if info.is_hdr else []
        transform_vf = (
            f"{hdr_prefix}vidstabtransform=input={_ffmpeg_filter_arg(transforms)}:smoothing={STABILIZATION_SMOOTHING}:"
            f"maxshift={STABILIZATION_MAX_SHIFT}:zoom={STABILIZATION_ZOOM}:optzoom=1:interpol=bicubic,format=yuv420p"
        )
        runner.run(
            [
                *base_args, "-map", "0:v:0", "-map", "0:a:0?", "-vf", transform_vf,
                *VIDEO_ENCODE_ARGS, *hdr_output_args, str(output),
            ],
            timeout_sec=timeout_sec,
        )
    probe_media(output)
    return output


def compress_video_to_budget(
    video_path: str | Path,
    *,
    max_size_mb: float,
    output_path: str | Path | None = None,
    timeout_sec: int = VIDEO_PROCESS_TIMEOUT_SEC,
) -> CompressionResult:
    """Downsize ``video_path`` so its file size fits ``max_size_mb``.

    Port of the origin ``video_processor.compress_video`` multi-strategy ladder
    (duration-derived target bitrate, then a 720p and a 480p resolution fallback).
    Used as the no-silent-degrade guard before submitting an oversized source to a
    provider with a hard input-size cap (e.g. VideoReTalk's 300MB limit) so the
    remote call does not fail on an over-budget upload.

    Unlike the origin (which returned ``None`` on failure and let the caller raise a
    bare ``ValueError``), this raises a typed :class:`FfmpegCommandError` with
    ``ErrorCode.render_failed`` when no strategy can reach the budget, so the
    failure carries a spec error code instead of degrading silently.
    """
    source = Path(video_path)
    info = probe_media(source)
    duration = float(info.duration_sec or 0)
    if info.media_type != "video" or duration <= 0:
        raise FfmpegCommandError(
            "Compress source must be a video with a positive duration.",
            error_code=ErrorCode.render_failed,
        )
    if max_size_mb <= 0:
        raise FfmpegCommandError(
            "Compress size budget must be positive.",
            error_code=ErrorCode.render_failed,
        )
    output = Path(output_path) if output_path else source.with_name(f"{source.stem}_compressed.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)

    target_video_bits = (max_size_mb * 8 * 1024 * 1024 * COMPRESS_BUDGET_MARGIN) / duration
    target_video_kbps = max(1, int(target_video_bits / 1000))
    current_width = int(info.width or 1920)
    current_height = int(info.height or 1080)
    portrait = current_height > current_width
    strategies = (
        CompressionStrategy("reduce_bitrate", target_video_kbps, None),
        CompressionStrategy(
            "720p",
            min(target_video_kbps, COMPRESS_720P_MAX_KBPS),
            (720, 1280) if portrait else (1280, 720),
        ),
        CompressionStrategy(
            "480p",
            min(target_video_kbps, COMPRESS_480P_MAX_KBPS),
            (480, 854) if portrait else (854, 480),
        ),
    )
    runner = FfmpegRunner(timeout_sec=timeout_sec)
    max_size_bytes = int(max_size_mb * 1024 * 1024)
    for strategy in strategies:
        args = [
            ffmpeg_bin(), *FFMPEG_QUIET_ARGS, "-i", str(source),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-b:v", f"{strategy.video_kbps}k",
            "-maxrate", f"{strategy.video_kbps}k",
            "-bufsize", f"{strategy.video_kbps * 2}k",
        ]
        if strategy.resolution is not None:
            args.extend(["-s", f"{strategy.resolution[0]}x{strategy.resolution[1]}"])
        args.extend(["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)])
        try:
            runner.run(args, timeout_sec=timeout_sec)
        except FfmpegCommandError:
            output.unlink(missing_ok=True)
            continue
        size_bytes = output.stat().st_size
        if size_bytes <= max_size_bytes:
            return CompressionResult(
                path=output,
                strategy=strategy.name,
                size_bytes=size_bytes,
                media_info=probe_media(output),
            )
        output.unlink(missing_ok=True)
    raise FfmpegCommandError(
        f"Could not compress video below {max_size_mb}MB after exhausting the bitrate/resolution ladder.",
        error_code=ErrorCode.render_failed,
    )


def extract_audio_segment(source: str | Path, start: float, end: float, output: str | Path) -> Path:
    """Cut [start, end) of ``source`` to an mp3 at ``output`` (audio only)."""
    out = Path(output)
    duration = max(0.0, float(end) - float(start))
    runner = FfmpegRunner()
    runner.run(
        [
            ffmpeg_bin(),
            *FFMPEG_QUIET_ARGS,
            "-ss",
            f"{float(start):.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-y",
            str(out),
        ]
    )
    return out


def trim_to_valid_segments(
    video_path: str | Path,
    segments: Sequence[object],
    output_path: str | Path | None = None,
    *,
    timeout_sec: int = VIDEO_PROCESS_TIMEOUT_SEC,
) -> Path:
    source = Path(video_path)
    info = probe_media(source)
    duration = float(info.duration_sec or 0)
    if info.media_type != "video" or duration <= 0:
        raise FfmpegCommandError("Trim source must be a video with duration.")
    windows = _normalize_segment_windows(segments, duration)
    output = Path(output_path) if output_path else source.with_name(f"{source.stem}_trimmed.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    runner = FfmpegRunner(timeout_sec=timeout_sec)
    with tempfile.TemporaryDirectory(prefix="cutagent_trim_") as temp_dir:
        segment_paths: list[Path] = []
        for index, (start, end) in enumerate(windows):
            target = Path(temp_dir) / f"segment_{index:03d}.mp4"
            runner.run(
                [
                    ffmpeg_bin(), *FFMPEG_QUIET_ARGS, "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(source),
                    "-map", "0:v:0", "-map", "0:a:0?", *VIDEO_ENCODE_ARGS, str(target),
                ],
                timeout_sec=timeout_sec,
            )
            segment_paths.append(target)
        if len(segment_paths) == 1:
            shutil.copyfile(segment_paths[0], output)
        else:
            concat_file = Path(temp_dir) / "concat.txt"
            concat_file.write_text("".join(_concat_file_line(path) for path in segment_paths), encoding="utf-8")
            runner.run(
                [
                    ffmpeg_bin(), *FFMPEG_QUIET_ARGS, "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(output),
                ],
                timeout_sec=timeout_sec,
            )
    probe_media(output)
    return output


def probe_video_frame_count(path: str | Path) -> int:
    media_path = Path(path)
    result = FfmpegRunner().run(
        [
            ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames,nb_frames", "-of", "json", str(media_path),
        ]
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FfmpegCommandError("ffprobe returned invalid JSON.", command=[ffprobe_bin(), str(media_path)]) from exc
    streams = payload.get("streams") or []
    if not streams:
        raise FfmpegCommandError(f"No video stream found in {media_path}.")
    stream = streams[0]
    frame_count = _int_or_none(stream.get("nb_read_frames")) or _int_or_none(stream.get("nb_frames"))
    if frame_count is None:
        raise FfmpegCommandError(f"Could not count frames in {media_path}.")
    return frame_count


def probe_stream_types(path: str | Path) -> set[str]:
    media_path = Path(path)
    result = FfmpegRunner().run(
        [
            ffprobe_bin(), "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(media_path),
        ]
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FfmpegCommandError("ffprobe returned invalid JSON.", command=[ffprobe_bin(), str(media_path)]) from exc
    return {str(stream.get("codec_type")) for stream in payload.get("streams") or [] if stream.get("codec_type")}


@dataclass(frozen=True)
class NormalizationResult:
    output_path: Path
    target_width: int
    target_height: int
    is_hdr: bool
    media_info: MediaInfo


def normalize_for_upload(
    video_path: str | Path,
    output_path: str | Path | None = None,
    *,
    timeout_sec: int = VIDEO_PROCESS_TIMEOUT_SEC,
) -> NormalizationResult:
    """Normalize an uploaded video to the strict delivery profile.

    Applies, in one transcode pass:
    - rotation correction from the display matrix / ``rotate`` tag,
    - optional letterbox/pillarbox crop (cropdetect on an embedded portrait),
    - HDR -> SDR (BT.709) tonemap when the source is HDR,
    - scale + pad to 1080p preserving orientation,
    - h264 / yuv420p / BT.709 output,
    and then runs a post-encode validate gate that *raises* (not warns) on any
    profile violation, so a malformed / mis-rotated source can never enter the
    pipeline silently mis-rendered.
    """
    source = Path(video_path)
    info = probe_media(source)
    if info.media_type != "video":
        raise FfmpegCommandError(
            f"Upload normalization source must be video: {source}",
            error_code=ErrorCode.upload_unsupported_type,
        )
    raw_streams = _probe_video_stream_raw(source)
    rotation = _stream_rotation(raw_streams)
    width, height = _display_dimensions(raw_streams, rotation)
    if width <= 0 or height <= 0:
        raise FfmpegCommandError(
            f"Upload normalization source has invalid dimensions: {width}x{height}",
            error_code=ErrorCode.upload_unsupported_type,
        )
    crop = _detect_embedded_portrait_crop(source, width, height, info.duration_sec)
    effective_w = int(crop["width"]) if crop else width
    effective_h = int(crop["height"]) if crop else height
    target_w, target_h = _target_resolution(effective_w, effective_h)
    is_hdr = bool(info.is_hdr)

    output = Path(output_path) if output_path else source.with_name(f"{source.stem}_normalized.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    vf = _build_normalize_vf(target_w, target_h, is_hdr=is_hdr, crop=crop)
    runner = FfmpegRunner(timeout_sec=timeout_sec)
    runner.run(
        [
            # ffmpeg auto-applies the display-matrix rotation before the vf chain
            # (default autorotate), so the output pixels are upright and the
            # target resolution computed from the post-rotation display
            # dimensions is correct. No -noautorotate / rotate-metadata fiddling.
            ffmpeg_bin(), *FFMPEG_QUIET_ARGS, "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", vf,
            "-c:v", "libx264", "-preset", UPLOAD_NORMALIZE_PRESET, "-crf", UPLOAD_NORMALIZE_CRF,
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-color_range", "tv", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709",
            "-c:a", "aac", "-b:a", UPLOAD_NORMALIZE_AUDIO_BITRATE, "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart+write_colr",
            str(output),
        ],
        timeout_sec=timeout_sec,
    )
    media_info = _validate_normalized_video(output, target_w, target_h)
    return NormalizationResult(
        output_path=output,
        target_width=target_w,
        target_height=target_h,
        is_hdr=is_hdr,
        media_info=media_info,
    )


def _build_normalize_vf(
    target_w: int,
    target_h: int,
    *,
    is_hdr: bool,
    crop: dict | None,
) -> str:
    parts: list[str] = []
    if crop:
        parts.append(
            f"crop={int(crop['width'])}:{int(crop['height'])}:{int(crop['x'])}:{int(crop['y'])}"
        )
    if is_hdr:
        parts.append(HDR_TONEMAP_VF)
    parts.append(
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1:1"
    )
    parts.append("format=yuv420p")
    return ",".join(parts)


def _validate_normalized_video(output: Path, expected_w: int, expected_h: int) -> MediaInfo:
    if not output.exists() or output.stat().st_size < 1024:
        raise FfmpegCommandError(
            "转码输出文件为空或不存在。",
            error_code=ErrorCode.upload_normalization_failed,
        )
    raw = _probe_video_stream_raw(output)
    codec = str(raw.get("codec_name", "")).lower()
    pix_fmt = str(raw.get("pix_fmt", "")).lower()
    width = _int_or_none(raw.get("width")) or 0
    height = _int_or_none(raw.get("height")) or 0
    color_space = str(raw.get("color_space", "")).lower()
    color_transfer = str(raw.get("color_transfer", "")).lower()
    color_primaries = str(raw.get("color_primaries", "")).lower()
    problems: list[str] = []
    if codec != "h264":
        problems.append(f"编码={codec}")
    if pix_fmt != "yuv420p":
        problems.append(f"像素格式={pix_fmt}")
    if (width, height) != (expected_w, expected_h):
        problems.append(f"分辨率={width}x{height}")
    for label, value in (("color_space", color_space), ("color_transfer", color_transfer), ("color_primaries", color_primaries)):
        if value != "bt709":
            problems.append(f"{label}={value}")
    if problems:
        raise FfmpegCommandError(
            "转码后校验失败: " + ", ".join(problems),
            error_code=ErrorCode.upload_normalization_failed,
        )
    return probe_media(output)


def _probe_video_stream_raw(path: str | Path) -> dict:
    media_path = Path(path)
    result = FfmpegRunner().run(
        [
            ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
            (
                "stream=codec_name,pix_fmt,width,height,r_frame_rate,duration,"
                "color_transfer,color_primaries,color_space:stream_tags=rotate:stream_side_data"
            ),
            "-of", "json", str(media_path),
        ]
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FfmpegCommandError("ffprobe returned invalid JSON.", command=[ffprobe_bin(), str(media_path)]) from exc
    streams = payload.get("streams") or []
    if not streams:
        raise FfmpegCommandError(f"No video stream found in {media_path}.")
    return streams[0]


def _normalized_rotation(value: object) -> int:
    try:
        rotation = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    normalized = rotation % 360
    if normalized > 180:
        normalized -= 360
    snapped = min((-180, -90, 0, 90, 180), key=lambda candidate: abs(candidate - normalized))
    return snapped if abs(snapped - normalized) <= 2 else normalized


def _stream_rotation(video_stream: dict) -> int:
    for side_data in video_stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and side_data.get("rotation") is not None:
            return _normalized_rotation(side_data.get("rotation"))
    tags = video_stream.get("tags") or {}
    if isinstance(tags, dict) and tags.get("rotate") is not None:
        return _normalized_rotation(tags.get("rotate"))
    return 0


def _display_dimensions(video_stream: dict, rotation: int) -> tuple[int, int]:
    width = _int_or_none(video_stream.get("width")) or 0
    height = _int_or_none(video_stream.get("height")) or 0
    if width <= 0 or height <= 0:
        return width, height
    if abs(rotation) % 180 == 90:
        return height, width
    return width, height


def _target_resolution(width: int, height: int) -> tuple[int, int]:
    # Standardize to 1080p while preserving orientation.
    return (1080, 1920) if height >= width else (1920, 1080)


def _crop_sample_times(duration: float) -> list[float]:
    if duration <= 0:
        return [0.5]
    start = max(0.15, min(duration * 0.1, duration - 0.1))
    end = max(start, duration * 0.8)
    if duration <= 2.0:
        points = [duration * 0.25, duration * 0.5, duration * 0.75]
    else:
        step = (end - start) / max(1, UPLOAD_CROP_SAMPLE_COUNT - 1)
        points = [start + step * idx for idx in range(UPLOAD_CROP_SAMPLE_COUNT)]
    sampled: list[float] = []
    for point in points:
        clamped = round(max(0.0, min(point, max(0.0, duration - 0.05))), 3)
        if not sampled or abs(sampled[-1] - clamped) >= 0.15:
            sampled.append(clamped)
    return sampled or [0.5]


def _run_cropdetect(source: Path, sample_time: float) -> dict | None:
    try:
        result = subprocess.run(
            [
                ffmpeg_bin(), "-hide_banner", "-nostdin", "-nostats",
                "-ss", f"{sample_time:.3f}", "-i", str(source),
                "-t", "0.6", "-vf", "cropdetect=24:16:0", "-f", "null", "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", result.stderr or "")
    if not matches:
        return None
    width, height, x, y = matches[-1]
    return {"width": int(width), "height": int(height), "x": int(x), "y": int(y)}


def _detect_embedded_portrait_crop(
    source: Path,
    width: int,
    height: int,
    duration: float | None,
) -> dict | None:
    """Detect a centered portrait track inside a letterboxed landscape source.

    Returns an even-aligned crop rect when the content is a stable embedded
    portrait (black sidebars), else None. Native portrait / true landscape both
    return None (no crop)."""
    if height >= width:
        return None
    sample_times = _crop_sample_times(float(duration or 0))
    crops = [crop for crop in (_run_cropdetect(source, t) for t in sample_times) if crop]
    if len(crops) < max(2, min(len(sample_times), 2)):
        return None
    widths = [c["width"] for c in crops]
    heights = [c["height"] for c in crops]
    xs = [c["x"] for c in crops]
    ys = [c["y"] for c in crops]
    if (
        max(widths) - min(widths) > UPLOAD_EMBEDDED_PORTRAIT_MAX_VARIANCE_PX
        or max(heights) - min(heights) > UPLOAD_EMBEDDED_PORTRAIT_MAX_VARIANCE_PX
        or max(xs) - min(xs) > UPLOAD_EMBEDDED_PORTRAIT_MAX_VARIANCE_PX
    ):
        return None
    crop = {
        "width": max(2, int(round(statistics.median(widths) / 2.0) * 2)),
        "height": max(2, int(round(statistics.median(heights) / 2.0) * 2)),
        "x": max(0, int(round(statistics.median(xs) / 2.0) * 2)),
        "y": max(0, int(round(statistics.median(ys) / 2.0) * 2)),
    }
    crop_width_ratio = crop["width"] / max(width, 1)
    crop_height_ratio = crop["height"] / max(height, 1)
    crop_aspect_ratio = crop["width"] / max(crop["height"], 1)
    margin_left = crop["x"] / max(width, 1)
    margin_right = max(0.0, width - crop["x"] - crop["width"]) / max(width, 1)
    is_embedded_portrait = (
        crop_width_ratio <= UPLOAD_EMBEDDED_PORTRAIT_MAX_WIDTH_RATIO
        and crop_height_ratio >= UPLOAD_EMBEDDED_PORTRAIT_MIN_HEIGHT_RATIO
        and crop_aspect_ratio <= UPLOAD_EMBEDDED_PORTRAIT_MAX_ASPECT_RATIO
        and margin_left >= UPLOAD_EMBEDDED_PORTRAIT_MIN_MARGIN_RATIO
        and margin_right >= UPLOAD_EMBEDDED_PORTRAIT_MIN_MARGIN_RATIO
    )
    return crop if is_embedded_portrait else None


def _color_value(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text or text in {"unknown", "n/a", "reserved"}:
        return None
    return text


def _is_hdr_color(transfer: str | None, primaries: str | None) -> bool:
    return (transfer in HDR_TRANSFERS) or (primaries in HDR_PRIMARIES)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _primary_stream(streams: list[dict]) -> dict:
    for stream in streams:
        if stream.get("codec_type") == "video":
            return stream
    for stream in streams:
        if stream.get("codec_type") == "audio":
            return stream
    return streams[0]


def _fps(stream: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        value = stream.get(key)
        if value and value != "0/0":
            parsed = float(Fraction(str(value)))
            if parsed > 0:
                return parsed
    return None


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ffmpeg_filter_arg(value: str | Path) -> str:
    escaped = str(value).replace("\\", "\\\\").replace("'", r"\'")
    return f"'{escaped}'"


def _normalize_segment_windows(segments: Sequence[object], duration: float) -> list[tuple[float, float]]:
    windows = sorted((_segment_bounds(segment) for segment in segments), key=lambda item: item[0])
    if not windows:
        raise FfmpegCommandError("Trim requires at least one valid segment.", error_code=ErrorCode.render_invalid_timeline)
    for start, end in windows:
        if start < 0 or end <= start or end > duration + 0.03:
            raise FfmpegCommandError("Trim segment is out of bounds.", error_code=ErrorCode.render_invalid_timeline)
    return [(max(0.0, start), min(duration, end)) for start, end in windows]


def _segment_bounds(segment: object) -> tuple[float, float]:
    if isinstance(segment, dict):
        start = segment.get("start_sec", segment.get("start", 0))
        end = segment.get("end_sec", segment.get("end", start))
    else:
        start = getattr(segment, "start_sec", getattr(segment, "start", 0))
        end = getattr(segment, "end_sec", getattr(segment, "end", start))
    return float(start), float(end)


def _concat_file_line(path: Path) -> str:
    return f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"


def _is_image(path: Path, fmt: str, duration: float | None) -> bool:
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return True
    image_formats = {"image2", "png_pipe", "jpeg_pipe", "webp_pipe"}
    return fmt in image_formats and duration in {None, 0}
