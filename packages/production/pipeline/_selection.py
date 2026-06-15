"""Pure helper that turns plan artifacts into selection-ledger entries.

Used by the FinalizeRunReport node to record which assets a run consumed (the
diversity ledger that drives usage-aware recency demotion on the next run).
"""

from __future__ import annotations

from packages.core.contracts import SelectionLedgerEntry, WorkflowRun
from packages.core.contracts.artifacts import ArtifactKind
from packages.production.pipeline._run_state import RunState


def selection_entries_from_state(run: WorkflowRun, state: RunState) -> list[SelectionLedgerEntry]:
    case_id = run.case_id or state.request.case_id
    entries: list[SelectionLedgerEntry] = []

    def add(medium: str, asset_id, slot_phase: str, diversity_key=None) -> None:
        if isinstance(asset_id, str) and asset_id:
            entries.append(
                SelectionLedgerEntry(
                    case_id=case_id,
                    run_id=run.id,
                    medium=medium,
                    asset_id=asset_id,
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
    recorded_portrait_keys: set[tuple[str, str]] = set()
    if isinstance(portrait_segments, list) and portrait_segments:
        for seg_index, segment in enumerate(portrait_segments):
            if not isinstance(segment, dict):
                continue
            asset_id = segment.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                continue
            slot_phase = str(segment.get("slot_phase") or "").strip() or (
                "portrait_opening" if seg_index == 0 else "portrait_main"
            )
            # One ledger row per (template, slot_phase) so a reused template does not
            # spam the ledger, while the opening row stays distinct from main rows.
            key = (asset_id, slot_phase)
            if key in recorded_portrait_keys:
                continue
            recorded_portrait_keys.add(key)
            add("portrait", asset_id, slot_phase, segment.get("diversity_key"))
    else:
        add("portrait", portrait_payload.get("asset_id"), "portrait_main")

    broll = state.artifacts.get(ArtifactKind.plan_broll)
    broll_payload = broll.payload if broll and isinstance(broll.payload, dict) else {}
    overlays = broll_payload.get("overlays")
    segments = broll_payload.get("segments")
    broll_items = overlays if isinstance(overlays, list) and overlays else segments
    if isinstance(broll_items, list):
        for index, item in enumerate(broll_items):
            if not isinstance(item, dict):
                continue
            slot_phase = str(item.get("overlay_id") or item.get("segment_id") or f"broll_{index + 1}")
            add("broll", item.get("asset_id"), slot_phase, item.get("diversity_key"))

    style = state.artifacts.get(ArtifactKind.plan_style)
    style_payload = style.payload if style and isinstance(style.payload, dict) else {}
    bgm = style_payload.get("bgm") if isinstance(style_payload.get("bgm"), dict) else {}
    font = style_payload.get("font") if isinstance(style_payload.get("font"), dict) else {}
    subtitle = style_payload.get("subtitle") if isinstance(style_payload.get("subtitle"), dict) else {}
    add("bgm", style_payload.get("bgm_asset_id") or bgm.get("asset_id"), "bgm")
    add(
        "font",
        style_payload.get("font_asset_id") or font.get("font_id") or subtitle.get("font_id"),
        "font",
    )
    return entries
