"""Canonical node order for the digital_human production workflow.

Kept dependency-free so progress / UI / reporting code can read the *expected*
node count for a template without importing the heavy pipeline engine (ffmpeg,
providers, every node handler). ``digital_human.py`` re-exports ``NODE_SEQUENCE``
from here so there is a single source of truth.
"""

from __future__ import annotations

NODE_SEQUENCE = [
    "ValidateRequest",
    "LoadCaseContext",
    "ResolveCreativeIntent",
    "TTS",
    "MaterialPackPlanning",
    "NarrationAlignment",
    "PortraitPlanning",
    "BrollPlanning",
    "StylePlanning",
    "TimelinePlanning",
    "PortraitTrackBuild",
    "LipSync",
    "RenderFinalTimeline",
    "SubtitleAndBgmMix",
    "ExportFinishedVideo",
    "FinalizeRunReport",
]

BROLL_ONLY_SEQUENCE = [
    "ValidateRequest",
    "LoadCaseContext",
    "ResolveCreativeIntent",
    "TTS",
    "MaterialPackPlanning",
    "NarrationAlignment",
    "BrollCoveragePlanning",
    "StylePlanning",
    "BrollTimelinePlanning",
    "BrollRenderBase",
    "SubtitleAndBgmMix",
    "ExportFinishedVideo",
    "FinalizeRunReport",
]

SEEDANCE_T2V_SEQUENCE = [
    "ValidateRequest",
    "LoadCaseContext",
    "SeedanceGenerateVideo",
    "ExportSeedanceVideo",
    "FinalizeRunReport",
]

# Expected total node count per workflow template id. Used to render run progress
# as completed / total across the *whole* pipeline (node runs are created lazily,
# so the count of existing node runs is not the denominator).
WORKFLOW_TEMPLATE_NODE_COUNTS = {
    "digital_human_v2": len(NODE_SEQUENCE),
    "broll_only_v1": len(BROLL_ONLY_SEQUENCE),
    "seedance_t2v_v1": len(SEEDANCE_T2V_SEQUENCE),
}


def expected_node_count(workflow_template_id: str | None) -> int:
    """Total nodes for a template id; 0 when unknown (caller falls back)."""
    return WORKFLOW_TEMPLATE_NODE_COUNTS.get(workflow_template_id or "", 0)
