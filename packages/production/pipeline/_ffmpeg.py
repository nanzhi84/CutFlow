"""BGM/final-media ffmpeg glue for the digital-human pipeline."""

from __future__ import annotations

import json
import logging
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packages.media.rendering import _escape_subtitle_filter_value, render_slot
from packages.media.video.ffmpeg import FfmpegRunner, ffmpeg_bin

logger = logging.getLogger("packages.production.pipeline._ffmpeg")

# Adaptive-mix tuning (ported from the OLD bgm_service auto_mix contract). These
# keep BGM perceptually under the voice via LUFS targeting + sidechain ducking.
AUTO_MIX_BGM_MARGIN_DB = 12.0  # keep BGM this many LUFS below the voice
AUTO_MIX_MIN_BGM_VOLUME = 0.02
AUTO_MIX_MAX_BGM_VOLUME = 0.6
AUTO_MIX_DUCKING_THRESHOLD = 0.05
AUTO_MIX_DUCKING_RATIO = 8.0
# The slider's historical neutral point: a requested volume of 0.3 means "trust the
# LUFS target as-is"; higher/lower shifts it as a taste preference.
AUTO_MIX_NEUTRAL_VOLUME = 0.3


def _extract_loudnorm_json(output: str) -> dict | None:
    """Pull the trailing JSON object printed by ffmpeg's ``loudnorm`` analysis."""
    text = output or ""
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def measure_loudness_lufs(media_path: Path) -> float | None:
    """Measure integrated loudness (LUFS) of an audio/video file via ffmpeg.

    Returns ``None`` (caller falls back to the requested fixed volume) on any
    probe failure -- a missing file, no audio stream, an unparseable measurement,
    or a degenerate ``-inf`` reading. This is a diagnostic measurement, never a
    hard failure: BGM mixing must still proceed with the user's requested volume.
    """
    path = Path(media_path)
    if not path.exists():
        return None
    args = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-vn",
        "-af",
        "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("[bgm] loudness probe failed for %s: %s", path, exc)
        return None
    data = _extract_loudnorm_json(f"{result.stdout or ''}\n{result.stderr or ''}")
    if not data:
        return None
    try:
        loudness = float(data.get("input_i"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(loudness) or loudness <= -99:
        return None
    return loudness


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


@dataclass
class AdaptiveMixResult:
    """Effective BGM gain plus the metadata that explains how it was derived."""

    bgm_volume: float
    metadata: dict[str, Any] = field(default_factory=dict)


def resolve_adaptive_bgm_volume(
    *,
    voice_path: Path,
    bgm_path: Path,
    requested_bgm_volume: float,
    auto_mix: bool,
    bgm_margin_db: float | None = None,
) -> AdaptiveMixResult:
    """Resolve the effective BGM gain.

    When ``auto_mix`` is off (or the loudness probes fail) the requested volume is
    used verbatim. When on, the BGM is targeted to ``voice_lufs - margin`` so it
    sits perceptually under the voice, then scaled by the slider as a taste offset
    and clamped to a sane range. The ``metadata`` mirrors the OLD ``last_mix_metadata``
    so the decision is observable.
    """
    requested = _clamp(float(requested_bgm_volume or 0.0), 0.0, 1.0)
    metadata: dict[str, Any] = {
        "auto_mix": bool(auto_mix),
        "requested_bgm_volume": round(requested, 4),
    }
    if requested <= 0:
        metadata["effective_bgm_volume"] = 0.0
        return AdaptiveMixResult(bgm_volume=0.0, metadata=metadata)
    if not auto_mix:
        metadata["effective_bgm_volume"] = round(requested, 4)
        return AdaptiveMixResult(bgm_volume=requested, metadata=metadata)

    voice_lufs = measure_loudness_lufs(voice_path)
    bgm_lufs = measure_loudness_lufs(bgm_path)
    metadata.update({"voice_lufs": voice_lufs, "bgm_lufs": bgm_lufs})
    if voice_lufs is None or bgm_lufs is None:
        metadata.update(
            {"effective_bgm_volume": round(requested, 4), "fallback_reason": "loudness_probe_failed"}
        )
        return AdaptiveMixResult(bgm_volume=requested, metadata=metadata)

    margin = float(bgm_margin_db if bgm_margin_db is not None else AUTO_MIX_BGM_MARGIN_DB)
    target_bgm_lufs = voice_lufs - margin
    gain = math.pow(10.0, (target_bgm_lufs - bgm_lufs) / 20.0)
    user_preference = requested / AUTO_MIX_NEUTRAL_VOLUME
    effective = gain * user_preference
    min_gain = min(AUTO_MIX_MIN_BGM_VOLUME, requested)
    max_gain = max(min_gain, AUTO_MIX_MAX_BGM_VOLUME)
    effective = _clamp(effective, min_gain, max_gain)
    metadata.update(
        {
            "target_bgm_lufs": round(target_bgm_lufs, 3),
            "bgm_margin_db": round(margin, 3),
            "effective_bgm_volume": round(effective, 4),
        }
    )
    logger.info(
        "[bgm] auto_mix: voice=%.1f LUFS bgm=%.1f LUFS target=%.1f LUFS volume=%.3f",
        voice_lufs,
        bgm_lufs,
        target_bgm_lufs,
        effective,
    )
    return AdaptiveMixResult(bgm_volume=effective, metadata=metadata)


def _build_bgm_audio_filters(
    *,
    bgm_volume: float,
    duration: float,
    auto_mix: bool,
    fade_in: float,
    fade_out: float,
) -> str:
    """Build the voice+BGM filter graph yielding ``[a]``.

    - the voice is split when ducking so it can drive the sidechain compressor;
    - ``auto_mix`` adds ``sidechaincompress`` so the BGM ducks under the voice in
      real time (not just a fixed attenuation);
    - ``afade`` applies the documented fade-in / fade-out on the BGM.
    """
    parts: list[str] = []
    voice_targets = "[voice][voicesc]" if auto_mix else "[voice]"
    parts.append(
        f"[1:a]aresample=48000,volume=1.0,apad=pad_dur=1,atrim=0:{duration:.3f},"
        f"asetpts=PTS-STARTPTS{',asplit=2' if auto_mix else ''}{voice_targets}"
    )

    bgm_chain = [f"[2:a]aresample=48000,volume={bgm_volume:.3f}"]
    if fade_in > 0:
        bgm_chain.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        fade_start = max(0.0, duration - fade_out)
        bgm_chain.append(f"afade=t=out:st={fade_start:.3f}:d={fade_out:.3f}")
    bgm_chain.extend([f"atrim=0:{duration:.3f}", "asetpts=PTS-STARTPTS"])
    parts.append(",".join(bgm_chain) + "[bgmraw]")

    if auto_mix:
        threshold = max(0.001, AUTO_MIX_DUCKING_THRESHOLD)
        ratio = max(1.0, AUTO_MIX_DUCKING_RATIO)
        parts.append(
            f"[bgmraw][voicesc]sidechaincompress=threshold={threshold}:ratio={ratio}:"
            "attack=30:release=650:makeup=1[bgm]"
        )
    else:
        parts.append("[bgmraw]anull[bgm]")

    parts.append("[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]")
    return ";".join(parts)


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
    fonts_dir: Path | None = None,
    auto_mix: bool = False,
    bgm_margin_db: float | None = None,
    fade_in: float = 1.0,
    fade_out: float = 1.5,
) -> AdaptiveMixResult | None:
    """Mux voice (+ optional BGM) and burn subtitles into the final video.

    When ``bgm_path`` is given and ``auto_mix`` is true, the BGM volume is resolved
    against the voice loudness (LUFS targeting) and the graph ducks the BGM under
    the voice via ``sidechaincompress`` with fade in/out -- the OLD adaptive-mix
    contract. Returns the resolved :class:`AdaptiveMixResult` when BGM was mixed
    (so the caller can record the decision), else ``None``.

    ``fonts_dir`` is handed to libass via the ``subtitles`` filter's ``fontsdir``
    option so an uploaded/selected subtitle font is actually available at burn
    time.
    """
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
    mix_result: AdaptiveMixResult | None = None
    if bgm_path is not None:
        mix_result = resolve_adaptive_bgm_volume(
            voice_path=Path(audio_path),
            bgm_path=Path(bgm_path),
            requested_bgm_volume=bgm_volume,
            auto_mix=auto_mix,
            bgm_margin_db=bgm_margin_db,
        )
        args.extend(["-stream_loop", "-1", "-i", str(bgm_path)])

    video_filters = "[0:v]"
    if subtitle_path is not None:
        escaped_subtitle = _escape_subtitle_filter_value(str(subtitle_path))
        subtitles_filter = f"subtitles='{escaped_subtitle}'"
        if fonts_dir is not None:
            escaped_fonts_dir = _escape_subtitle_filter_value(str(fonts_dir))
            subtitles_filter += f":fontsdir='{escaped_fonts_dir}'"
        video_filters += f"{subtitles_filter},"
    video_filters += f"fps={fps},format=yuv420p[v]"

    if bgm_path is None or mix_result is None:
        audio_filters = (
            f"[1:a]aresample=48000,apad=pad_dur=1,atrim=0:{duration:.3f},"
            "asetpts=PTS-STARTPTS[a]"
        )
    else:
        audio_filters = _build_bgm_audio_filters(
            bgm_volume=mix_result.bgm_volume,
            duration=duration,
            auto_mix=auto_mix,
            fade_in=max(0.0, float(fade_in)),
            fade_out=max(0.0, float(fade_out)),
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
    with render_slot("render.io.heavy"):
        FfmpegRunner(timeout_sec=60).run(args)
    return mix_result
