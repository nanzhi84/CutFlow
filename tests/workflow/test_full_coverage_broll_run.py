from __future__ import annotations

from packages.ai.gateway import ProviderGateway
from packages.ai.gateway.provider_gateway import SandboxProvider
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    AnnotationEditorVm,
    AnnotationMetaV4,
    AnnotationV4,
    ArtifactKind,
    ClipRetrievalV4,
    ClipSemanticsV4,
    ClipUsageV4,
    ClipV4,
    DigitalHumanVideoRequest,
    ErrorCode,
    Job,
    JobStatus,
    JobType,
    NodeStatus,
    RunStatus,
    UsageRole,
    UsageWindowV4,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.media.assets import store_file
from packages.media.video.ffmpeg import probe_media
from packages.production.pipeline.digital_human import (
    NODE_HANDLERS,
    build_digital_human_workflow,
    template_for,
)
from packages.production.pipeline.node_sequence import NODE_SEQUENCE
from packages.production.pipeline.reuse import ReuseSourceRun, compute_reuse_plan


def _seed_long_broll(repository: Repository, object_store, media_fixture_factory) -> None:
    source = media_fixture_factory.video(
        duration_sec=12.0,
        width=320,
        height=180,
        fps=30,
        filename="full_coverage_broll_source.mp4",
    )
    stored = store_file(object_store, source, purpose="seed-media")
    media_info = probe_media(source)
    artifact = repository.create_artifact(
        kind=ArtifactKind.uploaded_file,
        payload_schema="UploadedFileArtifact.v1",
        payload={
            "upload_session_id": None,
            "filename": source.name,
            "content_type": "video/mp4",
            "size_bytes": source.stat().st_size,
            "object_uri": stored.ref.uri,
            "sha256": stored.sha256,
            "metadata": {"asset_id": "asset_broll_demo"},
        },
        case_id="case_demo",
        uri=stored.ref.uri,
        sha256=stored.sha256,
        media_info=media_info,
    )
    base_asset = repository.media_assets["asset_broll_demo"]
    for index, asset_id in enumerate(
        [
            "asset_broll_demo",
            "asset_broll_demo_b",
            "asset_broll_demo_c",
            "asset_broll_demo_d",
        ]
    ):
        repository.media_assets[asset_id] = base_asset.model_copy(
            update={
                "id": asset_id,
                "title": f"Full coverage broll {index + 1}",
                "source_artifact_id": artifact.id,
            }
        )
        repository.annotations[asset_id] = AnnotationEditorVm(
            asset=repository.media_assets[asset_id],
            etag=f"full-coverage-broll-e2e-{index}",
            canonical=AnnotationV4(
                meta=AnnotationMetaV4(
                    asset_id=asset_id,
                    case_id="case_demo",
                    material_type="broll",
                    duration=12.0,
                ),
                clips=[
                    ClipV4(
                        segment_id=f"cover_process_{index}",
                        start=0.0,
                        end=12.0,
                        duration=12.0,
                        semantics=ClipSemanticsV4(
                            scene_type=f"施工过程{index + 1}",
                            action="补漆修复",
                            narrative_role=f"覆盖画面{index + 1}",
                        ),
                        usage=ClipUsageV4(role=UsageRole.cover, recommended_for_voiceover=True),
                        retrieval=ClipRetrievalV4(
                            summary="施工过程补漆修复展示",
                            keywords=["施工过程", "补漆", "修复", "展示"],
                            retrieval_sentence="展示施工过程和补漆修复细节",
                        ),
                        confidence=0.95,
                    ),
                ],
                usage_windows=[
                    UsageWindowV4(start=0.0, end=12.0, role=UsageRole.cover, confidence=0.9)
                ],
                quality_report={"usable_ratio": 0.95},
            ),
            projection={},
        )


def test_full_coverage_broll_run_finishes_on_main_chain(
    tmp_path,
    media_fixture_factory,
    monkeypatch,
):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store",
        lambda: object_store,
    )
    repository = Repository()
    _seed_long_broll(repository, object_store, media_fixture_factory)
    for profile_id, profile in list(repository.provider_profiles.items()):
        if profile.capability == "multimodal.embedding":
            del repository.provider_profiles[profile_id]
    runtime = build_digital_human_workflow(
        repository,
        provider_gateway=ProviderGateway(repository, object_store=object_store),
        prompt_registry=PromptRegistry(repository),
        seed_media=False,
    )
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        title="仅 B-roll 画外音",
        script="施工过程展示补漆修复。效果展示完工后的变化。",
        voice={"voice_id": "voice_sandbox"},
        workflow_template_id="digital_human_v2",
        broll={"enabled": True, "mode": "full_coverage", "min_segment_duration": 1.0},
        lipsync={"enabled": False},
        bgm={"enabled": False},
        output={"width": 160, "height": 90, "fps": 30},
        strictness={"strict_timestamps": False},
    )
    job = Job(
        id="job_full_coverage_broll",
        type=JobType.digital_human_video,
        status=JobStatus.queued,
        case_id="case_demo",
        created_by="usr_admin",
        request_schema=request.schema_version,
        request=request,
    )
    template = template_for(request.workflow_template_id)
    run = WorkflowRun(
        id="run_full_coverage_broll",
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id=template.workflow_template_id,
        workflow_version=template.version,
        status=RunStatus.admitted,
        requested_by="usr_admin",
    )
    repository.jobs[job.id] = job
    repository.runs[run.id] = run
    repository.node_runs[run.id] = []

    runtime.start_run(job=job, run=run, template=template)

    finished_run = repository.runs[run.id]
    produced_kinds = {artifact.kind for artifact in repository.artifacts.values()}
    node_ids = [node.node_id for node in repository.node_runs[run.id]]
    windows = next(
        artifact.payload
        for artifact in repository.artifacts.values()
        if artifact.kind == ArtifactKind.plan_timeline_windows
    )
    portrait = next(
        artifact.payload
        for artifact in repository.artifacts.values()
        if artifact.kind == ArtifactKind.plan_portrait
    )
    broll = next(
        artifact.payload
        for artifact in repository.artifacts.values()
        if artifact.kind == ArtifactKind.plan_broll
    )

    assert finished_run.status == RunStatus.succeeded
    assert ArtifactKind.video_finished in produced_kinds
    assert ArtifactKind.video_portrait_track not in produced_kinds
    assert ArtifactKind.video_lipsync not in produced_kinds
    assert portrait["segments"] == []
    assert windows["portrait_windows"] == []
    assert windows["broll_windows"]
    assert {overlay["window_id"] for overlay in broll["overlays"]} == {
        window["window_id"] for window in windows["broll_windows"]
    }
    assert node_ids == NODE_SEQUENCE
    assert all(
        node.status in {NodeStatus.succeeded, NodeStatus.degraded, NodeStatus.skipped}
        for node in repository.node_runs[run.id]
    )


class _CountingSandbox(SandboxProvider):
    """Counts what the vendor was actually asked to do. Paid work is a call, not a status."""

    def __init__(self) -> None:
        self.capabilities: list[str] = []

    def invoke(self, call):
        self.capabilities.append(call.capability_id)
        return super().invoke(call)


def test_full_coverage_resume_reuses_the_entire_paid_prefix_including_material_pack(
    tmp_path,
    media_fixture_factory,
    monkeypatch,
):
    """Resume reuses the whole paid prefix — TTS AND MaterialPackPlanning — and only
    re-runs from the failed node.

    Full-coverage PortraitTrackBuild/LipSync legitimately publish nothing, so that cannot
    invalidate the prefix. Run A dies at RenderFinalTimeline, well after both TTS and
    MaterialPackPlanning succeeded, so both are reused rather than re-billed/re-planned.
    Marking MaterialPackPlanning non-replayable (to re-establish released candidate leases)
    would instead re-run it and every paid node behind it, re-billing the lipsync on
    lipsync-enabled runs (issue #202 — enforced by the golden resume tests). A resumed run's
    run-scoped selection reservations must be re-established as a separate idempotent step
    keyed off the reused plan_material_pack artifact, not by re-executing the node.
    """
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store",
        lambda: object_store,
    )
    repository = Repository()
    _seed_long_broll(repository, object_store, media_fixture_factory)
    for profile_id, profile in list(repository.provider_profiles.items()):
        if profile.capability == "multimodal.embedding":
            del repository.provider_profiles[profile_id]
    gateway = ProviderGateway(repository, object_store=object_store)
    sandbox = _CountingSandbox()
    gateway.plugins["sandbox"] = sandbox
    runtime = build_digital_human_workflow(
        repository,
        provider_gateway=gateway,
        prompt_registry=PromptRegistry(repository),
        seed_media=False,
    )
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        title="仅 B-roll 画外音",
        script="施工过程展示补漆修复。效果展示完工后的变化。",
        voice={"voice_id": "voice_sandbox"},
        workflow_template_id="digital_human_v2",
        broll={"enabled": True, "mode": "full_coverage", "min_segment_duration": 1.0},
        lipsync={"enabled": False},
        bgm={"enabled": False},
        output={"width": 160, "height": 90, "fps": 30},
        strictness={"strict_timestamps": False},
    )
    job = Job(
        id="job_full_coverage_resume",
        type=JobType.digital_human_video,
        status=JobStatus.queued,
        case_id="case_demo",
        created_by="usr_admin",
        request_schema=request.schema_version,
        request=request,
    )
    template = template_for(request.workflow_template_id)
    repository.jobs[job.id] = job

    def _admit(run_id: str, *, resume_from: str | None = None) -> WorkflowRun:
        # Mirrors apps.api.services.jobs_runs._admit_run: a resume of a failed job re-queues
        # it before the new run starts.
        repository.jobs[job.id] = repository.jobs[job.id].model_copy(
            update={"status": JobStatus.queued}
        )
        run = WorkflowRun(
            id=run_id,
            job_id=job.id,
            case_id="case_demo",
            workflow_template_id=template.workflow_template_id,
            workflow_version=template.version,
            status=RunStatus.admitted,
            requested_by="usr_admin",
            resume_from_run_id=resume_from,
        )
        repository.runs[run.id] = run
        repository.node_runs[run.id] = []
        return run

    # Run A dies at the render step, well after TTS has been bought and paid for.
    render = NODE_HANDLERS["RenderFinalTimeline"]
    calls = {"render": 0}

    def _fail_render_once(ctx):
        calls["render"] += 1
        if calls["render"] == 1:
            raise NodeExecutionError(ErrorCode.render_failed, "simulated render crash")
        return render(ctx)

    monkeypatch.setitem(NODE_HANDLERS, "RenderFinalTimeline", _fail_render_once)

    failed_run = _admit("run_full_coverage_a")
    runtime.start_run(job=job, run=failed_run, template=template)
    assert repository.runs[failed_run.id].status == RunStatus.failed
    tts_calls_after_first_run = sandbox.capabilities.count("tts.speech")
    assert tts_calls_after_first_run == 1
    skipped_empty = {
        node.node_id
        for node in repository.node_runs[failed_run.id]
        if node.status == NodeStatus.skipped and not node.output_artifact_ids
    }
    assert {"PortraitTrackBuild", "LipSync"} <= skipped_empty, (
        "the premise of this test: full coverage really does leave zero-output skips"
    )

    plan = compute_reuse_plan(
        ReuseSourceRun(
            run=repository.runs[failed_run.id],
            node_runs=repository.node_runs[failed_run.id],
        ),
        template,
        repository.artifacts,
    )
    # Reuse the entire paid prefix; only the failed render node and its successors re-run.
    # MaterialPackPlanning MUST be reused — re-running it re-bills paid downstream nodes on
    # lipsync-enabled runs (issue #202).
    assert plan.rerun_from_node_id == "RenderFinalTimeline"
    assert "TTS" in plan.reused_node_ids
    assert "MaterialPackPlanning" in plan.reused_node_ids

    resumed = _admit("run_full_coverage_b", resume_from=failed_run.id)
    runtime.resume_run(source_run_id=failed_run.id, new_run=resumed, reuse_plan=plan.model_dump())

    assert repository.runs[resumed.id].status == RunStatus.succeeded
    assert sandbox.capabilities.count("tts.speech") == tts_calls_after_first_run, (
        "the resume re-ran TTS and paid the vendor for audio it already owned"
    )
    reused = {
        node.node_id
        for node in repository.node_runs[resumed.id]
        if node.skipped_reason == "resume.reused_artifact_prefix"
    }
    assert "TTS" in reused
    # MaterialPackPlanning is reused (part of the paid prefix), NOT re-run.
    assert "MaterialPackPlanning" in reused
