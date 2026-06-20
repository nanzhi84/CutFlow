from types import SimpleNamespace

import pytest

from packages.media.annotation import bgm


def test_snap_to_beats_picks_nearest():
    assert bgm.snap_to_beats(10.4, [0.0, 5.0, 10.0, 15.0]) == 10.0
    assert bgm.snap_to_beats(12.6, [0.0, 5.0, 10.0, 15.0]) == 15.0
    assert bgm.snap_to_beats(7.0, []) == 7.0


def test_detect_drops_finds_energy_jump():
    times = [float(i) for i in range(10)]
    energy = [0.1] * 5 + [0.9] * 5
    drops = bgm.detect_drops(energy, times)
    assert any(abs(d - 5.0) < 1.0 for d in drops)


def test_detect_drops_flat_signal_none():
    times = [float(i) for i in range(10)]
    energy = [0.5] * 10
    assert bgm.detect_drops(energy, times) == []


def test_segment_audio_track_covers_full_track_with_contiguous_segments():
    duration = 230.0
    times = [float(i) for i in range(231)]
    energy = [0.2] * 60 + [0.55] * 60 + [0.9] * 70 + [0.35] * 41
    beats = [round(i * 0.5, 3) for i in range(1, 460)]
    drops = [62.0, 128.0]

    segments = bgm.segment_audio_track(duration, energy, times, beats, drops)

    assert segments[0]["start"] == 0.0
    assert abs(segments[-1]["end"] - duration) < 1e-6
    assert len(segments) >= 4
    for prev, cur in zip(segments, segments[1:]):
        assert abs(prev["end"] - cur["start"]) <= 1e-6
    assert all(s["duration"] >= 24.0 for s in segments[:-1])
    assert any(s["duration"] >= 55.0 for s in segments)
    assert any(s["role_hint"] == "climax" for s in segments)


def test_segment_audio_track_keeps_repeated_loop_as_single_full_track_segment():
    duration = 180.0
    times = [float(i) for i in range(181)]
    repeating_phrase = [0.32, 0.34, 0.33, 0.35, 0.32, 0.34, 0.33, 0.35]
    energy = [repeating_phrase[i % len(repeating_phrase)] for i in range(181)]
    beats = [round(i * 0.5, 3) for i in range(1, 360)]

    segments = bgm.segment_audio_track(duration, energy, times, beats, [])

    assert len(segments) == 1
    assert segments[0] == {
        "start": 0.0,
        "end": 180.0,
        "duration": 180.0,
        "energy": pytest.approx(0.335, abs=0.001),
        "drop_anchor": None,
        "role_hint": "hook",
        "section_type": "loop",
        "section_label": "A",
        "repeat_group": "A",
        "loopable": True,
        "energy_profile": "stable",
    }


def test_segment_audio_track_splits_on_structural_changes_not_fixed_windows():
    duration = 180.0
    times = [float(i) for i in range(181)]
    energy = [0.22] * 43 + [0.68] * 51 + [0.38] * 45 + [0.88] * 42
    beats = [round(i * 0.5, 3) for i in range(1, 360)]
    drops = [43.0, 139.0]

    segments = bgm.segment_audio_track(duration, energy, times, beats, drops)

    assert [segment["start"] for segment in segments] == [0.0, 43.0, 94.0, 139.0]
    assert [segment["end"] for segment in segments] == [43.0, 94.0, 139.0, 180.0]
    assert [segment["section_label"] for segment in segments] == ["A", "B", "C", "D"]
    assert [segment["section_type"] for segment in segments] == [
        "intro",
        "drop",
        "verse",
        "drop",
    ]
    assert segments[1]["energy_profile"] == "rising"
    assert segments[3]["role_hint"] == "climax"


def test_segment_audio_track_short_track_is_single_segment():
    segments = bgm.segment_audio_track(
        42.0,
        [0.5] * 43,
        [float(i) for i in range(43)],
        [float(i) for i in range(43)],
        [],
    )

    assert segments[0]["start"] == 0.0
    assert segments[0]["end"] == 42.0
    assert segments[0]["duration"] == 42.0
    assert segments[0]["role_hint"] == "hook"
    assert segments[0]["section_type"] == "loop"
    assert segments[0]["loopable"] is True


def test_segment_audio_track_falls_back_without_beats():
    duration = 130.0
    times = [float(i) for i in range(131)]
    energy = [0.3] * 65 + [0.7] * 66

    segments = bgm.segment_audio_track(
        duration,
        energy,
        times,
        [],
        [],
    )

    assert segments[0]["start"] == 0.0
    assert segments[-1]["end"] == 130.0
    assert [s["duration"] for s in segments] == [65.0, 65.0]
    assert [s["section_type"] for s in segments] == ["intro", "chorus"]


def test_librosa_features_keep_fallback_segments_when_tempo_missing(monkeypatch, tmp_path):
    path = tmp_path / "flat.wav"
    path.write_bytes(b"placeholder")

    fake_librosa = SimpleNamespace(
        load=lambda *_args, **_kwargs: ([0.1] * 750, 10),
        beat=SimpleNamespace(beat_track=lambda **_kwargs: (0.0, [])),
        feature=SimpleNamespace(rms=lambda **_kwargs: [[0.2] * 75]),
        frames_to_time=lambda frames, *, sr: [float(frame) for frame in frames],
    )
    monkeypatch.setitem(__import__("sys").modules, "librosa", fake_librosa)

    features = bgm._extract_librosa_features(path)

    assert features is not None
    assert "bpm" not in features
    assert "tempo_bucket" not in features
    assert features["segments"][0]["start"] == 0.0
    assert features["segments"][-1]["end"] == 75.0
    assert [segment["duration"] for segment in features["segments"]] == [75.0]
