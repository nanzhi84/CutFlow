"""Caption Display v2 compiler coverage (issue #188, W2a).

Pure-logic tests: the width measurer is a deterministic stub (full-width glyphs
40px, half-width 20px), so no font IO is involved. These tests are the
acceptance core for the display compiler -- line-break 禁则, tail rebalancing,
protected tokens, cue merge/split, the four-state toggle matrix, and normal /
emphasis layer coexistence.
"""

from __future__ import annotations

import copy
import unicodedata

from packages.production.pipeline._caption_display import (
    CaptionDisplayResult,
    compile_caption_display,
    compile_planned_caption_display,
)

RES = (1920, 1080)


def stub_measure(text: str) -> float:
    """Full/wide glyphs = 40px, everything else = 20px. Deterministic."""
    return sum(40.0 if unicodedata.east_asian_width(c) in {"F", "W"} else 20.0 for c in text)


def unit(text: str, start: float, end: float, uid: str = "") -> dict:
    return {"unit_id": uid or text, "text": text, "start": start, "end": end, "confidence": 1.0}


def event(start: float, end: float, event_id: str, text: str = "花字") -> dict:
    return {"start": start, "end": end, "event_id": event_id, "text": text}


def compile_normal(
    units: list[dict],
    *,
    resolution: tuple[int, int] = RES,
    margin_l: int = 80,
    margin_r: int = 80,
    overlay_events: list[dict] | None = None,
    normal_enabled: bool = True,
    emphasis_enabled: bool = False,
) -> CaptionDisplayResult:
    return compile_caption_display(
        units=units,
        resolution=resolution,
        margin_l=margin_l,
        margin_r=margin_r,
        measure=stub_measure,
        metrics_source="hmtx",
        normal_enabled=normal_enabled,
        emphasis_enabled=emphasis_enabled,
        overlay_events=overlay_events or [],
    )


# --- line-head 禁则 (forbidden leading characters) --------------------------------


def test_period_never_starts_a_line():
    # issue bad example: the 。 must not float onto line 2 alone.
    text = "全海南门店联保，修完在哪都能售后。"
    # width ~= 9 full-width chars => forces a two-line split.
    result = compile_normal(
        [unit(text, 0.0, 4.0)], resolution=(460, 1080), margin_l=40, margin_r=40
    )
    (cue,) = result.normal_cues
    assert len(cue.lines) == 2
    # No line may start with a forbidden closer; the sentence stays comma-broken.
    assert not any(line[0] in "，。！？；：、）" for line in cue.lines)
    assert cue.lines[0].endswith("，")
    assert cue.lines[1] == "修完在哪都能售后。"


def test_comma_pulled_back_not_line_initial():
    text = "今天天气不错，我们出去玩吧"
    result = compile_normal(
        [unit(text, 0.0, 4.0)], resolution=(460, 1080), margin_l=40, margin_r=40
    )
    (cue,) = result.normal_cues
    assert len(cue.lines) == 2
    assert not any(line[0] in _leading_forbidden() for line in cue.lines)


def _leading_forbidden() -> str:
    return "，。！？；：、）》】」』"


# --- line-tail 禁则 (opening bracket must not end a line) -------------------------


def test_open_paren_never_ends_a_line():
    text = "这是一个很棒的（限时）优惠活动啊"
    result = compile_normal(
        [unit(text, 0.0, 4.0)], resolution=(500, 1080), margin_l=40, margin_r=40
    )
    (cue,) = result.normal_cues
    assert len(cue.lines) == 2
    assert not any(line.endswith("（") for line in cue.lines)


# --- 1-2 char tail rebalancing (16+2 -> balanced) --------------------------------


def test_tail_rebalanced_to_even_split():
    # 18 full-width chars, per-line capacity 16 (avail ~665px). A 16+2 split is
    # legal but the imbalance weight must drive an even ~9/9 split instead.
    text = "零一二三四五六七八九十甲乙丙丁戊己庚"
    assert len(text) == 18
    result = compile_normal(
        [unit(text, 0.0, 4.0)], resolution=(760, 1080), margin_l=30, margin_r=30
    )
    (cue,) = result.normal_cues
    assert len(cue.lines) == 2
    assert abs(len(cue.lines[0]) - len(cue.lines[1])) <= 1
    assert cue.lines[0] == "零一二三四五六七八"
    assert cue.lines[1] == "九十甲乙丙丁戊己庚"


def test_single_line_when_it_fits():
    result = compile_normal([unit("短句子", 0.0, 2.0)])
    (cue,) = result.normal_cues
    assert cue.lines == ["短句子"]


def test_reference_mode_can_keep_one_progressive_three_line_cue():
    result = compile_caption_display(
        units=[unit("一二三四五六七八九", 0.0, 3.0)],
        resolution=(360, 640),
        margin_l=20,
        margin_r=20,
        measure=stub_measure,
        metrics_source="hmtx",
        normal_enabled=True,
        emphasis_enabled=False,
        overlay_events=[],
        max_lines=3,
        max_line_width_px=140,
    )

    assert len(result.normal_cues) == 1
    assert result.normal_cues[0].lines == ["一二三", "四五六", "七八九"]


# --- protected tokens ------------------------------------------------------------


def test_size_and_price_tokens_not_split():
    # Balanced middle break falls inside "20cm×10cm"; protection forces the break
    # to land between the two tokens so both survive intact.
    text = "促销尺寸20cm×10cm现价99元起"
    result = compile_normal(
        [unit(text, 0.0, 4.0)], resolution=(478, 1080), margin_l=40, margin_r=40
    )
    (cue,) = result.normal_cues
    assert len(cue.lines) == 2
    assert any("20cm×10cm" in line for line in cue.lines)
    assert any("99元起" in line for line in cue.lines)


def test_speed_and_latin_tokens_not_split():
    text = "型号ABC123倍速1.0x畅快播放体验极好"
    result = compile_normal(
        [unit(text, 0.0, 4.0)], resolution=(520, 1080), margin_l=40, margin_r=40
    )
    (cue,) = result.normal_cues
    assert len(cue.lines) == 2
    assert any("ABC123" in line for line in cue.lines)
    assert any("1.0x" in line for line in cue.lines)


# --- C1 merge --------------------------------------------------------------------


def test_pure_punctuation_cue_merges_into_previous():
    units = [unit("你好世界", 0.0, 1.0), unit("。", 1.0, 1.5)]
    result = compile_normal(units)
    (cue,) = result.normal_cues
    assert cue.end == 1.5
    assert cue.source_unit_ids == [0, 1]
    assert cue.lines == ["你好世界。"]
    assert result.diagnostics.merged_units == 1


def test_tiny_cue_merges_only_within_gap():
    # gap 0.2 (<0.3) -> merge; a separate run with gap 0.6 (>=0.5) -> no merge.
    merged = compile_normal([unit("这是第一句话", 0.0, 1.0), unit("好", 1.2, 1.6)])
    assert len(merged.normal_cues) == 1
    assert merged.diagnostics.merged_units == 1

    kept = compile_normal([unit("这是第一句话", 0.0, 1.0), unit("好的呀", 1.6, 2.2)])
    assert len(kept.normal_cues) == 2
    assert kept.diagnostics.merged_units == 0


# --- C4 over-long time split -----------------------------------------------------


def test_overlong_cue_time_split_preserves_total_and_min_duration():
    # ~40 full-width chars cannot fit two lines at this width -> time split.
    text = "一二三四五六七八九十" * 4  # 40 chars
    result = compile_normal(
        [unit(text, 0.0, 6.0)], resolution=(760, 1080), margin_l=30, margin_r=30
    )
    assert len(result.normal_cues) >= 2
    assert result.diagnostics.split_cues >= 1
    # Total duration preserved, contiguous, each segment >= 0.6s.
    assert result.normal_cues[0].start == 0.0
    assert result.normal_cues[-1].end == 6.0
    for a, b in zip(result.normal_cues, result.normal_cues[1:]):
        assert a.end == b.start
    for cue in result.normal_cues:
        assert cue.end - cue.start >= 0.6 - 1e-9
        assert len(cue.lines) <= 2


# --- four-state toggle matrix ----------------------------------------------------


def test_toggle_truth_table():
    # 4s cue so the middle-carved [1.5, 4.0] fragment survives (>= 0.6s).
    units = [unit("普通字幕内容", 0.0, 4.0)]
    events = [event(0.5, 1.5, "e1")]

    on_on = compile_normal(units, overlay_events=events, normal_enabled=True, emphasis_enabled=True)
    assert on_on.normal_cues and on_on.emphasis_events

    on_off = compile_normal(
        units, overlay_events=events, normal_enabled=True, emphasis_enabled=False
    )
    assert on_off.normal_cues and on_off.emphasis_events == []
    # Normal captions are always retained when enabled.
    assert len(on_off.normal_cues) == 1 and on_off.normal_cues[0].suppressed_by is None

    off_on = compile_normal(
        units, overlay_events=events, normal_enabled=False, emphasis_enabled=True
    )
    assert off_on.normal_cues == [] and off_on.emphasis_events

    off_off = compile_normal(
        units, overlay_events=events, normal_enabled=False, emphasis_enabled=False
    )
    assert off_off.normal_cues == [] and off_off.emphasis_events == []


# --- no-punch mixed-layer coexistence --------------------------------------------


def _coexistence_units() -> list[dict]:
    # Three well-separated single-line cues (gaps >= 0.5 so no C1 merge; 6
    # meaningful chars so not "tiny"; short enough for one line at 1920px).
    return [
        unit("第一句话内容", 0.0, 2.0),
        unit("第二句话内容", 2.5, 5.0),
        unit("第三句话内容", 5.5, 8.0),
    ]


def _mixed(units, events):
    return compile_normal(units, overlay_events=events, emphasis_enabled=True)


def test_mixed_mode_never_punches_or_splits_normal_cues():
    events = [event(2.4, 5.1, "e2"), event(0.4, 1.0, "e1")]
    result = _mixed(_coexistence_units(), events)
    assert [(cue.start, cue.end) for cue in result.normal_cues] == [
        (0.0, 2.0),
        (2.5, 5.0),
        (5.5, 8.0),
    ]
    assert result.suppressed_cues == []
    assert result.diagnostics.suppressed_duplicates == 0
    assert result.diagnostics.dropped_fragments == 0


def test_non_mixed_mode_keeps_normal_cues():
    # emphasis disabled -> events ignored, cue 2 stays whole.
    result = compile_normal(_coexistence_units(), overlay_events=[event(2.4, 5.1, "e1")])
    assert len(result.normal_cues) == 3
    assert result.suppressed_cues == []
    assert result.diagnostics.suppressed_duplicates == 0


# --- planned path: normal / emphasis coexistence ---------------------------------


def n_window(lines: list[str], start_frame: int, end_frame: int, source_unit_ids: list) -> dict:
    return {
        "start_frame": start_frame,
        "end_frame": end_frame,
        "lines": list(lines),
        "source_unit_ids": list(source_unit_ids),
    }


def e_window(event_id: str, source_unit_ids: list) -> dict:
    return {"event_id": event_id, "source_unit_ids": list(source_unit_ids)}


def planned(
    normal_windows: list[dict], emphasis_windows: list[dict] | None = None, *, fps: int = 10
) -> dict:
    # fps=10 so an integer frame maps to a clean 0.1s grid.
    return {
        "fps": fps,
        "normal_windows": list(normal_windows),
        "emphasis_windows": list(emphasis_windows or []),
        "diagnostics": {},
    }


def compile_planned(
    caption_windows: dict,
    overlay_events: list[dict],
    *,
    normal_enabled: bool = True,
    emphasis_enabled: bool = True,
) -> CaptionDisplayResult:
    return compile_planned_caption_display(
        caption_windows=caption_windows,
        normal_enabled=normal_enabled,
        emphasis_enabled=emphasis_enabled,
        overlay_events=overlay_events,
    )


def test_planned_mixed_mode_keeps_every_normal_window_unchanged():
    windows = planned(
        [
            n_window(["第一句话内容"], 0, 20, ["unit_001"]),
            n_window(["第二句话内容"], 20, 40, ["unit_002"]),
        ],
        [e_window("e1", ["unit_001"])],
    )
    result = compile_planned(windows, [event(0.0, 4.0, "e1")])
    assert [(cue.start, cue.end, cue.source_unit_ids) for cue in result.normal_cues] == [
        (0.0, 2.0, ["unit_001"]),
        (2.0, 4.0, ["unit_002"]),
    ]
    assert result.suppressed_cues == []
    assert result.diagnostics.suppressed_duplicates == 0
    assert result.diagnostics.dropped_fragments == 0


def test_planned_renderer_prefers_explicit_display_span():
    window = n_window(["显示窗口"], 10, 50, ["unit_001"])
    window["display_span"] = {"start_frame": 20, "end_frame": 40}
    result = compile_planned(planned([window]), [])
    assert [(cue.start, cue.end) for cue in result.normal_cues] == [(2.0, 4.0)]


# --- determinism + input immutability --------------------------------------------


def test_deterministic_snapshot():
    units = [
        unit("这是一段比较长的旁白需要断行处理", 0.0, 3.0),
        unit("。", 3.0, 3.2),
        unit("第二段内容也不短要拆开来看效果", 3.5, 6.5),
    ]
    events = [event(1.0, 2.0, "e1")]
    kwargs = dict(
        resolution=(700, 1080),
        margin_l=40,
        margin_r=40,
        emphasis_enabled=True,
        overlay_events=events,
    )
    first = compile_normal(units, **kwargs)
    second = compile_normal(units, **kwargs)
    assert _snapshot(first) == _snapshot(second)


def test_input_units_and_events_not_mutated():
    units = [unit("你好世界", 0.0, 1.0), unit("。", 1.0, 1.5), unit("再来一段内容", 1.6, 3.0)]
    events = [event(0.5, 2.0, "e1")]
    units_before = copy.deepcopy(units)
    events_before = copy.deepcopy(events)
    compile_normal(units, overlay_events=events, emphasis_enabled=True)
    assert units == units_before
    assert events == events_before


def _snapshot(result: CaptionDisplayResult):
    return (
        [
            (
                round(c.start, 4),
                round(c.end, 4),
                tuple(c.lines),
                tuple(c.source_unit_ids),
                c.suppressed_by,
            )
            for c in result.normal_cues
        ],
        [
            (round(c.start, 4), round(c.end, 4), tuple(c.lines), c.suppressed_by)
            for c in result.suppressed_cues
        ],
        (
            result.diagnostics.merged_units,
            result.diagnostics.split_cues,
            result.diagnostics.suppressed_duplicates,
            result.diagnostics.dropped_fragments,
        ),
    )
