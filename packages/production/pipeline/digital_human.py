"""Thin orchestrator for the digital-human workflow.

This module owns the *engine*: the node sequence, the workflow template, the
run/node state machine, reuse/resume bookkeeping, and the shared services every
node leans on (artifact creation, media-source resolution, provider-profile
selection, the object store). The per-node business logic lives in
``packages.production.pipeline.nodes`` — one ``run(ctx)`` handler per entry in
``NODE_SEQUENCE`` — so capability work edits disjoint files.

``RunState`` / ``degradation_notice`` are re-exported here for backwards
compatibility with callers (and tests) that import them from this module.
``get_object_store`` is likewise imported into this namespace so it stays
monkeypatchable; node handlers reach it via ``NodeContext.object_store()`` which
resolves through ``LocalRuntimeAdapter._object_store``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from packages.ai.gateway import ProviderGateway
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    Job,
    MediaInfo,
    NodeError,
    NodeRun,
    NodeStatus,
    JobStatus,
    RunDebugReportArtifact,
    RunPublicReportArtifact,
    RunStatus,
    WarningCode,
    WorkflowRun,
    WorkflowTemplate,
    NodeSpec,
    WorkflowEdge,
    utcnow,
)
from packages.core.contracts.artifacts import NarrationUnit
from packages.core.storage import Repository
from packages.core.storage.object_store import get_object_store
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError, NodeOutput, WorkflowRuntimeAdapter, manifest_hash
from packages.production.pipeline.node_sequence import NODE_SEQUENCE
from packages.media.assets import local_object_path, store_file
from packages.media.rendering import generate_seed_audio, generate_seed_video
from packages.media.video.ffmpeg import FfmpegCommandError, probe_media
from packages.core.observability import (
    node_stage,
    record_funnel_event,
    record_node_run,
    record_workflow_run,
    workflow_stage,
)
from packages.core.contracts.state_machines import assert_transition
from packages.core.config.settings import sandbox_fallback_allowed
from packages.production.pipeline import nodes
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState, degradation_notice
from packages.production.pipeline.reuse import ReusePlan, ReuseSourceRun, compute_reuse_plan

__all__ = [
    "NODE_SEQUENCE",
    "RunState",
    "degradation_notice",
    "digital_human_template",
    "LocalRuntimeAdapter",
    "DigitalHumanWorkflow",
    "build_digital_human_workflow",
    "get_object_store",
]


# Per-node handler dispatch: each entry maps a node id to its free ``run(ctx)``
# function in ``packages.production.pipeline.nodes``.
NODE_HANDLERS = {
    "ValidateRequest": nodes.validate_request.run,
    "LoadCaseContext": nodes.load_case_context.run,
    "ResolveCreativeIntent": nodes.resolve_creative_intent.run,
    "TTS": nodes.tts.run,
    "MaterialPackPlanning": nodes.material_pack_planning.run,
    "NarrationAlignment": nodes.narration_alignment.run,
    "PortraitPlanning": nodes.portrait_planning.run,
    "BrollPlanning": nodes.broll_planning.run,
    "StylePlanning": nodes.style_planning.run,
    "TimelinePlanning": nodes.timeline_planning.run,
    "PortraitTrackBuild": nodes.portrait_track_build.run,
    "LipSync": nodes.lipsync.run,
    "RenderFinalTimeline": nodes.render_final_timeline.run,
    "SubtitleAndBgmMix": nodes.subtitle_and_bgm_mix.run,
    "ExportFinishedVideo": nodes.export_finished_video.run,
    "FinalizeRunReport": nodes.finalize_run_report.run,
}

logger = logging.getLogger(__name__)

_LIPSYNC_CONTENT_POLICY_MARKERS = (
    "input data may contain inappropriate content",
    "inappropriate content",
    "content policy",
    "sensitive content",
    "unsafe content",
)


def _is_lipsync_content_policy_error(message: str | None) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in _LIPSYNC_CONTENT_POLICY_MARKERS)


def digital_human_template() -> WorkflowTemplate:
    # ExportFinishedVideo makes a PAID image.generate call on the gated AI-cover
    # path, so it is declared here too: this gives it a non-None idempotency_key so
    # the reuse planner accounts for the side effect and can safely replay it,
    # instead of treating the node as pure and silently re-firing the paid call.
    provider_side_effect_nodes = {"TTS", "ResolveCreativeIntent", "LipSync", "ExportFinishedVideo"}
    nodes = [
        NodeSpec(
            node_id=node_id,
            input_schema=f"{node_id}.input.v1",
            output_artifact_kinds=[],
            side_effects=["provider_call"] if node_id in provider_side_effect_nodes else [],
            idempotency_key=(
                f"digital_human_v2:{node_id}:{{input_manifest_hash}}"
                if node_id in provider_side_effect_nodes
                else None
            ),
        )
        for node_id in NODE_SEQUENCE
    ]
    for spec in nodes:
        if spec.node_id == "ValidateRequest":
            spec.output_artifact_kinds.append(ArtifactKind.validated_production_spec)
        elif spec.node_id == "LoadCaseContext":
            spec.output_artifact_kinds.append(ArtifactKind.case_context)
        elif spec.node_id == "ResolveCreativeIntent":
            spec.output_artifact_kinds.append(ArtifactKind.creative_intent)
        elif spec.node_id == "TTS":
            spec.output_artifact_kinds.append(ArtifactKind.audio_tts)
        elif spec.node_id == "MaterialPackPlanning":
            spec.output_artifact_kinds.append(ArtifactKind.plan_material_pack)
        elif spec.node_id == "NarrationAlignment":
            spec.output_artifact_kinds.extend([ArtifactKind.audio_alignment, ArtifactKind.narration_units])
        elif spec.node_id == "PortraitPlanning":
            spec.output_artifact_kinds.append(ArtifactKind.plan_portrait)
        elif spec.node_id == "BrollPlanning":
            spec.output_artifact_kinds.append(ArtifactKind.plan_broll)
        elif spec.node_id == "StylePlanning":
            spec.output_artifact_kinds.append(ArtifactKind.plan_style)
        elif spec.node_id == "TimelinePlanning":
            spec.output_artifact_kinds.extend([ArtifactKind.plan_timeline, ArtifactKind.plan_render])
        elif spec.node_id == "PortraitTrackBuild":
            spec.output_artifact_kinds.append(ArtifactKind.video_portrait_track)
        elif spec.node_id == "LipSync":
            spec.output_artifact_kinds.extend([ArtifactKind.video_lipsync, ArtifactKind.lipsync_report])
        elif spec.node_id == "RenderFinalTimeline":
            spec.output_artifact_kinds.append(ArtifactKind.video_rendered)
        elif spec.node_id == "SubtitleAndBgmMix":
            spec.output_artifact_kinds.extend([ArtifactKind.video_final, ArtifactKind.subtitle_ass])
        elif spec.node_id == "ExportFinishedVideo":
            spec.output_artifact_kinds.extend(
                [ArtifactKind.video_finished, ArtifactKind.cover_image, ArtifactKind.publish_package]
            )
        elif spec.node_id == "FinalizeRunReport":
            spec.output_artifact_kinds.extend(
                [ArtifactKind.run_report_public, ArtifactKind.run_report_debug]
            )
    return WorkflowTemplate(
        workflow_template_id="digital_human_v2",
        version="v1",
        nodes=nodes,
        edges=[
            WorkflowEdge(from_node_id=NODE_SEQUENCE[index], to_node_id=NODE_SEQUENCE[index + 1])
            for index in range(len(NODE_SEQUENCE) - 1)
        ],
    )


class LocalRuntimeAdapter(WorkflowRuntimeAdapter):
    def __init__(
        self,
        repository: Repository,
        provider_gateway: ProviderGateway,
        prompt_registry: PromptRegistry,
        *,
        seed_media: bool = True,
    ) -> None:
        self.repository = repository
        self.provider_gateway = provider_gateway
        self.prompt_registry = prompt_registry
        self.template = digital_human_template()
        # ``seed_media`` generates demo seed media via ffmpeg/object-store on
        # construction. The per-activity Temporal scoping (see
        # ``TemporalActivityContext.build_runtime``) rehydrates real media
        # assets from SQL, so it skips this expensive bootstrap.
        if seed_media:
            self._ensure_seed_media_assets()

    # ------------------------------------------------------------------ seed
    def _ensure_seed_media_assets(self) -> None:
        seed_dir = Path(".data/generated-media/seed")
        seed_dir.mkdir(parents=True, exist_ok=True)
        specs = {
            "asset_portrait_demo": {
                "filename": "portrait_demo_15s.mp4",
                "content_type": "video/mp4",
                "generator": lambda path: generate_seed_video(
                    path, duration_sec=15, width=320, height=568, fps=30
                ),
            },
            "asset_broll_demo": {
                "filename": "broll_demo_4s.mp4",
                "content_type": "video/mp4",
                "generator": lambda path: generate_seed_video(
                    path, duration_sec=4, width=320, height=568, fps=30
                ),
            },
            "asset_bgm_demo": {
                "filename": "bgm_demo_15s.wav",
                "content_type": "audio/wav",
                "generator": lambda path: generate_seed_audio(path, duration_sec=15),
            },
        }
        for asset_id, spec in specs.items():
            asset = self.repository.media_assets.get(asset_id)
            if asset is None or asset.source_artifact_id:
                continue
            path = seed_dir / str(spec["filename"])
            try:
                if not path.exists():
                    spec["generator"](path)
                media_info = probe_media(path)
                stored = store_file(get_object_store(), path, purpose="seed-media", addressed=True)
            except FfmpegCommandError as exc:
                raise NodeExecutionError(exc.error_code, "Demo seed media generation failed.") from exc
            artifact = self.repository.create_artifact(
                kind=ArtifactKind.uploaded_file,
                payload_schema="UploadedFileArtifact.v1",
                payload={
                    "upload_session_id": None,
                    "filename": path.name,
                    "content_type": spec["content_type"],
                    "size_bytes": path.stat().st_size,
                    "object_uri": stored.ref.uri,
                    "sha256": stored.sha256,
                    "metadata": {"seed": "true", "asset_id": asset_id},
                },
                case_id=asset.case_id,
                uri=stored.ref.uri,
                sha256=stored.sha256,
                media_info=media_info,
            )
            self.repository.media_assets[asset_id] = asset.model_copy(
                update={
                    "source_artifact_id": artifact.id,
                    "annotation_status": "annotated",
                    "usable": True,
                    "updated_at": utcnow(),
                }
            )

    # --------------------------------------------------------------- runtime API
    def start_run(
        self,
        *,
        job: Job,
        run: WorkflowRun,
        template: WorkflowTemplate,
    ) -> None:
        self._execute(run.id, mode="new", from_run_id=None, reuse_plan=None)

    def resume_run(
        self,
        *,
        source_run_id: str,
        new_run: WorkflowRun,
        reuse_plan,
    ) -> None:
        self._execute(
            new_run.id,
            mode="resume",
            from_run_id=source_run_id,
            reuse_plan=ReusePlan.model_validate(reuse_plan),
        )

    def cancel_run(self, run_id: str, *, force: bool = False, reason: str | None = None) -> WorkflowRun:
        run = self.repository.runs[run_id]
        if run.status not in {RunStatus.created, RunStatus.admitted, RunStatus.running}:
            raise NodeExecutionError(
                ErrorCode.workflow_invalid_transition,
                f"Run {run_id} cannot be cancelled from {run.status}.",
            )
        self._mark_cancelled(run_id)
        self.repository.create_event(
            "workflow.run.cancelled",
            "run",
            run.id,
            {"force": force, "reason": reason or ""},
            dedupe_key=f"{run.id}:run:{RunStatus.cancelled.value}",
            status=RunStatus.cancelled.value,
            message="Run cancelled.",
        )
        return self.repository.runs[run_id]

    def _execute(
        self,
        run_id: str,
        *,
        mode: str,
        from_run_id: str | None,
        reuse_plan: ReusePlan | None,
    ) -> None:
        run = self.repository.runs[run_id]
        job = self.repository.jobs[run.job_id]
        request = self._request(job)
        state = RunState(request=request)
        start_index = 0
        if job.status != JobStatus.running:
            assert_transition("job", job.status, JobStatus.running)
            job = job.model_copy(update={"status": JobStatus.running, "updated_at": utcnow()})
            self.repository.jobs[job.id] = job
        assert_transition("run", run.status, RunStatus.running)
        run = run.model_copy(update={"status": RunStatus.running, "started_at": utcnow()})
        self.repository.runs[run.id] = run
        self.repository.create_event(
            "workflow.run.updated",
            "run",
            run.id,
            {"status": RunStatus.running.value},
            dedupe_key=f"{run.id}:run:{RunStatus.running.value}",
            status=RunStatus.running.value,
            message="Run is running.",
        )
        record_funnel_event(
            self.repository,
            event_type=workflow_stage(RunStatus.running),
            job_id=job.id,
            run_id=run.id,
            dedupe_aggregate_id=run.id,
            event_time=run.started_at,
        )
        if mode == "resume" and from_run_id:
            start_index = self._reuse_prefix(run, state, from_run_id, reuse_plan)
        for index, node_id in enumerate(NODE_SEQUENCE[start_index:], start=start_index):
            if self.repository.runs[run.id].status == RunStatus.cancelled:
                return
            if not self._execute_node(node_id, run, state):
                return
        self._complete_run(run.id)

    def run_node_activity(self, run_id: str, node_id: str) -> dict:
        run = self.repository.runs[run_id]
        job = self.repository.jobs[run.job_id]
        request = self._request(job)
        state = self._state_from_persisted_artifacts(run_id, request)
        if job.status != JobStatus.running:
            assert_transition("job", job.status, JobStatus.running)
            self.repository.jobs[job.id] = job.model_copy(
                update={"status": JobStatus.running, "updated_at": utcnow()}
            )
        if run.status == RunStatus.cancelling:
            self._mark_cancelled(run_id)
            return self._node_activity_summary(run_id, node_id)
        if run.status == RunStatus.admitted:
            assert_transition("run", run.status, RunStatus.running)
            run = run.model_copy(update={"status": RunStatus.running, "started_at": utcnow()})
            self.repository.runs[run.id] = run
            self.repository.create_event(
                "workflow.run.updated",
                "run",
                run.id,
                {"status": RunStatus.running.value},
                dedupe_key=f"{run.id}:run:{RunStatus.running.value}",
                status=RunStatus.running.value,
                message="Run is running.",
            )
            record_funnel_event(
                self.repository,
                event_type=workflow_stage(RunStatus.running),
                job_id=job.id,
                run_id=run.id,
                dedupe_aggregate_id=run.id,
                event_time=run.started_at,
            )
        if self.repository.runs[run_id].status != RunStatus.running:
            return self._node_activity_summary(run_id, node_id)
        if self._execute_node(node_id, self.repository.runs[run_id], state) and node_id == NODE_SEQUENCE[-1]:
            self._complete_run(run_id)
        return self._node_activity_summary(run_id, node_id)

    def apply_reuse_plan(
        self, run_id: str, source_run_id: str, reuse_plan: ReusePlan
    ) -> dict:
        run = self.repository.runs[run_id]
        request = self._request(self.repository.jobs[run.job_id])
        state = RunState(request=request)
        self._reuse_prefix(run, state, source_run_id, reuse_plan)
        return {
            "run_id": run_id,
            "source_run_id": source_run_id,
            "reused_node_ids": list(reuse_plan.reused_node_ids),
            "rerun_from_node_id": reuse_plan.rerun_from_node_id,
        }

    def request_cancel(self, run_id: str, *, force: bool = False, reason: str | None = None) -> WorkflowRun:
        return self.cancel_run(run_id, force=force, reason=reason)

    @staticmethod
    def _next_unfinished_node_id(node_runs: list[NodeRun]) -> str | None:
        """First template node not yet completed — the node that was due to run."""
        done = {
            node_run.node_id
            for node_run in node_runs
            if node_run.status in {NodeStatus.succeeded, NodeStatus.skipped, NodeStatus.degraded}
        }
        return next((node_id for node_id in NODE_SEQUENCE if node_id not in done), None)

    def mark_run_failed(self, run_id: str, *, reason: str = "Worker lost or node activity timed out.") -> WorkflowRun:
        """Fail a run whose node activity died without writing a terminal status.

        Used by the Temporal workflow when a ``run_node`` activity is lost to an
        infrastructure failure (e.g. the worker was restarted mid-node) and so
        never marked the run failed itself. Idempotent — a run already in a
        terminal state is returned unchanged. The run lands in ``failed`` with a
        retryable error so an operator can resume it; a run mid-cancellation is
        completed to ``cancelled`` instead.
        """
        run = self.repository.runs.get(run_id)
        if run is None:
            raise NodeExecutionError(ErrorCode.artifact_missing, f"Run {run_id} is missing.")
        if run.status in {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}:
            return run
        if run.status == RunStatus.cancelling:
            self._mark_cancelled(run_id)
            return self.repository.runs[run_id]

        # Anchor a retryable failed node so the run detail shows where it stopped
        # AND the run becomes resumable (can_resume keys off a retryable failed
        # node). Prefer the in-flight running node; but a worker that dies mid-node
        # never syncs that running node to storage, so fall back to synthesizing a
        # failed entry for the next node that was due to run.
        node_runs = self.repository.node_runs.setdefault(run_id, [])
        running_index = next(
            (i for i in range(len(node_runs) - 1, -1, -1) if node_runs[i].status == NodeStatus.running),
            None,
        )
        if running_index is not None:
            node_run = node_runs[running_index]
            error = NodeError(
                code=ErrorCode.workflow_worker_lost,
                message=reason,
                retryable=True,
                run_id=run_id,
                node_run_id=node_run.id,
            )
            failed_node = node_run.model_copy(
                update={
                    "status": NodeStatus.failed,
                    "error": error,
                    "finished_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )
            node_runs[running_index] = failed_node
        else:
            next_node_id = self._next_unfinished_node_id(node_runs)
            failed_node = None
            if next_node_id is not None:
                failed_node = NodeRun(
                    id=new_id("nr"),
                    run_id=run_id,
                    node_id=next_node_id,
                    node_version="v1",
                    status=NodeStatus.failed,
                    input_manifest_hash="",
                    error=NodeError(
                        code=ErrorCode.workflow_worker_lost,
                        message=reason,
                        retryable=True,
                        run_id=run_id,
                    ),
                    started_at=utcnow(),
                    finished_at=utcnow(),
                )
                node_runs.append(failed_node)
        if failed_node is not None:
            record_node_run(failed_node)
            self.repository.create_event(
                "workflow.node.failed",
                "run",
                run_id,
                {"node_id": failed_node.node_id, "error_code": ErrorCode.workflow_worker_lost.value},
                dedupe_key=f"{failed_node.id}:{NodeStatus.failed.value}",
                event_type="node_update",
                node_id=failed_node.node_id,
                status=NodeStatus.failed.value,
                message=f"Node {failed_node.node_id} failed.",
            )

        # admitted has no direct edge to failed; advance through running first.
        current = self.repository.runs[run_id]
        if current.status == RunStatus.admitted:
            assert_transition("run", current.status, RunStatus.running)
            self.repository.runs[run_id] = current.model_copy(
                update={"status": RunStatus.running, "updated_at": utcnow()}
            )
        assert_transition("run", self.repository.runs[run_id].status, RunStatus.failed)
        self.repository.runs[run_id] = self.repository.runs[run_id].model_copy(
            update={"status": RunStatus.failed, "finished_at": utcnow(), "updated_at": utcnow()}
        )
        record_workflow_run(self.repository.runs[run_id])
        self.repository.create_event(
            "workflow.run.updated",
            "run",
            run_id,
            {"status": RunStatus.failed.value, "reason": reason},
            dedupe_key=f"{run_id}:run:{RunStatus.failed.value}",
            status=RunStatus.failed.value,
            message="Run failed (worker lost).",
        )
        job = self.repository.jobs.get(run.job_id)
        if job is not None and job.status == JobStatus.running:
            self.repository.jobs[job.id] = job.model_copy(
                update={"status": JobStatus.failed, "updated_at": utcnow()}
            )
        return self.repository.runs[run_id]

    def _state_from_persisted_artifacts(
        self, run_id: str, request: DigitalHumanVideoRequest
    ) -> RunState:
        state = RunState(request=request)
        for artifact in self.repository.artifacts.values():
            if artifact.run_id == run_id:
                state.artifacts[artifact.kind] = artifact
        for node_run in self.repository.node_runs.get(run_id, []):
            for artifact_id in node_run.output_artifact_ids:
                artifact = self.repository.artifacts.get(artifact_id)
                if artifact is not None:
                    state.artifacts[artifact.kind] = artifact
        for node_run in self.repository.node_runs.get(run_id, []):
            state.provider_invocation_ids.extend(node_run.provider_invocation_ids)
            state.warnings.extend(node_run.warnings)
            state.degradations.extend(node_run.degradations)
        return state

    # --------------------------------------------------------------- engine loop
    def _execute_node(self, node_id: str, run: WorkflowRun, state: RunState) -> bool:
        job = self.repository.jobs[run.job_id]
        request = state.request
        node_run = NodeRun(
            id=new_id("nr"),
            run_id=run.id,
            node_id=node_id,
            node_version="v1",
            status=NodeStatus.pending,
            input_manifest_hash=manifest_hash(
                {
                    "node_id": node_id,
                    "request": request.model_dump(mode="json"),
                    "artifact_refs": {
                        kind.value: artifact.id for kind, artifact in state.artifacts.items()
                    },
                }
            ),
            started_at=utcnow(),
        )
        self.repository.node_runs[run.id].append(node_run)
        try:
            if not self._may_skip_without_running(node_id, state):
                assert_transition("node", node_run.status, NodeStatus.running)
                node_run = node_run.model_copy(update={"status": NodeStatus.running, "updated_at": utcnow()})
                self.repository.node_runs[run.id][-1] = node_run
                self.repository.create_event(
                    "workflow.node.updated",
                    "run",
                    run.id,
                    {"node_id": node_id, "status": NodeStatus.running.value},
                    dedupe_key=f"{node_run.id}:{NodeStatus.running.value}",
                    event_type="node_update",
                    node_id=node_id,
                    status=NodeStatus.running.value,
                    message=f"Node {node_id} is running.",
                )
                record_funnel_event(
                    self.repository,
                    event_type="node_started",
                    job_id=job.id,
                    run_id=run.id,
                    node_run_id=node_run.id,
                    dedupe_key=f"{node_run.id}:node_started",
                    event_time=node_run.updated_at,
                )
            output = self._run_node(node_id, run, node_run, state)
            # No silent fallback: any provider call this node made that the gateway
            # could not price (billing_status="unpriced") surfaces as a node-level
            # cost.unpriced warning instead of staying buried in usage metering.
            if WarningCode.cost_unpriced not in output.warnings:
                invocations = getattr(self.repository, "provider_invocations", {})
                for inv_id in output.provider_invocation_ids:
                    invocation = invocations.get(inv_id)
                    if invocation is not None and getattr(invocation, "billing_status", None) == "unpriced":
                        output.warnings.append(WarningCode.cost_unpriced)
                        break
            for artifact in output.artifacts:
                state.artifacts[artifact.kind] = artifact
            state.provider_invocation_ids.extend(output.provider_invocation_ids)
            state.warnings.extend(output.warnings)
            state.degradations.extend(output.degradations)
            status = output.status
            if status == NodeStatus.succeeded and output.degradations:
                status = NodeStatus.degraded
            if node_run.status == NodeStatus.pending and status != NodeStatus.skipped:
                assert_transition("node", node_run.status, NodeStatus.running)
                node_run = node_run.model_copy(update={"status": NodeStatus.running})
            assert_transition("node", node_run.status, status)
            patched = node_run.model_copy(
                update={
                    "status": status,
                    "output_artifact_ids": [artifact.id for artifact in output.artifacts],
                    "provider_invocation_ids": output.provider_invocation_ids,
                    "warnings": output.warnings,
                    "degradations": output.degradations,
                    "degradation_reason": "; ".join(item.message for item in output.degradations) or None,
                    "finished_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )
            self.repository.node_runs[run.id][-1] = patched
            record_node_run(patched)
            self.repository.create_event(
                "workflow.node.updated",
                "run",
                run.id,
                {"node_id": node_id, "status": status.value if hasattr(status, "value") else str(status)},
                dedupe_key=f"{patched.id}:{status.value if hasattr(status, 'value') else str(status)}",
                event_type="node_update",
                node_id=node_id,
                status=status.value if hasattr(status, "value") else str(status),
                message=f"Node {node_id} finished with {status.value if hasattr(status, 'value') else status}.",
            )
            funnel_stage = node_stage(status)
            if funnel_stage is not None:
                record_funnel_event(
                    self.repository,
                    event_type=funnel_stage,
                    job_id=job.id,
                    run_id=run.id,
                    node_run_id=patched.id,
                    dedupe_key=f"{patched.id}:{funnel_stage}",
                    event_time=patched.finished_at,
                )
            return True
        except NodeExecutionError as exc:
            if node_run.status == NodeStatus.pending:
                assert_transition("node", node_run.status, NodeStatus.running)
                node_run = node_run.model_copy(update={"status": NodeStatus.running})
                self.repository.node_runs[run.id][-1] = node_run
            assert_transition("node", node_run.status, NodeStatus.failed)
            error = exc.error.model_copy(
                update={"job_id": job.id, "run_id": run.id, "node_run_id": node_run.id}
            )
            failed_node = node_run.model_copy(
                update={
                    "status": NodeStatus.failed,
                    "error": error,
                    "finished_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )
            self.repository.node_runs[run.id][-1] = failed_node
            record_node_run(failed_node)
            # §6.6 release on failure: free this run's UNCOMMITTED reservations so a
            # sibling run can claim those slots. Committed picks stay held for diversity
            # memory ("失败任务默认保留用于多样性记忆"). Never let a reservation hiccup
            # mask the original node failure.
            try:
                self.repository.release_run_reservations(run_id=run.id, only_uncommitted=True)
            except Exception:
                logger.warning(
                    "Failed to release selection reservations for failed run %s.",
                    run.id,
                    exc_info=True,
                )
            self._write_report(run, state, failed=True)
            assert_transition("run", self.repository.runs[run.id].status, RunStatus.failed)
            self.repository.runs[run.id] = self.repository.runs[run.id].model_copy(
                update={"status": RunStatus.failed, "finished_at": utcnow(), "updated_at": utcnow()}
            )
            record_workflow_run(self.repository.runs[run.id])
            assert_transition("job", self.repository.jobs[job.id].status, JobStatus.failed)
            self.repository.jobs[job.id] = self.repository.jobs[job.id].model_copy(
                update={"status": JobStatus.failed, "updated_at": utcnow()}
            )
            self.repository.create_event(
                "workflow.node.failed",
                "run",
                run.id,
                {"node_id": node_id, "error_code": error.code.value},
                dedupe_key=f"{node_run.id}:{NodeStatus.failed.value}",
                event_type="node_update",
                node_id=node_id,
                status=NodeStatus.failed.value,
                message=f"Node {node_id} failed.",
            )
            record_funnel_event(
                self.repository,
                event_type="node_failed",
                job_id=job.id,
                run_id=run.id,
                node_run_id=failed_node.id,
                dedupe_key=f"{failed_node.id}:node_failed",
                event_time=failed_node.finished_at,
            )
            # §9.6: classify the terminal node failure into the failure taxonomy so
            # the failure-analysis view + QC/retry alerts have a real signal.
            try:
                self.repository.record_failure_taxonomy(
                    target_type="node_run",
                    target_id=failed_node.id,
                    error_code=error.code.value,
                    run_id=run.id,
                    job_id=job.id,
                    case_id=run.case_id,
                    node_id=node_id,
                    message=error.message,
                    dedupe_key=f"{failed_node.id}:failure",
                )
            except Exception:  # pragma: no cover - classification must never break a run
                pass
            return False

    def _complete_run(self, run_id: str) -> None:
        run = self.repository.runs[run_id]
        job = self.repository.jobs[run.job_id]
        final_status = RunStatus.succeeded
        assert_transition("run", self.repository.runs[run.id].status, final_status)
        self.repository.runs[run.id] = self.repository.runs[run.id].model_copy(
            update={"status": final_status, "finished_at": utcnow(), "updated_at": utcnow()}
        )
        record_workflow_run(self.repository.runs[run.id])
        assert_transition("job", self.repository.jobs[job.id].status, JobStatus.succeeded)
        self.repository.jobs[job.id] = self.repository.jobs[job.id].model_copy(
            update={"status": JobStatus.succeeded}
        )
        self.repository.create_event(
            "workflow.run.completed",
            "run",
            run.id,
            {"status": final_status.value},
            dedupe_key=f"{run.id}:run:{final_status.value}",
            status=final_status.value,
            message="Run completed.",
        )
        # NOTE: run-level "succeeded" is intentionally NOT a §9.5 funnel stage.
        # Technical success is observed via node_succeeded / finished_video_created,
        # and true yield via the publish stages — "成品率不得只看 workflow succeeded".

    def _mark_cancelled(self, run_id: str) -> None:
        run = self.repository.runs[run_id]
        if run.status == RunStatus.cancelled:
            return
        if run.status == RunStatus.running:
            assert_transition("run", run.status, RunStatus.cancelling)
            run = run.model_copy(update={"status": RunStatus.cancelling, "updated_at": utcnow()})
            self.repository.runs[run.id] = run
        assert_transition("run", self.repository.runs[run.id].status, RunStatus.cancelled)
        self.repository.runs[run.id] = self.repository.runs[run.id].model_copy(
            update={"status": RunStatus.cancelled, "finished_at": utcnow(), "updated_at": utcnow()}
        )
        # §6.6 release on cancel: free this run's uncommitted reservations so the slots
        # are reclaimable immediately (committed picks stay held for diversity memory).
        try:
            self.repository.release_run_reservations(run_id=run_id, only_uncommitted=True)
        except Exception:
            logger.warning(
                "Failed to release selection reservations for cancelled run %s.",
                run_id,
                exc_info=True,
            )
        record_workflow_run(self.repository.runs[run.id])
        self.repository.create_event(
            "workflow.run.cancelled",
            "run",
            run.id,
            {"status": RunStatus.cancelled.value},
            dedupe_key=f"{run.id}:run:{RunStatus.cancelled.value}",
            status=RunStatus.cancelled.value,
            message="Run cancelled.",
        )
        # Run-level cancellation is not a §9.5 funnel stage (no submitted-side
        # event maps to it); the run simply stops contributing further stages.
        job = self.repository.jobs[run.job_id]
        if job.status != JobStatus.cancelled:
            assert_transition("job", job.status, JobStatus.cancelled)
            self.repository.jobs[job.id] = job.model_copy(
                update={"status": JobStatus.cancelled, "updated_at": utcnow()}
            )

    def _node_activity_summary(self, run_id: str, node_id: str) -> dict:
        run = self.repository.runs[run_id]
        latest = next(
            (node for node in reversed(self.repository.node_runs.get(run_id, [])) if node.node_id == node_id),
            None,
        )
        return {
            "run_id": run_id,
            "node_id": node_id,
            "node_status": latest.status.value if latest else None,
            "run_status": run.status.value,
        }

    def _reuse_prefix(
        self,
        run: WorkflowRun,
        state: RunState,
        from_run_id: str,
        reuse_plan: ReusePlan | None,
    ) -> int:
        previous = self.repository.node_runs.get(from_run_id, [])
        if reuse_plan is None:
            reuse_plan = compute_reuse_plan(
                ReuseSourceRun(
                    run=self.repository.runs[from_run_id],
                    node_runs=previous,
                ),
                self.template,
                self.repository.artifacts,
            )
        previous_by_node = {node.node_id: node for node in previous}
        for node_id in reuse_plan.reused_node_ids:
            previous_node_run = previous_by_node[node_id]
            for artifact_id in previous_node_run.output_artifact_ids:
                artifact = self.repository.artifacts.get(artifact_id)
                if artifact is None:
                    raise NodeExecutionError(
                        ErrorCode.artifact_missing,
                        f"Reusable artifact {artifact_id} is missing.",
                    )
                state.artifacts[artifact.kind] = artifact
                if artifact.kind == ArtifactKind.run_report_public:
                    self.repository.runs[run.id] = self.repository.runs[run.id].model_copy(
                        update={"public_report_artifact_id": artifact.id, "updated_at": utcnow()}
                    )
                elif artifact.kind == ArtifactKind.run_report_debug:
                    self.repository.runs[run.id] = self.repository.runs[run.id].model_copy(
                        update={"debug_report_artifact_id": artifact.id, "updated_at": utcnow()}
                    )
            copied = previous_node_run.model_copy(
                update={
                    "id": new_id("nr"),
                    "run_id": run.id,
                    "status": NodeStatus.skipped,
                    "skipped_reason": "resume.reused_artifact_prefix",
                    "updated_at": utcnow(),
                }
            )
            self.repository.node_runs[run.id].append(copied)
        return reuse_plan.reused_count

    def _may_skip_without_running(self, node_id: str, state: RunState) -> bool:
        return (
            node_id == "ResolveCreativeIntent"
            and state.request.creative_intent_ref is not None
            or node_id == "LipSync"
            and not state.request.lipsync.enabled
            or node_id == "SubtitleAndBgmMix"
            and not state.request.subtitle.enabled
        )

    def _request(self, job: Job) -> DigitalHumanVideoRequest:
        request = job.request
        if not isinstance(request, DigitalHumanVideoRequest):
            raise NodeExecutionError(
                ErrorCode.validation_invalid_options,
                "DigitalHuman workflow requires DigitalHumanVideoRequest.",
            )
        return request

    # ------------------------------------------------------------ node dispatch
    def _run_node(
        self, node_id: str, run: WorkflowRun, node_run: NodeRun, state: RunState
    ) -> NodeOutput:
        ctx = NodeContext(adapter=self, run=run, node_run=node_run, state=state)
        return NODE_HANDLERS[node_id](ctx)

    # ----------------------------------------------- shared node-facing services
    def _object_store(self):
        """Single resolution point for the object store.

        Resolving through this module's ``get_object_store`` keeps the symbol
        monkeypatchable for tests that patch
        ``packages.production.pipeline.digital_human.get_object_store``.
        """
        return get_object_store()

    def _artifact(
        self,
        run: WorkflowRun,
        node_run: NodeRun,
        kind: ArtifactKind,
        payload,
        payload_schema: str,
        uri: str | None = None,
        sha256: str | None = None,
        media_info: MediaInfo | None = None,
    ) -> Artifact:
        return self.repository.create_artifact(
            kind=kind,
            payload_schema=payload_schema,
            payload=payload,
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id,
            uri=uri,
            sha256=sha256,
            media_info=media_info,
        )

    def _source_artifact_for_asset(self, asset_id: str | None) -> Artifact:
        if not asset_id:
            raise NodeExecutionError(ErrorCode.artifact_missing, "Media asset is missing.")
        asset = self.repository.media_assets.get(asset_id)
        if asset is None or not asset.source_artifact_id:
            raise NodeExecutionError(ErrorCode.artifact_missing, "Media source artifact is missing.")
        artifact = self.repository.artifacts.get(asset.source_artifact_id)
        if artifact is None or not artifact.uri:
            raise NodeExecutionError(ErrorCode.artifact_missing, "Media source artifact is missing.")
        return artifact

    def _artifact_path(self, artifact: Artifact) -> Path:
        if not artifact.uri:
            raise NodeExecutionError(ErrorCode.artifact_missing, "Artifact URI is missing.")
        try:
            return local_object_path(get_object_store(), artifact.uri)
        except ValueError as exc:
            raise NodeExecutionError(ErrorCode.artifact_missing, "Artifact URI is not locally readable.") from exc

    def _first_available_provider_profile(self, capability: str, *, include_sandbox: bool = True):
        for profile in self.repository.provider_profiles.values():
            if profile.capability != capability or not profile.enabled:
                continue
            if not include_sandbox and profile.provider_id == "sandbox":
                continue
            if profile.provider_id not in self.provider_gateway.plugins:
                continue
            if profile.secret_ref and not self.provider_gateway._secret_is_active(profile.secret_ref):
                continue
            return profile
        return None

    def _tts_provider_profile_id(self, request: DigitalHumanVideoRequest) -> str:
        explicit_profile_id = request.voice.provider_profile_id
        voice = self.repository.voices.get(request.voice.voice_id or "")
        voice_profile_id = voice.provider_profile_id if voice is not None else None
        profile_id = explicit_profile_id or voice_profile_id

        def _fallback_or_raise(reason: str) -> str:
            if sandbox_fallback_allowed():
                return "sandbox.tts.default"
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                f"未配置可用的真实 TTS 供应商（{reason}）。请在「设置」中配置并启用真实 TTS 供应商及密钥。",
            )

        if not profile_id:
            return _fallback_or_raise("声音未绑定供应商配置")
        profile = self._provider_profile_by_id(profile_id)
        if profile is None or profile.capability != "tts.speech":
            if explicit_profile_id:
                raise NodeExecutionError(
                    ErrorCode.provider_unsupported_option,
                    "TTS provider profile is missing or incompatible.",
                )
            return _fallback_or_raise("声音的供应商配置缺失或能力不匹配")
        if not profile.enabled:
            return _fallback_or_raise(f"供应商配置 {profile.id} 未启用")
        if profile.provider_id not in self.provider_gateway.plugins:
            return _fallback_or_raise(f"供应商 {profile.provider_id} 未注册")
        if profile.secret_ref and not self.provider_gateway._secret_is_active(profile.secret_ref):
            return _fallback_or_raise(f"供应商配置 {profile.id} 的密钥未激活")
        return profile.id

    def _image_cover_profile_id(self, request: DigitalHumanVideoRequest) -> str | None:
        """Return a real ``image.generate`` ProviderProfile id only when AI cover
        is requested AND an enabled real profile + active secret exist. Otherwise
        ``None`` -> the cover node uses the existing frame-based cover. AI cover is
        PAID, so without a configured+secret-active image profile we never call it."""
        explicit_profile_id = request.cover.template_id
        if explicit_profile_id:
            profile = self._provider_profile_by_id(explicit_profile_id)
            return profile.id if self._is_real_image_profile(profile) else None
        for profile in self.repository.provider_profiles.values():
            if self._is_real_image_profile(profile):
                return profile.id
        return None

    def _is_real_image_profile(self, profile) -> bool:
        if profile is None or profile.capability != "image.generate" or not profile.enabled:
            return False
        if profile.provider_id == "sandbox":
            return False
        if profile.provider_id not in self.provider_gateway.plugins:
            return False
        if profile.secret_ref and not self.provider_gateway._secret_is_active(profile.secret_ref):
            return False
        return True

    def _provider_profile_by_id(self, profile_id: str):
        reader = getattr(self.provider_gateway, "provider_reader", None)
        if reader is not None:
            profile = reader.get_profile(profile_id)
            if profile is not None:
                return profile
        return self.repository.provider_profiles.get(profile_id)

    def _is_real_lipsync_profile(self, profile) -> bool:
        """A real lipsync path is active only when the profile is enabled, its
        provider plugin is registered, it is NOT the sandbox provider, and its
        secret (if any) is active. Without a secret this returns False, so the
        sandbox pass-through path runs — byte-identical to today."""
        if profile is None or profile.capability != "lipsync.video" or not profile.enabled:
            return False
        if profile.provider_id == "sandbox":
            return False
        if profile.provider_id not in self.provider_gateway.plugins:
            return False
        if profile.secret_ref and not self.provider_gateway._secret_is_active(profile.secret_ref):
            return False
        return True

    def _resolve_lipsync_profile(self, request: DigitalHumanVideoRequest):
        """Return ``(profile, is_real)`` for the requested lipsync profile.

        ``is_real`` is True only when a real enabled profile + active secret
        exist. Otherwise the caller uses the requested profile as-is (the gateway
        routes the seeded sandbox provider for ``runninghub.heygem.default``)."""
        profile = self._provider_profile_by_id(request.lipsync.provider_profile_id)
        return profile, self._is_real_lipsync_profile(profile)

    def _select_lipsync_fallback_profile(self, current_profile, error_message: str):
        """Mirror the origin asymmetry: HeyGem -> VideoReTalk always; VideoReTalk
        -> HeyGem only on a content-policy error. Returns the first registered,
        enabled, secret-active real profile of the fallback provider, or None."""
        if current_profile is None:
            return None
        provider_id = current_profile.provider_id
        if provider_id == "runninghub.heygem":
            target_provider = "dashscope.videoretalk"
        elif provider_id == "dashscope.videoretalk" and _is_lipsync_content_policy_error(error_message):
            target_provider = "runninghub.heygem"
        else:
            return None
        for profile in self.repository.provider_profiles.values():
            if profile.provider_id != target_provider:
                continue
            if self._is_real_lipsync_profile(profile):
                return profile
        return None

    def _narration_units_from_segments(self, segments, fallback_duration: float) -> list[NarrationUnit]:
        units: list[NarrationUnit] = []
        if not isinstance(segments, list):
            segments = []
        for index, segment in enumerate(segments, start=1):
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            start = float(segment.get("start") or segment.get("start_sec") or 0)
            end = float(segment.get("end") or segment.get("end_sec") or start)
            if end <= start:
                end = start + 0.3
            units.append(
                NarrationUnit(
                    unit_id=f"unit_{index}",
                    text=text,
                    start=round(start, 3),
                    end=round(end, 3),
                    confidence=float(segment.get("confidence") or segment.get("word_confidence") or 0.8),
                )
            )
        if units:
            return units
        return [
            NarrationUnit(
                unit_id="unit_1",
                text="",
                start=0,
                end=round(fallback_duration, 3),
                confidence=0.5,
            )
        ]

    # --------------------------------------------------- node test entry points
    #
    # The pipeline dispatches every node through ``_run_node`` / ``NODE_HANDLERS``.
    # These two thin wrappers additionally preserve the historical
    # ``adapter._<node>(run, node_run, state)`` call surface used by unit tests
    # that build adapters via ``object.__new__`` and invoke a single node.
    def _narration_alignment(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        return nodes.narration_alignment.run(NodeContext(adapter=self, run=run, node_run=node_run, state=state))

    def _finalize_run_report(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        return nodes.finalize_run_report.run(NodeContext(adapter=self, run=run, node_run=node_run, state=state))

    # ----------------------------------------------------------- run reporting
    def _write_report(
        self,
        run: WorkflowRun,
        state: RunState,
        *,
        failed: bool,
        node_run: NodeRun | None = None,
    ) -> tuple[Artifact, Artifact]:
        node_runs = self.repository.node_runs.get(run.id, [])
        public = RunPublicReportArtifact(
            run_id=run.id,
            status=RunStatus.failed if failed else RunStatus.succeeded,
            summary="Run failed." if failed else "Run completed.",
            node_statuses={node.node_id: node.status for node in node_runs},
            warnings=state.warnings,
            degradations=[notice.code for notice in state.degradations],
        )
        debug = RunDebugReportArtifact(
            **public.model_dump(),
            artifact_ids=list(self.repository.artifacts.keys()),
            provider_invocation_ids=state.provider_invocation_ids,
            node_errors=[node.error for node in node_runs if node.error is not None],
        )
        public_artifact = self.repository.create_artifact(
            kind=ArtifactKind.run_report_public,
            payload_schema="RunPublicReportArtifact.v1",
            payload=public.model_dump(mode="json"),
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id if node_run else None,
        )
        debug_artifact = self.repository.create_artifact(
            kind=ArtifactKind.run_report_debug,
            payload_schema="RunDebugReportArtifact.v1",
            payload=debug.model_dump(mode="json"),
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id if node_run else None,
        )
        self.repository.runs[run.id] = self.repository.runs[run.id].model_copy(
            update={
                "public_report_artifact_id": public_artifact.id,
                "debug_report_artifact_id": debug_artifact.id,
                "updated_at": utcnow(),
            }
        )
        return public_artifact, debug_artifact


def build_digital_human_workflow(
    repository: Repository,
    *,
    provider_gateway: ProviderGateway | None = None,
    prompt_registry: PromptRegistry | None = None,
    seed_media: bool = True,
) -> LocalRuntimeAdapter:
    return LocalRuntimeAdapter(
        repository,
        provider_gateway or ProviderGateway(repository),
        prompt_registry or PromptRegistry(repository),
        seed_media=seed_media,
    )


DigitalHumanWorkflow = LocalRuntimeAdapter
