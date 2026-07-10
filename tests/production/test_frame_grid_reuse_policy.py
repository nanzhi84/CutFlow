"""Non-failed resume shapes re-run the frame-grid planning nodes.

The window-authority refactor deliberately does NOT version every artifact schema or bump
all node versions. That is only safe because TimelineWindowPlanning /
WindowQueryPlanning / WindowMaterialRetrieval / DeterministicEditingPlanning /
TimelinePlanning all carry ``reuse_policy="never"``: when deriving from a completed
run, their old artifacts are discarded and the nodes re-run, so a run cannot resume
onto a pre-window-contract B-roll plan. Failed-run continuation bypasses this policy
until the actual failed node so LipSync/provider failures resume at the failed node.
This test pins that the policy marker remains on those nodes.
"""

from __future__ import annotations

from packages.production.pipeline.digital_human import digital_human_template


def test_frame_grid_planning_nodes_never_reuse_on_resume():
    template = digital_human_template()
    by_id = {node.node_id: node for node in template.nodes}
    for node_id in (
        "TimelineWindowPlanning",
        "WindowQueryPlanning",
        "WindowMaterialRetrieval",
        "DeterministicEditingPlanning",
        "TimelinePlanning",
    ):
        assert node_id in by_id, f"{node_id} missing from digital_human_v2 template"
        assert by_id[node_id].reuse_policy == "never", (
            f"{node_id} must re-run on resume (B-roll window authority moved upstream, "
            "no schema versioning) — reuse_policy must stay 'never'"
        )
