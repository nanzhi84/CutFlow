"""Canonical node graph for the digital_human production workflows.

Kept dependency-free so progress / UI / reporting code can read the *expected*
node count, the node order, and the dependency edges for a template without
importing the heavy pipeline engine (ffmpeg, providers, every node handler).
``digital_human.py`` re-exports ``NODE_SEQUENCE`` from here so there is a single
source of truth.

Each workflow is a dependency DAG (``WORKFLOW_GRAPHS``): nodes plus the edges that
say which upstream node must finish before a node may run. The shipping
templates are linear chains (edge i -> i+1), so their behaviour is unchanged, but
the runtime schedules from the *edges* via ``topological_node_order`` — a
dependency ready-set, not a hard-coded list — so a non-linear template runs its
independent nodes in a valid dependency order. ``ready_nodes`` exposes the set of
nodes whose upstreams are all complete (the seam a future parallel scheduler fills;
#137).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

NODE_SEQUENCE = [
    "ValidateRequest",
    "LoadCaseContext",
    "ResolveCreativeIntent",
    "TTS",
    "MaterialPackPlanning",
    "NarrationAlignment",
    "NarrationBoundaryPlanning",
    "TimelineWindowPlanning",
    "WindowQueryPlanning",
    "WindowMaterialRetrieval",
    "DeterministicEditingPlanning",
    "TimelinePlanning",
    "PortraitTrackBuild",
    "LipSync",
    "RenderFinalTimeline",
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

# Same digital-human chain as NODE_SEQUENCE, but the deterministic portrait / B-roll /
# style assignment stage is replaced by the single LLM EditingAgentPlanning node
# (issue #136/#160). Upstream order matches v2 so EditingAgentPlanning sees the same
# MaterialPack + NarrationBoundary + per-window retrieval inputs.
EDITING_AGENT_SEQUENCE = [
    "ValidateRequest",
    "LoadCaseContext",
    "ResolveCreativeIntent",
    "TTS",
    "MaterialPackPlanning",
    "NarrationAlignment",
    "NarrationBoundaryPlanning",
    "TimelineWindowPlanning",
    "WindowQueryPlanning",
    "WindowMaterialRetrieval",
    "EditingAgentPlanning",
    "TimelinePlanning",
    "PortraitTrackBuild",
    "LipSync",
    "RenderFinalTimeline",
    "SubtitleAndBgmMix",
    "ExportFinishedVideo",
    "FinalizeRunReport",
]

# Expected total node count per workflow template id. Used to render run progress
# as completed / total across the *whole* pipeline (node runs are created lazily,
# so the count of existing node runs is not the denominator).
WORKFLOW_TEMPLATE_NODE_COUNTS = {
    "digital_human_v2": len(NODE_SEQUENCE),
    "seedance_t2v_v1": len(SEEDANCE_T2V_SEQUENCE),
    "digital_human_editing_agent_v1": len(EDITING_AGENT_SEQUENCE),
}


def _linear_edges(sequence: Sequence[str]) -> list[tuple[str, str]]:
    """Chain edges for a linear template: node i must finish before node i+1."""
    return [(sequence[index], sequence[index + 1]) for index in range(len(sequence) - 1)]


# Dependency DAG per template. The shipping templates are linear chains, expressed
# as a graph so the runtime can schedule from edges (and a future template can add
# non-linear edges without touching the runtime). ``edges`` are (from_node, to_node):
# ``from_node`` must complete before ``to_node`` becomes ready.
WORKFLOW_GRAPHS: dict[str, dict[str, list]] = {
    "digital_human_v2": {"nodes": list(NODE_SEQUENCE), "edges": _linear_edges(NODE_SEQUENCE)},
    "seedance_t2v_v1": {
        "nodes": list(SEEDANCE_T2V_SEQUENCE),
        "edges": _linear_edges(SEEDANCE_T2V_SEQUENCE),
    },
    "digital_human_editing_agent_v1": {
        "nodes": list(EDITING_AGENT_SEQUENCE),
        "edges": _linear_edges(EDITING_AGENT_SEQUENCE),
    },
}


def workflow_graph(workflow_template_id: str | None) -> dict[str, list] | None:
    """The nodes+edges graph for a template id, or None when unknown."""
    return WORKFLOW_GRAPHS.get(workflow_template_id or "")


def expected_node_count(workflow_template_id: str | None) -> int:
    """Total nodes for a template id; 0 when unknown (caller falls back)."""
    return WORKFLOW_TEMPLATE_NODE_COUNTS.get(workflow_template_id or "", 0)


def validate_graph_structure(
    nodes: Sequence[str], edges: Iterable[tuple[str, str]]
) -> None:
    """Reject a malformed workflow graph: duplicate node, dangling edge, or a cycle.

    Raises ``ValueError`` with a specific message. A DAG that passes here has a valid
    topological order (``topological_node_order`` will not raise on it).
    """
    seen: set[str] = set()
    for node in nodes:
        if node in seen:
            raise ValueError(f"duplicate node in workflow graph: {node!r}")
        seen.add(node)
    for from_node, to_node in edges:
        if from_node not in seen:
            raise ValueError(f"workflow edge from unknown node: {from_node!r}")
        if to_node not in seen:
            raise ValueError(f"workflow edge to unknown node: {to_node!r}")
    ordered = _kahn_order(nodes, edges)
    if len(ordered) != len(seen):
        stuck = [node for node in nodes if node not in set(ordered)]
        raise ValueError(f"workflow graph has a dependency cycle among: {stuck}")


def _kahn_order(
    nodes: Sequence[str], edges: Iterable[tuple[str, str]]
) -> list[str]:
    """Kahn topological sort with a STABLE tiebreak by original node position.

    Ready nodes are always visited in ``nodes`` declaration order, so the result is
    deterministic (never dependent on set/dict iteration order — a hard requirement
    for Temporal determinism, #137). A linear chain sorts back to its own sequence.
    Returns a partial order (shorter than ``nodes``) when a cycle blocks progress.
    """
    position = {node: index for index, node in enumerate(nodes)}
    indegree = {node: 0 for node in nodes}
    successors: dict[str, list[str]] = {node: [] for node in nodes}
    for from_node, to_node in edges:
        if from_node in indegree and to_node in indegree:
            successors[from_node].append(to_node)
            indegree[to_node] += 1
    ready = sorted((node for node in nodes if indegree[node] == 0), key=position.get)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        newly_ready = []
        for successor in successors[node]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                newly_ready.append(successor)
        if newly_ready:
            ready.extend(newly_ready)
            ready.sort(key=position.get)
    return order


def topological_node_order(
    nodes: Sequence[str], edges: Iterable[tuple[str, str]]
) -> list[str]:
    """Deterministic dependency order for the graph (raises on a cycle).

    For a linear template this returns the original sequence, so routing the runtime
    through this instead of the raw node list is behaviour-preserving.
    """
    edge_list = list(edges)
    order = _kahn_order(nodes, edge_list)
    if len(order) != len(list(nodes)):
        raise ValueError("workflow graph has a dependency cycle")
    return order


def ready_nodes(
    nodes: Sequence[str], edges: Iterable[tuple[str, str]], completed: Iterable[str]
) -> list[str]:
    """Nodes not yet done whose every upstream dependency is complete.

    Returned in ``nodes`` declaration order (stable). With single-threaded execution
    the runtime takes the first; a parallel scheduler could dispatch the whole set.
    """
    done = set(completed)
    dependencies: dict[str, set[str]] = {node: set() for node in nodes}
    for from_node, to_node in edges:
        if to_node in dependencies:
            dependencies[to_node].add(from_node)
    return [
        node
        for node in nodes
        if node not in done and dependencies[node] <= done
    ]
