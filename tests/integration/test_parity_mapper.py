from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import os
from typing import Any
from uuid import uuid4

import pytest

RUN_DB_TESTS = os.getenv("CUTAGENT_RUN_DB_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not RUN_DB_TESTS,
    reason="Set CUTAGENT_RUN_DB_TESTS=1 to run mapper parity integration tests.",
)

if RUN_DB_TESTS:
    from sqlalchemy import select

    from packages.core.contracts import (
        Artifact,
        ArtifactKind,
        CreateImportBatchRequest,
        DigitalHumanVideoRequest,
        Job,
        JobStatus,
        JobType,
        Money,
        NodeRun,
        NodeStatus,
        PerformanceObservation,
        ProviderInvocation,
        ProviderStatus,
        PublishRecord,
        RunDebugReportArtifact,
        RunPublicReportArtifact,
        RunStatus,
        UsageMeterRecord,
        WorkflowRun,
    )
    from packages.core.storage import Repository
    from packages.core.storage.bootstrap import get_sqlalchemy_session_factory_if_enabled
    from packages.core.storage.database import (
        ArtifactRow,
        PerformanceObservationRow,
        ProviderInvocationRow,
        PublishRecordRow,
        UsageMeterRecordRow,
    )
    from packages.core.storage.performance_mappers import performance_observation_to_row
    from packages.ops import SqlAlchemyOpsRepository
    from packages.production import SqlAlchemyProductionRepository
    from packages.production.sqlalchemy_mappers import (
        artifact_row_to_contract,
        performance_observation_row_to_contract,
        publish_record_row_to_contract,
    )


def _session_factory():
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run mapper parity tests.")
    return session_factory


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _without_timestamps(value: Any) -> Any:
    """Drop volatile audit timestamps for round-trip comparisons.

    created_at / updated_at are stamped at persist/import time, so a contract
    built in the test body and the same row read back differ by microseconds.
    The round-trip guarantee is about the meaningful fields, not the audit clock.
    """
    dumped = _dump(value)
    if isinstance(dumped, dict):
        return {k: v for k, v in dumped.items() if k not in {"created_at", "updated_at"}}
    return dumped


def _stored_provider_invocation(invocation: ProviderInvocation) -> dict[str, Any]:
    data = invocation.model_dump(mode="json")
    data.pop("usage", None)
    return data


def _stored_provider_invocation_row(row: ProviderInvocationRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "case_id": row.case_id,
        "run_id": row.run_id,
        "node_run_id": row.node_run_id,
        "provider_id": row.provider_id,
        "model_id": row.model_id,
        "provider_profile_id": row.provider_profile_id,
        "capability_id": row.capability_id,
        "prompt_version_id": row.prompt_version_id,
        "status": row.status,
        "price_item_id": row.price_item_id,
        "billing_status": row.billing_status,
        "duration_ms": row.duration_ms,
        "retry_count": row.retry_count,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "estimated_cost": row.estimated_cost,
        "actual_cost": row.actual_cost,
        "request_artifact_id": row.request_artifact_id,
        "response_artifact_id": row.response_artifact_id,
        "external_job_id": row.external_job_id,
        "error": row.error,
        "started_at": row.started_at.isoformat().replace("+00:00", "Z") if row.started_at else None,
        "finished_at": row.finished_at.isoformat().replace("+00:00", "Z") if row.finished_at else None,
        "created_at": row.created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": row.updated_at.isoformat().replace("+00:00", "Z"),
        "created_by": None,
        "version": 1,
        "schema_version": row.schema_version,
    }


def _stored_usage(usage: UsageMeterRecord) -> dict[str, Any]:
    return usage.model_dump(mode="json")


def _stored_usage_row(row: UsageMeterRecordRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "provider_invocation_id": row.provider_invocation_id,
        "provider_id": row.provider_id,
        "model_id": row.model_id,
        "capability_id": row.capability_id,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cached_input_tokens": row.cached_input_tokens,
        "audio_seconds": row.audio_seconds,
        "video_seconds": row.video_seconds,
        "image_count": row.image_count,
        "provider_credits": str(row.provider_credits) if row.provider_credits is not None else None,
        "raw_usage": row.raw_usage,
        "created_at": row.created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": row.updated_at.isoformat().replace("+00:00", "Z"),
        "created_by": None,
        "version": 1,
        "schema_version": row.schema_version,
    }


def test_workflow_snapshot_contracts_round_trip_through_sqlalchemy_read_paths():
    session_factory = _session_factory()
    production = SqlAlchemyProductionRepository(session_factory)
    ops = SqlAlchemyOpsRepository(session_factory)
    runtime_repository = Repository()
    suffix = uuid4().hex[:8]
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        title=f"Mapper parity {suffix}",
        script="mapper parity script",
        voice={"voice_id": "voice_sandbox"},
        broll={"enabled": False},
        bgm={"enabled": False},
        strictness={"strict_timestamps": False},
    )
    job = Job(
        id=f"job_mapper_{suffix}",
        type=JobType.digital_human_video,
        status=JobStatus.succeeded,
        case_id="case_demo",
        created_by="usr_admin",
        request_schema=request.schema_version,
        request=request,
        active_run_id=f"run_mapper_{suffix}",
        created_at=now,
        updated_at=now,
    )
    public_report = RunPublicReportArtifact(
        run_id=job.active_run_id,
        status=RunStatus.succeeded,
        summary="mapper parity public report",
        node_statuses={"ValidateRequest": NodeStatus.succeeded},
    )
    debug_report = RunDebugReportArtifact(
        **public_report.model_dump(mode="python"),
        artifact_ids=[f"art_mapper_payload_{suffix}"],
        provider_invocation_ids=[f"pinv_mapper_{suffix}"],
    )
    public_artifact = Artifact(
        id=f"art_mapper_public_{suffix}",
        case_id="case_demo",
        run_id=job.active_run_id,
        kind=ArtifactKind.run_report_public,
        uri=f"sandbox://mapper/{suffix}/public.json",
        sha256="0" * 64,
        payload_schema="RunPublicReportArtifact.v1",
        payload=public_report.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
    )
    debug_artifact = Artifact(
        id=f"art_mapper_debug_{suffix}",
        case_id="case_demo",
        run_id=job.active_run_id,
        kind=ArtifactKind.run_report_debug,
        uri=f"sandbox://mapper/{suffix}/debug.json",
        payload_schema="RunDebugReportArtifact.v1",
        payload=debug_report.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
    )
    payload_artifact = Artifact(
        id=f"art_mapper_payload_{suffix}",
        case_id="case_demo",
        run_id=job.active_run_id,
        node_run_id=f"node_mapper_{suffix}",
        kind=ArtifactKind.creative_intent,
        uri=f"sandbox://mapper/{suffix}/creative.json",
        payload_schema="CreativeIntentArtifact.v1",
        payload={"hook": "parity", "beats": ["one", "two"], "role": "main"},
        created_by_node_run_id=f"node_mapper_{suffix}",
        created_at=now,
        updated_at=now,
    )
    run = WorkflowRun(
        id=job.active_run_id,
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id=request.workflow_template_id,
        workflow_version="v1",
        status=RunStatus.succeeded,
        requested_by="usr_admin",
        public_report_artifact_id=public_artifact.id,
        debug_report_artifact_id=debug_artifact.id,
        started_at=now,
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    node_run = NodeRun(
        id=f"node_mapper_{suffix}",
        run_id=run.id,
        node_id="ValidateRequest",
        node_version="v1",
        status=NodeStatus.succeeded,
        input_manifest_hash="hash",
        output_artifact_ids=[payload_artifact.id],
        provider_invocation_ids=[f"pinv_mapper_{suffix}"],
        started_at=now,
        finished_at=now,
        created_at=now,
        updated_at=now,
    )
    usage = UsageMeterRecord(
        id=f"usage_mapper_{suffix}",
        provider_invocation_id=f"pinv_mapper_{suffix}",
        provider_id="sandbox",
        model_id="tts.local",
        capability_id="tts.speech",
        input_tokens=7,
        output_tokens=11,
        audio_seconds=1.5,
        raw_usage={"role": "main"},
        created_at=now,
        updated_at=now,
    )
    invocation = ProviderInvocation(
        id=usage.provider_invocation_id,
        case_id="case_demo",
        run_id=run.id,
        node_run_id=node_run.id,
        provider_id="sandbox",
        model_id="tts.local",
        provider_profile_id="sandbox.tts.default",
        capability_id="tts.speech",
        status=ProviderStatus.succeeded,
        usage=usage,
        billing_status="estimated",
        duration_ms=123,
        input_tokens=7,
        output_tokens=11,
        estimated_cost=Money(amount=Decimal("0.12"), currency="CNY"),
        started_at=now,
        finished_at=now,
        created_at=now,
        updated_at=now,
    )

    runtime_repository.artifacts.update(
        {
            public_artifact.id: public_artifact,
            debug_artifact.id: debug_artifact,
            payload_artifact.id: payload_artifact,
        }
    )
    runtime_repository.node_runs[run.id] = [node_run]
    runtime_repository.provider_invocations[invocation.id] = invocation
    runtime_repository.usage_records[usage.id] = usage

    production.sync_workflow_snapshot(job=job, run=run, repository=runtime_repository)

    detail = production.run_detail(run.id, "req_mapper")
    assert detail is not None
    assert detail.run.model_dump(mode="json") == run.model_dump(mode="json")
    assert [item.model_dump(mode="json") for item in detail.node_runs] == [
        node_run.model_dump(mode="json")
    ]

    report = production.run_report(run.id, "req_mapper")
    assert report is not None
    assert report.public_report.model_dump(mode="json") == public_report.model_dump(mode="json")
    assert report.debug_report is not None
    assert report.debug_report.model_dump(mode="json") == debug_report.model_dump(mode="json")

    with session_factory() as session:
        artifact = artifact_row_to_contract(session.get(ArtifactRow, payload_artifact.id))
        provider_row = session.get(ProviderInvocationRow, invocation.id)
        usage_row = session.get(UsageMeterRecordRow, usage.id)

    assert artifact.model_dump(mode="json") == payload_artifact.model_dump(mode="json")
    assert provider_row is not None
    assert _stored_provider_invocation_row(provider_row) == _stored_provider_invocation(invocation)
    assert usage_row is not None
    assert _stored_usage_row(usage_row) == _stored_usage(usage)

    usage_report = ops.provider_usage(provider_id="sandbox", case_id="case_demo")
    assert usage_report.invocations >= 1
    assert usage_report.estimated_cost.amount >= Decimal("0.12")


def test_publish_record_and_performance_observation_existing_mappers_round_trip():
    session_factory = _session_factory()
    production = SqlAlchemyProductionRepository(session_factory)
    suffix = uuid4().hex[:8]
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    import_report = production.create_import_batch(
        CreateImportBatchRequest(
            import_type="publish_record",
            rows=[
                {
                    "case_id": "case_demo",
                    "platform": "douyin",
                    "status": "draft",
                }
            ],
        ),
        request_id="req_mapper_import",
    )
    assert import_report is not None
    publish_record_id = import_report.results[0].internal_id
    expected_publish = PublishRecord(
        id=publish_record_id,
        case_id="case_demo",
        platform="douyin",
        status="draft",
    )

    observation = PerformanceObservation(
        id=f"obs_mapper_{suffix}",
        case_id="case_demo",
        publish_record_id=publish_record_id,
        platform="douyin",
        account_id="acct_mapper",
        window="7d",
        metric_name="views",
        metric_value=321.0,
        impressions=1000,
        views=321,
        completion_rate=0.42,
        raw_metrics={"source": "mapper"},
        observed_at=now,
        created_at=now,
        updated_at=now,
    )
    with session_factory() as session:
        session.add(performance_observation_to_row(observation))
        session.commit()
        publish_row = session.scalar(
            select(PublishRecordRow).where(PublishRecordRow.id == publish_record_id)
        )
        observation_row = session.scalar(
            select(PerformanceObservationRow).where(
                PerformanceObservationRow.id == observation.id
            )
        )

    assert publish_row is not None
    assert _without_timestamps(publish_record_row_to_contract(publish_row)) == _without_timestamps(
        expected_publish
    )
    assert observation_row is not None
    assert _without_timestamps(
        performance_observation_row_to_contract(observation_row)
    ) == _without_timestamps(observation)
