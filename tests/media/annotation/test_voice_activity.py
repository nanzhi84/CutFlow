from __future__ import annotations

from packages.core.contracts import SpeechIslandV4
from packages.media.annotation import detect_speech_islands, merge_speech_probabilities
from tests.media.annotation.fixtures import make_silent_wav, make_tone_wav


def test_detect_speech_islands_on_silence_is_empty(tmp_path):
    wav = make_silent_wav(tmp_path, duration=2.0)
    assert detect_speech_islands(wav) == []


def test_detect_speech_islands_on_tone_returns_island_list(tmp_path):
    # A pure tone is not guaranteed to be speech; assert the contract type, no crash.
    wav = make_tone_wav(tmp_path, duration=2.0, frequency=220)
    islands = detect_speech_islands(wav)
    assert isinstance(islands, list)
    assert all(isinstance(i, SpeechIslandV4) for i in islands)


def test_detect_speech_islands_missing_file_returns_empty():
    assert detect_speech_islands("/nonexistent.wav") == []


def test_merge_probabilities_groups_contiguous_speech_chunks():
    # chunk_sec=1.0 for easy math. Speech at chunks 2,3,4 (>=0.5); rest silence.
    probs = [0.1, 0.2, 0.9, 0.8, 0.7, 0.1, 0.0]
    islands = merge_speech_probabilities(
        probs, chunk_sec=1.0, threshold=0.5, min_speech_ms=500, speech_pad_ms=0
    )
    assert len(islands) == 1
    isl = islands[0]
    assert isl.start == 2.0
    assert isl.end == 5.0
    # confidence = mean of (0.9, 0.8, 0.7) = 0.8
    assert abs(isl.confidence - 0.8) < 1e-6


def test_merge_probabilities_padding_merges_adjacent_islands():
    # Two speech runs (chunks 1; chunks 4) padded by 1s each -> overlap -> merge.
    probs = [0.1, 0.9, 0.1, 0.1, 0.9, 0.1]
    islands = merge_speech_probabilities(
        probs, chunk_sec=1.0, threshold=0.5, min_speech_ms=0, speech_pad_ms=1000
    )
    assert len(islands) == 1
    assert islands[0].start == 0.0  # clamped at 0
    assert islands[0].end >= 5.0


def test_merge_probabilities_filters_short_fragments():
    # Single 1s chunk but min_speech_ms requires 2s -> dropped.
    probs = [0.1, 0.9, 0.1]
    assert (
        merge_speech_probabilities(
            probs, chunk_sec=1.0, threshold=0.5, min_speech_ms=2000, speech_pad_ms=0
        )
        == []
    )
