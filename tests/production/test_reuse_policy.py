from __future__ import annotations

import hashlib
from pathlib import Path

from packages.core import contracts as c
from packages.production.pipeline.reuse import ReuseSourceRun, compute_reuse_plan


def _template(*nodes: c.NodeSpec) -> c.WorkflowTemplate:
    return c.WorkflowTemplate(workflow_template_id="test", version="v1", nodes=list(nodes))


def _node(
    node_id: str,
    *,
    reuse_policy: str | None = None,
) -> c.NodeSpec:
    kwargs = {"reuse_policy": reuse_policy} if reuse_policy is not None else {}
    return c.NodeSpec(
        node_id=node_id,
        input_schema=f"{node_id}.input.v1",
        output_artifact_kinds=[c.ArtifactKind.run_report_debug],
        **kwargs,
    )


def _run(
    node_runs: list[c.NodeRun],
    *,
    status: c.RunStatus = c.RunStatus.succeeded,
) -> ReuseSourceRun:
    return ReuseSourceRun(
        run=c.WorkflowRun(
            id="run_source",
            job_id="job_1",
            workflow_template_id="test",
            workflow_version="v1",
            status=status,
        ),
        node_runs=node_runs,
    )


def _node_run(
    node_id: str,
    artifact_id: str,
    *,
    status: c.NodeStatus = c.NodeStatus.succeeded,
) -> c.NodeRun:
    error = None
    if status == c.NodeStatus.failed:
        error = c.NodeError(
            code=c.ErrorCode.provider_timeout,
            message="transient provider failure",
            retryable=True,
        )
    return c.NodeRun(
        id=f"nr_{node_id}",
        run_id="run_source",
        node_id=node_id,
        node_version="v1",
        status=status,
        input_manifest_hash="same",
        output_artifact_ids=[] if status == c.NodeStatus.failed else [artifact_id],
        error=error,
    )


def _artifact(artifact_id: str, path: Path) -> c.Artifact:
    return c.Artifact(
        id=artifact_id,
        run_id="run_source",
        kind=c.ArtifactKind.run_report_debug,
        uri=path.as_uri(),
        local_path=str(path),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        payload_schema="run_report_debug.payload.v1",
        schema_version="v1",
    )


def test_reuse_policy_never_forces_rerun_from_that_node(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    template = _template(_node("A"), _node("B", reuse_policy="never"))
    source = _run([_node_run("A", "art_a"), _node_run("B", "art_b")])
    artifacts = {
        "art_a": _artifact("art_a", first),
        "art_b": _artifact("art_b", second),
    }

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == ["A"]
    assert plan.rerun_from_node_id == "B"
    assert "B" not in plan.reused_node_ids
    assert plan.decisions[1].reason == "reuse_policy_forces_rerun"


def test_failed_run_resume_bypasses_never_policy_until_failed_node(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    template = _template(
        _node("A"),
        _node("NarrationBoundaryPlanning", reuse_policy="never"),
        _node("LipSync"),
    )
    source = _run(
        [
            _node_run("A", "art_a"),
            _node_run("NarrationBoundaryPlanning", "art_boundary"),
            _node_run("LipSync", "art_lipsync", status=c.NodeStatus.failed),
        ],
        status=c.RunStatus.failed,
    )
    artifacts = {
        "art_a": _artifact("art_a", first),
        "art_boundary": _artifact("art_boundary", second),
    }

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == ["A", "NarrationBoundaryPlanning"]
    assert plan.rerun_from_node_id == "LipSync"
    assert plan.decisions[-1].reason == "node_status_not_reusable"


def test_default_strict_reuses_completed_nodes(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    template = _template(_node("A"), _node("B"))
    source = _run([_node_run("A", "art_a"), _node_run("B", "art_b")])
    artifacts = {
        "art_a": _artifact("art_a", first),
        "art_b": _artifact("art_b", second),
    }

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == ["A", "B"]
    assert plan.rerun_from_node_id is None


def test_node_spec_exposes_only_the_consumed_reuse_shape():
    """Guard: the live reuse contract is reuse_policy/side_effects/idempotency_key.

    The dead ``resume_policy``/``ResumePolicy`` shape was never consumed and was
    removed; this asserts it stays gone and the consumed fields stay present.
    """
    fields = c.NodeSpec.model_fields
    assert "resume_policy" not in fields
    assert "reuse_policy" in fields
    assert "side_effects" in fields
    assert "idempotency_key" in fields
    assert not hasattr(c, "ResumePolicy")
