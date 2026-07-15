"""Thin orchestrator for the digital-human workflow.

This module owns the *engine*: the node sequence, the workflow template, the
run/node state machine, reuse/resume bookkeeping, and the shared services every
node leans on (artifact creation, media-source resolution, provider-profile
selection, the object store). The per-node business logic lives in
``packages.production.pipeline.nodes`` — one ``run(ctx)`` handler per entry in
``NODE_SEQUENCE`` — so capability work edits disjoint files.

``get_object_store`` is likewise imported into this namespace so it stays
monkeypatchable; node handlers reach it via ``NodeContext.object_store()`` which
resolves through ``LocalRuntimeAdapter._object_store``.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property
import logging
from pathlib import Path
import time
from typing import TYPE_CHECKING, TypeVar

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
    RetryPolicy,
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
from packages.core.workflow import (
    NodeExecutionError,
    NodeOutput,
    WorkflowRuntimeAdapter,
    manifest_hash,
)
from packages.production.pipeline.node_sequence import (
    EDITING_AGENT_SEQUENCE,
    EDITING_AGENT_V2_SEQUENCE,
    NODE_SEQUENCE,
    SEEDANCE_T2V_SEQUENCE,
    canonical_node_id,
    _linear_edges,
    topological_node_order,
    validate_graph_structure,
    workflow_graph,
)
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
from packages.production.pipeline import nodes
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._provider_profiles import ProviderProfileResolver
from packages.production.pipeline._run_state import RunState as _RunState
from packages.production.pipeline.ephemeral_gc import (
    failed_ephemeral_retention_policy,
    gc_ephemeral_artifacts,
    record_ephemeral_gc_event,
)
from packages.production.pipeline.reuse import ReusePlan, ReuseSourceRun, compute_reuse_plan
from packages.planning.editing import (
    SpokenSegment,
    build_narration_units_from_asr,
    build_narration_units_from_script_sentences,
    build_narration_units_without_asr,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from packages.production.sqlalchemy_repository import SqlAlchemyProductionRepository

__all__ = [
    "NODE_SEQUENCE",
    "SEEDANCE_T2V_SEQUENCE",
    "EDITING_AGENT_SEQUENCE",
    "EDITING_AGENT_V2_SEQUENCE",
    "digital_human_template",
    "editing_agent_v2_template",
    "seedance_t2v_template",
    "template_for",
    "LocalRuntimeAdapter",
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
    "NarrationBoundaryPlanning": nodes.narration_boundary_planning.run,
    "TimelineWindowPlanning": nodes.timeline_window_planning.run,
    "WindowQueryPlanning": nodes.window_query_planning.run,
    "WindowMaterialRetrieval": nodes.window_material_retrieval.run,
    "DeterministicEditingPlanning": nodes.deterministic_editing_planning.run,
    "EditingAgentPlanning": nodes.editing_agent_planning.run,
    "MediaSelectionAgentPlanning": nodes.media_selection_agent_planning.run,
    "TimelineAssemblyValidation": nodes.timeline_assembly_validation.run,
    # In-flight / historical v1 Temporal payloads still carry the old node id.
    "TimelinePlanning": nodes.timeline_assembly_validation.run,
    "PortraitTrackBuild": nodes.portrait_track_build.run,
    "LipSync": nodes.lipsync.run,
    "RenderFinalTimeline": nodes.render_final_timeline.run,
    "CaptionWindowPlanning": nodes.caption_window_planning.run,
    "PostProcessAgentPlanning": nodes.postprocess_agent_planning.run,
    "SubtitleAndBgmMix": nodes.subtitle_and_bgm_mix.run,
    "ExportFinishedVideo": nodes.export_finished_video.run,
    "SeedanceGenerateVideo": nodes.seedance_generate_video.run,
    "ExportSeedanceVideo": nodes.export_seedance_video.run,
    "FinalizeRunReport": nodes.finalize_run_report.run,
}

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


def _extend_unique(target: list[_T], values: list[_T]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


_PROVIDER_SIDE_EFFECT_NODES = {
    "TTS",
    "ResolveCreativeIntent",
    "NarrationAlignment",
    "LipSync",
    "ExportFinishedVideo",
    "SeedanceGenerateVideo",
    "EditingAgentPlanning",
    "MediaSelectionAgentPlanning",
    "PostProcessAgentPlanning",
    "WindowQueryPlanning",
    "WindowMaterialRetrieval",
}
_NON_REPLAYABLE_SIDE_EFFECTS = {
    # NOTE: MaterialPackPlanning is deliberately NOT marked non-replayable. It sits
    # mid-chain, so stopping reuse *before* it forces every downstream node to re-run on
    # resume — including the paid PortraitTrackBuild/LipSync. That re-run mints a fresh
    # portrait-track artifact id, so LipSync's input_manifest_hash changes, its
    # idempotency key changes, and the already-paid lipsync is billed a SECOND time
    # (issue #202 money-safety; the golden resume tests enforce this). Re-establishing a
    # resumed run's run-scoped selection reservations must be a separate idempotent step
    # keyed off the reused plan_material_pack artifact, NOT re-execution of the node.
    #
    # These nodes create run-owned domain rows; copying their artifacts cannot recreate
    # FinishedVideo/VideoVersion/PublishPackage/selection-ledger state. They are terminal,
    # so re-running them on resume does not cascade into paid upstream re-runs.
    "ExportFinishedVideo": ["domain_write"],
    "ExportSeedanceVideo": ["domain_write"],
    "FinalizeRunReport": ["ledger_commit"],
}
_TIMELINE_REUSE_BREAK_NODES = {
    "NarrationBoundaryPlanning",
    "TimelineWindowPlanning",
    "WindowQueryPlanning",
    "WindowMaterialRetrieval",
    "DeterministicEditingPlanning",
    "TimelineAssemblyValidation",
    "TimelinePlanning",
    "EditingAgentPlanning",
    "MediaSelectionAgentPlanning",
}
_MATERIAL_PACK_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    backoff_seconds=1,
    retryable_error_codes=[ErrorCode.validation_conflict],
)
# Every node that spends money: a worker crash / heartbeat timeout mid-node must re-run
# the activity so the durable idempotency key can recover the call — poll an in-flight
# vendor task, or replay a result the vendor already returned — instead of leaving the
# run stuck on work that was paid for. Business failures (NodeExecutionError) are still
# caught in _execute_node and never reach this retry, so it cannot amplify them.
_INFRA_RETRY_NODES = set(_PROVIDER_SIDE_EFFECT_NODES)
# Backoff has to outlast the crash it recovers from: the Gateway only takes over a
# still-'submitted' row once it is stale beyond 2x the provider timeout, and waits just
# seconds to see whether the previous holder is alive. Re-entering ~1s after a heartbeat
# timeout would burn attempts 2 and 3 against a row that has not settled yet.
_INFRA_RETRY_POLICY = RetryPolicy(max_attempts=3, backoff_seconds=60)

_NODE_OUTPUT_KINDS: dict[str, list[ArtifactKind]] = {
    "ValidateRequest": [ArtifactKind.validated_production_spec],
    "LoadCaseContext": [ArtifactKind.case_context],
    "ResolveCreativeIntent": [ArtifactKind.creative_intent],
    "TTS": [ArtifactKind.audio_tts, ArtifactKind.audio_alignment_raw],
    "MaterialPackPlanning": [ArtifactKind.plan_material_pack],
    "NarrationAlignment": [ArtifactKind.audio_alignment, ArtifactKind.narration_units],
    "NarrationBoundaryPlanning": [ArtifactKind.plan_narration_boundary],
    "TimelineWindowPlanning": [ArtifactKind.plan_timeline_windows],
    "WindowQueryPlanning": [ArtifactKind.plan_window_queries],
    "WindowMaterialRetrieval": [ArtifactKind.plan_window_material_retrieval],
    "DeterministicEditingPlanning": [
        ArtifactKind.plan_media_assignment,
        ArtifactKind.plan_portrait,
        ArtifactKind.plan_broll,
        ArtifactKind.plan_style,
    ],
    "EditingAgentPlanning": [
        ArtifactKind.plan_media_assignment,
        ArtifactKind.plan_portrait,
        ArtifactKind.plan_broll,
        ArtifactKind.plan_style,
        ArtifactKind.plan_editing_diagnostics,
    ],
    "MediaSelectionAgentPlanning": [
        ArtifactKind.plan_media_assignment,
        ArtifactKind.plan_portrait,
        ArtifactKind.plan_broll,
        ArtifactKind.plan_media_selection_diagnostics,
    ],
    "TimelineAssemblyValidation": [ArtifactKind.plan_timeline, ArtifactKind.plan_render],
    "TimelinePlanning": [ArtifactKind.plan_timeline, ArtifactKind.plan_render],
    "PortraitTrackBuild": [ArtifactKind.video_portrait_track],
    "LipSync": [ArtifactKind.video_lipsync, ArtifactKind.lipsync_report],
    "RenderFinalTimeline": [ArtifactKind.video_rendered],
    "CaptionWindowPlanning": [ArtifactKind.plan_caption_windows],
    "PostProcessAgentPlanning": [
        ArtifactKind.plan_style,
        ArtifactKind.plan_postprocess_diagnostics,
    ],
    "SubtitleAndBgmMix": [
        ArtifactKind.video_final,
        ArtifactKind.subtitle_ass,
        ArtifactKind.plan_caption_display,
    ],
    "ExportFinishedVideo": [
        ArtifactKind.video_finished,
        ArtifactKind.cover_image,
        ArtifactKind.cover_thumbnail,
        ArtifactKind.publish_package,
        ArtifactKind.provider_raw_request,
    ],
    "SeedanceGenerateVideo": [ArtifactKind.video_rendered],
    "ExportSeedanceVideo": [
        ArtifactKind.video_finished,
        ArtifactKind.cover_image,
        ArtifactKind.cover_thumbnail,
        ArtifactKind.publish_package,
    ],
    "FinalizeRunReport": [ArtifactKind.run_report_public, ArtifactKind.run_report_debug],
}

# Bump only nodes whose persisted semantics changed. The runtime writes these exact
# versions into NodeRun so a resume of an older run reuses its valid prefix but must
# re-enter the caption chain at the first incompatible node.
_NODE_VERSIONS = {
    # v3 invalidates streamed/synchronous narration so a resume re-enters at TTS after
    # production switched to durable async ICL 2.0 single-file MP3 + native timing.
    "TTS": "v3",
    # v3 repairs sparse historical token streams by script position and enforces
    # globally unique claims/monotonic char spans at the artifact boundary.
    "CaptionWindowPlanning": "v4",
    "PostProcessAgentPlanning": "v2",
    "SubtitleAndBgmMix": "v2",
}


def _node_retry_policy(node_id: str) -> RetryPolicy:
    if node_id == "MaterialPackPlanning":
        return _MATERIAL_PACK_RETRY_POLICY
    if node_id in _INFRA_RETRY_NODES:
        return _INFRA_RETRY_POLICY
    return RetryPolicy()


def _build_template(template_id: str, version: str, sequence: list[str]) -> WorkflowTemplate:
    # Dependency edges come from the template's DAG graph (node_sequence.WORKFLOW_GRAPHS);
    # the shipping templates are linear chains, so this is the same edge list as before.
    # A template with no registered graph falls back to a linear chain of its sequence.
    graph = workflow_graph(template_id)
    node_ids = list(graph["nodes"]) if graph else list(sequence)
    edge_pairs = list(graph["edges"]) if graph else _linear_edges(sequence)

    # Store nodes in TOPOLOGICAL (dependency) order so every consumer that iterates
    # template.nodes — the local runtime, the Temporal payload, and the reuse planner —
    # agrees on execution order by construction, not just for the linear templates. For a
    # linear template this is exactly the sequence, so nothing changes. topological_node_order
    # raises on a cycle, so a malformed graph fails fast here at construction.
    ordered_ids = topological_node_order(node_ids, edge_pairs)

    # ExportFinishedVideo makes a PAID image.generate call on the gated AI-cover
    # path, so it is declared here too: this gives it a non-None idempotency_key so
    # the reuse planner accounts for the side effect and can safely replay it,
    # instead of treating the node as pure and silently re-firing the paid call.
    node_specs = [
        NodeSpec(
            node_id=node_id,
            node_version=_NODE_VERSIONS.get(node_id, "v1"),
            output_artifact_kinds=list(_NODE_OUTPUT_KINDS[node_id]),
            retry_policy=_node_retry_policy(node_id),
            side_effects=(
                (["provider_call"] if node_id in _PROVIDER_SIDE_EFFECT_NODES else [])
                + list(_NON_REPLAYABLE_SIDE_EFFECTS.get(node_id, []))
            ),
            idempotency_key=(
                f"{template_id}:{node_id}:{{input_manifest_hash}}"
                if node_id in _PROVIDER_SIDE_EFFECT_NODES
                else None
            ),
            reuse_policy="never" if node_id in _TIMELINE_REUSE_BREAK_NODES else "strict",
        )
        for node_id in ordered_ids
    ]
    template = WorkflowTemplate(
        workflow_template_id=template_id,
        version=version,
        nodes=node_specs,
        edges=[WorkflowEdge(from_node_id=a, to_node_id=b) for a, b in edge_pairs],
    )
    _validate_workflow_template(template)
    return template


def _validate_workflow_template(template: WorkflowTemplate) -> None:
    """Fail fast on a malformed template graph (#137 acceptance).

    Detects a dependency cycle, an edge to/from an unknown node, a duplicate node,
    a node with no registered handler, and a node with no declared output kinds — so a
    broken graph is rejected at construction, never mid-run.
    """
    node_ids = [spec.node_id for spec in template.nodes]
    edges = [(edge.from_node_id, edge.to_node_id) for edge in template.edges]
    validate_graph_structure(node_ids, edges)
    for node_id in node_ids:
        if node_id not in NODE_HANDLERS:
            raise ValueError(
                f"workflow template {template.workflow_template_id!r} node {node_id!r} "
                "has no registered handler"
            )
        if node_id not in _NODE_OUTPUT_KINDS:
            raise ValueError(
                f"workflow template {template.workflow_template_id!r} node {node_id!r} "
                "declares no output artifact kinds"
            )


def digital_human_template() -> WorkflowTemplate:
    return _build_template("digital_human_v2", "v1", NODE_SEQUENCE)


def seedance_t2v_template() -> WorkflowTemplate:
    return _build_template("seedance_t2v_v1", "v1", SEEDANCE_T2V_SEQUENCE)


def editing_agent_template() -> WorkflowTemplate:
    return _build_template("digital_human_editing_agent_v1", "v1", EDITING_AGENT_SEQUENCE)


def editing_agent_v2_template() -> WorkflowTemplate:
    return _build_template("digital_human_editing_agent_v2", "v1", EDITING_AGENT_V2_SEQUENCE)


_TEMPLATE_BUILDERS = {
    "digital_human_v2": digital_human_template,
    "seedance_t2v_v1": seedance_t2v_template,
    "digital_human_editing_agent_v1": editing_agent_template,
    "digital_human_editing_agent_v2": editing_agent_v2_template,
}


def template_for(workflow_template_id: str) -> WorkflowTemplate:
    try:
        return _TEMPLATE_BUILDERS[workflow_template_id]()
    except KeyError as exc:
        # workflow_template_id is a free-form request field, so an unknown id reaches
        # here at job admission. Raise NodeExecutionError (not a bare ValueError) so
        # the API handler maps it to a 4xx ErrorEnvelope instead of an uncaught 500.
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            f"Unknown workflow template id: {workflow_template_id}",
        ) from exc


def input_manifest_hash(node_id: str, request: DigitalHumanVideoRequest, state: _RunState) -> str:
    """The identity of one node execution's inputs, and a coordinate of its provider key.

    ``artifact_refs`` MUST stay artifact IDS, never a content digest or any other stable
    fingerprint. It is the only thing that separates a RETRY from a RESUME: both re-drive
    the same job under the same Job-scoped provider-call key, but a retry re-runs the
    chain from the top, so its prefix artifacts are new rows with new ids and every
    downstream manifest — and key — differs. Swap ids for content, and a retry of a
    deterministic chain would hash identically, land on the old key, and hand back the
    very result the operator asked to recompute (and, for ``reuse_valid_artifacts=False``,
    silently refuse to re-buy the work they explicitly asked to re-buy).
    """
    return manifest_hash(
        {
            "node_id": node_id,
            "request": request.model_dump(mode="json"),
            "artifact_refs": {
                kind.value: artifact.id for kind, artifact in state.artifacts.items()
            },
        }
    )


class LocalRuntimeAdapter(WorkflowRuntimeAdapter):
    def __init__(
        self,
        repository: Repository,
        provider_gateway: ProviderGateway,
        prompt_registry: PromptRegistry,
        *,
        seed_media: bool = True,
        snapshot_sync: Callable[[Job, WorkflowRun, Repository], None] | None = None,
        production_repository: "SqlAlchemyProductionRepository | None" = None,
    ) -> None:
        self.repository = repository
        self.provider_gateway = provider_gateway
        self.prompt_registry = prompt_registry
        self._snapshot_sync = snapshot_sync
        self.production_repository = production_repository
        # ``seed_media`` generates demo seed media via ffmpeg/object-store on
        # construction. The per-activity Temporal scoping (see
        # ``TemporalActivityContext.build_runtime``) rehydrates real media
        # assets from SQL, so it skips this expensive bootstrap.
        if seed_media:
            self._ensure_seed_media_assets()

    @cached_property
    def provider_profiles(self) -> ProviderProfileResolver:
        """Provider-profile selection rules (real-vs-sandbox capability gating).

        Lazily derived from this adapter's ``repository`` + ``provider_gateway``
        and cached per instance, so adapters built via ``object.__new__`` in tests
        (which set those two attributes directly and skip ``__init__``) get a
        working resolver with no extra wiring."""
        return ProviderProfileResolver(self.repository, self.provider_gateway)

    # ------------------------------------------------------------------ seed
    def _ensure_seed_media_assets(self) -> None:
        seed_dir = Path(".data/generated-media/seed")
        seed_dir.mkdir(parents=True, exist_ok=True)
        # The distinct demo portrait assets (issue #102 asset-level uniqueness needs >1
        # for a multi-segment main track) share one underlying 15s seed video: the
        # object store is content-addressed, so each asset gets its own artifact row
        # over the same bytes without generating extra videos.
        portrait_spec = {
            "filename": "portrait_demo_15s.mp4",
            "content_type": "video/mp4",
            "generator": lambda path: generate_seed_video(
                path, duration_sec=15, width=320, height=568, fps=30
            ),
        }
        specs = {
            "asset_portrait_demo": portrait_spec,
            "asset_portrait_demo_b": portrait_spec,
            "asset_portrait_demo_c": portrait_spec,
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
                raise NodeExecutionError(
                    exc.error_code, "Demo seed media generation failed."
                ) from exc
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

    def cancel_run(
        self, run_id: str, *, force: bool = False, reason: str | None = None
    ) -> WorkflowRun:
        run = self.repository.runs[run_id]
        if run.status == RunStatus.cancelled:
            return run
        if run.status not in {
            RunStatus.created,
            RunStatus.admitted,
            RunStatus.running,
            RunStatus.cancelling,
        }:
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

    def _mark_run_running(self, run: WorkflowRun, job: Job) -> WorkflowRun:
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
        return run

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
        state = _RunState(request=request)
        start_index = 0
        if job.status != JobStatus.running:
            assert_transition("job", job.status, JobStatus.running)
            job = job.model_copy(update={"status": JobStatus.running, "updated_at": utcnow()})
            self.repository.jobs[job.id] = job
        run = self._mark_run_running(run, job)
        if mode == "resume" and from_run_id:
            start_index = self._reuse_prefix(run, state, from_run_id, reuse_plan)
        sequence = self._sequence_for_run(run)
        for _index, node_id in enumerate(sequence[start_index:], start=start_index):
            if self.repository.runs[run.id].status == RunStatus.cancelled:
                return
            if not self._execute_node(node_id, run, state):
                return
        self._complete_run(run.id)

    def run_node_activity(self, run_id: str, node_id: str) -> dict:
        run = self.repository.runs[run_id]
        job = self.repository.jobs[run.job_id]
        # A lost activity completion (the node's snapshot committed to Postgres but the
        # Temporal completion was never received) re-runs this activity. If a terminal
        # NodeRun for this canonical node already exists, the paid work is done: return
        # the existing summary WITHOUT re-entering the gateway. This precedes the
        # job-status assert_transition below because a run that already reached succeeded
        # (last node's completion lost) would trip that transition first.
        if self._node_already_terminal(run_id, node_id):
            if node_id == self._sequence_for_run(run)[-1] and run.status == RunStatus.running:
                # A real recovery window, not dead defense: the node's own snapshot is
                # committed inside _execute_node (last node terminal, run still running),
                # while _complete_run's run=succeeded only reaches Postgres in the
                # activity-level sync that follows. A crash between those two commits
                # leaves exactly this state, and the replay must finish the run.
                self._complete_run(run_id)
            return self._node_activity_summary(run_id, node_id)
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
            run = self._mark_run_running(run, job)
        if self.repository.runs[run_id].status != RunStatus.running:
            return self._node_activity_summary(run_id, node_id)
        run = self.repository.runs[run_id]
        if self._execute_node(node_id, run, state) and node_id == self._sequence_for_run(run)[-1]:
            self._complete_run(run_id)
        return self._node_activity_summary(run_id, node_id)

    def apply_reuse_plan(self, run_id: str, source_run_id: str, reuse_plan: ReusePlan) -> dict:
        run = self.repository.runs[run_id]
        request = self._request(self.repository.jobs[run.job_id])
        state = _RunState(request=request)
        self._reuse_prefix(run, state, source_run_id, reuse_plan)
        return {
            "run_id": run_id,
            "source_run_id": source_run_id,
            "reused_node_ids": list(reuse_plan.reused_node_ids),
            "rerun_from_node_id": reuse_plan.rerun_from_node_id,
        }

    def request_cancel(
        self, run_id: str, *, force: bool = False, reason: str | None = None
    ) -> WorkflowRun:
        return self.cancel_run(run_id, force=force, reason=reason)

    def _sync_snapshot(self, run_id: str) -> None:
        snapshot_sync = getattr(self, "_snapshot_sync", None)
        if snapshot_sync is None:
            return
        run = self.repository.runs[run_id]
        job = self.repository.jobs[run.job_id]
        snapshot_sync(job, run, self.repository)

    def _template_for_run(self, run: WorkflowRun) -> WorkflowTemplate:
        return template_for(run.workflow_template_id)

    def _sequence_for_run(self, run: WorkflowRun) -> list[str]:
        # A node runs only after its upstreams: a deterministic topological order derived
        # from the template's dependency edges. _build_template already stores template.nodes
        # in this order (so the Temporal payload and the reuse planner, which iterate
        # template.nodes, agree with the local runtime); recomputing it here from the edges
        # keeps the local scheduler correct for any template regardless of node-list order.
        # For the linear shipping templates this is the exact same sequence as before (#137).
        template = self._template_for_run(run)
        node_ids = [spec.node_id for spec in template.nodes]
        edges = [(edge.from_node_id, edge.to_node_id) for edge in template.edges]
        return topological_node_order(node_ids, edges)

    def _node_version_for_run(self, run: WorkflowRun, node_id: str) -> str:
        return self._node_spec_for_run(run, node_id).node_version

    def _node_spec_for_run(self, run: WorkflowRun, node_id: str) -> NodeSpec:
        template = self._template_for_run(run)
        return next(spec for spec in template.nodes if spec.node_id == node_id)

    def _node_already_terminal(self, run_id: str, node_id: str) -> bool:
        """True when this canonical node already has a terminal NodeRun in this run.

        Existence over "latest row": a re-invoked activity appends new NodeRuns and the
        hydrated list has no reliable order, so match the done-set semantics of
        _next_unfinished_node_id (canonical id + terminal status set)."""
        target = canonical_node_id(node_id)
        return any(
            canonical_node_id(node_run.node_id) == target
            and node_run.status in {NodeStatus.succeeded, NodeStatus.skipped, NodeStatus.degraded}
            for node_run in self.repository.node_runs.get(run_id, [])
        )

    def _next_unfinished_node_id(self, run: WorkflowRun, node_runs: list[NodeRun]) -> str | None:
        """First template node not yet completed — the node that was due to run."""
        done = {
            canonical_node_id(node_run.node_id)
            for node_run in node_runs
            if node_run.status in {NodeStatus.succeeded, NodeStatus.skipped, NodeStatus.degraded}
        }
        return next(
            (
                node_id
                for node_id in self._sequence_for_run(run)
                if canonical_node_id(node_id) not in done
            ),
            None,
        )

    def mark_run_failed(
        self, run_id: str, *, reason: str = "Worker lost or node activity timed out."
    ) -> WorkflowRun:
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
            (
                i
                for i in range(len(node_runs) - 1, -1, -1)
                if node_runs[i].status == NodeStatus.running
            ),
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
            next_node_id = self._next_unfinished_node_id(run, node_runs)
            failed_node = None
            if next_node_id is not None:
                failed_node = NodeRun(
                    id=new_id("nr"),
                    run_id=run_id,
                    node_id=next_node_id,
                    node_version=self._node_version_for_run(run, next_node_id),
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
                {
                    "node_id": failed_node.node_id,
                    "error_code": ErrorCode.workflow_worker_lost.value,
                },
                dedupe_key=f"{failed_node.id}:{NodeStatus.failed.value}",
                event_type="node_update",
                node_id=failed_node.node_id,
                status=NodeStatus.failed.value,
                message=f"Node {failed_node.node_id} failed.",
            )
        try:
            self.repository.release_run_reservations(run_id=run_id, only_uncommitted=True)
        except Exception:
            logger.warning(
                "Failed to release selection reservations for worker-lost run %s.",
                run_id,
                exc_info=True,
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
        state = self._terminal_state_from_repository(run_id)
        if state is not None:
            self._terminal_ephemeral_gc(run_id, state, terminal_status=RunStatus.failed)
        return self.repository.runs[run_id]

    def _state_from_persisted_artifacts(
        self, run_id: str, request: DigitalHumanVideoRequest
    ) -> _RunState:
        state = _RunState(request=request)
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

    def _terminal_state_from_repository(self, run_id: str) -> _RunState | None:
        run = self.repository.runs.get(run_id)
        if run is None:
            return None
        job = self.repository.jobs.get(run.job_id)
        if job is None:
            return None
        try:
            return self._state_from_persisted_artifacts(run_id, self._request(job))
        except Exception:
            logger.warning("Failed to hydrate terminal state for run %s.", run_id, exc_info=True)
            return None

    def _terminal_ephemeral_gc(
        self,
        run_id: str,
        state: _RunState,
        *,
        terminal_status: RunStatus,
    ) -> None:
        try:
            # A failed / worker-lost run can still be RESUMED reusing its valid
            # prefix (spec §20.2.6 — see the lipsync-timeout resume path); deleting
            # its ephemeral intermediates at the terminal hook would break resume.
            # So retain them and let the time-based object-store sweep
            # (scripts/gc_objectstore.py) reclaim them after the resume window.
            # Only a cancelled run — which never resumes — is GC'd immediately.
            if terminal_status != RunStatus.cancelled:
                record_ephemeral_gc_event(
                    self.repository,
                    run_id=run_id,
                    terminal_status=terminal_status.value,
                    deleted_uris=[],
                    skipped=True,
                    retention_policy="retain_for_resume",
                )
                return
            # Cancelled run: GC now, unless a debug-retention knob asks to keep it.
            retention_policy = failed_ephemeral_retention_policy()
            if retention_policy is not None:
                record_ephemeral_gc_event(
                    self.repository,
                    run_id=run_id,
                    terminal_status=terminal_status.value,
                    deleted_uris=[],
                    skipped=True,
                    retention_policy=retention_policy,
                )
                return
            deleted_uris = gc_ephemeral_artifacts(self._object_store(), state, run_id=run_id)
            record_ephemeral_gc_event(
                self.repository,
                run_id=run_id,
                terminal_status=terminal_status.value,
                deleted_uris=deleted_uris,
                skipped=False,
            )
        except Exception:
            logger.warning("Failed to run terminal ephemeral GC for run %s.", run_id, exc_info=True)

    # --------------------------------------------------------------- engine loop
    def _execute_node(self, node_id: str, run: WorkflowRun, state: _RunState) -> bool:
        job = self.repository.jobs[run.job_id]
        request = state.request
        node_run = NodeRun(
            id=new_id("nr"),
            run_id=run.id,
            node_id=node_id,
            node_version=self._node_version_for_run(run, node_id),
            status=NodeStatus.pending,
            input_manifest_hash=input_manifest_hash(node_id, request, state),
            started_at=utcnow(),
        )
        self.repository.node_runs[run.id].append(node_run)
        try:
            if not self._may_skip_without_running(node_id, state):
                assert_transition("node", node_run.status, NodeStatus.running)
                node_run = node_run.model_copy(
                    update={"status": NodeStatus.running, "updated_at": utcnow()}
                )
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
            output = self._run_node_with_declared_retries(node_id, run, node_run, state)
            declared_kinds = set(self._node_spec_for_run(run, node_id).output_artifact_kinds)
            undeclared_kinds = sorted(
                {
                    artifact.kind
                    for artifact in output.artifacts
                    if artifact.kind not in declared_kinds
                },
                key=lambda kind: kind.value,
            )
            if undeclared_kinds:
                raise NodeExecutionError(
                    ErrorCode.artifact_schema_mismatch,
                    f"Node {node_id} returned undeclared artifact kinds: "
                    + ", ".join(kind.value for kind in undeclared_kinds),
                    details={
                        "node_id": node_id,
                        "undeclared_kinds": [kind.value for kind in undeclared_kinds],
                        "declared_kinds": sorted(kind.value for kind in declared_kinds),
                    },
                )
            # No silent fallback: any provider call this node made that the gateway
            # could not price (billing_status="unpriced") surfaces as a node-level
            # cost.unpriced warning instead of staying buried in usage metering.
            if WarningCode.cost_unpriced not in output.warnings:
                invocations = self.repository.provider_invocations
                for inv_id in output.provider_invocation_ids:
                    invocation = invocations.get(inv_id)
                    if invocation is not None and invocation.billing_status == "unpriced":
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
                    "degradation_reason": "; ".join(item.message for item in output.degradations)
                    or None,
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
                {"node_id": node_id, "status": status.value},
                dedupe_key=f"{patched.id}:{status.value}",
                event_type="node_update",
                node_id=node_id,
                status=status.value,
                message=f"Node {node_id} finished with {status.value}.",
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
            self._sync_snapshot(run.id)
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
            failed_provider_invocation_ids = [
                invocation.id
                for invocation in self.repository.provider_invocations.values()
                if invocation.run_id == run.id and invocation.node_run_id == node_run.id
            ]
            failed_output_artifact_ids = [
                artifact.id
                for artifact in self.repository.artifacts.values()
                if artifact.run_id == run.id and artifact.node_run_id == node_run.id
            ]
            for invocation_id in failed_provider_invocation_ids:
                if invocation_id not in state.provider_invocation_ids:
                    state.provider_invocation_ids.append(invocation_id)
            failed_node = node_run.model_copy(
                update={
                    "status": NodeStatus.failed,
                    "error": error,
                    "output_artifact_ids": failed_output_artifact_ids,
                    "provider_invocation_ids": failed_provider_invocation_ids,
                    "finished_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )
            self.repository.node_runs[run.id][-1] = failed_node
            record_node_run(failed_node)
            # §6.6 release on failure: free this run's UNCOMMITTED reservations so a
            # sibling run can claim those slots. Committed picks stay as audit records;
            # future diversity pressure comes from the selection ledger. Never let a
            # reservation hiccup mask the original node failure.
            try:
                self.repository.release_run_reservations(run_id=run.id, only_uncommitted=True)
            except Exception:
                logger.warning(
                    "Failed to release selection reservations for failed run %s.",
                    run.id,
                    exc_info=True,
                )
            self._write_report(run, state, failed=True)
            self._terminal_ephemeral_gc(run.id, state, terminal_status=RunStatus.failed)
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
        # are reclaimable immediately. Committed picks remain as audit records only.
        try:
            self.repository.release_run_reservations(run_id=run_id, only_uncommitted=True)
        except Exception:
            logger.warning(
                "Failed to release selection reservations for cancelled run %s.",
                run_id,
                exc_info=True,
            )
        state = self._terminal_state_from_repository(run_id)
        if state is not None:
            try:
                self._write_report(
                    self.repository.runs[run.id],
                    state,
                    failed=False,
                    status=RunStatus.cancelled,
                )
            except Exception:
                logger.warning(
                    "Failed to write cancelled report for run %s.", run_id, exc_info=True
                )
            self._terminal_ephemeral_gc(run_id, state, terminal_status=RunStatus.cancelled)
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
            (
                node
                for node in reversed(self.repository.node_runs.get(run_id, []))
                if node.node_id == node_id
            ),
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
        state: _RunState,
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
                template_for(run.workflow_template_id),
                self.repository.artifacts,
            )
        previous_by_node = {canonical_node_id(node.node_id): node for node in previous}
        decisions_by_node = {
            canonical_node_id(decision.node_id): decision for decision in reuse_plan.decisions
        }
        for node_id in reuse_plan.reused_node_ids:
            previous_node_run = previous_by_node[canonical_node_id(node_id)]
            decision = decisions_by_node.get(canonical_node_id(node_id))
            reusable_artifact_ids = (
                list(decision.artifact_ids)
                if decision is not None
                else list(previous_node_run.output_artifact_ids)
            )
            for artifact_id in reusable_artifact_ids:
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
                    "node_id": node_id,
                    "output_artifact_ids": reusable_artifact_ids,
                    "status": NodeStatus.skipped,
                    "skipped_reason": "resume.reused_artifact_prefix",
                    "updated_at": utcnow(),
                }
            )
            self.repository.node_runs[run.id].append(copied)
            _extend_unique(state.provider_invocation_ids, copied.provider_invocation_ids)
            _extend_unique(state.warnings, copied.warnings)
            _extend_unique(state.degradations, copied.degradations)
        return reuse_plan.reused_count

    def _may_skip_without_running(self, node_id: str, state: _RunState) -> bool:
        return (
            node_id == "ResolveCreativeIntent"
            and state.request.creative_intent_ref is not None
            or node_id in {"PortraitTrackBuild", "LipSync"}
            and state.request.broll.enabled
            and state.request.broll.mode == "full_coverage"
            or node_id == "LipSync"
            and not state.request.lipsync.enabled
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
        self, node_id: str, run: WorkflowRun, node_run: NodeRun, state: _RunState
    ) -> NodeOutput:
        ctx = NodeContext(adapter=self, run=run, node_run=node_run, state=state)
        return NODE_HANDLERS[node_id](ctx)

    def _run_node_with_declared_retries(
        self, node_id: str, run: WorkflowRun, node_run: NodeRun, state: _RunState
    ) -> NodeOutput:
        policy = self._node_spec_for_run(run, node_id).retry_policy
        retryable_codes = set(policy.retryable_error_codes)
        delay = policy.backoff_seconds
        for attempt in range(1, policy.max_attempts + 1):
            try:
                return self._run_node(node_id, run, node_run, state)
            except NodeExecutionError as exc:
                if (
                    attempt >= policy.max_attempts
                    or exc.error.code not in retryable_codes
                    or not exc.error.retryable
                ):
                    raise
                if delay > 0:
                    time.sleep(delay)
                delay *= policy.backoff_multiplier
        raise AssertionError("declared node retry loop exhausted without returning or raising")

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
        size_bytes: int | None = None,
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
            size_bytes=size_bytes,
            sha256=sha256,
            media_info=media_info,
        )

    def _source_artifact_for_asset(self, asset_id: str | None) -> Artifact:
        if not asset_id:
            raise NodeExecutionError(ErrorCode.artifact_missing, "Media asset is missing.")
        asset = self.repository.media_assets.get(asset_id)
        if asset is None:
            raise NodeExecutionError(
                ErrorCode.artifact_missing, f"Media asset is missing: {asset_id}"
            )
        if not asset.source_artifact_id:
            raise NodeExecutionError(
                ErrorCode.artifact_missing,
                f"Media asset source artifact is missing: {asset_id}",
            )
        artifact = self.repository.artifacts.get(asset.source_artifact_id)
        if artifact is None or not artifact.uri:
            raise NodeExecutionError(
                ErrorCode.artifact_missing,
                f"Media source artifact is missing: {asset_id}",
            )
        return artifact

    def _artifact_path(self, artifact: Artifact) -> Path:
        if not artifact.uri:
            raise NodeExecutionError(ErrorCode.artifact_missing, "Artifact URI is missing.")
        try:
            return local_object_path(get_object_store(), artifact.uri)
        except ValueError as exc:
            raise NodeExecutionError(
                ErrorCode.artifact_missing, "Artifact URI is not locally readable."
            ) from exc

    def _narration_units_from_segments(
        self,
        segments,
        fallback_duration: float,
        *,
        script: str | None = None,
    ) -> list[NarrationUnit]:
        spoken: list[SpokenSegment] = []
        if not isinstance(segments, list):
            segments = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            start = float(segment.get("start") or segment.get("start_sec") or 0)
            end = float(segment.get("end") or segment.get("end_sec") or start)
            if end <= start:
                end = start + 0.3
            spoken.append(SpokenSegment(start=round(start, 3), end=round(end, 3), text=text))
        script_text = str(script or "").strip()
        if script_text:
            units = build_narration_units_from_script_sentences(
                script=script_text,
                asr_segments=spoken,
                video_duration=fallback_duration,
            )
            if not units:
                units = build_narration_units_without_asr(script_text, fallback_duration)
        else:
            units = build_narration_units_from_asr(spoken, fallback_duration)
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

    # ----------------------------------------------------------- run reporting
    def _write_report(
        self,
        run: WorkflowRun,
        state: _RunState,
        *,
        failed: bool,
        node_run: NodeRun | None = None,
        status: RunStatus | None = None,
    ) -> tuple[Artifact, Artifact]:
        node_runs = self.repository.node_runs.get(run.id, [])
        terminal_status = status or (RunStatus.failed if failed else RunStatus.succeeded)
        summaries = {
            RunStatus.failed: "Run failed.",
            RunStatus.cancelled: "Run cancelled.",
            RunStatus.succeeded: "Run completed.",
        }
        public = RunPublicReportArtifact(
            run_id=run.id,
            status=terminal_status,
            summary=summaries.get(terminal_status, f"Run {terminal_status.value}."),
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
    snapshot_sync: Callable[[Job, WorkflowRun, Repository], None] | None = None,
    production_repository: "SqlAlchemyProductionRepository | None" = None,
) -> LocalRuntimeAdapter:
    return LocalRuntimeAdapter(
        repository,
        provider_gateway or ProviderGateway(repository),
        prompt_registry or PromptRegistry(repository),
        seed_media=seed_media,
        snapshot_sync=snapshot_sync,
        production_repository=production_repository,
    )
