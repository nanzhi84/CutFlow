"""StylePlanning node: subtitle/BGM/font style plan with degradations."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, NodeStatus, normalize_bgm_mood
from packages.core.contracts.artifacts import EmphasisHint, OverlayEvent
from packages.core.workflow import NodeOutput
from packages.production.pipeline._materialize import materialize_style_from_selection
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.nodes._creative_intent import load_creative_intent


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    node_run = ctx.node_run
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    narration_units = state.artifacts.get(ArtifactKind.narration_units)
    units = (narration_units.payload or {}).get("units", []) if narration_units is not None else []
    creative_intent = load_creative_intent(state)
    overlay_events = _derive_overlay_events(creative_intent.emphasis, units)
    payload, warnings, degradations = materialize_style_from_selection(
        request=state.request,
        material=material,
        overlay_events=overlay_events,
        target_bgm_mood=_target_bgm_mood(creative_intent.intent),
    )
    degradations = [
        notice.model_copy(update={"node_id": node_run.node_id}) for notice in degradations
    ]
    artifact = ctx.artifact(
        ArtifactKind.plan_style,
        payload,
        "StylePlanArtifact.v1",
    )
    return NodeOutput(
        status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
        artifacts=[artifact],
        warnings=warnings,
        degradations=degradations,
    )


def _derive_overlay_events(emphasis: list[EmphasisHint], units: list[dict]) -> list[OverlayEvent]:
    """Place each emphasis phrase onto the narration sentence that contains it.

    Deterministic substring match (whitespace/case-insensitive) against the real
    narration timeline; the matched sentence supplies the timing, the phrase itself is
    the overlay text. A phrase matching no sentence is dropped: emphasis is an additive
    花字 overlay, so a miss leaves the baseline subtitles untouched rather than degrading
    them (hence no DegradationNotice). At most one overlay per narration sentence (two
    phrases sharing a sentence would render as same-time, same-position banners), so a
    later phrase whose only match is an already-claimed sentence is dropped. Phrases keep
    the LLM's order.
    """
    events: list[OverlayEvent] = []
    used: set[int] = set()
    for hint in emphasis:
        needle = _compact_text(hint.phrase)
        if not needle:
            continue
        for index, unit in enumerate(units):
            if index in used:
                continue
            if needle in _compact_text(str(unit.get("text", ""))):
                used.add(index)
                events.append(
                    OverlayEvent(
                        start=float(unit.get("start", 0) or 0),
                        end=float(unit.get("end", 0) or 0),
                        text=hint.phrase,
                    )
                )
                break
    return events


def _compact_text(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if not ch.isspace())


def _target_bgm_mood(intent: dict | None) -> str:
    if not isinstance(intent, dict):
        return ""
    for key in ("bgm_mood", "music_mood", "mood"):
        mood = normalize_bgm_mood(intent.get(key))
        if mood:
            return mood
    return ""
