"""Workflow templates are dependency DAGs, scheduled by a topological ready-set (#137).

These tests prove the Phase-1/2 slice: the registered shipping templates keep their exact
linear behaviour when expressed as a graph, the pure scheduler orders nodes by their
edges (not by list position) and exposes the ready-set of independent nodes, graph
validation rejects cycles / unknown nodes / duplicates, template validation rejects a
node with no handler or no declared outputs, and the local runtime schedules from the
edges (a non-linear template runs in dependency order).
"""

from __future__ import annotations

import pytest

from packages.core.contracts import (
    NodeSpec,
    RunStatus,
    WorkflowEdge,
    WorkflowRun,
    WorkflowTemplate,
)
from packages.production.pipeline import digital_human as dh
from packages.production.pipeline import node_sequence as ns
from packages.production.pipeline.node_sequence import (
    EDITING_AGENT_SEQUENCE,
    EDITING_AGENT_V2_SEQUENCE,
    NODE_SEQUENCE,
    SEEDANCE_T2V_SEQUENCE,
    ready_nodes,
    topological_node_order,
    validate_graph_structure,
    workflow_graph,
)


REGISTERED_TEMPLATE_IDS = tuple(ns.WORKFLOW_GRAPHS.keys())


# --- Registered shipping templates stay linear (behaviour-preserving) ------------------


@pytest.mark.parametrize(
    "sequence",
    [NODE_SEQUENCE, SEEDANCE_T2V_SEQUENCE, EDITING_AGENT_SEQUENCE, EDITING_AGENT_V2_SEQUENCE],
)
def test_linear_template_topological_order_equals_its_sequence(sequence):
    edges = ns._linear_edges(sequence)
    assert topological_node_order(sequence, edges) == list(sequence)


@pytest.mark.parametrize("template_id", REGISTERED_TEMPLATE_IDS)
def test_registered_graph_edges_form_a_linear_chain(template_id):
    # Independent oracle (not `== _linear_edges(...)`, which would just recompute the
    # definition): the edges must be exactly a chain node[i] -> node[i+1].
    graph = workflow_graph(template_id)
    assert graph is not None
    nodes, edges = graph["nodes"], graph["edges"]
    assert len(edges) == len(nodes) - 1
    assert [from_node for from_node, _ in edges] == nodes[:-1]
    assert [to_node for _, to_node in edges] == nodes[1:]
    for (_, prev_to), (next_from, _) in zip(edges, edges[1:]):
        assert prev_to == next_from


def test_template_registries_are_in_sync():
    assert set(ns.WORKFLOW_GRAPHS) == set(ns.WORKFLOW_TEMPLATE_NODE_COUNTS)
    assert set(ns.WORKFLOW_GRAPHS) == set(dh._TEMPLATE_BUILDERS)


def test_workflow_graph_unknown_template_is_none():
    assert workflow_graph("does_not_exist") is None
    assert workflow_graph(None) is None


# --- Pure scheduler orders by edges, not by list position ------------------------------


DIAMOND_NODES = ["A", "B", "C", "D"]
DIAMOND_EDGES = [("A", "B"), ("A", "C"), ("B", "D"), ("C", "D")]


def test_diamond_topological_order_respects_dependencies():
    order = topological_node_order(DIAMOND_NODES, DIAMOND_EDGES)
    # A first, D last; B and C after A and before D — the exact interleave is the stable
    # declaration-order tiebreak.
    assert order[0] == "A"
    assert order[-1] == "D"
    assert order.index("B") > order.index("A")
    assert order.index("C") > order.index("A")
    assert order.index("D") > order.index("B")
    assert order.index("D") > order.index("C")


def test_topological_order_comes_from_edges_not_node_list_order():
    # Nodes listed in reverse; the order still obeys the edges (proves it is not just
    # echoing the node list).
    order = topological_node_order(["D", "C", "B", "A"], DIAMOND_EDGES)
    assert order[0] == "A"
    assert order[-1] == "D"
    assert order.index("B") > order.index("A")


def test_ready_nodes_exposes_independent_parallelizable_set():
    # After only A completes, B and C are BOTH ready (no dependency between them) — the
    # seam a parallel scheduler dispatches together.
    assert ready_nodes(DIAMOND_NODES, DIAMOND_EDGES, {"A"}) == ["B", "C"]
    # D is not ready until both B and C are done.
    assert ready_nodes(DIAMOND_NODES, DIAMOND_EDGES, {"A", "B"}) == ["C"]
    assert ready_nodes(DIAMOND_NODES, DIAMOND_EDGES, {"A", "B", "C"}) == ["D"]
    assert ready_nodes(DIAMOND_NODES, DIAMOND_EDGES, {"A", "B", "C", "D"}) == []


def test_ready_nodes_and_topological_order_are_deterministic():
    # Same inputs -> same output every call (no set/dict iteration nondeterminism).
    for _ in range(5):
        assert topological_node_order(DIAMOND_NODES, DIAMOND_EDGES) == ["A", "B", "C", "D"]
        assert ready_nodes(DIAMOND_NODES, DIAMOND_EDGES, {"A"}) == ["B", "C"]


def test_empty_graph_is_valid_and_orders_to_empty():
    validate_graph_structure([], [])  # no raise
    assert topological_node_order([], []) == []
    assert ready_nodes([], [], set()) == []


def test_isolated_node_with_no_edges_is_ordered_and_immediately_ready():
    # A node incident to no edge must still appear in the order and be ready from the start
    # (a regression that dropped edge-less nodes would corrupt the run).
    nodes = ["A", "B", "C"]
    edges = [("A", "B")]  # C is isolated
    assert topological_node_order(nodes, edges) == ["A", "B", "C"]
    assert ready_nodes(nodes, edges, set()) == ["A", "C"]
    validate_graph_structure(nodes, edges)  # no raise


# --- Graph validation ------------------------------------------------------------------


def test_validate_graph_structure_accepts_a_dag():
    validate_graph_structure(DIAMOND_NODES, DIAMOND_EDGES)  # no raise


def test_validate_graph_structure_detects_cycle():
    with pytest.raises(ValueError, match="cycle"):
        validate_graph_structure(["A", "B", "C"], [("A", "B"), ("B", "C"), ("C", "A")])


def test_validate_graph_structure_detects_unknown_edge_endpoint():
    with pytest.raises(ValueError, match="unknown node"):
        validate_graph_structure(["A", "B"], [("A", "Z")])
    with pytest.raises(ValueError, match="unknown node"):
        validate_graph_structure(["A", "B"], [("Z", "B")])


def test_validate_graph_structure_detects_duplicate_node():
    with pytest.raises(ValueError, match="duplicate node"):
        validate_graph_structure(["A", "A", "B"], [("A", "B")])


def test_topological_order_raises_on_cycle():
    with pytest.raises(ValueError, match="cycle"):
        topological_node_order(["A", "B"], [("A", "B"), ("B", "A")])


# --- Template-level validation (handler + declared outputs) ----------------------------


def _spec(node_id: str) -> NodeSpec:
    return NodeSpec(node_id=node_id, input_schema=f"{node_id}.input.v1", output_artifact_kinds=[])


def test_shipping_templates_pass_validation():
    # template_for() builds + validates; unchanged templates must not raise.
    for template_id in REGISTERED_TEMPLATE_IDS:
        dh.template_for(template_id)  # no raise


@pytest.mark.parametrize("template_id", REGISTERED_TEMPLATE_IDS)
def test_build_template_stores_nodes_in_topological_order(template_id):
    # _build_template must emit template.nodes already in dependency order, so the Temporal
    # payload and the reuse planner (which iterate template.nodes, NOT the local runtime's
    # topo re-derivation) agree with the local runtime's execution order (#137).
    template = dh.template_for(template_id)
    node_ids = [spec.node_id for spec in template.nodes]
    edges = [(edge.from_node_id, edge.to_node_id) for edge in template.edges]
    assert node_ids == topological_node_order(node_ids, edges)


@pytest.mark.parametrize("template_id", REGISTERED_TEMPLATE_IDS)
def test_template_node_count_matches_graph_registry(template_id):
    template = dh.template_for(template_id)
    assert len(template.nodes) == ns.expected_node_count(template_id)
    assert len(template.nodes) == len(ns.WORKFLOW_GRAPHS[template_id]["nodes"])


@pytest.mark.parametrize("template_id", REGISTERED_TEMPLATE_IDS)
def test_all_template_nodes_have_handlers_and_declared_outputs(template_id):
    template = dh.template_for(template_id)
    for spec in template.nodes:
        assert spec.node_id in dh.NODE_HANDLERS
        assert spec.node_id in dh._NODE_OUTPUT_KINDS
        assert spec.output_artifact_kinds == dh._NODE_OUTPUT_KINDS[spec.node_id]
        assert spec.output_artifact_kinds


@pytest.mark.parametrize("template_id", REGISTERED_TEMPLATE_IDS)
def test_provider_side_effect_nodes_have_idempotency_keys(template_id):
    template = dh.template_for(template_id)
    for spec in template.nodes:
        if spec.node_id in dh._PROVIDER_SIDE_EFFECT_NODES:
            assert "provider_call" in spec.side_effects
            assert spec.idempotency_key == f"{template_id}:{spec.node_id}:{{input_manifest_hash}}"


@pytest.mark.parametrize("template_id", REGISTERED_TEMPLATE_IDS)
def test_timeline_replanning_break_nodes_never_reuse(template_id):
    template = dh.template_for(template_id)
    for spec in template.nodes:
        if spec.node_id in dh._TIMELINE_REUSE_BREAK_NODES:
            assert spec.reuse_policy == "never"


def test_editing_agent_template_replaces_legacy_planning_nodes():
    template = dh.template_for("digital_human_editing_agent_v1")
    node_ids = [spec.node_id for spec in template.nodes]
    assert node_ids == EDITING_AGENT_SEQUENCE
    assert "EditingAgentPlanning" in node_ids
    assert "PortraitPlanning" not in node_ids
    assert "BrollPlanning" not in node_ids
    assert "StylePlanning" not in node_ids


def test_editing_agent_v2_separates_media_and_postprocess_planning():
    template = dh.template_for("digital_human_editing_agent_v2")
    node_ids = [spec.node_id for spec in template.nodes]

    assert node_ids == EDITING_AGENT_V2_SEQUENCE
    assert "EditingAgentPlanning" not in node_ids
    assert node_ids[node_ids.index("WindowMaterialRetrieval") + 1] == (
        "MediaSelectionAgentPlanning"
    )
    assert node_ids[node_ids.index("RenderFinalTimeline") + 1 :][:3] == [
        "CaptionWindowPlanning",
        "PostProcessAgentPlanning",
        "SubtitleAndBgmMix",
    ]
    by_id = {spec.node_id: spec for spec in template.nodes}
    assert by_id["MediaSelectionAgentPlanning"].reuse_policy == "never"
    assert by_id["CaptionWindowPlanning"].reuse_policy == "strict"
    assert by_id["PostProcessAgentPlanning"].reuse_policy == "strict"
    assert "provider_call" in by_id["MediaSelectionAgentPlanning"].side_effects
    assert "provider_call" in by_id["PostProcessAgentPlanning"].side_effects


def test_validate_template_rejects_node_without_handler():
    template = WorkflowTemplate(
        workflow_template_id="bad",
        version="v1",
        nodes=[_spec("ValidateRequest"), _spec("NoSuchNode")],
        edges=[WorkflowEdge(from_node_id="ValidateRequest", to_node_id="NoSuchNode")],
    )
    with pytest.raises(ValueError, match="no registered handler"):
        dh._validate_workflow_template(template)


def test_validate_template_accepts_a_node_with_handler_and_outputs():
    template = WorkflowTemplate(
        workflow_template_id="ok",
        version="v1",
        nodes=[_spec("ValidateRequest")],
        edges=[],
    )
    dh._validate_workflow_template(template)  # no raise: handler + outputs both declared


def test_validate_template_rejects_node_without_declared_outputs(monkeypatch):
    # A node that HAS a handler but is missing from _NODE_OUTPUT_KINDS must be rejected
    # (drop TTS's declared outputs so the handler check passes and the output check fires).
    monkeypatch.delitem(dh._NODE_OUTPUT_KINDS, "TTS")
    template = WorkflowTemplate(
        workflow_template_id="bad",
        version="v1",
        nodes=[_spec("ValidateRequest"), _spec("TTS")],
        edges=[WorkflowEdge(from_node_id="ValidateRequest", to_node_id="TTS")],
    )
    with pytest.raises(ValueError, match="no output artifact kinds|declares no output"):
        dh._validate_workflow_template(template)


# --- Local runtime schedules from the edges --------------------------------------------


def _diamond_template() -> WorkflowTemplate:
    # Nodes intentionally listed out of dependency order to prove the runtime derives the
    # order from edges, not from the node list.
    return WorkflowTemplate(
        workflow_template_id="diamond_test",
        version="v1",
        nodes=[_spec("D"), _spec("B"), _spec("C"), _spec("A")],
        edges=[
            WorkflowEdge(from_node_id="A", to_node_id="B"),
            WorkflowEdge(from_node_id="A", to_node_id="C"),
            WorkflowEdge(from_node_id="B", to_node_id="D"),
            WorkflowEdge(from_node_id="C", to_node_id="D"),
        ],
    )


def test_local_runtime_sequence_follows_dependency_order(monkeypatch):
    adapter = object.__new__(dh.LocalRuntimeAdapter)
    monkeypatch.setattr(adapter, "_template_for_run", lambda run: _diamond_template())
    run = WorkflowRun(
        id="run_diamond",
        job_id="job_diamond",
        case_id="case_demo",
        workflow_template_id="diamond_test",
        workflow_version="v1",
        status=RunStatus.running,
    )
    order = adapter._sequence_for_run(run)
    # A runs first, D last, and each middle node after A — dependency order, NOT the
    # scrambled [D, B, C, A] node-list order.
    assert order[0] == "A"
    assert order[-1] == "D"
    assert order.index("B") > 0 and order.index("C") > 0
    assert order != ["D", "B", "C", "A"]
