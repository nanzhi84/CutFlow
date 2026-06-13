"""Pure forced-alignment subtitle helpers (sentence split + segment reshape)."""

from __future__ import annotations

from packages.media.audio.forced_alignment import (
    split_text_into_lines,
    subtitle_segments_to_asr_shape,
)


def test_consecutive_sentence_punctuation_stays_with_its_sentence():
    # Runs like …… and ?! must NOT be split into orphan single-char lines:
    # MiniMax emits one subtitle segment per newline, so an orphan "…" would
    # become its own NarrationUnit on the strict tts_subtitle path.
    assert split_text_into_lines("句子……结束。").split("\n") == ["句子……", "结束。"]
    assert split_text_into_lines("真的吗?!好的。").split("\n") == ["真的吗?!", "好的。"]
    assert split_text_into_lines("等等……还有呢？！对。").split("\n") == [
        "等等……",
        "还有呢？！",
        "对。",
    ]


def test_basic_sentence_split_is_unchanged():
    assert split_text_into_lines("第一句。第二句！第三句？").split("\n") == [
        "第一句。",
        "第二句！",
        "第三句？",
    ]
    assert split_text_into_lines("没有标点的尾句") == "没有标点的尾句"
    assert split_text_into_lines("") == ""


def test_subtitle_segments_to_asr_shape_converts_ms_to_seconds():
    segments = [
        {"time_begin": 0, "time_end": 1500, "text": "你好"},
        {"time_begin": 1500, "time_end": 3000, "text": ""},  # dropped (empty)
        {"time_begin": 3000, "time_end": 4200, "text": "世界"},
    ]
    assert subtitle_segments_to_asr_shape(segments) == [
        {"start": 0.0, "end": 1.5, "text": "你好"},
        {"start": 3.0, "end": 4.2, "text": "世界"},
    ]
