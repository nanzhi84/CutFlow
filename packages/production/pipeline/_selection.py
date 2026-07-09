"""Pure helper that turns plan artifacts into selection-ledger entries.

Used by the FinalizeRunReport node to record which assets a run consumed (the
diversity ledger that drives usage-aware recency demotion on the next run).
"""

from __future__ import annotations

from packages.core.contracts import ArtifactKind, SelectionLedgerEntry, WorkflowRun
from packages.production._broll_overlays import broll_overlays_from_plan
from packages.production.pipeline._run_state import RunState


def selection_entries_from_state(run: WorkflowRun, state: RunState) -> list[SelectionLedgerEntry]:
    case_id = run.case_id or state.request.case_id
    entries: list[SelectionLedgerEntry] = []

    def add(
        medium: str,
        asset_id,
        slot_phase: str,
        diversity_key=None,
        clip_id=None,
    ) -> None:
        if isinstance(asset_id, str) and asset_id:
            entries.append(
                SelectionLedgerEntry(
                    case_id=case_id,
                    run_id=run.id,
                    medium=medium,
                    asset_id=asset_id,
                    clip_id=clip_id if isinstance(clip_id, str) and clip_id else None,
                    slot_phase=slot_phase,
                    diversity_key=diversity_key if isinstance(diversity_key, str) else None,
                )
            )

    # Portrait: record the per-segment template usage with a distinct slot_phase for the
    # opening segment ("portrait_opening") so the next run's recency context can fire the
    # opening guard (no-consecutive-opening-reuse). Falls back to the single top-level
    # asset_id (main) when a run somehow has no per-segment plan.
    portrait = state.artifacts.get(ArtifactKind.plan_portrait)
    portrait_payload = portrait.payload if portrait and isinstance(portrait.payload, dict) else {}
    portrait_segments = portrait_payload.get("segments")
    recorded_portrait_keys: set[tuple[str, str | None, str]] = set()
    if isinstance(portrait_segments, list) and portrait_segments:
        for seg_index, segment in enumerate(portrait_segments):
            if not isinstance(segment, dict):
                continue
            asset_id = segment.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                continue
            clip_id = segment.get("clip_id")
            normalized_clip_id = clip_id if isinstance(clip_id, str) and clip_id else None
            slot_phase = str(segment.get("slot_phase") or "").strip() or (
                "portrait_opening" if seg_index == 0 else "portrait_main"
            )
            # One ledger row per (template, clip, slot_phase) so repeated timeline
            # use of the same source clip does not spam the ledger, while distinct
            # clips from the same asset remain independently ranked.
            key = (asset_id, normalized_clip_id, slot_phase)
            if key in recorded_portrait_keys:
                continue
            recorded_portrait_keys.add(key)
            add(
                "portrait",
                asset_id,
                slot_phase,
                segment.get("diversity_key"),
                normalized_clip_id,
            )
    else:
        add("portrait", portrait_payload.get("asset_id"), "portrait_main")

    # overlays is the canonical B-roll structure; the helper derives them from a
    # legacy persisted plan's segments only when overlays are absent (#104).
    broll = state.artifacts.get(ArtifactKind.plan_broll)
    broll_payload = broll.payload if broll and isinstance(broll.payload, dict) else {}
    for overlay in broll_overlays_from_plan(broll_payload):
        add(
            "broll",
            overlay.asset_id,
            overlay.overlay_id,
            overlay.diversity_key,
            overlay.clip_id,
        )

    style = state.artifacts.get(ArtifactKind.plan_style)
    style_payload = style.payload if style and isinstance(style.payload, dict) else {}
    bgm = style_payload.get("bgm") if isinstance(style_payload.get("bgm"), dict) else {}
    font = style_payload.get("font") if isinstance(style_payload.get("font"), dict) else {}
    subtitle = (
        style_payload.get("subtitle") if isinstance(style_payload.get("subtitle"), dict) else {}
    )
    if not bgm or bgm.get("enabled") is not False:
        add(
            "bgm",
            style_payload.get("bgm_asset_id") or bgm.get("asset_id"),
            "bgm",
            clip_id=bgm.get("segment_id"),
        )
    normal_font_id = style_payload.get("font_asset_id") or font.get("font_id") or subtitle.get("font_id")
    emphasis_font_id = (
        style_payload.get("emphasis_font_asset_id")
        or font.get("emphasis_font_id")
        or subtitle.get("emphasis_font_id")
    )
    add("font", normal_font_id, "font")
    if emphasis_font_id and emphasis_font_id != normal_font_id:
        add("font", emphasis_font_id, "font_emphasis")
    return entries
