"""Voice-activity detection (VAD) sensor.

Provides speech islands for frame sampling and for snapping window boundaries on
cut-less material. Pipeline: ffmpeg extracts 16 kHz mono pcm_s16le -> pysilero
emits a per-chunk (512-sample) speech probability -> the pure
``merge_speech_probabilities`` merges/filters/pads runs into islands. No speech
(e.g. b-roll) returns [] as a normal path, not a degradation.

Sensor discipline: deterministic and unit-testable. The "per-chunk probability
-> islands" logic is the pure ``merge_speech_probabilities``; the real model only
produces the probability sequence.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from packages.core.contracts import SpeechIslandV4
from packages.media.video.ffmpeg import FfmpegCommandError, FfmpegRunner, ffmpeg_bin

logger = logging.getLogger(__name__)

# Silero VAD v5 fixed chunk size: 512 samples (16-bit mono -> 1024 bytes).
_CHUNK_SAMPLES = 512
_CHUNK_BYTES = _CHUNK_SAMPLES * 2
# Default decision threshold: probability >= this counts as a speech chunk.
_DEFAULT_SPEECH_THRESHOLD = 0.5
_PCM_TIMEOUT_SEC = 120


def detect_speech_islands(
    media_path: str,
    *,
    min_speech_ms: int = 250,
    speech_pad_ms: int = 400,
    sample_rate: int = 16000,
    speech_threshold: float = _DEFAULT_SPEECH_THRESHOLD,
) -> list[SpeechIslandV4]:
    """Detect speech islands in a media file.

    No speech (pure silence / b-roll) returns [] as a normal path.
    """
    if not media_path or not os.path.exists(media_path):
        logger.warning("[vad] media not found, returning empty: %s", media_path)
        return []

    pcm = _extract_pcm_s16le_mono(media_path, sample_rate=sample_rate)
    if not pcm or len(pcm) < _CHUNK_BYTES:
        return []

    probabilities = _run_silero_probabilities(pcm)
    if not probabilities:
        return []

    chunk_sec = _CHUNK_SAMPLES / float(sample_rate)
    return merge_speech_probabilities(
        probabilities,
        chunk_sec=chunk_sec,
        threshold=speech_threshold,
        min_speech_ms=min_speech_ms,
        speech_pad_ms=speech_pad_ms,
    )


def merge_speech_probabilities(
    probabilities: Sequence[float],
    *,
    chunk_sec: float,
    threshold: float = _DEFAULT_SPEECH_THRESHOLD,
    min_speech_ms: int = 250,
    speech_pad_ms: int = 400,
) -> list[SpeechIslandV4]:
    """Merge a per-chunk speech-probability sequence into speech islands (pure).

    Steps: 1) group contiguous chunks with prob >= threshold into raw islands;
    2) drop raw islands shorter than ``min_speech_ms``; 3) pad both ends by
    ``speech_pad_ms`` (start not below 0); 4) merge overlapping/touching islands
    (confidence is duration-weighted); 5) island confidence = mean speech
    probability over the span. ``chunk_sec`` = chunk_samples / sample_rate.
    """
    if not probabilities or chunk_sec <= 0:
        return []

    min_speech_sec = max(0.0, min_speech_ms) / 1000.0
    pad_sec = max(0.0, speech_pad_ms) / 1000.0

    # 1. Group contiguous speech chunks into raw islands (chunk span + prob sum).
    raw: list[dict] = []
    run_start: int | None = None
    run_prob_sum = 0.0
    for idx, prob in enumerate(probabilities):
        is_speech = float(prob) >= threshold
        if is_speech:
            if run_start is None:
                run_start = idx
                run_prob_sum = 0.0
            run_prob_sum += float(prob)
        else:
            if run_start is not None:
                raw.append(_make_raw_island(run_start, idx, run_prob_sum, chunk_sec))
                run_start = None
    if run_start is not None:
        raw.append(
            _make_raw_island(run_start, len(probabilities), run_prob_sum, chunk_sec)
        )

    # 2. Drop fragments by unpadded raw duration.
    raw = [isl for isl in raw if (isl["end"] - isl["start"]) >= min_speech_sec]
    if not raw:
        return []

    # 3. Pad both ends (start clamped to 0).
    for isl in raw:
        isl["start"] = max(0.0, isl["start"] - pad_sec)
        isl["end"] = isl["end"] + pad_sec

    # 4. Merge overlapping/touching islands after padding (duration-weighted conf).
    merged: list[dict] = []
    for isl in raw:
        if merged and isl["start"] <= merged[-1]["end"]:
            prev = merged[-1]
            prev["end"] = max(prev["end"], isl["end"])
            prev["weight"] += isl["weight"]
            prev["conf_weighted"] += isl["conf_weighted"]
        else:
            merged.append(isl)

    # 5. Materialize into SpeechIslandV4.
    islands: list[SpeechIslandV4] = []
    for isl in merged:
        weight = isl["weight"]
        confidence = isl["conf_weighted"] / weight if weight > 0 else 0.0
        islands.append(
            SpeechIslandV4(
                start=round(isl["start"], 6),
                end=round(isl["end"], 6),
                confidence=round(min(1.0, max(0.0, confidence)), 6),
            )
        )
    return islands


def _make_raw_island(
    start_chunk: int,
    end_chunk: int,
    prob_sum: float,
    chunk_sec: float,
) -> dict:
    """Build a raw island record (chunk span is right-open [start_chunk, end_chunk)).

    weight = number of speech chunks (duration weight for merge); conf_weighted =
    mean prob * weight = prob_sum.
    """
    chunk_count = max(1, end_chunk - start_chunk)
    return {
        "start": start_chunk * chunk_sec,
        "end": end_chunk * chunk_sec,
        "weight": chunk_count,
        "conf_weighted": prob_sum,
    }


def _extract_pcm_s16le_mono(media_path: str, *, sample_rate: int) -> bytes:
    """ffmpeg-extract any media to raw mono 16-bit PCM (no wav header).

    Failure (no audio track / ffmpeg unavailable) returns b"" so the caller
    treats it as no speech.
    """
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-i",
        media_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    try:
        result = FfmpegRunner(timeout_sec=_PCM_TIMEOUT_SEC).run(cmd, text=False)
    except (FfmpegCommandError, OSError) as exc:  # pragma: no cover - local ffmpeg
        logger.warning("[vad] ffmpeg PCM extract failed: %s", exc)
        return b""
    return result.stdout or b""


def _run_silero_probabilities(pcm: bytes) -> list[float]:
    """Run Silero VAD per chunk (512 samples), returning each chunk's speech prob.

    Silero holds internal state; reset() before each run for determinism. A
    trailing partial chunk is zero-padded so sentence ends are not dropped.
    """
    try:
        from pysilero_vad import SileroVoiceActivityDetector
    except Exception as exc:  # pragma: no cover - dependency missing
        logger.warning("[vad] pysilero_vad unavailable: %s", exc)
        return []

    detector = SileroVoiceActivityDetector()
    detector.reset()

    probabilities: list[float] = []
    total = len(pcm)
    idx = 0
    while idx < total:
        chunk = pcm[idx : idx + _CHUNK_BYTES]
        if len(chunk) < _CHUNK_BYTES:
            chunk = chunk + b"\x00" * (_CHUNK_BYTES - len(chunk))
        try:
            prob = float(detector.process_chunk(chunk))
        except Exception as exc:  # pragma: no cover - single-chunk failure
            logger.debug("[vad] chunk inference failed, recording 0: %s", exc)
            prob = 0.0
        probabilities.append(prob)
        idx += _CHUNK_BYTES
    return probabilities
