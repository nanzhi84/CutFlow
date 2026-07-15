"""Per-node handlers for the digital-human workflow.

Each module exposes a single ``def run(ctx: NodeContext) -> NodeOutput`` that
owns one entry in ``digital_human.NODE_SEQUENCE``. The orchestrator
(``digital_human.LocalRuntimeAdapter``) dispatches to these handlers; capability
work for a node should edit only that node's module.
"""

from __future__ import annotations

from packages.production.pipeline.nodes import (
    bgm_agent_planning,
    caption_composition_planning,
    deterministic_editing_planning,
    export_finished_video,
    export_seedance_video,
    finalize_run_report,
    lipsync,
    load_case_context,
    material_pack_planning,
    media_selection_agent_planning,
    narration_alignment,
    narration_boundary_planning,
    portrait_track_build,
    render_final_timeline,
    resolve_creative_intent,
    seedance_generate_video,
    subtitle_and_bgm_mix,
    timeline_assembly_validation,
    timeline_window_planning,
    tts,
    validate_request,
    window_material_retrieval,
    window_query_planning,
)

__all__ = [
    "bgm_agent_planning",
    "caption_composition_planning",
    "deterministic_editing_planning",
    "export_finished_video",
    "export_seedance_video",
    "finalize_run_report",
    "lipsync",
    "load_case_context",
    "material_pack_planning",
    "media_selection_agent_planning",
    "narration_alignment",
    "narration_boundary_planning",
    "portrait_track_build",
    "render_final_timeline",
    "resolve_creative_intent",
    "seedance_generate_video",
    "subtitle_and_bgm_mix",
    "timeline_assembly_validation",
    "timeline_window_planning",
    "tts",
    "validate_request",
    "window_material_retrieval",
    "window_query_planning",
]
