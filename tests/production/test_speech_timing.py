"""Unit tests for provider-neutral speech timing normalization.

These are pure-function tests (no storage): they exercise
``normalize_timing_for_script`` directly and feed its provider-raw segments into
``build_narration_units_from_script_sentences`` to prove the end-to-end timing
the caption pipeline actually consumes.
"""

from __future__ import annotations

from packages.core.contracts import (
    SpeechSegmentTiming,
    SpeechTiming,
    SpeechTokenTiming,
)
from packages.planning.editing.narration import (
    SpokenSegment,
    build_narration_units_from_script_sentences,
)
from packages.planning.editing.text import clean_text_for_timing
from packages.production.pipeline._speech_timing import (
    normalize_speech_text,
    normalize_timing_for_script,
)


def _tokens(pairs: list[tuple[str, float, float]]) -> list[SpeechTokenTiming]:
    return [SpeechTokenTiming(text=text, start=start, end=end) for text, start, end in pairs]


def _assert_monotonic_within(tokens: list[SpeechTokenTiming], duration: float) -> None:
    cursor = 0.0
    for token in tokens:
        assert 0.0 <= token.start < token.end <= duration + 1e-9
        assert token.start >= cursor - 1e-9
        cursor = token.end


def test_regression_ten_asr_segments_map_proportionally_not_by_index():
    """Reproduces run_c6168766640e: paraformer re-segmented five real sentences
    into ten ~6s ASR blocks, and the old blind index-zip pinned script sentence 1
    to ASR block 1 (ending at 6.31s, 4 chars over 6 seconds). With provider-raw
    segments + proportional char mapping, sentence 1 ends early and every unit's
    character density stays sane."""

    boundaries = [0.2, 6.31, 7.92, 14.24, 14.91, 21.03, 23.19, 29.29, 31.0, 37.3, 37.58]
    # A single continuous spoken paragraph re-segmented at the ASR block edges,
    # so each block's character count tracks its duration (steady speech) while
    # the block boundaries cut across the script's ten sentences.
    spoken = (
        "大家好今天要给各位介绍一款特别实用的家用好物它的整体做工非常扎实"
        "选用的材料也十分足所以拿在手里就能感觉到分量而且外观颜色搭配得很耐看"
        "最关键的是现在下单价格特别实惠还能额外获得赠品名额有限先到先得"
        "错过了今天这个活动之后就很难再有这样的价格了赶紧点击下方链接下单吧"
    )
    span = boundaries[-1] - boundaries[0]
    total_chars = len(spoken)
    asr_segments: list[SpeechSegmentTiming] = []
    cut = 0
    for index in range(10):
        end_fraction = (boundaries[index + 1] - boundaries[0]) / span
        next_cut = round(total_chars * end_fraction)
        asr_segments.append(
            SpeechSegmentTiming(
                text=spoken[cut:next_cut],
                start=boundaries[index],
                end=boundaries[index + 1],
            )
        )
        cut = next_cut

    script = (
        "开场讲重点。"
        "这款产品的做工特别扎实。"
        "选用的材料也非常足实在。"
        "整体的颜色搭配很耐看。"
        "最关键的是它价格实惠。"
        "现在下单可以立减五十元。"
        "而且还额外赠送精美礼品。"
        "库存数量有限先到先得。"
        "错过了今天就没有这个价。"
        "赶紧点击下方链接下单吧。"
    )

    timing = SpeechTiming(segments=asr_segments, granularity="segment", text_basis="normalized")
    segments, _tokens_out, _diag = normalize_timing_for_script(
        timing, script=script, duration=37.58
    )
    # Segments keep the provider's own re-segmented text and timing; they are NOT
    # rewritten with script sentences (the deleted blind-binding behavior).
    assert [segment.text for segment in segments] == [seg.text for seg in asr_segments]

    units = build_narration_units_from_script_sentences(
        script=script,
        asr_segments=[
            SpokenSegment(start=segment.start, end=segment.end, text=segment.text)
            for segment in segments
        ],
        video_duration=37.58,
    )
    assert len(units) == 10
    assert units[0].end < 3.0
    for unit in units:
        clean_len = len(clean_text_for_timing(unit.text))
        if clean_len > 4:
            density = (unit.end - unit.start) / clean_len
            assert 0.05 <= density <= 0.8, (unit.text, density)


def test_anchor_alignment_survives_number_expansion_and_typos():
    """~25% of provider tokens differ (惠 misheard as 会, 99 spoken as 九十九);
    matched tokens anchor to real time, the rest interpolate, and the output
    still covers the full display script in monotonic time."""

    script = "限时优惠99元包邮"
    timing = SpeechTiming(
        segments=[SpeechSegmentTiming(text="限时优会九十九元包邮", start=0.0, end=2.7)],
        tokens=_tokens(
            [
                ("限", 0.0, 0.3),
                ("时", 0.3, 0.6),
                ("优", 0.6, 0.9),
                ("会", 0.9, 1.2),  # typo for 惠
                ("九", 1.2, 1.4),  # 99 spoken as 九十九
                ("十", 1.4, 1.6),
                ("九", 1.6, 1.8),
                ("元", 1.8, 2.1),
                ("包", 2.1, 2.4),
                ("邮", 2.4, 2.7),
            ]
        ),
        granularity="token",
        text_basis="normalized",
    )
    _segments, tokens, diagnostics = normalize_timing_for_script(
        timing, script=script, duration=2.7
    )

    matched = diagnostics["token_matched"]
    fallback = diagnostics["char_fallback"]
    assert matched + fallback == 8  # 限 时 优 惠 99 元 包 邮
    assert matched / (matched + fallback) > 0.6
    assert fallback >= 1  # 99 (and 惠) never matched -> interpolated
    assert "".join(token.text for token in tokens) == "限时优惠99元包邮"
    _assert_monotonic_within(tokens, 2.7)
    # Anchored tokens keep the provider's real time.
    by_text = {token.text: token for token in tokens}
    assert by_text["限"].start == 0.0
    assert by_text["元"].start == 1.8


def test_perfect_match_preserves_native_token_times():
    script = "限时五折抢购"
    native = _tokens(
        [
            ("限", 0.0, 0.3),
            ("时", 0.3, 0.55),
            ("五", 0.55, 0.8),
            ("折", 0.8, 1.1),
            ("抢", 1.1, 1.35),
            ("购", 1.35, 1.6),
        ]
    )
    timing = SpeechTiming(
        segments=[SpeechSegmentTiming(text=script, start=0.0, end=1.6)],
        tokens=native,
        granularity="token",
        text_basis="original",
    )
    _segments, tokens, diagnostics = normalize_timing_for_script(
        timing, script=script, duration=1.6
    )
    assert diagnostics["token_matched"] == 6
    assert diagnostics["char_fallback"] == 0
    assert [(token.text, token.start, token.end) for token in tokens] == [
        (native_token.text, native_token.start, native_token.end) for native_token in native
    ]


def test_merged_v3_tts_tokens_anchor_every_display_token():
    """Volcengine v3 emits word-level tokens that attach punctuation and merge
    several characters into one token (``今天``/``天气``/``不错，``). Each such token
    must anchor the whole run of display characters it spans, sharing its
    interval subdivided monotonically by character weight — no interpolation."""

    script = "今天天气不错"
    timing = SpeechTiming(
        segments=[SpeechSegmentTiming(text="今天天气不错，", start=0.0, end=1.5)],
        tokens=_tokens(
            [
                ("今天", 0.0, 0.4),
                ("天气", 0.4, 0.9),
                ("不错，", 0.9, 1.5),  # punctuation attached
            ]
        ),
        granularity="token",
        text_basis="original",
    )
    _segments, tokens, diagnostics = normalize_timing_for_script(
        timing, script=script, duration=1.5
    )
    assert diagnostics["token_matched"] == 6
    assert diagnostics["char_fallback"] == 0
    assert "".join(token.text for token in tokens) == "今天天气不错"
    _assert_monotonic_within(tokens, 1.5)
    # Merge-group boundaries land exactly on the provider token boundaries.
    assert tokens[0].start == 0.0
    assert tokens[1].end == 0.4 and tokens[2].start == 0.4
    assert tokens[3].end == 0.9 and tokens[4].start == 0.9
    assert tokens[-1].end == 1.5


def test_segments_stay_provider_raw_and_tokens_cover_display_script():
    """Segments keep provider text/time; tokens are rebuilt over the display
    script so number normalization never corrupts caption text."""

    timing = SpeechTiming(
        segments=[SpeechSegmentTiming(text="只要两千元", start=0.0, end=1.0)],
        tokens=_tokens([("只要", 0.0, 0.3), ("两千元", 0.3, 1.0)]),
        granularity="token",
        text_basis="normalized",
    )
    segments, tokens, diagnostics = normalize_timing_for_script(
        timing, script="只要2000元", duration=1.0
    )
    assert segments[0].text == "只要两千元"  # raw, not the display script
    assert "".join(token.text for token in tokens) == "只要2000元"
    _assert_monotonic_within(tokens, 1.0)
    assert diagnostics["token_matched"] + diagnostics["char_fallback"] == len(tokens)


def test_tokens_without_segments_synthesize_one_span_and_empty_is_empty():
    token_only = SpeechTiming(
        tokens=_tokens([("你", 0.1, 0.45), ("好", 0.45, 0.8)]),
        granularity="token",
        text_basis="original",
    )
    segments, tokens, diagnostics = normalize_timing_for_script(
        token_only, script="你好", duration=1.0
    )
    assert [(segment.start, segment.end) for segment in segments] == [(0.1, 0.8)]
    assert "".join(token.text for token in tokens) == "你好"
    assert diagnostics["token_matched"] == 2

    assert normalize_timing_for_script(
        SpeechTiming(granularity="segment"), script="", duration=0
    )[:2] == ([], [])


def test_invalid_provider_rows_are_dropped_and_counted():
    timing = SpeechTiming(
        segments=[
            SpeechSegmentTiming(text="有效", start=0.0, end=1.0),
            SpeechSegmentTiming(text="  ", start=1.0, end=2.0),  # blank -> dropped
        ],
        granularity="segment",
        text_basis="original",
    )
    segments, _tokens_out, diagnostics = normalize_timing_for_script(
        timing, script="有效内容", duration=2.0
    )
    assert [segment.text for segment in segments] == ["有效"]
    assert diagnostics["invalid_dropped"] == 1


def test_normalize_speech_text_folds_width_and_case_keeps_alnum():
    assert normalize_speech_text("Ｈello，世界！99") == "hello世界99"
