"""CreativeIntent 的字幕内强调语义与历史 payload 容错。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    DigitalHumanVideoRequest,
    ErrorCode,
)
from packages.core.contracts.artifacts import CreativeIntentArtifact, EmphasisHint
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline.nodes.resolve_creative_intent import (
    _MAX_EMPHASIS,
    _intent_to_artifact,
)


class _Art:
    def __init__(self, payload):
        self.kind = ArtifactKind.creative_intent
        self.payload = payload


class _State:
    def __init__(self, artifacts):
        self.artifacts = artifacts


def _state_with(payload):
    return _State({ArtifactKind.creative_intent: _Art(payload)})


def test_creative_intent_round_trips_caption_run_hints():
    artifact = CreativeIntentArtifact(
        intent={"hook": "h", "beats": ["a"]},
        emphasis=[
            EmphasisHint(phrase="限时五折", priority=90, display_mode="inline"),
            EmphasisHint(phrase="最后一天", priority=70, display_mode="whole_cue"),
        ],
    )

    restored = CreativeIntentArtifact.model_validate(artifact.model_dump(mode="json"))

    assert restored.emphasis == artifact.emphasis


def test_intent_to_artifact_accepts_only_exact_typed_script_phrases():
    script = "今天限时五折，最后一天。"
    output = {
        "intent": {
            "hook": "h",
            "beats": ["a"],
            "bgm_mood": "高能推进",
            "emphasis": [
                {
                    "phrase": "限时五折",
                    "priority": 90,
                    "display_mode": "inline",
                    "intensity": "hero",
                },
                {
                    "phrase": "最后一天",
                    "priority": 70,
                    "display_mode": "whole_cue",
                    "intensity": "invalid",
                },
                {"phrase": "限时五折", "priority": 20, "display_mode": "inline"},
                {"phrase": "脚本没有", "priority": 100, "display_mode": "inline"},
                {"phrase": "限时五折", "priority": True, "display_mode": "inline"},
                {"phrase": "限时五折", "priority": 50, "display_mode": "banner"},
                "限时五折",
            ],
        }
    }

    artifact = _intent_to_artifact(output, script)

    assert artifact.intent is not None
    assert artifact.intent["bgm_mood"] == "高能"
    assert [(item.phrase, item.priority, item.display_mode) for item in artifact.emphasis] == [
        ("限时五折", 90, "inline"),
        ("最后一天", 70, "whole_cue"),
        ("限时五折", 20, "inline"),
    ]
    assert [item.intensity for item in artifact.emphasis] == ["hero", "normal", "normal"]


def test_intent_to_artifact_preserves_order_and_caps_hint_count():
    phrases = [f"短语{i}" for i in range(20)]
    script = "，".join(phrases)
    artifact = _intent_to_artifact(
        {
            "intent": {
                "hook": "h",
                "beats": [],
                "emphasis": [
                    {"phrase": phrase, "priority": index, "display_mode": "inline"}
                    for index, phrase in enumerate(phrases)
                ],
            }
        },
        script,
    )

    assert [item.phrase for item in artifact.emphasis] == phrases[:_MAX_EMPHASIS]


def test_intent_to_artifact_enforces_intensity_cardinality() -> None:
    phrases = [f"短语{i}" for i in range(8)]
    artifact = _intent_to_artifact(
        {
            "intent": {
                "emphasis": [
                    {
                        "phrase": phrase,
                        "priority": 50,
                        "display_mode": "inline",
                        "intensity": "hero" if index < 2 else "strong",
                    }
                    for index, phrase in enumerate(phrases)
                ]
            }
        },
        "，".join(phrases),
    )

    assert [item.intensity for item in artifact.emphasis] == [
        "hero",
        "normal",
        "strong",
        "strong",
        "strong",
        "normal",
        "normal",
        "normal",
    ]


def test_intent_to_artifact_missing_hints_defaults_to_empty():
    artifact = _intent_to_artifact({"intent": {"hook": "h", "beats": ["a"]}}, "脚本")
    assert artifact.emphasis == []


def test_load_creative_intent_tolerates_historical_extra_fields():
    from packages.production.pipeline.nodes._creative_intent import load_creative_intent

    payload = {
        "scene_type": "hard_ad",
        "intent": {"hook": "h", "beats": ["a"]},
        "overlay_events": [],
        "emphasis": [{"phrase": "限时五折", "priority": 80, "display_mode": "inline"}],
    }

    artifact = load_creative_intent(_state_with(payload))

    assert artifact.intent == {"hook": "h", "beats": ["a"]}
    assert artifact.emphasis[0].phrase == "限时五折"


def test_load_creative_intent_falls_back_when_known_field_is_invalid():
    from packages.production.pipeline.nodes._creative_intent import load_creative_intent

    artifact = load_creative_intent(
        _state_with({"intent": {"hook": "h", "beats": ["a"]}, "emphasis": "invalid"})
    )

    assert artifact.intent == {"hook": "h", "beats": ["a"]}
    assert artifact.emphasis == []


def test_resolve_creative_intent_ref_rejects_cross_case_artifact():
    from packages.production.pipeline.nodes import resolve_creative_intent

    artifact = Artifact(
        id="art_foreign_intent",
        case_id="case_other",
        kind=ArtifactKind.creative_intent,
        uri="artifact://art_foreign_intent",
        payload_schema="CreativeIntentArtifact.v1",
        payload={"intent": None, "emphasis": []},
    )
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="脚本",
        voice={"voice_id": "voice_sandbox"},
        creative_intent_ref=ArtifactRef(
            artifact_id=artifact.id,
            kind=ArtifactKind.creative_intent,
            uri=artifact.uri or "artifact://missing",
        ),
    )
    ctx = SimpleNamespace(
        state=SimpleNamespace(request=request),
        run=SimpleNamespace(case_id="case_demo"),
        node_run=SimpleNamespace(),
        repository=SimpleNamespace(artifacts={artifact.id: artifact}),
    )

    with pytest.raises(NodeExecutionError) as exc:
        resolve_creative_intent.run(ctx)

    assert exc.value.error.code == ErrorCode.artifact_schema_mismatch


def test_creative_intent_prompt_requests_caption_run_semantics():
    from packages.core.storage.repository import Repository

    repository = Repository()
    binding = next(
        item for item in repository.prompt_bindings.values() if item.node_id == "ResolveCreativeIntent"
    )
    content = repository.prompt_versions[binding.prompt_version_id].content

    assert "phrase" in content
    assert "priority" in content
    assert "display_mode" in content
    assert "intensity" in content
    assert "hero 最多 1 个" in content
    assert "inline" in content
    assert "whole_cue" in content
