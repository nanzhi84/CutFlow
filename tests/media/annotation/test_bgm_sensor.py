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


def test_candidate_windows_capped_and_snapped_and_bounded():
    duration = 60.0
    times = [float(i) for i in range(61)]
    energy = [0.2] * 20 + [0.9] * 10 + [0.3] * 31
    beats = [float(i) for i in range(0, 61, 2)]
    drops = bgm.detect_drops(energy, times)
    wins = bgm.candidate_windows(duration, energy, times, beats, drops, max_windows=3)
    assert 1 <= len(wins) <= 3
    for w in wins:
        assert 0 <= w["start"] < w["end"] <= duration
        assert w["start"] in beats and w["end"] in beats
        assert 0.0 <= w["energy"] <= 1.0


def test_candidate_windows_short_track_single_window():
    duration = 18.0
    times = [float(i) for i in range(19)]
    energy = [0.5] * 19
    wins = bgm.candidate_windows(
        duration,
        energy,
        times,
        [],
        [],
        max_windows=3,
        target_len=20.0,
    )
    assert len(wins) == 1
    assert wins[0]["start"] == 0.0
    assert abs(wins[0]["end"] - duration) < 1e-6
