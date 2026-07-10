from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, Field

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
    WorkflowTemplate,
)


REUSABLE_NODE_STATUSES = {NodeStatus.succeeded, NodeStatus.degraded, NodeStatus.skipped}


class ReuseSourceRun(BaseModel):
    run: WorkflowRun
    node_runs: list[NodeRun]
    expected_input_manifest_hashes: dict[str, str] = Field(default_factory=dict)


class ReuseDecision(BaseModel):
    node_id: str
    reusable: bool
    reason: str
    artifact_ids: list[str] = Field(default_factory=list)


class ReusePlan(BaseModel):
    source_run_id: str
    reused_node_ids: list[str] = Field(default_factory=list)
    rerun_from_node_id: str | None = None
    decisions: list[ReuseDecision] = Field(default_factory=list)

    @property
    def reused_count(self) -> int:
        return len(self.reused_node_ids)


def _file_digest(artifact: Artifact) -> str | None:
    path_value = artifact.local_path
    if path_value is None and artifact.uri and artifact.uri.startswith("file://"):
        path_value = artifact.uri.removeprefix("file://")
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_schema_version(template_node, kind: ArtifactKind) -> str:
    return template_node.output_artifact_schema_versions.get(kind, "v1")


def compute_reuse_plan(
    source_run: ReuseSourceRun,
    template: WorkflowTemplate,
    artifacts: Mapping[str, Artifact],
) -> ReusePlan:
    plan = ReusePlan(source_run_id=source_run.run.id)
    previous_by_node = {node.node_id: node for node in source_run.node_runs}
    template_by_node = {node.node_id: node for node in template.nodes}
    failed_resume_anchor = _failed_resume_anchor(source_run, template, previous_by_node)

    for template_node in template.nodes:
        if template_node.reuse_policy == "never" and failed_resume_anchor is None:
            return _stop(plan, template_node.node_id, "reuse_policy_forces_rerun")
        previous_node = previous_by_node.get(template_node.node_id)
        if previous_node is None:
            return _stop(plan, template_node.node_id, "node_run_missing")
        if previous_node.status not in REUSABLE_NODE_STATUSES:
            return _stop(plan, template_node.node_id, "node_status_not_reusable")
        if previous_node.node_version != template_node.node_version:
            return _stop(plan, template_node.node_id, "node_version_mismatch")
        expected_input_hash = source_run.expected_input_manifest_hashes.get(template_node.node_id)
        if expected_input_hash is not None and expected_input_hash != previous_node.input_manifest_hash:
            return _stop(plan, template_node.node_id, "input_manifest_hash_mismatch")
        if template_node.side_effects and template_node.idempotency_key is None:
            return _stop(plan, template_node.node_id, "side_effect_not_reusable")

        output_artifacts: list[Artifact] = []
        for artifact_id in previous_node.output_artifact_ids:
            artifact = artifacts.get(artifact_id)
            if artifact is None:
                return _stop(plan, template_node.node_id, "artifact_missing", previous_node.output_artifact_ids)
            if artifact.kind not in template_node.output_artifact_kinds:
                return _stop(plan, template_node.node_id, "artifact_kind_mismatch", previous_node.output_artifact_ids)
            if artifact.schema_version != _expected_schema_version(template_node, artifact.kind):
                return _stop(
                    plan,
                    template_node.node_id,
                    "artifact_schema_version_mismatch",
                    previous_node.output_artifact_ids,
                )
            digest = _file_digest(artifact)
            if digest == "":
                return _stop(plan, template_node.node_id, "artifact_file_missing", previous_node.output_artifact_ids)
            if digest is not None and artifact.sha256 is not None and digest != artifact.sha256:
                return _stop(plan, template_node.node_id, "sha256_mismatch", previous_node.output_artifact_ids)
            output_artifacts.append(artifact)

        if not output_artifacts and template_node.output_artifact_kinds:
            return _stop(plan, template_node.node_id, "artifact_missing")
        plan.reused_node_ids.append(template_node.node_id)
        plan.decisions.append(
            ReuseDecision(
                node_id=template_node.node_id,
                reusable=True,
                reason="reused",
                artifact_ids=list(previous_node.output_artifact_ids),
            )
        )

    for previous_node in source_run.node_runs:
        if previous_node.node_id not in template_by_node and plan.rerun_from_node_id is None:
            return _stop(plan, previous_node.node_id, "node_not_in_template")
    return plan


def _failed_resume_anchor(
    source_run: ReuseSourceRun,
    template: WorkflowTemplate,
    previous_by_node: Mapping[str, NodeRun],
) -> str | None:
    """First failed template node for a failed source run.

    A failed-run resume is an operator continuation, so successful prefix nodes
    should remain reusable even when their template spec carries
    ``reuse_policy="never"`` for other resume shapes. Artifact/schema/hash checks
    below still apply; only the policy break is bypassed until the actual failed
    node stops the plan.
    """
    if source_run.run.status != RunStatus.failed:
        return None
    for template_node in template.nodes:
        previous_node = previous_by_node.get(template_node.node_id)
        if previous_node is not None and previous_node.status == NodeStatus.failed:
            return template_node.node_id
    return None


def _stop(
    plan: ReusePlan,
    node_id: str,
    reason: str,
    artifact_ids: list[str] | None = None,
) -> ReusePlan:
    plan.rerun_from_node_id = node_id
    plan.decisions.append(
        ReuseDecision(
            node_id=node_id,
            reusable=False,
            reason=reason,
            artifact_ids=artifact_ids or [],
        )
    )
    return plan
