from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from packages.ai.gateway import ProviderCall, ProviderGateway
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    Artifact,
    ArtifactRef,
    ArtifactKind,
    DegradationNotice,
    DigitalHumanVideoRequest,
    ErrorCode,
    FinishedVideo,
    Job,
    MediaInfo,
    NodeRun,
    NodeStatus,
    JobStatus,
    RunDebugReportArtifact,
    RunPublicReportArtifact,
    RunStatus,
    ScriptVersion,
    ValidatedProductionSpec,
    VideoVersion,
    WarningCode,
    WorkflowRun,
    WorkflowTemplate,
    NodeSpec,
    WorkflowEdge,
    utcnow,
)
from packages.core.contracts.artifacts import (
    AlignmentArtifact,
    AlignmentSegment,
    BgmPlan,
    BrollPlanArtifact,
    CaseContextArtifact,
    CreativeIntentArtifact,
    FontPlan,
    LipSyncReportArtifact,
    MaterialCandidate,
    MaterialPackArtifact,
    NarrationUnit,
    NarrationUnitsArtifact,
    PortraitPlanArtifact,
    RenderPlanArtifact,
    StylePlanArtifact,
    SubtitleStylePlan,
    TimelinePlanArtifact,
    TimelineTrackSegment,
    TimelineValidationReport,
)
from packages.core.contracts.state_machines import assert_transition
from packages.core.observability import record_node_run, record_workflow_run
from packages.core.storage import Repository
from packages.core.storage.object_store import get_object_store
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError, NodeOutput, WorkflowRuntimeAdapter, manifest_hash
from packages.media.assets import local_object_path, store_file
from packages.media.audio import synthesize_sandbox_tts
from packages.media.video.ffmpeg import (
    FfmpegCommandError,
    FfmpegRunner,
    extract_thumbnails,
    ffmpeg_bin,
    probe_media,
    probe_video_frame_count,
)
from packages.production.pipeline.reuse import ReusePlan, ReuseSourceRun, compute_reuse_plan


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

_EPHEMERAL_ARTIFACT_KINDS = {
    ArtifactKind.video_portrait_track,
    ArtifactKind.video_lipsync,
    ArtifactKind.video_rendered,
}

logger = logging.getLogger(__name__)


def digital_human_template() -> WorkflowTemplate:
    provider_side_effect_nodes = {"TTS", "ResolveCreativeIntent", "LipSync"}
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


@dataclass
class RunState:
    request: DigitalHumanVideoRequest
    artifacts: dict[ArtifactKind, Artifact] = field(default_factory=dict)
    provider_invocation_ids: list[str] = field(default_factory=list)
    warnings: list[WarningCode] = field(default_factory=list)
    degradations: list[DegradationNotice] = field(default_factory=list)

    def require(self, kind: ArtifactKind) -> Artifact:
        if kind not in self.artifacts:
            raise NodeExecutionError(ErrorCode.artifact_missing, f"Missing artifact {kind.value}.")
        return self.artifacts[kind]


def degradation_notice(
    code: WarningCode,
    message: str,
    *,
    node_id: str | None = None,
    affects_true_yield: bool = False,
) -> DegradationNotice:
    return DegradationNotice(
        code=code,
        message=message,
        node_id=node_id,
        affects_true_yield=affects_true_yield,
    )


class LocalRuntimeAdapter(WorkflowRuntimeAdapter):
    def __init__(
        self,
        repository: Repository,
        provider_gateway: ProviderGateway,
        prompt_registry: PromptRegistry,
    ) -> None:
        self.repository = repository
        self.provider_gateway = provider_gateway
        self.prompt_registry = prompt_registry
        self.template = digital_human_template()
        self._ensure_seed_media_assets()

    def _ensure_seed_media_assets(self) -> None:
        seed_dir = Path(".data/generated-media/seed")
        seed_dir.mkdir(parents=True, exist_ok=True)
        specs = {
            "asset_portrait_demo": {
                "filename": "portrait_demo_15s.mp4",
                "content_type": "video/mp4",
                "generator": lambda path: self._generate_seed_video(
                    path, duration_sec=15, width=320, height=568, fps=30
                ),
            },
            "asset_broll_demo": {
                "filename": "broll_demo_4s.mp4",
                "content_type": "video/mp4",
                "generator": lambda path: self._generate_seed_video(
                    path, duration_sec=4, width=320, height=568, fps=30
                ),
            },
            "asset_bgm_demo": {
                "filename": "bgm_demo_15s.wav",
                "content_type": "audio/wav",
                "generator": lambda path: self._generate_seed_audio(path, duration_sec=15),
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

    def _generate_seed_video(
        self,
        output_path: Path,
        *,
        duration_sec: float,
        width: int,
        height: int,
        fps: int,
    ) -> None:
        FfmpegRunner().run(
            [
                ffmpeg_bin(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={width}x{height}:rate={fps}",
                "-t",
                f"{duration_sec:.3f}",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )

    def _generate_seed_audio(self, output_path: Path, *, duration_sec: float) -> None:
        FfmpegRunner().run(
            [
                ffmpeg_bin(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:sample_rate=44100:duration={duration_sec:.3f}",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ]
        )

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

    def get_run_status(self, run_id: str) -> RunStatus | None:
        run = self.repository.runs.get(run_id)
        return run.status if run else None

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
        return self.cancel_run(run_id, force=force, reason=reason) or self.repository.runs[run_id]

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
            output = self._run_node(node_id, run, node_run, state)
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
        self.repository.record_yield_funnel_event(
            job_id=job.id,
            run_id=run.id,
            event_type=f"workflow_{final_status.value}",
            dedupe_key=f"{run.id}:workflow_{final_status.value}",
            event_time=self.repository.runs[run.id].updated_at,
        )

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

    def _run_node(
        self, node_id: str, run: WorkflowRun, node_run: NodeRun, state: RunState
    ) -> NodeOutput:
        handlers = {
            "ValidateRequest": self._validate_request,
            "LoadCaseContext": self._load_case_context,
            "ResolveCreativeIntent": self._resolve_creative_intent,
            "TTS": self._tts,
            "MaterialPackPlanning": self._material_pack_planning,
            "NarrationAlignment": self._narration_alignment,
            "PortraitPlanning": self._portrait_planning,
            "BrollPlanning": self._broll_planning,
            "StylePlanning": self._style_planning,
            "TimelinePlanning": self._timeline_planning,
            "PortraitTrackBuild": self._portrait_track_build,
            "LipSync": self._lipsync,
            "RenderFinalTimeline": self._render_final_timeline,
            "SubtitleAndBgmMix": self._subtitle_and_bgm_mix,
            "ExportFinishedVideo": self._export_finished_video,
            "FinalizeRunReport": self._finalize_run_report,
        }
        return handlers[node_id](run, node_run, state)

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

    def _validate_request(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        request = state.request
        if request.case_id not in self.repository.cases:
            raise NodeExecutionError(ErrorCode.validation_missing_case, "Case does not exist.")
        if not request.script.strip():
            raise NodeExecutionError(ErrorCode.validation_missing_script, "Script is required.")
        voice_id = request.voice.voice_id or "voice_sandbox"
        if voice_id not in self.repository.voices or not self.repository.voices[voice_id].enabled:
            raise NodeExecutionError(ErrorCode.validation_missing_voice, "Voice is missing or disabled.")
        if request.lipsync.enabled:
            profile = self.repository.provider_profiles.get(request.lipsync.provider_profile_id)
            if profile is None or profile.capability != "lipsync.video":
                raise NodeExecutionError(
                    ErrorCode.provider_unsupported_option,
                    "LipSync provider profile is missing or incompatible.",
                )
        artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.validated_production_spec,
            ValidatedProductionSpec(
                request=request,
                workflow_template_id=self.template.workflow_template_id,
                workflow_version=self.template.version,
            ).model_dump(mode="json"),
            "ValidatedProductionSpec.v1",
        )
        return NodeOutput(artifacts=[artifact])

    def _load_case_context(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        case = self.repository.cases[state.request.case_id]
        payload = CaseContextArtifact(
            case_id=case.id,
            case_profile=case.model_dump(mode="json"),
            active_memories=[
                memory.model_dump(mode="json")
                for memory in self.repository.memories.values()
                if memory.case_id == case.id and memory.status == "active"
            ],
            recent_script_versions=[
                script
                for script in self.repository.scripts.values()
                if script.case_id == case.id
            ][-10:],
            performance_summary={
                "observations": [
                    obs.model_dump(mode="json")
                    for obs in self.repository.performance_observations.values()
                    if obs.case_id == case.id
                ][-50:]
            },
        ).model_dump(mode="json")
        return NodeOutput(
            artifacts=[
                self._artifact(
                    run,
                    node_run,
                    ArtifactKind.case_context,
                    payload,
                    "CaseContextArtifact.v1",
                )
            ]
        )

    def _resolve_creative_intent(
        self, run: WorkflowRun, node_run: NodeRun, state: RunState
    ) -> NodeOutput:
        if state.request.creative_intent_ref:
            existing = self.repository.artifacts.get(state.request.creative_intent_ref.artifact_id)
            if existing is None:
                raise NodeExecutionError(ErrorCode.artifact_missing, "Creative intent artifact missing.")
            return NodeOutput(artifacts=[existing], status=NodeStatus.skipped)
        profile = self._first_available_provider_profile("llm.chat", include_sandbox=False)
        if profile is None:
            profile = self.repository.provider_profiles["sandbox.llm.default"]
        prompt_invocation, rendered = self.prompt_registry.render(
            node_id="ResolveCreativeIntent",
            variables={"script": state.request.script},
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id,
            provider_profile_id=profile.id,
        )
        invocation, result = self.provider_gateway.invoke(
            ProviderCall(
                case_id=run.case_id,
                run_id=run.id,
                node_run_id=node_run.id,
                provider_profile_id=profile.id,
                capability_id="llm.chat",
                prompt_version_id=prompt_invocation.prompt_version_id,
                input={"prompt": rendered, "script": state.request.script},
            )
        )
        if result is None or invocation.error:
            raise NodeExecutionError(
                invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
                invocation.error.message if invocation.error else "Provider failed.",
                retryable=True,
            )
        self.prompt_registry.validate_output(
            prompt_version_id=prompt_invocation.prompt_version_id,
            output=result.output,
        )
        prompt_invocation = prompt_invocation.model_copy(
            update={"provider_invocation_id": invocation.id, "updated_at": utcnow()}
        )
        self.repository.prompt_invocations[prompt_invocation.id] = prompt_invocation
        artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.creative_intent,
            CreativeIntentArtifact(intent=result.output.get("intent")).model_dump(mode="json"),
            "CreativeIntentArtifact.v1",
        )
        return NodeOutput(artifacts=[artifact], provider_invocation_ids=[invocation.id])

    def _tts(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        voice_id = state.request.voice.voice_id or "voice_sandbox"
        provider_profile_id = self._tts_provider_profile_id(state.request)
        invocation, result = self.provider_gateway.invoke(
            ProviderCall(
                case_id=run.case_id,
                run_id=run.id,
                node_run_id=node_run.id,
                provider_profile_id=provider_profile_id,
                capability_id="tts.speech",
                input={"text": state.request.script, "voice_id": voice_id},
            )
        )
        if result is None or invocation.error:
            raise NodeExecutionError(
                invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
                invocation.error.message if invocation.error else "TTS provider failed.",
                retryable=True,
            )
        provider_artifact_id = result.output.get("audio_artifact_id")
        if isinstance(provider_artifact_id, str) and provider_artifact_id in self.repository.artifacts:
            return NodeOutput(
                artifacts=[self.repository.artifacts[provider_artifact_id]],
                provider_invocation_ids=[invocation.id],
            )
        object_store = get_object_store()
        try:
            with tempfile.TemporaryDirectory(prefix="cutagent-tts-") as directory:
                wav_path = Path(directory) / f"{run.id}_tts.wav"
                synthesize_sandbox_tts(
                    state.request.script,
                    wav_path,
                    speed=state.request.voice.speed,
                    volume=state.request.voice.volume,
                )
                media_info = probe_media(wav_path)
                stored = store_file(object_store, wav_path, purpose="generated-audio")
        except FfmpegCommandError as exc:
            raise NodeExecutionError(exc.error_code, "Sandbox TTS audio generation failed.") from exc
        artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.audio_tts,
            None,
            "uri-only",
            uri=stored.ref.uri,
            sha256=stored.sha256,
            media_info=media_info,
        )
        return NodeOutput(artifacts=[artifact], provider_invocation_ids=[invocation.id])

    def _material_pack_planning(
        self, run: WorkflowRun, node_run: NodeRun, state: RunState
    ) -> NodeOutput:
        request = state.request
        assets = list(self.repository.media_assets.values())
        portrait = [
            asset.id
            for asset in assets
            if asset.usable
            and asset.kind == "portrait"
            and (asset.case_id in {None, request.case_id})
            and (
                request.portrait.template_mode == "agent"
                or asset.id == request.portrait.specific_template_id
                or asset.id in request.portrait.template_sequence_ids
            )
        ]
        broll = [
            asset.id
            for asset in assets
            if asset.usable
            and asset.kind == "broll"
            and (asset.case_id in {None, request.case_id})
            and (request.broll.case_id is None or asset.case_id == request.broll.case_id)
        ]
        bgm = [
            asset.id
            for asset in assets
            if asset.usable and asset.kind == "bgm" and asset.case_id in {None, request.case_id}
        ]
        fonts = [
            asset.id
            for asset in assets
            if asset.usable and asset.kind == "font" and asset.case_id in {None, request.case_id}
        ]
        payload = MaterialPackArtifact(
            case_id=request.case_id,
            portrait_candidates=[
                MaterialCandidate(asset_id=asset_id, score=1, reason="seeded usable portrait")
                for asset_id in portrait
            ],
            broll_candidates=[
                MaterialCandidate(asset_id=asset_id, score=1, reason="seeded usable b-roll")
                for asset_id in broll
            ],
            bgm_candidates=[
                MaterialCandidate(asset_id=asset_id, score=1, reason="seeded usable bgm")
                for asset_id in bgm
            ],
            font_candidates=[
                MaterialCandidate(asset_id=asset_id, score=1, reason="seeded usable font")
                for asset_id in fonts
            ],
            diagnostics={
                "portrait_missing": not bool(portrait),
                "broll_missing": request.broll.enabled and not bool(broll),
                "bgm_missing": request.bgm.enabled and not bool(bgm),
            },
            reservations=[new_id("reserve")],
        ).model_dump(mode="json")
        return NodeOutput(
            artifacts=[
                self._artifact(
                    run,
                    node_run,
                    ArtifactKind.plan_material_pack,
                    payload,
                    "MaterialPackPlanArtifact.v1",
                )
            ]
        )

    def _narration_alignment(
        self, run: WorkflowRun, node_run: NodeRun, state: RunState
    ) -> NodeOutput:
        tts = state.require(ArtifactKind.audio_tts)
        duration = float(tts.media_info.duration_sec if tts.media_info and tts.media_info.duration_sec else 1)

        def estimated_output(
            *,
            provider_invocation_ids: list[str] | None = None,
            warnings: list[WarningCode] | None = None,
            degradations: list[DegradationNotice] | None = None,
        ) -> NodeOutput:
            parts = [part.strip() for part in re.split(r"[。！？.!?；;]+", state.request.script) if part.strip()]
            if not parts:
                parts = [state.request.script]
            weights = [max(1, len([char for char in part if not char.isspace()])) for part in parts]
            total_weight = sum(weights)
            units: list[NarrationUnit] = []
            cursor = 0.0
            for index, (text, weight) in enumerate(zip(parts, weights, strict=True)):
                if index == len(parts) - 1:
                    end = duration
                else:
                    end = cursor + duration * (weight / total_weight)
                units.append(
                    NarrationUnit(
                        unit_id=f"unit_{index + 1}",
                        text=text,
                        start=round(cursor, 3),
                        end=round(end, 3),
                        confidence=0.5,
                    )
                )
                cursor = end
            alignment = AlignmentArtifact(
                audio_artifact_id=tts.id,
                segments=[
                    AlignmentSegment(
                        text=unit.text,
                        start_sec=unit.start,
                        end_sec=unit.end,
                        word_confidence=unit.confidence,
                    )
                    for unit in units
                ],
            )
            narration = NarrationUnitsArtifact(
                source="estimated",
                units=units,
                strict=False,
                warnings=[WarningCode.timestamp_estimated.value],
            )
            return NodeOutput(
                artifacts=[
                    self._artifact(
                        run,
                        node_run,
                        ArtifactKind.audio_alignment,
                        alignment.model_dump(mode="json"),
                        "AlignmentArtifact.v1",
                    ),
                    self._artifact(
                        run,
                        node_run,
                        ArtifactKind.narration_units,
                        narration.model_dump(mode="json"),
                        "NarrationUnitsArtifact.v1",
                    ),
                ],
                warnings=warnings or [],
                degradations=degradations or [],
                provider_invocation_ids=provider_invocation_ids or [],
            )

        asr_profile = self._first_available_provider_profile("asr.transcribe")
        if asr_profile is not None and tts.uri:
            audio_url = get_object_store().signed_url(tts.uri).url
            invocation, result = self.provider_gateway.invoke(
                ProviderCall(
                    case_id=run.case_id,
                    run_id=run.id,
                    node_run_id=node_run.id,
                    provider_profile_id=asr_profile.id,
                    capability_id="asr.transcribe",
                    input={"audio_uri": audio_url, "language_hints": ["zh"]},
                )
            )
            if result is None or invocation.error:
                if not state.request.strictness.strict_timestamps:
                    error_code = (
                        invocation.error.code.value
                        if invocation.error and hasattr(invocation.error.code, "value")
                        else str(invocation.error.code if invocation.error else ErrorCode.provider_remote_failed.value)
                    )
                    degradation = DegradationNotice(
                        code=WarningCode.timestamp_estimated,
                        message="ASR unavailable; estimated narration timestamps used.",
                        node_id=node_run.node_id,
                        details={
                            "reason": "asr_unavailable_estimated_fallback",
                            "provider_invocation_id": invocation.id,
                            "provider_error_code": error_code,
                        },
                    )
                    return estimated_output(
                        provider_invocation_ids=[invocation.id],
                        warnings=[WarningCode.timestamp_estimated],
                        degradations=[degradation],
                    )
                raise NodeExecutionError(
                    invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
                    invocation.error.message if invocation.error else "ASR provider failed.",
                    retryable=True,
                )
            units = self._narration_units_from_segments(result.output.get("segments", []), duration)
            alignment = AlignmentArtifact(
                audio_artifact_id=tts.id,
                segments=[
                    AlignmentSegment(
                        text=unit.text,
                        start_sec=unit.start,
                        end_sec=unit.end,
                        word_confidence=unit.confidence,
                    )
                    for unit in units
                ],
            )
            narration = NarrationUnitsArtifact(
                source="asr",
                units=units,
                strict=True,
                warnings=[],
            )
            return NodeOutput(
                artifacts=[
                    self._artifact(
                        run,
                        node_run,
                        ArtifactKind.audio_alignment,
                        alignment.model_dump(mode="json"),
                        "AlignmentArtifact.v1",
                    ),
                    self._artifact(
                        run,
                        node_run,
                        ArtifactKind.narration_units,
                        narration.model_dump(mode="json"),
                        "NarrationUnitsArtifact.v1",
                    ),
                ],
                provider_invocation_ids=[invocation.id],
            )
        if state.request.strictness.strict_timestamps:
            raise NodeExecutionError(
                ErrorCode.render_invalid_timeline,
                "Estimated narration timestamps are not allowed in strict alignment mode.",
            )
        return estimated_output()

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
        voice = self.repository.voices.get(request.voice.voice_id or "voice_sandbox")
        voice_profile_id = voice.provider_profile_id if voice is not None else None
        profile_id = explicit_profile_id or voice_profile_id
        if not profile_id:
            return "sandbox.tts.default"
        profile = self._provider_profile_by_id(profile_id)
        if profile is None or profile.capability != "tts.speech":
            if explicit_profile_id:
                raise NodeExecutionError(
                    ErrorCode.provider_unsupported_option,
                    "TTS provider profile is missing or incompatible.",
                )
            return "sandbox.tts.default"
        if not profile.enabled:
            return "sandbox.tts.default"
        if profile.provider_id not in self.provider_gateway.plugins:
            return "sandbox.tts.default"
        if profile.secret_ref and not self.provider_gateway._secret_is_active(profile.secret_ref):
            return "sandbox.tts.default"
        return profile.id

    def _provider_profile_by_id(self, profile_id: str):
        reader = getattr(self.provider_gateway, "provider_reader", None)
        if reader is not None:
            profile = reader.get_profile(profile_id)
            if profile is not None:
                return profile
        return self.repository.provider_profiles.get(profile_id)

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

    def _portrait_planning(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        material = state.require(ArtifactKind.plan_material_pack).payload or {}
        narration = state.require(ArtifactKind.narration_units).payload or {}
        portraits = [item.get("asset_id") for item in material.get("portrait_candidates", []) if item.get("asset_id")]
        if state.request.strictness.portrait_insufficient_policy == "hard_fail" and not portraits:
            raise NodeExecutionError(
                ErrorCode.material_insufficient_portrait,
                "Portrait main track cannot cover the full audio.",
            )
        duration = max([float(unit.get("end", 0)) for unit in narration.get("units", [])] or [1])
        asset_id = portraits[0] if portraits else None
        source_artifact = self._source_artifact_for_asset(asset_id) if asset_id else None
        source_duration = (
            float(source_artifact.media_info.duration_sec or 0)
            if source_artifact and source_artifact.media_info
            else 0
        )
        if asset_id and source_duration + (1 / state.request.output.fps) < duration:
            raise NodeExecutionError(
                ErrorCode.material_insufficient_portrait,
                "Portrait source window cannot cover the full audio.",
            )
        payload = PortraitPlanArtifact(
            fps=state.request.output.fps,
            total_duration=duration,
            asset_id=asset_id,
            duration_sec=duration,
            segments=[
                {
                    "asset_id": asset_id,
                    "start_sec": 0,
                    "end_sec": duration,
                    "source_start": 0,
                    "source_end": duration,
                    "role": "main",
                    "unit_ids": [unit.get("unit_id") for unit in narration.get("units", [])],
                }
            ],
        ).model_dump(mode="json")
        return NodeOutput(
            artifacts=[
                self._artifact(run, node_run, ArtifactKind.plan_portrait, payload, "PortraitPlanArtifact.v1")
            ]
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

    def _transcode_video_segment(
        self,
        source_path: Path,
        output_path: Path,
        *,
        source_start: float,
        duration: float,
        width: int,
        height: int,
        fps: int,
    ) -> None:
        FfmpegRunner().run(
            [
                ffmpeg_bin(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{source_start:.3f}",
                "-t",
                f"{duration:.3f}",
                "-i",
                str(source_path),
                "-an",
                "-vf",
                (
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},fps={fps},setsar=1"
                ),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )

    def _concat_video_segments(self, segments: list[Path], output_path: Path) -> None:
        concat_list = output_path.with_suffix(".txt")
        concat_list.write_text(
            "\n".join(f"file '{str(path).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'" for path in segments),
            encoding="utf-8",
        )
        FfmpegRunner().run(
            [
                ffmpeg_bin(),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )

    def _render_video_timeline(
        self,
        *,
        main_path: Path,
        output_path: Path,
        broll_segments: list[dict],
        total_frames: int,
        width: int,
        height: int,
        fps: int,
    ) -> None:
        args = [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(main_path),
        ]
        overlay_inputs: list[tuple[dict, Path]] = []
        for segment in broll_segments:
            source_artifact = self._source_artifact_for_asset(segment.get("asset_id"))
            source_path = self._artifact_path(source_artifact)
            source_info = source_artifact.media_info or probe_media(source_path)
            source_duration = float(source_info.duration_sec or 0)
            source_start = float(segment.get("source_start", 0) or 0)
            source_end = float(segment.get("source_end", 0) or 0)
            if source_start < 0 or source_end <= source_start or source_end > source_duration + (1 / fps):
                raise NodeExecutionError(ErrorCode.render_invalid_timeline, "B-roll source window is out of bounds.")
            overlay_inputs.append((segment, source_path))
            args.extend(["-i", str(source_path)])

        filters = [
            (
                f"[0:v]fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},trim=start_frame=0:end_frame={total_frames},"
                "setpts=PTS-STARTPTS,setsar=1[base0]"
            )
        ]
        previous_label = "base0"
        total_duration = total_frames / fps
        for index, (segment, _) in enumerate(overlay_inputs, start=1):
            timeline_start = float(segment.get("start_sec", 0) or 0)
            timeline_end = float(segment.get("end_sec", 0) or 0)
            if timeline_start < 0 or timeline_end <= timeline_start or timeline_end > total_duration + (1 / fps):
                raise NodeExecutionError(ErrorCode.render_invalid_timeline, "B-roll timeline window is out of bounds.")
            source_start = float(segment.get("source_start", 0) or 0)
            source_end = float(segment.get("source_end", 0) or 0)
            overlay_label = f"ov{index}"
            next_label = f"base{index}"
            filters.append(
                (
                    f"[{index}:v]trim=start={source_start:.3f}:end={source_end:.3f},"
                    "setpts=PTS-STARTPTS,"
                    f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height},setsar=1,"
                    f"setpts=PTS-STARTPTS+{timeline_start:.3f}/TB[{overlay_label}]"
                )
            )
            filters.append(
                (
                    f"[{previous_label}][{overlay_label}]overlay="
                    f"enable='between(t,{timeline_start:.3f},{timeline_end:.3f})':"
                    f"x=0:y=0:eof_action=pass[{next_label}]"
                )
            )
            previous_label = next_label

        args.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                f"[{previous_label}]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        FfmpegRunner(timeout_sec=60).run(args)

    def _write_ass_subtitles(
        self,
        output_path: Path,
        *,
        narration: dict,
        style: dict,
        width: int,
        height: int,
    ) -> None:
        subtitle = style.get("subtitle", {}) if isinstance(style.get("subtitle"), dict) else {}
        font_size = int(subtitle.get("font_size") or 64)
        margin_v = int(height * 0.12)
        position = subtitle.get("position")
        if isinstance(position, dict) and "y" in position:
            margin_v = max(20, int(height * (1 - float(position["y"]))))
        lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "",
            "[V4+ Styles]",
            (
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
                "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
                "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
            ),
            (
                f"Style: Default,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
                f"1,0,0,0,100,100,0,0,1,4,1,2,80,80,{margin_v},1"
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
        for unit in narration.get("units", []):
            text = self._ass_escape(str(unit.get("text", "")))
            if not text:
                continue
            lines.append(
                "Dialogue: 0,"
                f"{self._ass_time(float(unit.get('start', 0) or 0))},"
                f"{self._ass_time(float(unit.get('end', 0) or 0))},"
                f"Default,,0,0,0,,{text}"
            )
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _render_final_media(
        self,
        *,
        rendered_path: Path,
        audio_path: Path,
        output_path: Path,
        subtitle_path: Path | None,
        bgm_path: Path | None,
        bgm_volume: float,
        duration: float,
        fps: int,
    ) -> None:
        args = [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(rendered_path),
            "-i",
            str(audio_path),
        ]
        if bgm_path is not None:
            args.extend(["-stream_loop", "-1", "-i", str(bgm_path)])
        escaped_subtitle = str(subtitle_path).replace("\\", "\\\\").replace(":", "\\:") if subtitle_path else None
        video_filters = "[0:v]"
        if escaped_subtitle:
            video_filters += f"subtitles={escaped_subtitle},"
        video_filters += f"fps={fps},format=yuv420p[v]"
        if bgm_path is None:
            audio_filters = (
                f"[1:a]aresample=48000,apad=pad_dur=1,atrim=0:{duration:.3f},"
                "asetpts=PTS-STARTPTS[a]"
            )
        else:
            audio_filters = (
                f"[1:a]aresample=48000,volume=1.0,apad=pad_dur=1,atrim=0:{duration:.3f},"
                "asetpts=PTS-STARTPTS[voice];"
                f"[2:a]aresample=48000,volume={bgm_volume:.3f},atrim=0:{duration:.3f},"
                "asetpts=PTS-STARTPTS[bgm];"
                "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=0[a]"
            )
        args.extend(
            [
                "-filter_complex",
                f"{video_filters};{audio_filters}",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        FfmpegRunner(timeout_sec=60).run(args)

    @staticmethod
    def _ass_time(seconds: float) -> str:
        centiseconds = round(max(seconds, 0) * 100)
        hours, remainder = divmod(centiseconds, 3600 * 100)
        minutes, remainder = divmod(remainder, 60 * 100)
        secs, cs = divmod(remainder, 100)
        return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"

    @staticmethod
    def _ass_escape(text: str) -> str:
        return text.replace("{", "").replace("}", "").replace("\n", r"\N")

    def _broll_planning(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        material = state.require(ArtifactKind.plan_material_pack).payload or {}
        broll = [item.get("asset_id") for item in material.get("broll_candidates", []) if item.get("asset_id")]
        if not state.request.broll.enabled:
            return NodeOutput(
                artifacts=[
                    self._artifact(
                        run,
                        node_run,
                        ArtifactKind.plan_broll,
                        BrollPlanArtifact(enabled=False, segments=[]).model_dump(mode="json"),
                        "BrollPlanArtifact.v1",
                    )
                ]
            )
        if state.request.broll.enabled and not broll:
            artifact = self._artifact(
                run,
                node_run,
                ArtifactKind.plan_broll,
                BrollPlanArtifact(
                    enabled=True,
                    segments=[],
                    skipped_reason=WarningCode.broll_skipped_no_material.value,
                ).model_dump(mode="json"),
                "BrollPlanArtifact.v1",
            )
            return NodeOutput(
                status=NodeStatus.degraded,
                artifacts=[artifact],
                degradations=[
                    degradation_notice(
                        WarningCode.broll_skipped_no_material,
                        "No b-roll material available.",
                        node_id=node_run.node_id,
                        affects_true_yield=True,
                    )
                ],
            )
        segments = []
        for index, asset_id in enumerate(broll[: state.request.broll.max_inserts]):
            start_sec = index * 3
            end_sec = start_sec + 2
            segments.append(
                {
                    "asset_id": asset_id,
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "source_start": 0,
                    "source_end": end_sec - start_sec,
                    "reason": "seeded usable b-roll",
                    "confidence": 1,
                }
            )
        return NodeOutput(
            artifacts=[
                self._artifact(
                    run,
                    node_run,
                    ArtifactKind.plan_broll,
                    BrollPlanArtifact(
                        enabled=state.request.broll.enabled,
                        segments=segments,
                        overlays=[
                            {
                                "overlay_id": f"broll_{index + 1}",
                                "asset_id": segment["asset_id"],
                                "timeline_start": segment["start_sec"],
                                "timeline_end": segment["end_sec"],
                                "source_start": segment["source_start"],
                                "source_end": segment["source_end"],
                                "reason": segment["reason"],
                                "confidence": segment["confidence"],
                            }
                            for index, segment in enumerate(segments)
                        ],
                    ).model_dump(mode="json"),
                    "BrollPlanArtifact.v1",
                )
            ]
        )

    def _style_planning(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        material = state.require(ArtifactKind.plan_material_pack).payload or {}
        bgm_candidates = [item.get("asset_id") for item in material.get("bgm_candidates", []) if item.get("asset_id")]
        font_candidates = [item.get("asset_id") for item in material.get("font_candidates", []) if item.get("asset_id")]
        degradations: list[DegradationNotice] = []
        warnings: list[WarningCode] = []
        bgm_asset_id = state.request.bgm.bgm_id or (bgm_candidates[0] if bgm_candidates else None)
        if state.request.bgm.enabled and not bgm_asset_id:
            degradations.append(
                degradation_notice(
                    WarningCode.bgm_skipped_library_unannotated,
                    "BGM library is not annotated.",
                    node_id=node_run.node_id,
                    affects_true_yield=False,
                )
            )
            warnings.append(WarningCode.bgm_skipped_library_unannotated)
        font_asset_id = font_candidates[0] if font_candidates else "case_default_font"
        if not font_candidates:
            warnings.append(WarningCode.font_default_used)
        artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.plan_style,
            StylePlanArtifact(
                subtitle=SubtitleStylePlan(
                    enabled=state.request.subtitle.enabled,
                    style_preset=state.request.subtitle.style_preset,
                    font_id=state.request.subtitle.font_id,
                    font_size=state.request.subtitle.font_size,
                    position=state.request.subtitle.position,
                ),
                bgm=BgmPlan(
                    enabled=state.request.bgm.enabled,
                    asset_id=bgm_asset_id,
                    volume=state.request.bgm.volume,
                    auto_mix=state.request.bgm.auto_mix,
                ),
                font=FontPlan(font_id=font_asset_id, size=state.request.subtitle.font_size),
                font_asset_id=font_asset_id,
                bgm_asset_id=bgm_asset_id,
                subtitle_enabled=state.request.subtitle.enabled,
            ).model_dump(mode="json"),
            "StylePlanArtifact.v1",
        )
        return NodeOutput(
            status=NodeStatus.degraded if degradations else NodeStatus.succeeded,
            artifacts=[artifact],
            warnings=warnings,
            degradations=degradations,
        )

    def _timeline_planning(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        portrait_artifact = state.require(ArtifactKind.plan_portrait)
        broll_artifact = state.require(ArtifactKind.plan_broll)
        portrait = portrait_artifact.payload or {}
        broll = broll_artifact.payload or {}
        duration = float(portrait.get("duration_sec", 0))
        if duration <= 0:
            raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Timeline duration is invalid.")
        fps = 30
        total_frames = max(1, round(duration * fps))

        def to_frame(seconds: float) -> int:
            return round(seconds * fps)

        raw_segments: list[dict] = []
        for index, segment in enumerate(portrait.get("segments", [])):
            raw_segments.append(
                {
                    "track_id": "portrait",
                    "segment_id": f"portrait_{index + 1}",
                    "asset_ref": self.repository.artifact_ref(portrait_artifact.id),
                    "start_sec": float(segment.get("start_sec", 0)),
                    "end_sec": float(segment.get("end_sec", duration)),
                    "source_start_sec": float(segment.get("source_start", 0)),
                    "source_end_sec": float(segment.get("source_end", segment.get("end_sec", duration))),
                }
            )
        for index, segment in enumerate(broll.get("segments", [])):
            raw_segments.append(
                {
                    "track_id": "broll",
                    "segment_id": f"broll_{index + 1}",
                    "asset_ref": self.repository.artifact_ref(broll_artifact.id),
                    "start_sec": float(segment.get("start_sec", 0)),
                    "end_sec": float(segment.get("end_sec", 0)),
                    "source_start_sec": float(segment.get("source_start", 0)),
                    "source_end_sec": float(segment.get("source_end", segment.get("end_sec", 0))),
                }
            )

        negative_duration = any(segment["end_sec"] <= segment["start_sec"] for segment in raw_segments)
        out_of_bounds = any(
            segment["start_sec"] < 0 or to_frame(segment["end_sec"]) > total_frames
            for segment in raw_segments
        )
        overlap = False
        by_track: dict[str, list[dict]] = {}
        for segment in raw_segments:
            by_track.setdefault(segment["track_id"], []).append(segment)
        for segments in by_track.values():
            ordered = sorted(segments, key=lambda item: item["start_sec"])
            previous_end = None
            for segment in ordered:
                if previous_end is not None and segment["start_sec"] < previous_end:
                    overlap = True
                previous_end = max(previous_end or segment["end_sec"], segment["end_sec"])
        if negative_duration or out_of_bounds or overlap:
            raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Timeline validation failed.")

        tracks = [
            TimelineTrackSegment(
                track_id=segment["track_id"],
                segment_id=segment["segment_id"],
                asset_ref=segment["asset_ref"],
                timeline_start_frame=to_frame(segment["start_sec"]),
                timeline_end_frame=to_frame(segment["end_sec"]),
                source_start_frame=to_frame(segment.get("source_start_sec", segment["start_sec"])),
                source_end_frame=to_frame(segment.get("source_end_sec", segment["end_sec"])),
            )
            for segment in raw_segments
        ]
        validation = TimelineValidationReport(
            valid=True,
            checks={
                "overlap": not overlap,
                "negative_duration": not negative_duration,
                "out_of_bounds": not out_of_bounds,
            },
        )
        timeline = TimelinePlanArtifact(
            fps=fps,
            total_frames=total_frames,
            tracks=tracks,
            validation=validation,
        )
        render_plan = RenderPlanArtifact(
            timeline_artifact_id="pending",
            render_size=(state.request.output.width, state.request.output.height),
            fps=fps,
            tracks=tracks,
        )
        timeline_artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.plan_timeline,
            timeline.model_dump(mode="json"),
            "TimelinePlanArtifact.v1",
        )
        render_plan = render_plan.model_copy(update={"timeline_artifact_id": timeline_artifact.id})
        return NodeOutput(
            artifacts=[
                timeline_artifact,
                self._artifact(
                    run,
                    node_run,
                    ArtifactKind.plan_render,
                    render_plan.model_dump(mode="json"),
                    "RenderPlanArtifact.v1",
                ),
            ]
        )

    def _portrait_track_build(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        portrait = state.require(ArtifactKind.plan_portrait).payload or {}
        duration = float(portrait.get("duration_sec", 0) or 0)
        segments = portrait.get("segments", [])
        if not segments:
            raise NodeExecutionError(ErrorCode.material_insufficient_portrait, "Portrait plan has no segments.")
        fps = int(portrait.get("fps") or state.request.output.fps)
        width = state.request.output.width
        height = state.request.output.height
        try:
            with tempfile.TemporaryDirectory(prefix="cutagent-portrait-") as directory:
                temp_dir = Path(directory)
                segment_paths: list[Path] = []
                for index, segment in enumerate(segments):
                    source_artifact = self._source_artifact_for_asset(segment.get("asset_id"))
                    source_path = self._artifact_path(source_artifact)
                    source_info = source_artifact.media_info or probe_media(source_path)
                    source_duration = float(source_info.duration_sec or 0)
                    source_start = float(segment.get("source_start", 0) or 0)
                    source_end = float(segment.get("source_end", segment.get("end_sec", 0)) or 0)
                    if source_start < 0 or source_end <= source_start or source_end > source_duration + (1 / fps):
                        raise NodeExecutionError(
                            ErrorCode.render_invalid_timeline,
                            "Portrait source window is out of bounds.",
                        )
                    output_path = temp_dir / f"portrait_segment_{index + 1}.mp4"
                    self._transcode_video_segment(
                        source_path,
                        output_path,
                        source_start=source_start,
                        duration=source_end - source_start,
                        width=width,
                        height=height,
                        fps=fps,
                    )
                    segment_paths.append(output_path)
                concat_path = temp_dir / "portrait_track.mp4"
                self._concat_video_segments(segment_paths, concat_path)
                media_info = probe_media(concat_path)
                if abs(float(media_info.duration_sec or 0) - duration) > (1 / fps):
                    raise NodeExecutionError(
                        ErrorCode.render_invalid_timeline,
                        "Portrait track duration does not match the plan.",
                    )
                stored = store_file(
                    get_object_store(),
                    concat_path,
                    purpose="generated-video",
                    tier="ephemeral",
                )
        except FfmpegCommandError as exc:
            raise NodeExecutionError(exc.error_code, "Portrait track build failed.") from exc
        artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.video_portrait_track,
            None,
            "uri-only",
            uri=stored.ref.uri,
            sha256=stored.sha256,
            media_info=media_info,
        )
        return NodeOutput(artifacts=[artifact])

    def _lipsync(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        portrait = state.require(ArtifactKind.video_portrait_track)
        audio = state.require(ArtifactKind.audio_tts)
        duration = float(audio.media_info.duration_sec if audio.media_info and audio.media_info.duration_sec else 0)
        if not state.request.lipsync.enabled:
            artifact = self._artifact(
                run,
                node_run,
                ArtifactKind.video_lipsync,
                None,
                "uri-only",
                uri=portrait.uri,
                sha256=portrait.sha256,
                media_info=portrait.media_info,
            )
            report = self._artifact(
                run,
                node_run,
                ArtifactKind.lipsync_report,
                LipSyncReportArtifact(
                    skipped=True,
                    skipped_reason="request.disabled",
                    input_video_artifact_id=portrait.id,
                    input_audio_artifact_id=audio.id,
                    output_video_artifact_id=artifact.id,
                ).model_dump(mode="json"),
                "LipSyncReportArtifact.v1",
            )
            return NodeOutput(status=NodeStatus.skipped, artifacts=[artifact, report])
        invocation, result = self.provider_gateway.invoke(
            ProviderCall(
                case_id=run.case_id,
                run_id=run.id,
                node_run_id=node_run.id,
                provider_profile_id=state.request.lipsync.provider_profile_id,
                capability_id="lipsync.video",
                input={"portrait_uri": portrait.uri or "", "audio_uri": audio.uri or "", "duration_sec": duration},
            )
        )
        if result is None or invocation.error:
            raise NodeExecutionError(
                invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
                invocation.error.message if invocation.error else "LipSync provider failed.",
                retryable=True,
            )
        provider_artifact_id = result.output.get("video_artifact_id")
        if isinstance(provider_artifact_id, str) and provider_artifact_id in self.repository.artifacts:
            artifact = self.repository.artifacts[provider_artifact_id]
            report = self._artifact(
                run,
                node_run,
                ArtifactKind.lipsync_report,
                LipSyncReportArtifact(
                    provider_invocation_id=invocation.id,
                    provider_profile_id=state.request.lipsync.provider_profile_id,
                    input_video_artifact_id=portrait.id,
                    input_audio_artifact_id=audio.id,
                    output_video_artifact_id=artifact.id,
                ).model_dump(mode="json"),
                "LipSyncReportArtifact.v1",
            )
            return NodeOutput(artifacts=[artifact, report], provider_invocation_ids=[invocation.id])
        artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.video_lipsync,
            None,
            "uri-only",
            uri=portrait.uri,
            sha256=portrait.sha256,
            media_info=portrait.media_info,
        )
        report = self._artifact(
            run,
            node_run,
            ArtifactKind.lipsync_report,
            LipSyncReportArtifact(
                provider_invocation_id=invocation.id,
                provider_profile_id=state.request.lipsync.provider_profile_id,
                skipped=True,
                skipped_reason="sandbox.pass_through",
                input_video_artifact_id=portrait.id,
                input_audio_artifact_id=audio.id,
                output_video_artifact_id=artifact.id,
                warnings=["sandbox_lipsync_passthrough"],
            ).model_dump(mode="json"),
            "LipSyncReportArtifact.v1",
        )
        return NodeOutput(artifacts=[artifact, report], provider_invocation_ids=[invocation.id])

    def _render_final_timeline(
        self, run: WorkflowRun, node_run: NodeRun, state: RunState
    ) -> NodeOutput:
        lipsync = state.require(ArtifactKind.video_lipsync)
        render_plan = state.require(ArtifactKind.plan_render).payload or {}
        timeline = state.require(ArtifactKind.plan_timeline).payload or {}
        broll_plan = state.require(ArtifactKind.plan_broll).payload or {}
        render_size = render_plan.get("render_size", [state.request.output.width, state.request.output.height])
        width = int(render_size[0])
        height = int(render_size[1])
        fps = int(render_plan.get("fps") or state.request.output.fps)
        total_frames = int(timeline.get("total_frames") or 0)
        if total_frames <= 0:
            raise NodeExecutionError(ErrorCode.render_invalid_timeline, "Render plan has no frames.")
        try:
            with tempfile.TemporaryDirectory(prefix="cutagent-render-") as directory:
                output_path = Path(directory) / "rendered.mp4"
                self._render_video_timeline(
                    main_path=self._artifact_path(lipsync),
                    output_path=output_path,
                    broll_segments=list(broll_plan.get("segments", [])),
                    total_frames=total_frames,
                    width=width,
                    height=height,
                    fps=fps,
                )
                media_info = probe_media(output_path)
                frame_count = probe_video_frame_count(output_path)
                if frame_count != total_frames:
                    raise NodeExecutionError(
                        ErrorCode.render_invalid_timeline,
                        "Rendered timeline frame count does not match the plan.",
                    )
                if media_info.width != width or media_info.height != height or round(media_info.fps or 0) != fps:
                    raise NodeExecutionError(
                        ErrorCode.render_invalid_timeline,
                        "Rendered timeline media info does not match the plan.",
                    )
                stored = store_file(
                    get_object_store(),
                    output_path,
                    purpose="generated-video",
                    tier="ephemeral",
                )
        except FfmpegCommandError as exc:
            raise NodeExecutionError(exc.error_code, "Final timeline rendering failed.") from exc
        artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.video_rendered,
            None,
            "uri-only",
            uri=stored.ref.uri,
            sha256=stored.sha256,
            media_info=media_info,
        )
        return NodeOutput(artifacts=[artifact])

    def _subtitle_and_bgm_mix(
        self, run: WorkflowRun, node_run: NodeRun, state: RunState
    ) -> NodeOutput:
        rendered = state.require(ArtifactKind.video_rendered)
        audio = state.require(ArtifactKind.audio_tts)
        timeline = state.require(ArtifactKind.plan_timeline).payload or {}
        style = state.require(ArtifactKind.plan_style).payload or {}
        narration = state.require(ArtifactKind.narration_units).payload or {}
        fps = int(timeline.get("fps") or state.request.output.fps)
        total_frames = int(timeline.get("total_frames") or 0)
        duration = total_frames / fps if total_frames else float(rendered.media_info.duration_sec or 0)
        subtitle_artifact = None
        try:
            with tempfile.TemporaryDirectory(prefix="cutagent-final-") as directory:
                temp_dir = Path(directory)
                subtitle_path = temp_dir / "subtitle.ass" if state.request.subtitle.enabled else None
                if subtitle_path is not None:
                    self._write_ass_subtitles(
                        subtitle_path,
                        narration=narration,
                        style=style,
                        width=state.request.output.width,
                        height=state.request.output.height,
                    )
                bgm_path = None
                bgm_plan = style.get("bgm") if isinstance(style.get("bgm"), dict) else {}
                bgm_asset_id = style.get("bgm_asset_id") or (bgm_plan or {}).get("asset_id")
                if bgm_plan and bgm_plan.get("enabled") and bgm_asset_id:
                    bgm_path = self._artifact_path(self._source_artifact_for_asset(bgm_asset_id))
                output_path = temp_dir / "final.mp4"
                self._render_final_media(
                    rendered_path=self._artifact_path(rendered),
                    audio_path=self._artifact_path(audio),
                    output_path=output_path,
                    subtitle_path=subtitle_path,
                    bgm_path=bgm_path,
                    bgm_volume=float((bgm_plan or {}).get("volume", state.request.bgm.volume)),
                    duration=duration,
                    fps=fps,
                )
                media_info = probe_media(output_path)
                if probe_video_frame_count(output_path) != total_frames:
                    raise NodeExecutionError(
                        ErrorCode.render_invalid_timeline,
                        "Final video frame count does not match the timeline.",
                    )
                final_stored = store_file(get_object_store(), output_path, purpose="generated-video")
                if subtitle_path is not None:
                    subtitle_stored = store_file(get_object_store(), subtitle_path, purpose="subtitles")
                    subtitle_artifact = self._artifact(
                        run,
                        node_run,
                        ArtifactKind.subtitle_ass,
                        None,
                        "uri-only",
                        uri=subtitle_stored.ref.uri,
                        sha256=subtitle_stored.sha256,
                        media_info=probe_media(subtitle_path),
                    )
        except FfmpegCommandError as exc:
            code = ErrorCode.render_subtitle_failed if state.request.subtitle.enabled else exc.error_code
            raise NodeExecutionError(code, "Subtitle/BGM mix rendering failed.") from exc
        final = self._artifact(
            run,
            node_run,
            ArtifactKind.video_final,
            None,
            "uri-only",
            uri=final_stored.ref.uri,
            sha256=final_stored.sha256,
            media_info=media_info,
        )
        artifacts = [final]
        if subtitle_artifact is not None:
            artifacts.append(subtitle_artifact)
        return NodeOutput(artifacts=artifacts)

    def _export_finished_video(
        self, run: WorkflowRun, node_run: NodeRun, state: RunState
    ) -> NodeOutput:
        final = state.require(ArtifactKind.video_final)
        timeline = state.require(ArtifactKind.plan_timeline)
        style = state.require(ArtifactKind.plan_style)
        script = ScriptVersion(
            id=state.request.script_version_id or new_id("script"),
            case_id=state.request.case_id,
            title=state.request.title or "Untitled script",
            script=state.request.script,
            creative_intent_artifact_id=state.artifacts.get(ArtifactKind.creative_intent).id
            if ArtifactKind.creative_intent in state.artifacts
            else None,
        )
        self.repository.scripts[script.id] = script
        video_artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.video_finished,
            None,
            "uri-only",
            uri=final.uri,
            sha256=final.sha256,
            media_info=final.media_info,
        )
        try:
            with tempfile.TemporaryDirectory(prefix="cutagent-cover-") as directory:
                thumbnails = extract_thumbnails(
                    self._artifact_path(final),
                    Path(directory),
                    labels=("first", "mid"),
                )
                selected = thumbnails[-1]
                cover_stored = store_file(get_object_store(), selected.path, purpose="covers")
        except FfmpegCommandError as exc:
            raise NodeExecutionError(exc.error_code, "Finished video cover extraction failed.") from exc
        cover_artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.cover_image,
            None,
            "uri-only",
            uri=cover_stored.ref.uri,
            sha256=cover_stored.sha256,
            media_info=selected.media_info,
        )
        finished = FinishedVideo(
            id=new_id("fv"),
            case_id=state.request.case_id,
            run_id=run.id,
            title=state.request.title or script.title,
            video_artifact=self.repository.artifact_ref(video_artifact.id),
            cover_artifact=self.repository.artifact_ref(cover_artifact.id),
            subtitle_artifact=(
                self.repository.artifact_ref(state.artifacts[ArtifactKind.subtitle_ass].id)
                if ArtifactKind.subtitle_ass in state.artifacts
                else None
            ),
            duration_sec=float(final.media_info.duration_sec if final.media_info and final.media_info.duration_sec else 0),
        )
        self.repository.finished_videos[finished.id] = finished
        video_version = VideoVersion(
            id=new_id("vv"),
            case_id=state.request.case_id,
            script_version_id=script.id,
            finished_video_id=finished.id,
            timeline_plan_artifact_id=timeline.id,
            style_plan_artifact_id=style.id,
        )
        self.repository.video_versions[video_version.id] = video_version
        package = self.repository.create_publish_package_from_finished_video(
            finished,
            title=finished.title,
            description=state.request.publish_content,
        )
        self.repository.create_event(
            "workflow.finished_video.created",
            "run",
            run.id,
            {"finished_video_id": finished.id, "publish_package_id": package.id},
            dedupe_key=f"finished_video:{finished.id}",
            event_type="artifact_created",
            node_id=node_run.node_id,
            status=NodeStatus.running.value,
            message=f"Finished video {finished.id} created.",
        )
        self.repository.record_yield_funnel_event(
            job_id=run.job_id,
            run_id=run.id,
            finished_video_id=finished.id,
            publish_package_id=package.id,
            event_type="finished_video_created",
            dedupe_key=f"{finished.id}:finished_video_created",
            event_time=finished.created_at,
        )
        package_artifact = self._artifact(
            run,
            node_run,
            ArtifactKind.publish_package,
            package.model_dump(mode="json"),
            "PublishPackageArtifact.v1",
        )
        return NodeOutput(artifacts=[video_artifact, cover_artifact, package_artifact])

    def _finalize_run_report(self, run: WorkflowRun, node_run: NodeRun, state: RunState) -> NodeOutput:
        public_artifact, debug_artifact = self._write_report(run, state, failed=False, node_run=node_run)
        for artifact in state.artifacts.values():
            if artifact.kind not in _EPHEMERAL_ARTIFACT_KINDS or not artifact.uri:
                continue
            try:
                get_object_store().delete(artifact.uri)
            except Exception:
                logger.warning(
                    "Failed to delete ephemeral artifact %s at %s.",
                    artifact.id,
                    artifact.uri,
                    exc_info=True,
                )
        return NodeOutput(artifacts=[public_artifact, debug_artifact])

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
) -> LocalRuntimeAdapter:
    return LocalRuntimeAdapter(
        repository,
        provider_gateway or ProviderGateway(repository),
        prompt_registry or PromptRegistry(repository),
    )


DigitalHumanWorkflow = LocalRuntimeAdapter
