"""Audio-pause DETECTION (ffmpeg silencedetect) -> pause windows.

Real ffmpeg, no network. A wav with real silence gaps yields pause windows landing
on the gaps; a steady tone (the sandbox TTS shape) yields no reliable pauses, so the
boundary planner falls back to semantic-only boundaries.
"""

from __future__ import annotations

from pathlib import Path

from packages.media.audio import detect_silence_windows
from packages.media.audio import silence as silence_mod
from packages.media.video.ffmpeg import FfmpegRunner, ffmpeg_bin
from tests.fixtures.media import generate_test_audio


def _tone_silence_tone_wav(directory: Path, *, sample_rate: int = 16000) -> Path:
    """A 6s clip: 2s tone, 2s silence, 2s tone (one real silence window ~[2, 4])."""
    path = directory / "tone_silence_tone.wav"
    if path.exists():
        return path
    # Concat-via-filter: two sine bursts separated by an explicit silent gap.
    filter_complex = (
        "sine=frequency=440:sample_rate={sr}:duration=2[a];"
        "anullsrc=r={sr}:cl=mono:d=2[g];"
        "sine=frequency=440:sample_rate={sr}:duration=2[b];"
        "[a][g][b]concat=n=3:v=0:a=1[out]"
    ).format(sr=sample_rate)
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
            f"sine=frequency=440:sample_rate={sample_rate}:duration=2",
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )
    return path


def test_silencedetect_finds_real_gap(tmp_path):
    wav = _tone_silence_tone_wav(tmp_path)

    windows = detect_silence_windows(wav)

    assert windows, "a real silence gap must be detected"
    # The detected gap must land inside the [2, 4] silence we synthesized.
    gap = max(windows, key=lambda w: w["duration"])
    assert 1.5 <= gap["start"] <= 2.5
    assert 3.5 <= gap["end"] <= 4.5
    assert gap["duration"] >= 1.0
    # Window payload shape is complete (start/end/duration/center seconds).
    assert set(gap) == {"start", "end", "duration", "center"}
    assert abs(gap["center"] - (gap["start"] + gap["duration"] / 2.0)) < 1e-3


def test_steady_tone_has_no_reliable_pauses(tmp_path):
    # The sandbox TTS shape: a steady 440Hz tone -> no silences -> semantic-only path.
    tone = generate_test_audio(tmp_path, duration_sec=6, frequency=440)

    windows = detect_silence_windows(tone)

    assert windows == []


def test_missing_file_returns_no_pauses_without_raising(tmp_path):
    assert detect_silence_windows(tmp_path / "does_not_exist.wav") == []
    assert detect_silence_windows("") == []


def test_detection_is_cached_per_path(tmp_path, monkeypatch):
    wav = _tone_silence_tone_wav(tmp_path)
    silence_mod._DETECTION_CACHE.clear()

    calls = {"count": 0}
    original_run = FfmpegRunner.run

    def counting_run(self, args, *, timeout_sec=None):
        calls["count"] += 1
        return original_run(self, args, timeout_sec=timeout_sec)

    monkeypatch.setattr(FfmpegRunner, "run", counting_run)
    first = detect_silence_windows(wav)
    second = detect_silence_windows(wav)

    assert first == second
    assert calls["count"] == 1, "second detection on the same path must hit the cache"


def test_detection_cache_invalidates_when_file_changes_at_same_path(tmp_path, monkeypatch):
    # A regenerated/overwritten file at the SAME path must re-detect, not serve a
    # stale cache keyed only by path (latent today: callers use uuid-unique paths,
    # but the cache must be robust to content-addressed/overwritten paths).
    silence_mod._DETECTION_CACHE.clear()
    path = tmp_path / "audio.wav"
    path.write_bytes(b"\x00" * 1000)

    outputs = [
        "silence_start: 1.0\nsilence_end: 2.0 | silence_duration: 1.0\n",  # first content: a pause
        "",  # second content: no pause
    ]
    calls = {"count": 0}

    class _Res:
        def __init__(self, stderr):
            self.stdout = ""
            self.stderr = stderr

    def fake_run(self, args, *, timeout_sec=None):
        out = outputs[min(calls["count"], len(outputs) - 1)]
        calls["count"] += 1
        return _Res(out)

    monkeypatch.setattr(FfmpegRunner, "run", fake_run)

    first = detect_silence_windows(path)
    assert first, "first content has a parsed pause window"

    path.write_bytes(b"\x00" * 2000)  # overwrite at the same path (size + mtime change)
    second = detect_silence_windows(path)

    assert calls["count"] == 2, "a changed file at the same path must re-run detection, not hit a stale cache"
    assert second == [], "result must reflect the new (pause-free) content, not the cached windows"


def test_parse_handles_silence_lines_without_explicit_start():
    # silence_end carries duration; a missing silence_start is back-computed.
    output = "[silencedetect @ 0x] silence_end: 3.5 | silence_duration: 1.2\n"
    windows = silence_mod._parse_silence_windows(output)

    assert len(windows) == 1
    assert windows[0]["end"] == 3.5
    assert windows[0]["duration"] == 1.2
    assert windows[0]["start"] == 2.3


def test_adjacent_windows_are_merged():
    windows = [
        {"start": 1.0, "end": 1.5, "duration": 0.5, "center": 1.25},
        {"start": 1.51, "end": 2.0, "duration": 0.49, "center": 1.755},  # gap < 0.02
        {"start": 3.0, "end": 3.4, "duration": 0.4, "center": 3.2},
    ]

    merged = silence_mod._merge_adjacent_windows(windows)

    assert len(merged) == 2
    assert merged[0]["start"] == 1.0
    assert merged[0]["end"] == 2.0
    assert merged[1]["start"] == 3.0
