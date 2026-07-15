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
    outputs: list[c.ArtifactKind] | None = None,
) -> c.NodeSpec:
    kwargs = {"reuse_policy": reuse_policy} if reuse_policy is not None else {}
    return c.NodeSpec(
        node_id=node_id,
        output_artifact_kinds=outputs or [c.ArtifactKind.run_report_debug],
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


def test_failed_resume_reuses_historical_timeline_node_under_new_name(tmp_path):
    timeline = tmp_path / "timeline.json"
    timeline.write_text("timeline", encoding="utf-8")
    template = _template(
        _node("TimelineAssemblyValidation", reuse_policy="never"),
        _node("LipSync"),
    )
    source = _run(
        [
            _node_run("TimelinePlanning", "art_timeline"),
            _node_run("LipSync", "art_lipsync", status=c.NodeStatus.failed),
        ],
        status=c.RunStatus.failed,
    )
    artifacts = {"art_timeline": _artifact("art_timeline", timeline)}

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == ["TimelineAssemblyValidation"]
    assert plan.rerun_from_node_id == "LipSync"


def test_failed_resume_drops_legacy_window_portrait_double_write(tmp_path):
    windows_path = tmp_path / "windows.json"
    portrait_path = tmp_path / "portrait.json"
    windows_path.write_text("windows", encoding="utf-8")
    portrait_path.write_text("portrait", encoding="utf-8")
    template = _template(
        _node(
            "TimelineWindowPlanning",
            reuse_policy="never",
            outputs=[c.ArtifactKind.plan_timeline_windows],
        ),
        _node("LipSync"),
    )
    timeline_window_run = _node_run("TimelineWindowPlanning", "art_windows")
    timeline_window_run = timeline_window_run.model_copy(
        update={"output_artifact_ids": ["art_windows", "art_legacy_portrait"]}
    )
    source = _run(
        [
            timeline_window_run,
            _node_run("LipSync", "art_lipsync", status=c.NodeStatus.failed),
        ],
        status=c.RunStatus.failed,
    )
    artifacts = {
        "art_windows": c.Artifact(
            id="art_windows",
            run_id="run_source",
            kind=c.ArtifactKind.plan_timeline_windows,
            uri=windows_path.as_uri(),
            local_path=str(windows_path),
            sha256=hashlib.sha256(windows_path.read_bytes()).hexdigest(),
            payload_schema="TimelineWindowsPlan.v1",
        ),
        "art_legacy_portrait": c.Artifact(
            id="art_legacy_portrait",
            run_id="run_source",
            kind=c.ArtifactKind.plan_portrait,
            uri=portrait_path.as_uri(),
            local_path=str(portrait_path),
            sha256=hashlib.sha256(portrait_path.read_bytes()).hexdigest(),
            payload_schema="PortraitPlanArtifact.v1",
        ),
    }

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == ["TimelineWindowPlanning"]
    assert plan.decisions[0].artifact_ids == ["art_windows"]
    assert plan.rerun_from_node_id == "LipSync"


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


def test_async_icl2_tts_invalidates_synchronous_v2_node_run():
    from packages.production.pipeline.digital_human import digital_human_template

    tts_spec = next(node for node in digital_human_template().nodes if node.node_id == "TTS")
    assert tts_spec.node_version == "v3"
    source_tts = _node_run("TTS", "art_old_sync_tts").model_copy(update={"node_version": "v2"})

    plan = compute_reuse_plan(_run([source_tts]), _template(tts_spec), {})

    assert plan.reused_node_ids == []
    assert plan.rerun_from_node_id == "TTS"
    assert plan.decisions[0].reason == "node_version_mismatch"


def test_resume_stops_before_non_replayable_run_owned_side_effects():
    from packages.production.pipeline.digital_human import (
        digital_human_template,
        seedance_t2v_template,
    )

    specs = {
        spec.node_id: spec
        for template in (digital_human_template(), seedance_t2v_template())
        for spec in template.nodes
    }
    # MaterialPackPlanning is intentionally NOT in this list. It sits mid-chain, so marking
    # it non-replayable would stop reuse before it and force paid downstream nodes to re-run
    # on resume, re-billing the already-paid lipsync (issue #202). That money-safety contract
    # is enforced by the golden test test_resume_answers_from_the_lipsync_the_failed_run_already_paid_for.
    # Only TERMINAL run-owned-write nodes belong here — re-running them cannot cascade into
    # paid upstream re-runs. Guard against re-introducing the mid-chain money bug:
    assert "selection_reservation" not in specs["MaterialPackPlanning"].side_effects

    for node_id in (
        "ExportFinishedVideo",
        "ExportSeedanceVideo",
        "FinalizeRunReport",
    ):
        spec = specs[node_id]
        source_node = _node_run(node_id, f"art_{node_id}").model_copy(
            update={"node_version": spec.node_version}
        )

        plan = compute_reuse_plan(_run([source_node]), _template(spec), {})

        assert plan.reused_node_ids == []
        assert plan.rerun_from_node_id == node_id
        assert plan.decisions[0].reason == "side_effect_not_reusable"


def _skipped_without_output(node_id: str) -> c.NodeRun:
    """What ``_may_skip_without_running`` leaves behind: skipped, and empty-handed.

    Full-coverage B-roll turns PortraitTrackBuild / LipSync into no-ops, so the node runs
    but publishes nothing — the template still DECLARES output kinds for it.
    """
    return c.NodeRun(
        id=f"nr_{node_id}",
        run_id="run_source",
        node_id=node_id,
        node_version="v1",
        status=c.NodeStatus.skipped,
        input_manifest_hash="same",
        output_artifact_ids=[],
    )


def test_legitimately_empty_skipped_node_does_not_stop_reuse(tmp_path):
    # A4. Reuse used to read "no artifacts + declared output kinds" as corruption and stop
    # dead at PortraitTrackBuild — which on the full-coverage path is simply a node that
    # produces nothing. Everything paid for BEFORE it (TTS, the planners) then re-ran and
    # re-billed on every resume.
    tts_path = tmp_path / "tts.json"
    tts_path.write_text("tts", encoding="utf-8")
    template = _template(
        _node("TTS"),
        _node("PortraitTrackBuild", outputs=[c.ArtifactKind.video_portrait_track]),
        _node("LipSync", outputs=[c.ArtifactKind.video_lipsync]),
        _node("RenderFinalTimeline"),
    )
    source = _run(
        [
            _node_run("TTS", "art_tts"),
            _skipped_without_output("PortraitTrackBuild"),
            _skipped_without_output("LipSync"),
            _node_run("RenderFinalTimeline", "art_render", status=c.NodeStatus.failed),
        ],
        status=c.RunStatus.failed,
    )
    artifacts = {"art_tts": _artifact("art_tts", tts_path)}

    plan = compute_reuse_plan(source, template, artifacts)

    assert plan.reused_node_ids == ["TTS", "PortraitTrackBuild", "LipSync"]
    assert plan.rerun_from_node_id == "RenderFinalTimeline"
    # The skipped nodes are reused as what they were: empty.
    empty = {d.node_id: d.artifact_ids for d in plan.decisions if d.reusable}
    assert empty["PortraitTrackBuild"] == [] and empty["LipSync"] == []


def test_a_node_whose_recorded_artifacts_vanished_still_stops_reuse(tmp_path):
    # The other side of A4: an empty output list is only benign when the node never
    # recorded any ids. A node that DID record them and whose rows are gone is genuinely
    # broken, and reusing it would hand the new run a chain built on nothing.
    tts_path = tmp_path / "tts.json"
    tts_path.write_text("tts", encoding="utf-8")
    template = _template(_node("TTS"), _node("PortraitTrackBuild"), _node("LipSync"))
    source = _run(
        [
            _node_run("TTS", "art_tts"),
            _node_run("PortraitTrackBuild", "art_gone", status=c.NodeStatus.skipped),
            _node_run("LipSync", "art_lipsync", status=c.NodeStatus.failed),
        ],
        status=c.RunStatus.failed,
    )

    plan = compute_reuse_plan(source, template, {"art_tts": _artifact("art_tts", tts_path)})

    assert plan.reused_node_ids == ["TTS"]
    assert plan.rerun_from_node_id == "PortraitTrackBuild"
    assert plan.decisions[-1].reason == "artifact_missing"
    assert plan.decisions[-1].artifact_ids == ["art_gone"]


def test_node_spec_exposes_only_the_consumed_reuse_shape():
    """Guard: the live reuse contract is reuse_policy/side_effects/idempotency_key.

    Dead resume and payload-schema declarations were never consumed by Temporal or
    the reuse planner. Keep only the fields that change live replay behaviour.
    """
    fields = c.NodeSpec.model_fields
    assert "resume_policy" not in fields
    assert "input_schema" not in fields
    assert "output_artifact_schema_versions" not in fields
    assert "reuse_policy" in fields
    assert "side_effects" in fields
    assert "idempotency_key" in fields
    assert not hasattr(c, "ResumePolicy")
