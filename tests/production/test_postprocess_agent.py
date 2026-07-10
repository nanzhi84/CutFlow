from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    WarningCode,
)
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline._materialize import (
    eligible_bgm_candidates,
    materialize_style_from_selection,
)
from packages.production.pipeline._postprocess_agent import (
    PostProcessCaptionChoice,
    PostProcessSelection,
    materialize_overlay_events,
    parse_postprocess_selection,
    unwrap_postprocess_provider_output,
    validate_postprocess_selection,
)
from packages.production.pipeline.nodes import postprocess_agent_planning


def _window(event_id: str, start: int, end: int, animation: str = "pop_in") -> dict:
    anchor_id = f"{event_id}__upper_left"
    option_id = f"{event_id}__option"
    return {
        "event_id": event_id,
        "text": "限时五折",
        "normalized_text": "限时五折",
        "start_frame": start,
        "end_frame": end,
        "source_unit_ids": ["u1"],
        "anchor_candidates": [
            {
                "anchor_id": anchor_id,
                "rect": {"x": 0.05, "y": 0.1, "w": 0.4, "h": 0.1},
                "text_align": "left",
                "allowed_animation_ids": [animation],
            }
        ],
        "caption_options": [
            {
                "caption_option_id": option_id,
                "anchor_id": anchor_id,
                "typography_variant_id": "emphasis_default_v1",
                "animation_id": animation,
            }
        ],
    }


def _choice(event_id: str, priority: int = 50) -> PostProcessCaptionChoice:
    return PostProcessCaptionChoice(
        event_id=event_id,
        caption_option_id=f"{event_id}__option",
        priority=priority,
        reason="节奏合适",
    )


def test_parser_requires_exact_three_top_level_fields():
    selection, errors = parse_postprocess_selection(
        {"bgm_id": None, "caption_choices": [], "analysis": "克制后处理"}
    )
    assert errors == []
    assert selection.bgm_id is None
    _selection, errors = parse_postprocess_selection({"bgm_id": None, "captions": []})
    assert errors


@pytest.mark.parametrize(
    ("field", "value", "error_fragment"),
    [
        ("bgm_id", 7, "bgm_id"),
        ("event_id", 7, "event_id"),
        ("caption_option_id", 7, "caption_option_id"),
        ("reason", 7, "reason"),
        ("priority", "80", "priority"),
        ("priority", True, "priority"),
    ],
)
def test_parser_rejects_coercible_but_wrong_scalar_types(field, value, error_fragment):
    choice = {
        "event_id": "e1",
        "caption_option_id": "o1",
        "priority": 80,
        "reason": "合适",
    }
    output = {"bgm_id": None, "caption_choices": [choice], "analysis": "严格类型"}
    if field == "bgm_id":
        output[field] = value
    else:
        choice[field] = value

    _selection, errors = parse_postprocess_selection(output)

    assert any(error_fragment in error for error in errors)


def test_null_bgm_is_valid_even_when_candidates_exist():
    errors = validate_postprocess_selection(
        PostProcessSelection(bgm_id=None, analysis="不需要配乐"),
        caption_windows={"fps": 30, "emphasis_windows": []},
        bgm_candidates=[{"candidate_id": "bgmseg_1", "asset_id": "bgm_1"}],
        bgm_enabled=True,
        emphasis_enabled=True,
    )
    assert errors == []


def test_dashscope_envelope_is_exact_and_intent_is_strictly_parsed():
    direct = {"bgm_id": None, "caption_choices": [], "analysis": "direct"}
    payload, errors = unwrap_postprocess_provider_output(direct)
    assert errors == [] and payload == direct

    intent = {"bgm_id": None, "caption_choices": [], "analysis": "wrapped"}
    payload, errors = unwrap_postprocess_provider_output(
        {"content": "模型输出", "intent": intent}
    )
    assert errors == [] and payload == intent
    selection, parse_errors = parse_postprocess_selection(payload)
    assert parse_errors == [] and selection.analysis == "wrapped"

    _payload, errors = unwrap_postprocess_provider_output(
        {"content": "模型输出", "intent": intent, "usage": {}}
    )
    assert any("unknown fields" in error for error in errors)
    _payload, errors = unwrap_postprocess_provider_output(
        {"content": [], "intent": "not-an-object"}
    )
    assert any("content" in error for error in errors)
    assert any("intent" in error for error in errors)


def test_validator_rejects_dense_or_excess_punch_choices():
    windows = [_window("e1", 0, 30, "punch"), _window("e2", 40, 70, "punch")]
    windows.append(_window("e3", 120, 150, "punch"))
    errors = validate_postprocess_selection(
        PostProcessSelection(
            bgm_id=None,
            caption_choices=[_choice("e1"), _choice("e2"), _choice("e3")],
            analysis="",
        ),
        caption_windows={"fps": 30, "emphasis_windows": windows},
        bgm_candidates=[],
        bgm_enabled=False,
        emphasis_enabled=True,
    )
    assert any("punch" in error for error in errors)
    assert any("0.8s" in error for error in errors)


def test_materializer_uses_only_authoritative_option_geometry_and_frames():
    window = _window("e1", 30, 60)
    events, diagnostics = materialize_overlay_events(
        PostProcessSelection(
            bgm_id=None,
            caption_choices=[_choice("e1")],
            analysis="",
        ),
        caption_windows={"fps": 30, "emphasis_windows": [window]},
    )
    assert len(events) == 1
    assert events[0].start == 1.0
    assert events[0].end == 2.0
    assert events[0].rect is not None and events[0].rect.x == 0.05
    assert diagnostics[0]["caption_option_id"] == "e1__option"


def test_strict_style_materialization_never_auto_selects_bgm():
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_demo"},
        subtitle={"font_id": "font_demo"},
        bgm={"enabled": True},
    )
    material = {
        "bgm_candidates": [
            {
                "asset_id": "bgm_1",
                "metadata": {
                    "clip_id": "clip_1",
                    "source_start": 0.0,
                    "source_end": 30.0,
                    "duration": 30.0,
                },
            }
        ]
    }
    payload, warnings, degradations = materialize_style_from_selection(
        request=request,
        material=material,
        overlay_events=[],
        bgm_id=None,
        strict_bgm_selection=True,
    )
    assert payload["bgm_asset_id"] is None
    assert payload["bgm"]["asset_id"] is None
    assert payload["bgm"]["enabled"] is False
    assert warnings == []
    assert degradations == []


def test_stable_bgm_segment_id_selects_exact_second_segment_of_same_asset():
    material = {
        "bgm_candidates": [
            _bgm_candidate("asset_song", "clip_1", 0.0, 20.0, "energetic"),
            _bgm_candidate("asset_song", "clip_2", 30.0, 55.0, "calm"),
        ]
    }
    candidates = eligible_bgm_candidates(material)
    assert candidates[0]["candidate_id"] != candidates[1]["candidate_id"]
    selected_id = candidates[1]["candidate_id"]
    errors = validate_postprocess_selection(
        PostProcessSelection(bgm_id=selected_id, analysis="选舒缓段"),
        caption_windows={"fps": 30, "emphasis_windows": []},
        bgm_candidates=candidates,
        bgm_enabled=True,
        emphasis_enabled=False,
    )
    assert errors == []

    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_demo"},
        subtitle={"enabled": False},
        bgm={"enabled": True},
    )
    payload, warnings, degradations = materialize_style_from_selection(
        request=request,
        material=material,
        overlay_events=[],
        bgm_id=selected_id,
        strict_bgm_selection=True,
    )
    assert payload["bgm_asset_id"] == "asset_song"
    assert payload["bgm"]["segment_id"] == "clip_2"
    assert payload["bgm"]["source_start"] == 30.0
    assert payload["bgm"]["source_end"] == 55.0
    assert payload["bgm"]["mood"] == "calm"
    assert warnings == [] and degradations == []


def test_disabled_bgm_and_emphasis_ignore_stale_candidates_without_provider_call():
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_demo"},
        subtitle={"normal_enabled": True, "emphasis_enabled": False},
        bgm={"enabled": False},
    )
    material = {
        "bgm_candidates": [
            {
                "asset_id": "stale_bgm",
                "metadata": {"clip_id": "clip_1", "duration": 20.0},
            }
        ]
    }
    caption_windows = _caption_plan([_window("stale", 0, 30)])
    state = RunState(
        request=request,
        artifacts={
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack, material, "MaterialPackArtifact.v1"
            ),
            ArtifactKind.plan_caption_windows: _artifact(
                ArtifactKind.plan_caption_windows,
                caption_windows,
                "CaptionWindowsPlan.v1",
            ),
        },
    )

    class _NoProviderContext:
        node_run = SimpleNamespace(node_id="PostProcessAgentPlanning")

        def __init__(self, run_state: RunState):
            self.state = run_state

        def first_available_provider_profile(self, *_args, **_kwargs):
            raise AssertionError("disabled post-processing must not resolve a provider")

        def artifact(self, kind, payload, payload_schema):
            return _artifact(kind, payload, payload_schema)

    output = postprocess_agent_planning.run(_NoProviderContext(state))

    assert output.provider_invocation_ids == []
    style = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_style
    )
    assert style["overlay_events"] == []
    assert style["bgm_asset_id"] is None
    assert style["bgm"]["enabled"] is False
    diagnostics = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_postprocess_diagnostics
    )
    assert diagnostics["candidate_counts"] == {
        "bgm": 0,
        "caption_events": 0,
        "caption_options": 0,
    }


def test_missing_bgm_candidates_degrades_independently_while_caption_agent_runs():
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_demo"},
        subtitle={"normal_enabled": True, "emphasis_enabled": True},
        bgm={"enabled": True},
    )
    state = RunState(
        request=request,
        artifacts={
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack, {}, "MaterialPackArtifact.v1"
            ),
            ArtifactKind.plan_caption_windows: _artifact(
                ArtifactKind.plan_caption_windows,
                _caption_plan([_window("e1", 0, 30)]),
                "CaptionWindowsPlan.v1",
            ),
        },
    )

    class _NoProviderContext:
        node_run = SimpleNamespace(node_id="PostProcessAgentPlanning")

        def __init__(self, run_state: RunState):
            self.state = run_state
            self.profile_resolutions = 0

        def first_available_provider_profile(self, *_args, **_kwargs):
            self.profile_resolutions += 1
            return None

        def artifact(self, kind, payload, payload_schema):
            return _artifact(kind, payload, payload_schema)

    context = _NoProviderContext(state)
    output = postprocess_agent_planning.run(context)

    assert context.profile_resolutions == 1
    assert WarningCode.bgm_skipped_library_unannotated in output.warnings
    assert WarningCode.postprocess_planning_failed in output.warnings
    diagnostics = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_postprocess_diagnostics
    )
    assert diagnostics["candidate_counts"]["caption_events"] == 1


def test_invalid_caption_plan_degrades_before_provider_resolution():
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_demo"},
        bgm={"enabled": False},
    )
    state = RunState(
        request=request,
        artifacts={
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack, {}, "MaterialPackArtifact.v1"
            ),
            ArtifactKind.plan_caption_windows: _artifact(
                ArtifactKind.plan_caption_windows,
                {"fps": 30, "emphasis_windows": []},
                "CaptionWindowsPlan.v1",
            ),
        },
    )

    class _Context:
        node_run = SimpleNamespace(node_id="PostProcessAgentPlanning")

        def __init__(self, run_state):
            self.state = run_state

        def first_available_provider_profile(self, *_args, **_kwargs):
            raise AssertionError("invalid caption plan must not reach provider resolution")

        def artifact(self, kind, payload, payload_schema):
            return _artifact(kind, payload, payload_schema)

    output = postprocess_agent_planning.run(_Context(state))

    assert WarningCode.postprocess_planning_failed in output.warnings
    assert output.provider_invocation_ids == []
    style = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_style
    )
    assert style["overlay_events"] == []
    assert style["bgm_asset_id"] is None


def test_materialization_error_converges_to_normal_caption_only(monkeypatch):
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_demo"},
        subtitle={"normal_enabled": True, "emphasis_enabled": False},
        bgm={"enabled": False},
    )
    state = RunState(
        request=request,
        artifacts={
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack, {}, "MaterialPackArtifact.v1"
            ),
            ArtifactKind.plan_caption_windows: _artifact(
                ArtifactKind.plan_caption_windows,
                _caption_plan([], emphasis_enabled=False),
                "CaptionWindowsPlan.v1",
            ),
        },
    )
    original = postprocess_agent_planning.materialize_style_from_selection
    calls = 0

    def _fail_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("materializer exploded")
        return original(**kwargs)

    monkeypatch.setattr(
        postprocess_agent_planning,
        "materialize_style_from_selection",
        _fail_once,
    )

    class _Context:
        node_run = SimpleNamespace(node_id="PostProcessAgentPlanning")

        def __init__(self, run_state):
            self.state = run_state

        def artifact(self, kind, payload, payload_schema):
            return _artifact(kind, payload, payload_schema)

    output = postprocess_agent_planning.run(_Context(state))

    assert calls == 2
    assert WarningCode.postprocess_planning_failed in output.warnings
    style = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_style
    )
    assert style["subtitle"]["normal_enabled"] is True
    assert style["overlay_events"] == []
    assert style["bgm"]["enabled"] is False


def _artifact(kind: ArtifactKind, payload: dict, payload_schema: str) -> Artifact:
    return Artifact(
        id=f"art_{kind.value}",
        kind=kind,
        payload=payload,
        payload_schema=payload_schema,
    )


def _caption_plan(windows: list[dict], *, emphasis_enabled: bool = True) -> dict:
    return {
        "policy_version": "caption_windows_v1",
        "source_video_artifact_id": "video_1",
        "source_timeline_artifact_id": "timeline_1",
        "fps": 30,
        "width": 1080,
        "height": 1920,
        "normal_enabled": True,
        "emphasis_enabled": emphasis_enabled,
        "normal_safe_rect": None,
        "normal_windows": [],
        "emphasis_windows": windows,
        "diagnostics": {},
    }


def _bgm_candidate(
    asset_id: str,
    clip_id: str,
    source_start: float,
    source_end: float,
    mood: str,
) -> dict:
    return {
        "asset_id": asset_id,
        "metadata": {
            "clip_id": clip_id,
            "source_start": source_start,
            "source_end": source_end,
            "duration": source_end - source_start,
            "mood": mood,
        },
    }
