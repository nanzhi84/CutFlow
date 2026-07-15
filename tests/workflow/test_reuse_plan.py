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
    version: str = "v1",
    outputs: list[c.ArtifactKind] | None = None,
    side_effects: list[str] | None = None,
    idempotency_key: str | None = None,
) -> c.NodeSpec:
    return c.NodeSpec(
        node_id=node_id,
        node_version=version,
        output_artifact_kinds=outputs or [c.ArtifactKind.run_report_debug],
        side_effects=side_effects or [],
        idempotency_key=idempotency_key,
    )


def _run(node_runs: list[c.NodeRun]) -> ReuseSourceRun:
    return ReuseSourceRun(
        run=c.WorkflowRun(
            id="run_source",
            job_id="job_1",
            workflow_template_id="test",
            workflow_version="v1",
            status=c.RunStatus.succeeded,
        ),
        node_runs=node_runs,
    )


def _node_run(
    node_id: str,
    artifact_id: str,
    *,
    version: str = "v1",
    input_hash: str = "same",
    status: c.NodeStatus = c.NodeStatus.succeeded,
) -> c.NodeRun:
    return c.NodeRun(
        id=f"nr_{node_id}",
        run_id="run_source",
        node_id=node_id,
        node_version=version,
        status=status,
        input_manifest_hash=input_hash,
        output_artifact_ids=[artifact_id],
    )


def _artifact(
    artifact_id: str,
    kind: c.ArtifactKind,
    path: Path,
    *,
    sha256: str | None = None,
    schema_version: str = "v1",
) -> c.Artifact:
    digest = sha256 or hashlib.sha256(path.read_bytes()).hexdigest()
    return c.Artifact(
        id=artifact_id,
        run_id="run_source",
        kind=kind,
        uri=path.as_uri(),
        local_path=str(path),
        sha256=digest,
        payload_schema=f"{kind.value}.payload.v1",
        schema_version=schema_version,
    )


def test_complete_prefix_is_reused(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    template = _template(_node("A"), _node("B"))
    source = _run([_node_run("A", "art_a"), _node_run("B", "art_b")])
    artifacts = {
        "art_a": _artifact("art_a", c.ArtifactKind.run_report_debug, first),
        "art_b": _artifact("art_b", c.ArtifactKind.run_report_debug, second),
    }

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == ["A", "B"]
    assert plan.rerun_from_node_id is None


def test_missing_middle_artifact_reruns_from_that_node_and_keeps_suffix_unreused(tmp_path):
    first = tmp_path / "first.json"
    third = tmp_path / "third.json"
    first.write_text("first", encoding="utf-8")
    third.write_text("third", encoding="utf-8")
    template = _template(_node("A"), _node("B"), _node("C"))
    source = _run(
        [
            _node_run("A", "art_a"),
            _node_run("B", "art_b"),
            _node_run("C", "art_c"),
        ]
    )
    artifacts = {
        "art_a": _artifact("art_a", c.ArtifactKind.run_report_debug, first),
        "art_c": _artifact("art_c", c.ArtifactKind.run_report_debug, third),
    }

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == ["A"]
    assert plan.rerun_from_node_id == "B"
    assert "C" not in plan.reused_node_ids
    assert plan.decisions[1].reason == "artifact_missing"


def test_sha256_mismatch_stops_reuse_at_that_node(tmp_path):
    path = tmp_path / "artifact.json"
    path.write_text("actual", encoding="utf-8")
    template = _template(_node("A"))
    source = _run([_node_run("A", "art_a")])
    artifacts = {
        "art_a": _artifact("art_a", c.ArtifactKind.run_report_debug, path, sha256="bad")
    }

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == []
    assert plan.rerun_from_node_id == "A"
    assert plan.decisions[0].reason == "sha256_mismatch"


def test_node_version_change_stops_reuse(tmp_path):
    path = tmp_path / "artifact.json"
    path.write_text("payload", encoding="utf-8")
    template = _template(_node("A", version="v2"))
    source = _run([_node_run("A", "art_a", version="v1")])
    artifacts = {"art_a": _artifact("art_a", c.ArtifactKind.run_report_debug, path)}

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == []
    assert plan.rerun_from_node_id == "A"
    assert plan.decisions[0].reason == "node_version_mismatch"


def test_provider_profile_change_via_input_hash_change_stops_reuse(tmp_path):
    path = tmp_path / "artifact.json"
    path.write_text("payload", encoding="utf-8")
    template = _template(_node("TTS"))
    source = _run([_node_run("TTS", "art_tts", input_hash="old-profile-hash")])
    source.expected_input_manifest_hashes["TTS"] = "new-profile-hash"
    artifacts = {"art_tts": _artifact("art_tts", c.ArtifactKind.run_report_debug, path)}

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == []
    assert plan.rerun_from_node_id == "TTS"
    assert plan.decisions[0].reason == "input_manifest_hash_mismatch"


def test_side_effect_node_without_idempotency_key_is_not_reused(tmp_path):
    path = tmp_path / "artifact.json"
    path.write_text("payload", encoding="utf-8")
    template = _template(_node("TTS", side_effects=["provider_call"]))
    source = _run([_node_run("TTS", "art_tts")])
    artifacts = {"art_tts": _artifact("art_tts", c.ArtifactKind.run_report_debug, path)}

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == []
    assert plan.rerun_from_node_id == "TTS"
    assert plan.decisions[0].reason == "side_effect_not_reusable"
