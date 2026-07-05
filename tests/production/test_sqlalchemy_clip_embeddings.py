from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from sqlalchemy.dialects import postgresql

from apps.api.services import clip_embeddings
from packages.core import contracts as c
from packages.core.contracts.artifacts import ClipEmbeddingRecord
from packages.production.sqlalchemy_repository import (
    _clip_embedding_record_from_row,
    _nearest_clip_embeddings_statement,
)


def test_clip_embedding_row_mapper_preserves_index_contract():
    row = SimpleNamespace(
        clip_embedding_key="clipemb_test",
        asset_id="asset_broll",
        asset_revision="asset:asset_broll:v1:v1:test",
        clip_id="clip_1",
        source_start=1.0,
        source_end=3.0,
        source_frames_available=60,
        index_namespace="broll",
        embedding_scope="clip",
        embedding_input_type="video_clip",
        embedding_input_ref="asset_broll:clip_1:1.000000:3.000000",
        sample_policy={"policy_id": "test"},
        embedding_id="emb_1",
        embedding=[1.0, *([0.0] * 1023)],
        provider_profile_id="sandbox.embedding.default",
        embedding_model="qwen3-vl-embedding",
        embedding_dimension=1024,
        normalization="l2",
        instruct="video_clip_retrieval_v1",
        index_version="clip-video-qwen3-v2",
    )

    record = _clip_embedding_record_from_row(row)

    assert record.clip_embedding_key == "clipemb_test"
    assert record.index_namespace == "broll"
    assert record.embedding == [1.0, *([0.0] * 1023)]
    assert record.embedding_model == "qwen3-vl-embedding"
    assert record.embedding_dimension == 1024


def test_clip_embedding_row_mapper_accepts_pgvector_text():
    row = SimpleNamespace(
        clip_embedding_key="clipemb_text",
        asset_id="asset_broll",
        asset_revision="asset:asset_broll:v1:v1:test",
        clip_id="clip_1",
        source_start=1.0,
        source_end=3.0,
        source_frames_available=60,
        index_namespace="broll",
        embedding_scope="clip",
        embedding_input_type="video_clip",
        embedding_input_ref="asset_broll:clip_1:1.000000:3.000000",
        sample_policy={"policy_id": "test"},
        embedding_id="emb_text",
        embedding="[1," + ",".join(["0"] * 1023) + "]",
        provider_profile_id="sandbox.embedding.default",
        embedding_model="qwen3-vl-embedding",
        embedding_dimension=1024,
        normalization="l2",
        instruct="video_clip_retrieval_v1",
        index_version="clip-video-qwen3-v2",
    )

    record = _clip_embedding_record_from_row(row)

    assert record.embedding == [1.0, *([0.0] * 1023)]


def test_clip_embedding_record_rejects_non_finite_vectors():
    with pytest.raises(ValueError, match="finite"):
        ClipEmbeddingRecord(
            clip_embedding_key="clipemb_bad",
            asset_id="asset_broll",
            asset_revision="asset:asset_broll:v1:v1:test",
            clip_id="clip_1",
            source_start=1.0,
            source_end=3.0,
            source_frames_available=60,
            index_namespace="broll",
            embedding_input_ref="asset_broll:clip_1:1.000000:3.000000",
            embedding_id="emb_bad",
            embedding=[float("nan"), *([0.0] * 1023)],
            provider_profile_id="sandbox.embedding.default",
        )


def test_nearest_clip_embeddings_statement_orders_by_pgvector_cosine_distance():
    statement = _nearest_clip_embeddings_statement(
        clip_embedding_keys=["clipemb_a", "clipemb_b"],
        namespace="broll",
        provider_profile_id="dashscope.multimodal_embedding.prod",
        embedding_model="qwen3-vl-embedding",
        embedding_dimension=1024,
        normalization="l2",
        index_version="clip-video-qwen3-v2",
        min_source_frames_available=60,
        limit=12,
    )

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "clip_embedding_index.embedding <=> %(query_embedding)s" in sql
    assert "ORDER BY distance" in sql
    assert "clip_embedding_index.embedding_model = %(embedding_model_1)s" in sql
    assert "clip_embedding_index.index_version = %(index_version_1)s" in sql
    assert "clip_embedding_index.source_frames_available >= %(source_frames_available_1)s" in sql


def test_enqueue_clip_embeddings_returns_job_status(monkeypatch):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    payload = c.ClipEmbeddingIndexRequest(
        case_id="case_test",
        asset_ids=["asset_1"],
        namespace="all",
        provider_profile_id="dashscope.multimodal_embedding.prod",
        limit=5,
    )

    def fake_status(
        _request,
        *,
        case_id: str,
        namespace: c.ClipEmbeddingNamespace,
        asset_id: str | None = None,
        asset_ids: list[str] | None = None,
    ) -> c.ClipEmbeddingIndexStatusResponse:
        assert case_id == "case_test"
        assert namespace == "all"
        assert asset_id is None
        assert asset_ids == ["asset_1"]
        return c.ClipEmbeddingIndexStatusResponse(
            case_id=case_id,
            namespace=namespace,
            candidate_count=3,
            indexed_count=1,
            pending_count=2,
            annotated_asset_count=1,
            skipped_asset_count=0,
            request_id="req_test",
        )

    monkeypatch.setattr(clip_embeddings, "clip_embedding_status", fake_status)

    response = clip_embeddings.enqueue_clip_embeddings(payload, request, BackgroundTasks())

    assert response.schema_version == "clip_embedding_index_job_response.v1"
    assert response.status == c.JobStatus.queued
    assert response.queued_count == 2
    assert response.pending_count == 2
    assert response.limit == 5
    stored = clip_embeddings.clip_embedding_job_status(request, response.job_id)
    assert stored.schema_version == "clip_embedding_job_status.v1"
    assert stored.job_id == response.job_id


def test_run_clip_embedding_job_publishes_incremental_progress(monkeypatch):
    app = SimpleNamespace(state=SimpleNamespace())
    request = SimpleNamespace(app=app)
    payload = c.ClipEmbeddingIndexRequest(
        case_id="case_test",
        namespace="all",
        provider_profile_id="dashscope.multimodal_embedding.prod",
        limit=5,
    )
    job = c.ClipEmbeddingJobStatusResponse(
        job_id="embjob_test",
        case_id="case_test",
        namespace="all",
        status=c.JobStatus.queued,
        provider_profile_id=payload.provider_profile_id,
        limit=payload.limit,
        queued_count=2,
        candidate_count=2,
        pending_count=2,
        remaining_count=2,
        request_id="req_job",
    )
    clip_embeddings._store_job(app, job)

    snapshots: list[c.ClipEmbeddingJobStatusResponse] = []
    original_update_job = clip_embeddings._update_job

    def spy_update_job(*args, **kwargs):
        updated = original_update_job(*args, **kwargs)
        if updated is not None:
            snapshots.append(updated)
        return updated

    def fake_index_clip_embeddings(
        _payload: c.ClipEmbeddingIndexRequest,
        _request,
        progress_callback=None,
    ) -> c.ClipEmbeddingIndexResponse:
        assert progress_callback is not None
        progress_callback(
            c.ClipEmbeddingIndexResponse(
                case_id="case_test",
                namespace="all",
                candidate_count=2,
                indexed_count=1,
                pending_count=1,
                annotated_asset_count=1,
                skipped_asset_count=0,
                provider_profile_id=payload.provider_profile_id,
                processed_count=1,
                indexed_now_count=1,
                failed_count=0,
                remaining_count=1,
                request_id="req_partial",
            )
        )
        return c.ClipEmbeddingIndexResponse(
            case_id="case_test",
            namespace="all",
            candidate_count=2,
            indexed_count=2,
            pending_count=0,
            annotated_asset_count=1,
            skipped_asset_count=0,
            provider_profile_id=payload.provider_profile_id,
            processed_count=2,
            indexed_now_count=2,
            failed_count=0,
            remaining_count=0,
            request_id="req_done",
        )

    monkeypatch.setattr(clip_embeddings, "_update_job", spy_update_job)
    monkeypatch.setattr(clip_embeddings, "index_clip_embeddings", fake_index_clip_embeddings)

    clip_embeddings._run_clip_embedding_job(app, job.job_id, payload)

    assert any(snapshot.status == c.JobStatus.running for snapshot in snapshots)
    assert any(snapshot.processed_count == 1 and snapshot.remaining_count == 1 for snapshot in snapshots)
    stored = clip_embeddings.clip_embedding_job_status(request, job.job_id)
    assert stored.status == c.JobStatus.succeeded
    assert stored.processed_count == 2
    assert stored.remaining_count == 0
