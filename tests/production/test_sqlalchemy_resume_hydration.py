from packages.core.contracts import (
    ArtifactKind,
    JobStatus,
    JobType,
    NodeStatus,
    RunStatus,
)
from packages.core.storage import Repository
from packages.core.storage.database import ArtifactRow, JobRow, NodeRunRow, WorkflowRunRow
from packages.production import SqlAlchemyProductionRepository


def test_hydrate_chained_resume_loads_artifacts_owned_by_ancestor_run(
    db_session_factory,
) -> None:
    """A third attempt must hydrate artifacts referenced through the second attempt.

    Resume copies successful node runs with their original artifact ids. Those artifact
    rows remain owned by the first attempt, so loading only the current and immediate
    source run ids makes apply_reuse_plan falsely report that the artifact is missing.
    """

    job_id = "job_chained_resume_hydration"
    origin_run_id = "run_chained_resume_origin"
    source_run_id = "run_chained_resume_source"
    current_run_id = "run_chained_resume_current"
    artifact_id = "art_chained_resume_validated_spec"

    with db_session_factory() as session:
        session.add(
            JobRow(
                id=job_id,
                type=JobType.digital_human_video.value,
                status=JobStatus.running.value,
                case_id="case_demo",
                created_by="usr_admin",
                request_schema="DigitalHumanVideoRequest.v1",
                request={
                    "case_id": "case_demo",
                    "script": "验证连续恢复时的 artifact 装载。",
                    "voice": {"voice_id": "voice_demo_cn"},
                    "strictness": {"strict_timestamps": False},
                    "workflow_template_id": "digital_human_editing_agent_v2",
                },
                active_run_id=current_run_id,
            )
        )
        session.add_all(
            [
                WorkflowRunRow(
                    id=origin_run_id,
                    job_id=job_id,
                    case_id="case_demo",
                    workflow_template_id="digital_human_editing_agent_v2",
                    workflow_version="v1",
                    status=RunStatus.failed.value,
                    run_attempt=1,
                    requested_by="usr_admin",
                ),
                WorkflowRunRow(
                    id=source_run_id,
                    job_id=job_id,
                    case_id="case_demo",
                    workflow_template_id="digital_human_editing_agent_v2",
                    workflow_version="v1",
                    status=RunStatus.failed.value,
                    run_attempt=2,
                    resume_from_run_id=origin_run_id,
                    requested_by="usr_admin",
                ),
                WorkflowRunRow(
                    id=current_run_id,
                    job_id=job_id,
                    case_id="case_demo",
                    workflow_template_id="digital_human_editing_agent_v2",
                    workflow_version="v1",
                    status=RunStatus.admitted.value,
                    run_attempt=3,
                    resume_from_run_id=source_run_id,
                    requested_by="usr_admin",
                ),
            ]
        )
        session.flush()
        session.add(
            ArtifactRow(
                id=artifact_id,
                case_id="case_demo",
                run_id=origin_run_id,
                node_run_id="nr_chained_resume_origin",
                kind=ArtifactKind.validated_production_spec.value,
                payload_schema="ValidatedProductionSpec.v1",
                payload={"source": origin_run_id},
            )
        )
        session.add_all(
            [
                NodeRunRow(
                    id="nr_chained_resume_origin",
                    run_id=origin_run_id,
                    node_id="ValidateRequest",
                    node_version="v1",
                    status=NodeStatus.succeeded.value,
                    input_manifest_hash="origin-input",
                    output_artifact_ids=[artifact_id],
                ),
                NodeRunRow(
                    id="nr_chained_resume_source",
                    run_id=source_run_id,
                    node_id="ValidateRequest",
                    node_version="v1",
                    status=NodeStatus.skipped.value,
                    input_manifest_hash="origin-input",
                    output_artifact_ids=[artifact_id],
                    skipped_reason="resume.reused_artifact_prefix",
                ),
            ]
        )
        session.commit()

    runtime_repository = Repository()
    SqlAlchemyProductionRepository(db_session_factory).hydrate_workflow_runtime_snapshot(
        runtime_repository,
        current_run_id,
    )

    assert runtime_repository.node_runs[source_run_id][0].output_artifact_ids == [artifact_id]
    assert runtime_repository.artifacts[artifact_id].run_id == origin_run_id


def test_hydrate_excludes_failed_node_diagnostic_artifacts(db_session_factory) -> None:
    job_id = "job_resume_diagnostic_filter"
    run_id = "run_resume_diagnostic_filter"
    output_id = "art_resume_committed_output"
    diagnostic_id = "art_resume_failed_diagnostic"
    with db_session_factory() as session:
        session.add(
            JobRow(
                id=job_id,
                type=JobType.digital_human_video.value,
                status=JobStatus.failed.value,
                case_id="case_demo",
                created_by="usr_admin",
                request_schema="DigitalHumanVideoRequest.v1",
                request={
                    "case_id": "case_demo",
                    "script": "失败节点诊断产物不能进入恢复状态。",
                    "voice": {"voice_id": "voice_demo_cn"},
                },
                active_run_id=run_id,
            )
        )
        session.add(
            WorkflowRunRow(
                id=run_id,
                job_id=job_id,
                case_id="case_demo",
                workflow_template_id="digital_human_v2",
                workflow_version="v1",
                status=RunStatus.failed.value,
                requested_by="usr_admin",
            )
        )
        session.flush()
        session.add_all(
            [
                ArtifactRow(
                    id=output_id,
                    case_id="case_demo",
                    run_id=run_id,
                    node_run_id="nr_resume_succeeded",
                    kind=ArtifactKind.validated_production_spec.value,
                    payload_schema="ValidatedProductionSpec.v1",
                    payload={"committed": True},
                ),
                ArtifactRow(
                    id=diagnostic_id,
                    case_id="case_demo",
                    run_id=run_id,
                    node_run_id="nr_resume_failed",
                    kind=ArtifactKind.provider_raw_response.value,
                    payload_schema="ProviderRawResponse.v1",
                    payload={"debug": True},
                ),
            ]
        )
        session.add_all(
            [
                NodeRunRow(
                    id="nr_resume_succeeded",
                    run_id=run_id,
                    node_id="ValidateRequest",
                    node_version="v1",
                    status=NodeStatus.succeeded.value,
                    input_manifest_hash="input-success",
                    output_artifact_ids=[output_id],
                ),
                NodeRunRow(
                    id="nr_resume_failed",
                    run_id=run_id,
                    node_id="TTS",
                    node_version="v1",
                    status=NodeStatus.failed.value,
                    input_manifest_hash="input-failed",
                    output_artifact_ids=[diagnostic_id],
                ),
            ]
        )
        session.commit()

    runtime_repository = Repository()
    SqlAlchemyProductionRepository(db_session_factory).hydrate_workflow_runtime_snapshot(
        runtime_repository,
        run_id,
    )

    assert output_id in runtime_repository.artifacts
    assert diagnostic_id not in runtime_repository.artifacts


def test_hydrate_loads_explicit_creative_intent_reference(db_session_factory) -> None:
    job_id = "job_creative_intent_ref"
    run_id = "run_creative_intent_ref"
    artifact_id = "art_creative_intent_ref"
    with db_session_factory() as session:
        session.add(
            JobRow(
                id=job_id,
                type=JobType.digital_human_video.value,
                status=JobStatus.running.value,
                case_id="case_demo",
                created_by="usr_admin",
                request_schema="DigitalHumanVideoRequest.v1",
                request={
                    "case_id": "case_demo",
                    "script": "使用已有创作意图。",
                    "voice": {"voice_id": "voice_demo_cn"},
                    "creative_intent_ref": {
                        "artifact_id": artifact_id,
                        "kind": ArtifactKind.creative_intent.value,
                        "uri": f"artifact://{artifact_id}",
                        "schema_version": "v1",
                    },
                },
                active_run_id=run_id,
            )
        )
        session.add(
            WorkflowRunRow(
                id=run_id,
                job_id=job_id,
                case_id="case_demo",
                workflow_template_id="digital_human_v2",
                workflow_version="v1",
                status=RunStatus.admitted.value,
                run_attempt=1,
                requested_by="usr_admin",
            )
        )
        session.add(
            ArtifactRow(
                id=artifact_id,
                case_id="case_demo",
                kind=ArtifactKind.creative_intent.value,
                payload_schema="CreativeIntentArtifact.v1",
                payload={"intent": {"hook": "开场", "beats": ["卖点"]}, "emphasis": []},
            )
        )
        session.commit()

    runtime_repository = Repository()
    SqlAlchemyProductionRepository(db_session_factory).hydrate_workflow_runtime_snapshot(
        runtime_repository, run_id
    )

    assert runtime_repository.artifacts[artifact_id].kind == ArtifactKind.creative_intent
