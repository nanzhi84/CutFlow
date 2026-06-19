from types import SimpleNamespace

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


def test_segment_audio_track_short_track_is_single_segment():
    segments = bgm.segment_audio_track(
        42.0,
        [0.5] * 43,
        [float(i) for i in range(43)],
        [float(i) for i in range(43)],
        [],
    )

    assert segments == [
        {
            "start": 0.0,
            "end": 42.0,
            "duration": 42.0,
            "energy": 0.5,
            "drop_anchor": None,
            "role_hint": "hook",
        }
    ]


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
    assert [s["duration"] for s in segments] == [60.0, 70.0]


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
