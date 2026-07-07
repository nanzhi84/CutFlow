"""CreativeIntent 强调字幕链路：容错读取 helper + resolver 映射 + StylePlanning 派生。"""

from __future__ import annotations

from packages.core.contracts import ArtifactKind
from packages.core.contracts.artifacts import CreativeIntentArtifact, EmphasisHint


class _Art:
    def __init__(self, payload):
        self.kind = ArtifactKind.creative_intent
        self.payload = payload


class _State:
    def __init__(self, artifacts):
        self.artifacts = artifacts


def _state_with(payload):
    return _State({ArtifactKind.creative_intent: _Art(payload)})


# --- 契约 ---


def test_creative_intent_defaults_are_empty():
    ci = CreativeIntentArtifact()
    assert ci.intent is None
    assert ci.emphasis == []


def test_creative_intent_round_trips_emphasis():
    ci = CreativeIntentArtifact(
        intent={"hook": "h", "beats": ["a"]},
        emphasis=[EmphasisHint(phrase="限时五折")],
    )
    dumped = ci.model_dump(mode="json")
    again = CreativeIntentArtifact.model_validate(dumped)
    assert again.emphasis[0].phrase == "限时五折"


def test_resolve_creative_intent_normalizes_bgm_mood():
    from packages.production.pipeline.nodes.resolve_creative_intent import _intent_to_artifact

    artifact = _intent_to_artifact(
        {"intent": {"hook": "h", "beats": ["a"], "bgm_mood": "高能推进"}}
    )

    assert artifact.intent["bgm_mood"] == "高能"


# --- load_creative_intent helper ---


def test_load_missing_returns_defaults():
    from packages.production.pipeline.nodes._creative_intent import load_creative_intent

    ci = load_creative_intent(_State({}))
    assert ci.emphasis == []
    assert ci.intent is None


def test_load_reads_emphasis():
    from packages.production.pipeline.nodes._creative_intent import load_creative_intent

    ci = load_creative_intent(
        _state_with(
            {
                "intent": {"hook": "h", "beats": ["a"]},
                "emphasis": [{"phrase": "限时五折"}],
            }
        )
    )
    assert [e.phrase for e in ci.emphasis] == ["限时五折"]


def test_load_tolerates_legacy_payload_with_removed_fields():
    """老 run 的 creative_intent payload 带已删字段（scene_type/cover_focus 等），不得撞 extra=forbid。"""
    from packages.production.pipeline.nodes._creative_intent import load_creative_intent

    legacy = {
        "scene_type": "hard_ad",
        "style_hint": "",
        "density": "medium",
        "closing_cta": "",
        "intent": {"hook": "h", "beats": ["a"]},
        "cover_focus": {},
        "overlay_events": [],
        "script_features_hint": {},
    }
    ci = load_creative_intent(_state_with(legacy))
    assert ci.intent == {"hook": "h", "beats": ["a"]}
    assert ci.emphasis == []


def test_load_falls_back_to_intent_on_invalid_known_field():
    """已声明字段类型非法（model_validate 失败）时，走 except 兜底，仍能保住 intent。"""
    from packages.production.pipeline.nodes._creative_intent import load_creative_intent

    ci = load_creative_intent(
        _state_with({"intent": {"hook": "h", "beats": ["a"]}, "emphasis": "not-a-list"})
    )
    assert ci.intent == {"hook": "h", "beats": ["a"]}
    assert ci.emphasis == []


# --- resolver: _intent_to_artifact ---


def test_intent_to_artifact_maps_emphasis():
    from packages.production.pipeline.nodes.resolve_creative_intent import _intent_to_artifact

    out = {
        "intent": {
            "hook": "h",
            "beats": ["a", "b"],
            "emphasis": ["限时五折", "只要九块九"],
        }
    }
    art = _intent_to_artifact(out)
    assert [e.phrase for e in art.emphasis] == ["限时五折", "只要九块九"]
    assert art.intent["hook"] == "h"


def test_intent_to_artifact_filters_and_dedups_emphasis():
    from packages.production.pipeline.nodes.resolve_creative_intent import _intent_to_artifact

    out = {
        "intent": {
            "hook": "h",
            "beats": [],
            # 去重、丢空/单字/超长、丢非字符串
            "emphasis": ["五折", "五折", "", "x", "字" * 40, 123, "  限时  "],
        }
    }
    art = _intent_to_artifact(out)
    assert [e.phrase for e in art.emphasis] == ["五折", "限时"]


def test_intent_to_artifact_missing_new_fields_defaults():
    from packages.production.pipeline.nodes.resolve_creative_intent import _intent_to_artifact

    art = _intent_to_artifact({"intent": {"hook": "h", "beats": ["a"]}})
    assert art.emphasis == []


def test_intent_to_artifact_caps_emphasis_count():
    from packages.production.pipeline.nodes.resolve_creative_intent import (
        _MAX_EMPHASIS,
        _intent_to_artifact,
    )

    art = _intent_to_artifact(
        {"intent": {"hook": "h", "beats": [], "emphasis": [f"短语{i}" for i in range(20)]}}
    )
    assert len(art.emphasis) == _MAX_EMPHASIS


# --- StylePlanning: _derive_overlay_events ---


def _units(*triples):
    return [{"text": t, "start": s, "end": e} for (t, s, e) in triples]


def test_derive_overlay_matches_phrase_to_narration_sentence():
    from packages.production.pipeline.nodes.style_planning import _derive_overlay_events

    units = _units(("今天给大家带来限时五折活动", 0.0, 2.0), ("到店即可参与", 2.0, 3.0))
    events = _derive_overlay_events([EmphasisHint(phrase="限时五折")], units)
    assert len(events) == 1
    assert events[0].text == "限时五折"
    assert (events[0].start, events[0].end) == (0.0, 2.0)


def test_derive_overlay_unmatched_phrase_dropped():
    from packages.production.pipeline.nodes.style_planning import _derive_overlay_events

    events = _derive_overlay_events(
        [EmphasisHint(phrase="限时五折")], _units(("完全不相关的一句话", 0.0, 2.0))
    )
    assert events == []


def test_derive_overlay_empty_emphasis_no_events():
    from packages.production.pipeline.nodes.style_planning import _derive_overlay_events

    assert _derive_overlay_events([], _units(("一句话", 0.0, 1.0))) == []


def test_derive_overlay_one_per_sentence():
    """两个短语命中同一句旁白时只出一条花字（避免同时同位置叠印）。"""
    from packages.production.pipeline.nodes.style_planning import _derive_overlay_events

    units = _units(("今天限时五折只要九块九", 0.0, 2.0), ("赶紧来", 2.0, 3.0))
    events = _derive_overlay_events(
        [EmphasisHint(phrase="限时五折"), EmphasisHint(phrase="九块九")], units
    )
    assert len(events) == 1
    assert events[0].text == "限时五折"  # 保留 LLM 顺序里第一个命中该句的短语


# --- _subtitles: emphasis rendering ---

_NARR = {"units": [{"text": "今天限时五折活动", "start": 0.0, "end": 2.0}]}
_STYLE = {"subtitle": {"font_size": 64}}


def _write(tmp_path, **kwargs):
    from packages.production.pipeline._subtitles import write_ass_subtitles

    out = tmp_path / "s.ass"
    write_ass_subtitles(out, narration=_NARR, style=_STYLE, width=1080, height=1920, **kwargs)
    return out.read_text(encoding="utf-8")


def test_emphasis_overlay_renders_layer1_dialogue(tmp_path):
    txt = _write(
        tmp_path,
        overlay_events=[{"start": 0.0, "end": 2.0, "text": "限时五折", "style": "emphasis"}],
    )
    assert "Style: Emphasis," in txt
    assert "Dialogue: 1," in txt  # emphasis layered above the Layer 0 narration
    assert "Emphasis,,0,0,0,,限时五折" in txt
    assert "&H0000FFFF" in txt  # yellow emphasis primary colour


def test_no_overlay_is_byte_identical_to_legacy(tmp_path):
    """无 overlay 时输出与旧路径逐字节一致：不传 vs 传空列表 vs None。"""
    from packages.production.pipeline._subtitles import write_ass_subtitles

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    a = tmp_path / "a" / "s.ass"
    b = tmp_path / "b" / "s.ass"
    c = tmp_path / "c" / "s.ass"
    write_ass_subtitles(a, narration=_NARR, style=_STYLE, width=1080, height=1920)
    write_ass_subtitles(b, narration=_NARR, style=_STYLE, width=1080, height=1920, overlay_events=[])
    write_ass_subtitles(
        c, narration=_NARR, style=_STYLE, width=1080, height=1920, overlay_events=None
    )
    legacy = a.read_text(encoding="utf-8")
    assert legacy == b.read_text(encoding="utf-8") == c.read_text(encoding="utf-8")
    assert "Emphasis" not in legacy
    assert "Dialogue: 1," not in legacy
    # 原 Default 样式行逐字节不变（防字幕风格回归）
    assert ",1,4,1,2," in legacy and "&H00FFFFFF" in legacy


def test_emphasis_text_is_ass_escaped(tmp_path):
    txt = _write(
        tmp_path,
        overlay_events=[{"start": 0.0, "end": 2.0, "text": "限{时}五折", "style": "emphasis"}],
    )
    assert "限时五折" in txt  # 花括号被 ass_escape 去掉，避免 ASS 注入/破帧


# --- 水源守卫：绑定到 ResolveCreativeIntent 的 prompt 必须真的让 LLM 产出 emphasis ---


def test_creative_intent_prompt_requests_emphasis():
    """整条花字链路只有在 LLM 被要求输出 emphasis 时才有数据；prompt 若漏掉 emphasis，
    上面所有派生/渲染逻辑都收不到非空输入，功能形同虚设。锁住这个契约。"""
    from packages.core.storage.repository import Repository

    repo = Repository()
    binding = next(
        b for b in repo.prompt_bindings.values() if b.node_id == "ResolveCreativeIntent"
    )
    content = repo.prompt_versions[binding.prompt_version_id].content
    assert "emphasis" in content
