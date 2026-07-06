from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from sqlalchemy.dialects import postgresql

from apps.api.services import clip_embeddings
from packages.ai.gateway import ProviderResult
from packages.core import contracts as c
from packages.core.contracts.artifacts import ClipEmbeddingRecord
from packages.core.storage.database import (
    AnnotationRow,
    ArtifactRow,
    CaseRow,
    ClipEmbeddingJobRow,
    MediaAssetRow,
)
from packages.core.workflow import NodeExecutionError
from packages.planning.material.clip_embedding import (
    CLIP_INDEX_VERSION,
    asset_revision_token,
    build_clip_embedding_record,
    candidate_clip_embedding_key,
    candidate_source_span,
    deterministic_dense_embedding,
    normalize_vector,
    sample_policy_hash,
)
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
        index_version=CLIP_INDEX_VERSION,
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
        index_version=CLIP_INDEX_VERSION,
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


def _clip_candidate() -> dict:
    return {
        "asset_id": "asset_broll",
        "metadata": {
            "clip_id": "clip_1",
            "source_start": 1.0,
            "source_end": 3.0,
        },
    }


def test_clip_embedding_helpers_validate_keys_vectors_and_source_spans():
    candidate = _clip_candidate()
    asset = c.MediaAssetRecord(
        id="asset_broll",
        title="施工前现场",
        kind="video",
        annotation_status="annotated",
        usable=True,
    )

    assert sample_policy_hash({"b": 2, "a": 1}) == sample_policy_hash({"a": 1, "b": 2})
    assert asset_revision_token(None) == "asset:unknown"
    assert candidate_source_span(candidate) == ("clip_1", 1.0, 3.0)
    assert candidate_clip_embedding_key(candidate=candidate, asset=asset, namespace="broll").startswith(
        "clipemb_"
    )
    dense = deterministic_dense_embedding("seed", dimension=8)
    assert len(dense) == 8
    assert math.isclose(sum(value * value for value in dense), 1.0, rel_tol=1e-6)
    padded = normalize_vector([3.0, 4.0], dimension=3)
    assert padded == [0.6, 0.8, 0.0]
    record = build_clip_embedding_record(
        candidate=candidate,
        asset=asset,
        namespace="broll",
        provider_profile_id="sandbox.embedding.default",
        embedding=[1.0, *([0.0] * 1023)],
    )
    assert record.index_version == CLIP_INDEX_VERSION
    assert record.source_frames_available == 60

    for bad_candidate, message in (
        ({"asset_id": "asset_broll", "metadata": {"source_start": 1.0, "source_end": 3.0}}, "clip_id"),
        (
            {
                "asset_id": "asset_broll",
                "metadata": {"clip_id": "clip_1", "source_start": 3.0, "source_end": 3.0},
            },
            "greater",
        ),
        (
            {
                "asset_id": "asset_broll",
                "metadata": {"clip_id": "clip_1", "source_start": "nan", "source_end": 3.0},
            },
            "finite",
        ),
        (
            {
                "asset_id": "asset_broll",
                "metadata": {"clip_id": "clip_1", "source_start": "oops", "source_end": 3.0},
            },
            "finite",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            candidate_source_span(bad_candidate)

    for values, message in (
        ([float("nan"), 1.0], "finite"),
        ([0.0, 0.0], "non-zero"),
    ):
        with pytest.raises(ValueError, match=message):
            normalize_vector(values, dimension=2)
    for embedding, message in (
        (None, "required"),
        ([1.0, 0.0], "dimension mismatch"),
        ([float("inf"), *([0.0] * 1023)], "finite"),
        ([0.0] * 1024, "non-zero"),
    ):
        with pytest.raises(ValueError, match=message):
            build_clip_embedding_record(
                candidate=candidate,
                asset=asset,
                namespace="broll",
                provider_profile_id="sandbox.embedding.default",
                embedding=embedding,
            )


def test_nearest_clip_embeddings_statement_orders_by_pgvector_cosine_distance():
    statement = _nearest_clip_embeddings_statement(
        clip_embedding_keys=["clipemb_a", "clipemb_b"],
        namespace="broll",
        provider_profile_id="dashscope.multimodal_embedding.prod",
        embedding_model="qwen3-vl-embedding",
        embedding_dimension=1024,
        normalization="l2",
        index_version=CLIP_INDEX_VERSION,
        min_source_frames_available=60,
        limit=12,
    )

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "clip_embedding_index.embedding <=> %(query_embedding)s" in sql
    assert "ORDER BY distance" in sql
    assert "clip_embedding_index.embedding_model = %(embedding_model_1)s" in sql
    assert "clip_embedding_index.index_version = %(index_version_1)s" in sql
    assert "clip_embedding_index.source_frames_available >= %(source_frames_available_1)s" in sql
    compiled = statement.compile(dialect=postgresql.dialect())
    assert compiled.params["index_version_1"] == CLIP_INDEX_VERSION


def test_asset_revision_token_prefers_source_artifact_over_annotation_timestamp():
    base_time = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    asset = c.MediaAssetRecord(
        id="asset_broll",
        title="施工前现场",
        kind="video",
        source_artifact_id="artifact_source_a",
        updated_at=base_time,
        version=7,
        schema_version="v4",
    )
    retagged = asset.model_copy(update={"updated_at": base_time + timedelta(hours=1)})
    replaced_source = asset.model_copy(update={"source_artifact_id": "artifact_source_b"})
    legacy = asset.model_copy(update={"source_artifact_id": None})
    legacy_retagged = legacy.model_copy(update={"updated_at": base_time + timedelta(hours=1)})

    assert asset_revision_token(asset) == "asset:asset_broll:v7:v4:src:artifact_source_a"
    assert asset_revision_token(retagged) == asset_revision_token(asset)
    assert asset_revision_token(replaced_source) != asset_revision_token(asset)
    assert asset_revision_token(legacy) != asset_revision_token(legacy_retagged)
    assert asset_revision_token(legacy).endswith(base_time.isoformat())


def _annotation(asset_id: str) -> c.AnnotationV4:
    return c.AnnotationV4(
        meta=c.AnnotationMetaV4(
            asset_id=asset_id,
            case_id="case_test",
            material_type="video",
            duration=6.0,
        ),
        clips=[
            c.ClipV4(
                segment_id="portrait_1",
                start=0.0,
                end=4.0,
                duration=4.0,
                semantics=c.ClipSemanticsV4(
                    subject_type="person",
                    mouth_visible=True,
                    contains_face=True,
                    face_count_max=1,
                    narrative_role="主讲",
                ),
                usage=c.ClipUsageV4(role=c.UsageRole.main, recommended_for_lip_sync=True),
                retrieval=c.ClipRetrievalV4(
                    summary="主讲解释施工流程",
                    keywords=["施工", "主讲"],
                    retrieval_sentence="主讲解释施工流程",
                ),
            ),
            c.ClipV4(
                segment_id="broll_1",
                start=0.0,
                end=5.0,
                duration=5.0,
                semantics=c.ClipSemanticsV4(
                    scene_type="施工前",
                    narrative_role="现场展示",
                    contains_face=False,
                    face_count_max=0,
                ),
                usage=c.ClipUsageV4(role=c.UsageRole.cover),
                retrieval=c.ClipRetrievalV4(
                    summary="施工前现场",
                    keywords=["施工前", "现场"],
                    retrieval_sentence="施工前现场",
                ),
            ),
        ],
        quality_events=[
            c.QualityEventV4(
                event_id="qe_1",
                event_type=c.QualityEventType.shake,
                start=1.0,
                end=2.0,
                risk_tier="hard",
            )
        ],
        usage_windows=[c.UsageWindowV4(start=0.0, end=5.0, role=c.UsageRole.cover)],
        quality_report={"usable_ratio": 0.9},
    )


def _seed_embedding_source_rows(db_session_factory, *, asset_id: str = "asset_embed") -> None:
    annotation = _annotation(asset_id)
    with db_session_factory() as session:
        session.add(CaseRow(id="case_test", name="测试案例", status="active"))
        session.flush()
        session.add(
            ArtifactRow(
                id=f"art_{asset_id}",
                case_id="case_test",
                run_id="run_test",
                node_run_id="nr_test",
                kind="uploaded.file",
                uri="/tmp/source.mp4",
                payload_schema="UploadedFileArtifact.v1",
                payload={},
            )
        )
        session.flush()
        session.add(
            MediaAssetRow(
                id=asset_id,
                case_id="case_test",
                title="原厂施工素材",
                kind="video",
                source_artifact_id=f"art_{asset_id}",
                tags=["施工", "案例"],
                annotation_status="annotated",
                usable=True,
                duration_sec=6.0,
            )
        )
        session.flush()
        session.add(
            AnnotationRow(
                id=f"anno_{asset_id}",
                asset_id=asset_id,
                etag="etag",
                canonical_schema="AnnotationV4",
                canonical=annotation.model_dump(mode="json"),
                projection_schema="AnnotationEditorVm.v1",
                projection={"usable": True},
                editable_paths=[],
            )
        )
        session.add(
            MediaAssetRow(
                id=f"{asset_id}_skipped",
                case_id="case_test",
                title="缺少源文件",
                kind="video",
                source_artifact_id=None,
                tags=[],
                annotation_status="annotated",
                usable=True,
                duration_sec=6.0,
            )
        )
        session.commit()


def _request(db_session_factory, *, provider_gateway=None):
    state = SimpleNamespace(sqlalchemy_session_factory=db_session_factory)
    if provider_gateway is not None:
        state.provider_gateway = provider_gateway
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_clip_embedding_status_builds_candidates_and_rejects_ambiguous_scope(db_session_factory):
    _seed_embedding_source_rows(db_session_factory)
    request = _request(db_session_factory)

    status = clip_embeddings.clip_embedding_status(
        request,
        case_id="case_test",
        namespace="all",
    )

    assert status.candidate_count == 4
    assert status.pending_count == 4
    assert status.annotated_asset_count == 1
    assert status.skipped_asset_count == 1
    assert status.embedding_model == "qwen3-vl-embedding"

    with pytest.raises(NodeExecutionError):
        clip_embeddings.clip_embedding_status(
            request,
            case_id="case_test",
            namespace="all",
            asset_id="asset_embed",
            asset_ids=["asset_embed"],
        )


class _FakeEmbeddingGateway:
    def __init__(self, output: dict | None = None, error: c.ProviderError | None = None) -> None:
        self.output = output
        self.error = error
        self.calls = []

    def invoke(self, call):
        self.calls.append(call)
        invocation = c.ProviderInvocation(
            id=f"pinv_{len(self.calls)}",
            provider_id="sandbox",
            model_id="qwen3-vl-embedding",
            provider_profile_id=call.provider_profile_id,
            capability_id=call.capability_id,
            status=c.ProviderStatus.failed if self.error else c.ProviderStatus.succeeded,
            error=self.error,
        )
        if self.error:
            return invocation, None
        return invocation, ProviderResult(output=self.output or {"embedding": [1.0, *([0.0] * 1023)]})


def test_index_clip_embeddings_indexes_pending_records_and_reports_progress(
    db_session_factory,
    monkeypatch,
):
    _seed_embedding_source_rows(db_session_factory)
    gateway = _FakeEmbeddingGateway({"embedding": [1.0, *([0.0] * 1023)], "embedding_id": "emb_ok"})
    request = _request(db_session_factory, provider_gateway=gateway)
    monkeypatch.setattr(
        clip_embeddings,
        "_prepare_clip_video",
        lambda _request, candidate: (f"s3://bucket/{candidate.key}.mp4", "https://oss.example.com/clip.mp4"),
    )
    snapshots: list[c.ClipEmbeddingIndexResponse] = []

    response = clip_embeddings.index_clip_embeddings(
        c.ClipEmbeddingIndexRequest(
            case_id="case_test",
            namespace="all",
            provider_profile_id="dashscope.multimodal_embedding.prod",
            limit=2,
        ),
        request,
        progress_callback=snapshots.append,
    )

    assert response.processed_count == 2
    assert response.indexed_now_count == 2
    assert response.failed_count == 0
    assert response.remaining_count == 2
    assert {item.status for item in response.results} == {"indexed"}
    assert snapshots[0].processed_count == 0
    assert snapshots[-1].processed_count == 2
    assert gateway.calls[0].input == {
        "video_url": "https://oss.example.com/clip.mp4",
        "model": "qwen3-vl-embedding",
        "dimension": 1024,
        "normalization": "l2",
        "instruct": "video_clip_retrieval_v1",
        "index_version": CLIP_INDEX_VERSION,
    }
    assert "text" not in gateway.calls[0].input
    assert "retrieval_intent" not in gateway.calls[0].input
    assert "contents" not in gateway.calls[0].input
    with db_session_factory() as session:
        existing, last_indexed_at = clip_embeddings._existing_index_snapshot(
            session,
            {item.clip_embedding_key for item in response.results},
        )
    assert len(existing) == 2
    assert last_indexed_at is not None


def test_call_embedding_provider_classifies_failures(monkeypatch):
    asset_row = MediaAssetRow(
        id="asset_embed",
        case_id="case_test",
        title="素材",
        kind="video",
        annotation_status="annotated",
        usable=True,
    )
    candidate = clip_embeddings._IndexCandidate(
        namespace="broll",
        candidate=_clip_candidate(),
        asset_row=asset_row,
        annotation=_annotation("asset_embed"),
        clip=_annotation("asset_embed").clips[1],
        source_uri="/tmp/source.mp4",
        text="施工前现场",
        key="clipemb_test",
    )
    monkeypatch.setattr(
        clip_embeddings,
        "_prepare_clip_video",
        lambda *_args, **_kwargs: ("s3://bucket/clip.mp4", "https://oss.example.com/clip.mp4"),
    )
    success_gateway = _FakeEmbeddingGateway(
        {"embedding": [1.0, *([0.0] * 1023)], "embedding_id": "emb_ok"}
    )
    success_request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(provider_gateway=success_gateway))
    )

    prepared, message, fatal = clip_embeddings._call_embedding_provider(
        success_request,
        provider_profile_id="dashscope.multimodal_embedding.prod",
        case_id="case_test",
        candidate=candidate,
    )

    assert prepared is not None
    assert prepared.input_ref == "s3://bucket/clip.mp4"
    assert message is None
    assert fatal is False
    assert success_gateway.calls[0].input == {
        "video_url": "https://oss.example.com/clip.mp4",
        "model": "qwen3-vl-embedding",
        "dimension": 1024,
        "normalization": "l2",
        "instruct": "video_clip_retrieval_v1",
        "index_version": CLIP_INDEX_VERSION,
    }
    assert "text" not in success_gateway.calls[0].input
    assert "retrieval_intent" not in success_gateway.calls[0].input
    assert "contents" not in success_gateway.calls[0].input

    auth_request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                provider_gateway=_FakeEmbeddingGateway(
                    error=c.ProviderError(
                        code=c.ErrorCode.provider_auth_failed,
                        message="bad key",
                    )
                )
            )
        )
    )

    prepared, message, fatal = clip_embeddings._call_embedding_provider(
        auth_request,
        provider_profile_id="dashscope.multimodal_embedding.prod",
        case_id="case_test",
        candidate=candidate,
    )

    assert prepared is None
    assert message == "bad key"
    assert fatal is True

    bad_output_request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(provider_gateway=_FakeEmbeddingGateway({"embedding": "not-a-vector"}))
        )
    )
    prepared, message, fatal = clip_embeddings._call_embedding_provider(
        bad_output_request,
        provider_profile_id="dashscope.multimodal_embedding.prod",
        case_id="case_test",
        candidate=candidate,
    )
    assert prepared is None
    assert message == "Embedding provider did not return a vector."
    assert fatal is True


def test_clip_embedding_video_preparation_helpers(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    small = tmp_path / "small.mp4"
    small.write_bytes(b"small")
    assert clip_embeddings._source_path_for_uri(None, str(source)) == source
    assert clip_embeddings._fit_embedding_video_budget(small, tmp_path) == small
    assert clip_embeddings._safe_filename("clip / 1?.mp4") == "clip___1__mp4"
    assert clip_embeddings._is_public_http_url("https://oss.example.com/clip.mp4") is True
    assert clip_embeddings._is_public_http_url("http://127.0.0.1/clip.mp4") is False
    assert clip_embeddings._is_public_http_url("http://10.0.0.2/clip.mp4") is False
    assert clip_embeddings._is_public_http_url("file:///tmp/clip.mp4") is False
    with pytest.raises(clip_embeddings._EmbeddingPreparationError, match="source_start"):
        clip_embeddings._finite_float("oops", field_name="source_start")
    with pytest.raises(clip_embeddings._EmbeddingPreparationError, match="non-negative"):
        clip_embeddings._finite_float(-1, field_name="source_start")
    with pytest.raises(clip_embeddings._EmbeddingPreparationError, match="cannot be materialized"):
        clip_embeddings._source_path_for_uri(None, str(tmp_path / "missing.mp4"))

    large = tmp_path / "large.mp4"
    large.write_bytes(b"x")
    monkeypatch.setattr(clip_embeddings, "_MAX_EMBEDDING_VIDEO_BYTES", 0)
    compressed = tmp_path / "compressed.mp4"
    compressed.write_bytes(b"ok")
    monkeypatch.setattr(
        clip_embeddings,
        "compress_video_to_budget",
        lambda *_args, **_kwargs: SimpleNamespace(
            path=compressed,
            size_bytes=clip_embeddings._MAX_EMBEDDING_VIDEO_BYTES,
        ),
    )
    assert clip_embeddings._fit_embedding_video_budget(large, tmp_path) == compressed


def test_enqueue_clip_embeddings_returns_job_status(monkeypatch, db_session_factory):
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(sqlalchemy_session_factory=db_session_factory),
        )
    )
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
    with db_session_factory() as session:
        row = session.get(ClipEmbeddingJobRow, response.job_id)
        assert row is not None
        assert row.status == c.JobStatus.queued.value
        assert row.payload["job_id"] == response.job_id
    restarted_request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(sqlalchemy_session_factory=db_session_factory),
        )
    )
    assert clip_embeddings.clip_embedding_job_status(restarted_request, response.job_id).job_id == (
        response.job_id
    )


def test_run_clip_embedding_job_publishes_incremental_progress(monkeypatch, db_session_factory):
    app = SimpleNamespace(state=SimpleNamespace(sqlalchemy_session_factory=db_session_factory))
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


def test_clip_embedding_job_reconcile_marks_interrupted_jobs_failed(db_session_factory):
    app = SimpleNamespace(state=SimpleNamespace(sqlalchemy_session_factory=db_session_factory))
    for job_id, status in [
        ("embjob_queued", c.JobStatus.queued),
        ("embjob_running", c.JobStatus.running),
        ("embjob_succeeded", c.JobStatus.succeeded),
    ]:
        clip_embeddings._store_job(
            app,
            c.ClipEmbeddingJobStatusResponse(
                job_id=job_id,
                case_id="case_test",
                namespace="all",
                status=status,
                provider_profile_id="dashscope.multimodal_embedding.prod",
                limit=5,
                request_id=f"req_{job_id}",
            ),
        )

    clip_embeddings.reconcile_interrupted_clip_embedding_jobs(app)

    queued = clip_embeddings._read_job(app, "embjob_queued")
    running = clip_embeddings._read_job(app, "embjob_running")
    succeeded = clip_embeddings._read_job(app, "embjob_succeeded")
    assert queued is not None and queued.status == c.JobStatus.failed
    assert running is not None and running.status == c.JobStatus.failed
    assert queued.error_message == "API 重启中断，请重新发起索引"
    assert running.finished_at is not None
    assert succeeded is not None and succeeded.status == c.JobStatus.succeeded


def test_clip_embedding_job_late_progress_cannot_overwrite_terminal_status(db_session_factory):
    app = SimpleNamespace(state=SimpleNamespace(sqlalchemy_session_factory=db_session_factory))
    clip_embeddings._store_job(
        app,
        c.ClipEmbeddingJobStatusResponse(
            job_id="embjob_terminal",
            case_id="case_test",
            namespace="all",
            status=c.JobStatus.succeeded,
            provider_profile_id="dashscope.multimodal_embedding.prod",
            limit=5,
            processed_count=2,
            remaining_count=0,
            request_id="req_terminal",
        ),
    )

    updated = clip_embeddings._update_job(
        app,
        "embjob_terminal",
        status=c.JobStatus.running,
        processed_count=1,
        remaining_count=1,
    )

    assert updated is not None
    assert updated.status == c.JobStatus.succeeded
    stored = clip_embeddings._read_job(app, "embjob_terminal")
    assert stored is not None
    assert stored.status == c.JobStatus.succeeded
    assert stored.processed_count == 2
    assert stored.remaining_count == 0
