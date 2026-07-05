from __future__ import annotations

import pytest

from packages.ai.gateway import ProviderGateway, ProviderResult
from packages.ai.gateway.provider_gateway import _deterministic_embedding
from packages.ai.prompts import PromptRegistry
from apps.api.services.clip_embeddings import _upsert_record
from packages.core.contracts import (
    AnnotationEditorVm,
    AnnotationMetaV4,
    AnnotationV4,
    Artifact,
    ArtifactKind,
    ClipRetrievalV4,
    ClipSemanticsV4,
    ClipUsageV4,
    ClipV4,
    DigitalHumanVideoRequest,
    ErrorCode,
    MediaAssetRecord,
    NodeRun,
    NodeStatus,
    ProviderError,
    ProviderInvocation,
    ProviderStatus,
    RunStatus,
    UsageRole,
    UsageWindowV4,
    WarningCode,
    WorkflowRun,
)
from packages.core.contracts.artifacts import ClipEmbeddingRecord
from packages.core.storage.database import MediaAssetRow
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.planning.material import build_clip_embedding_record
from packages.production.pipeline import nodes
from packages.production.pipeline._editing_agent import (
    BrollChoice,
    EditingSelection,
    index_candidates,
    validate_selection,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
from packages.production.sqlalchemy_repository import SqlAlchemyProductionRepository


def _adapter(tmp_path) -> LocalRuntimeAdapter:
    repository = Repository()
    object_store = LocalObjectStore(root=tmp_path)
    return LocalRuntimeAdapter(
        repository,
        provider_gateway=ProviderGateway(repository, object_store=object_store),
        prompt_registry=PromptRegistry(repository),
        seed_media=False,
    )


def _run(template_id: str = "digital_human_v2") -> WorkflowRun:
    return WorkflowRun(
        id="run_window_retrieval",
        job_id="job_window_retrieval",
        case_id="case_demo",
        workflow_template_id=template_id,
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run(node_id: str) -> NodeRun:
    return NodeRun(
        id=f"nr_{node_id}",
        run_id="run_window_retrieval",
        node_id=node_id,
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _artifact(kind: ArtifactKind, payload: dict) -> Artifact:
    return Artifact(
        id=f"art_{kind.value.replace('.', '_')}",
        case_id="case_demo",
        run_id="run_window_retrieval",
        node_run_id="nr_input",
        kind=kind,
        payload=payload,
        payload_schema=f"{kind.value}.v1",
    )


def _request() -> DigitalHumanVideoRequest:
    return DigitalHumanVideoRequest(
        case_id="case_demo",
        script="今天看施工前后变化。第一步先看施工前现场。",
        title="案例",
        voice={"voice_id": "voice_sandbox"},
        broll={"enabled": True, "max_inserts": 2},
    )


def _windows() -> dict:
    return {
        "fps": 30,
        "total_frames": 180,
        "portrait_windows": [
            {
                "window_id": "pwin_000",
                "start_frame": 0,
                "end_frame": 120,
                "unit_ids": ["unit_1"],
                "boundary_source": "semantic",
                "phase": "opening",
            }
        ],
        "broll_windows": [
            {
                "window_id": "bwin_000",
                "start_frame": 30,
                "end_frame": 90,
                "length_frames": 60,
                "host_unit_ids": ["unit_1"],
                "host_portrait_window_ids": ["pwin_000"],
                "text": "施工前现场",
                "boundary_source": "narration_unit",
            }
        ],
        "default_assignment": {},
        "compile_diagnostics": {},
    }


def _narration() -> dict:
    return {
        "units": [
            {
                "unit_id": "unit_1",
                "text": "今天看施工前后变化，第一步先看施工前现场。",
                "start": 0.0,
                "end": 4.0,
                "confidence": 0.9,
            }
        ]
    }


def _material() -> dict:
    return {
        "case_id": "case_demo",
        "portrait_candidates": [
            {
                "asset_id": "portrait_a",
                "score": 1.0,
                "reason": "eligible portrait clip",
                "metadata": {
                    "clip_id": "portrait_clip",
                    "source_start": 0.0,
                    "source_end": 8.0,
                    "source_frames_available": 240,
                },
            }
        ],
        "broll_candidates": [
            {
                "asset_id": "broll_a",
                "score": 1.0,
                "reason": "eligible b-roll clip",
                "metadata": {
                    "clip_id": "clip_a",
                    "source_start": 0.0,
                    "source_end": 4.0,
                    "source_frames_available": 120,
                    "matched_keywords": ["施工前"],
                    "scene_name": "施工前",
                    "diversity_key": "scene:a",
                },
            },
            {
                "asset_id": "broll_b",
                "score": 1.0,
                "reason": "eligible b-roll clip",
                "metadata": {
                    "clip_id": "clip_b",
                    "source_start": 0.0,
                    "source_end": 1.0,
                    "source_frames_available": 30,
                    "matched_keywords": ["现场"],
                    "scene_name": "现场",
                    "diversity_key": "scene:b",
                },
            },
        ],
        "font_candidates": [],
        "bgm_candidates": [],
    }


def _complete_default_portrait_windows() -> dict:
    windows = _windows()
    default_segment = {
        "segment_id": "portrait_1",
        "asset_id": "portrait_a",
        "clip_id": "portrait_clip",
        "start_sec": 0.0,
        "end_sec": 4.0,
        "source_start": 0.0,
        "source_end": 4.0,
        "role": "main",
        "source_mode": "lipsynced",
        "boundary_source": "semantic",
        "boundary_reason": None,
        "unit_ids": ["unit_1"],
        "slot_phase": "portrait_opening",
        "recently_used_material": False,
        "timeline_start_frame": 0,
        "timeline_end_frame": 120,
        "source_start_frame": 0,
        "source_end_frame": 120,
    }
    windows["default_assignment"] = {
        "portrait": [
            {
                "window_id": "portrait_a:portrait_clip",
                "segment_payload": default_segment,
            }
        ],
        "portrait_plan_payload": {
            "fps": 30,
            "total_duration": 4.0,
            "asset_id": "portrait_a",
            "duration_sec": 4.0,
            "segments": [default_segment],
            "diagnostics": {"planner": "timeline_window_default", "segment_count": 1},
        },
        "engine": "compiler_default",
    }
    return windows


def _annotate_broll(repository: Repository, *, asset_id: str = "broll_a") -> None:
    repository.media_assets[asset_id] = MediaAssetRecord(
        id=asset_id,
        case_id="case_demo",
        title=asset_id,
        kind="video",
        annotation_status="annotated",
        usable=True,
    )
    annotation = AnnotationV4(
        meta=AnnotationMetaV4(
            asset_id=asset_id,
            case_id="case_demo",
            material_type="broll",
            duration=4.0,
        ),
        clips=[
            ClipV4(
                segment_id="clip_a",
                start=0.0,
                end=4.0,
                duration=4.0,
                semantics=ClipSemanticsV4(scene_type="施工前", narrative_role="现场展示"),
                usage=ClipUsageV4(role=UsageRole.cover),
                retrieval=ClipRetrievalV4(
                    summary="施工前现场",
                    keywords=["施工前", "现场"],
                    retrieval_sentence="施工前现场",
                ),
            )
        ],
        usage_windows=[
            UsageWindowV4(start=0.0, end=4.0, role=UsageRole.cover, confidence=0.9)
        ],
        quality_report={"usable_ratio": 0.9},
    )
    repository.annotations[asset_id] = AnnotationEditorVm(
        asset=repository.media_assets[asset_id],
        etag="etag-broll",
        canonical=annotation.model_dump(mode="json"),
        projection={"usable": True},
    )


def _ctx(adapter: LocalRuntimeAdapter, node_id: str, artifacts: dict[ArtifactKind, Artifact]):
    return NodeContext(
        adapter=adapter,
        run=_run(),
        node_run=_node_run(node_id),
        state=RunState(request=_request(), artifacts=artifacts),
    )


def _seed_clip_embedding_record(db_session_factory, record) -> None:
    with db_session_factory() as session:
        session.merge(
            MediaAssetRow(
                id=record.asset_id,
                case_id=None,
                title=record.asset_id,
                kind="video",
                tags=[],
                annotation_status="annotated",
                usable=True,
            )
        )
        session.flush()
        _upsert_record(session, record)
        session.commit()


def test_window_query_planning_emits_only_window_id_and_intent(tmp_path):
    adapter = _adapter(tmp_path)
    ctx = _ctx(
        adapter,
        "WindowQueryPlanning",
        {
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
        },
    )

    output = nodes.window_query_planning.run(ctx)

    payload = output.artifacts[0].payload
    assert len(payload["window_queries"]) == 2
    assert all(set(item) == {"window_id", "retrieval_intent"} for item in payload["window_queries"])
    assert {item["window_id"] for item in payload["window_queries"]} == {
        "pwin_000",
        "bwin_000",
    }


def test_window_material_retrieval_uses_material_pack_pool_and_sql_hnsw_index(
    tmp_path,
    db_session_factory,
):
    adapter = _adapter(tmp_path)
    adapter.production_repository = SqlAlchemyProductionRepository(db_session_factory)
    material = _material()
    for asset_id in ("portrait_a", "broll_a", "broll_b"):
        adapter.repository.media_assets[asset_id] = MediaAssetRecord(
            id=asset_id,
            case_id="case_demo",
            title=asset_id,
            kind="video",
            annotation_status="annotated",
            usable=True,
        )
    query_ctx = _ctx(
        adapter,
        "WindowQueryPlanning",
        {
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
        },
    )
    query_output = nodes.window_query_planning.run(query_ctx)
    query_artifact = query_output.artifacts[0]
    broll_intent = next(
        item["retrieval_intent"]
        for item in query_artifact.payload["window_queries"]
        if item["window_id"] == "bwin_000"
    )
    indexed = index_candidates(material)
    broll_a = indexed.broll_by_id["bc_000"]
    record = build_clip_embedding_record(
        candidate=broll_a,
        asset=adapter.repository.media_assets["broll_a"],
        namespace="broll",
        provider_profile_id="sandbox.embedding.default",
        embedding=_deterministic_embedding(
            f"sandbox.embedding.default:multimodal.embedding:{broll_intent}",
            dimension=1024,
        ),
    )
    _seed_clip_embedding_record(db_session_factory, record)
    retrieval_ctx = _ctx(
        adapter,
        "WindowMaterialRetrieval",
        {
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack,
                material,
            ),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.plan_window_queries: query_artifact,
        },
    )

    output = nodes.window_material_retrieval.run(retrieval_ctx)

    payload = output.artifacts[0].payload
    assert output.provider_invocation_ids
    assert payload["candidates_by_window"]["bwin_000"][0]["candidate_id"] == "bc_000"
    assert "why_retrieved" not in payload["candidates_by_window"]["bwin_000"][0]
    trace = payload["candidates_by_window"]["bwin_000"][0]["retrieval_trace"]
    assert trace["source"] == "postgres_hnsw_clip_embedding_index"
    assert trace["embedding_model"] == "qwen3-vl-embedding"
    assert trace["embedding_dimension"] == 1024
    assert payload["diagnostics"]["retrieval_backend"] == "postgres_hnsw"
    assert payload["diagnostics"]["rejected_candidates"][0]["reason"] == "source_too_short"
    assert adapter.repository.clip_embedding_index == {}


def test_window_material_retrieval_status_ignores_normal_source_too_short_filtering():
    assert (
        nodes.window_material_retrieval._is_retrieval_degraded(
            diagnostics={
                "rejected_candidates": [],
                "missing_clip_embeddings": ["clipemb_missing"],
            },
            candidates_by_window={"bwin_000": [object()]},
        )
        is True
    )
    assert (
        nodes.window_material_retrieval._is_retrieval_degraded(
            diagnostics={
                "rejected_candidates": [{"reason": "source_too_short"}],
                "missing_clip_embeddings": [],
            },
            candidates_by_window={"pwin_000": [object()], "bwin_000": [object()]},
        )
        is False
    )
    assert (
        nodes.window_material_retrieval._is_retrieval_degraded(
            diagnostics={
                "rejected_candidates": [{"reason": "source_too_short"}],
                "missing_clip_embeddings": [],
            },
            candidates_by_window={"pwin_000": []},
        )
        is True
    )
    assert (
        nodes.window_material_retrieval._is_retrieval_degraded(
            diagnostics={
                "rejected_candidates": [{"reason": "query_embedding_failed"}],
                "missing_clip_embeddings": [],
            },
            candidates_by_window={"pwin_000": [object()]},
        )
        is True
    )


def test_window_material_retrieval_query_embedding_validation_edges():
    valid = nodes.window_material_retrieval._valid_query_embedding(
        {
            "dimension": "1024",
            "embedding": [2.0, *([0.0] * 1023)],
        }
    )
    assert valid is not None
    assert valid[0] == 1.0
    bad_outputs = [
        None,
        {"model": "other", "dimension": 1024, "embedding": [1.0, *([0.0] * 1023)]},
        {"dimension": True, "embedding": [1.0, *([0.0] * 1023)]},
        {"dimension": "1024x", "embedding": [1.0, *([0.0] * 1023)]},
        {"dimension": 1024, "normalization": "raw", "embedding": [1.0, *([0.0] * 1023)]},
        {"dimension": 1024, "index_version": "old", "embedding": [1.0, *([0.0] * 1023)]},
        {"dimension": 1024, "embedding": "not-a-list"},
        {"dimension": 1024, "embedding": ["oops", *([0.0] * 1023)]},
        {"dimension": 1024, "embedding": [1.0, 0.0]},
        {"dimension": 1024, "embedding": [0.0] * 1024},
    ]
    assert all(nodes.window_material_retrieval._valid_query_embedding(item) is None for item in bad_outputs)
    assert nodes.window_material_retrieval._embedding_output_metadata("bad") == {"type": "str"}
    assert nodes.window_material_retrieval._source_frames_available(
        {"metadata": {}},
        namespace="portrait",
    ) == 0


def test_window_material_retrieval_degrades_when_provider_profile_missing(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(adapter.provider_profiles, "first_available", lambda *_args, **_kwargs: None)
    ctx = _ctx(
        adapter,
        "WindowMaterialRetrieval",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(ArtifactKind.plan_timeline_windows, _windows()),
            ArtifactKind.plan_window_queries: _artifact(
                ArtifactKind.plan_window_queries,
                {"window_queries": [{"window_id": "bwin_000", "retrieval_intent": "施工前现场"}]},
            ),
        },
    )

    output = nodes.window_material_retrieval.run(ctx)

    assert output.status == NodeStatus.degraded
    payload = output.artifacts[0].payload
    assert payload["diagnostics"]["query_embedding_provider_missing"] is True
    assert payload["candidates_by_window"] == {}


def test_window_material_retrieval_degrades_missing_queries_without_provider_call(tmp_path):
    adapter = _adapter(tmp_path)
    ctx = _ctx(
        adapter,
        "WindowMaterialRetrieval",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(ArtifactKind.plan_timeline_windows, _windows()),
            ArtifactKind.plan_window_queries: _artifact(
                ArtifactKind.plan_window_queries,
                {"window_queries": []},
            ),
        },
    )

    output = nodes.window_material_retrieval.run(ctx)

    assert output.status == NodeStatus.degraded
    payload = output.artifacts[0].payload
    assert payload["candidates_by_window"] == {"pwin_000": [], "bwin_000": []}
    assert {item["reason"] for item in payload["diagnostics"]["rejected_candidates"]} == {
        "missing_window_query"
    }


def _failed_provider_invocation(message: str = "timeout") -> ProviderInvocation:
    return ProviderInvocation(
        id="pinv_failed",
        provider_id="sandbox",
        model_id="qwen3-vl-embedding",
        provider_profile_id="sandbox.embedding.default",
        capability_id="multimodal.embedding",
        status=ProviderStatus.failed,
        error=ProviderError(code=ErrorCode.provider_timeout, message=message),
    )


def test_window_material_retrieval_degrades_provider_error_and_bad_output(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    ctx = _ctx(
        adapter,
        "WindowMaterialRetrieval",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(ArtifactKind.plan_timeline_windows, _windows()),
            ArtifactKind.plan_window_queries: _artifact(
                ArtifactKind.plan_window_queries,
                {
                    "window_queries": [
                        {"window_id": "pwin_000", "retrieval_intent": "口播主轨"},
                        {"window_id": "bwin_000", "retrieval_intent": "施工前现场"},
                    ]
                },
            ),
        },
    )
    calls = {"count": 0}

    def fake_invoke(_call):
        calls["count"] += 1
        if calls["count"] == 1:
            return _failed_provider_invocation(), None
        return (
            ProviderInvocation(
                id="pinv_bad_output",
                provider_id="sandbox",
                model_id="qwen3-vl-embedding",
                provider_profile_id="sandbox.embedding.default",
                capability_id="multimodal.embedding",
                status=ProviderStatus.succeeded,
            ),
            ProviderResult(output={"dimension": 1024, "embedding": "not-a-vector"}),
        )

    monkeypatch.setattr(adapter.provider_gateway, "invoke", fake_invoke)

    output = nodes.window_material_retrieval.run(ctx)

    assert output.status == NodeStatus.degraded
    payload = output.artifacts[0].payload
    assert [item["reason"] for item in payload["diagnostics"]["rejected_candidates"]] == [
        "query_embedding_failed",
        "query_embedding_incompatible",
    ]
    assert output.provider_invocation_ids == ["pinv_failed", "pinv_bad_output"]


def test_window_material_retrieval_sql_results_ignore_stale_embedding_keys():
    known = nodes.window_material_retrieval._RetrievalCandidate(
        candidate_id="bc_known",
        candidate={
            "asset_id": "broll_a",
            "metadata": {"recency_penalty": 2.0, "recent_usage": {"recency_penalty": 3.0}},
        },
        clip_embedding_key="clipemb_known",
        source_frames=90,
        index=4,
    )
    known_record = ClipEmbeddingRecord(
        clip_embedding_key="clipemb_known",
        asset_id="broll_a",
        asset_revision="asset:broll_a:v1:v1:test",
        clip_id="clip_a",
        source_start=0.0,
        source_end=3.0,
        source_frames_available=90,
        index_namespace="broll",
        embedding_input_ref="s3://bucket/clip.mp4",
        embedding_id="emb_known",
        embedding=[1.0, *([0.0] * 1023)],
        provider_profile_id="sandbox.embedding.default",
    )
    stale_record = known_record.model_copy(
        update={
            "clip_embedding_key": "clipemb_stale",
            "embedding_id": "emb_stale",
        }
    )

    class FakeRepository:
        def nearest_clip_embeddings(self, **kwargs):
            assert kwargs["limit"] == 1
            return [(stale_record, 0.01), (known_record, 0.2)]

    ranked = nodes.window_material_retrieval._retrieve_for_window_from_sql(
        production_repository=FakeRepository(),
        namespace="broll",
        eligible=[known],
        query_embedding=[1.0, *([0.0] * 1023)],
        provider_profile_id="sandbox.embedding.default",
        required_frames=60,
    )

    assert [candidate.candidate_id for candidate in ranked] == ["bc_known"]
    assert ranked[0].semantic_similarity == 0.8
    assert ranked[0].recency_adjustment == -0.3


def test_window_material_retrieval_requires_sql_hnsw_repository(tmp_path):
    adapter = _adapter(tmp_path)
    material = _material()
    for asset_id in ("portrait_a", "broll_a", "broll_b"):
        adapter.repository.media_assets[asset_id] = MediaAssetRecord(
            id=asset_id,
            case_id="case_demo",
            title=asset_id,
            kind="video",
            annotation_status="annotated",
            usable=True,
        )
    query_artifact = _artifact(
        ArtifactKind.plan_window_queries,
        {
            "window_queries": [
                {"window_id": "pwin_000", "retrieval_intent": "口播主轨"},
                {"window_id": "bwin_000", "retrieval_intent": "施工前现场"},
            ]
        },
    )
    retrieval_ctx = _ctx(
        adapter,
        "WindowMaterialRetrieval",
        {
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack,
                material,
            ),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.plan_window_queries: query_artifact,
        },
    )

    with pytest.raises(NodeExecutionError) as exc:
        nodes.window_material_retrieval.run(retrieval_ctx)

    assert exc.value.error.code == ErrorCode.validation_invalid_options
    assert exc.value.error.details["required_backend"] == "postgres_hnsw"


def test_deterministic_editing_planning_consumes_window_retrieval_topk(tmp_path):
    adapter = _adapter(tmp_path)
    material = _material()
    material["broll_candidates"][1]["score"] = 2.0
    material["broll_candidates"][1]["metadata"]["source_end"] = 4.0
    material["broll_candidates"][1]["metadata"]["source_frames_available"] = 120
    retrieval = {
        "candidates_by_window": {
            "pwin_000": [
                {
                    "candidate_id": "pc_000",
                    "retrieval_score": 1.0,
                    "source_frames_available": 240,
                    "required_frames": 120,
                }
            ],
            "bwin_000": [
                {
                    "candidate_id": "bc_000",
                    "retrieval_score": 0.99,
                    "source_frames_available": 120,
                    "required_frames": 60,
                }
            ]
        },
        "diagnostics": {},
    }
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, material),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.plan_window_material_retrieval: _artifact(
                ArtifactKind.plan_window_material_retrieval,
                retrieval,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.creative_intent: _artifact(ArtifactKind.creative_intent, {"intent": {}}),
        },
    )

    output = nodes.deterministic_editing_planning.run(ctx)

    portrait_payload = next(
        artifact.payload for artifact in output.artifacts if artifact.kind == ArtifactKind.plan_portrait
    )
    broll_payload = next(
        artifact.payload for artifact in output.artifacts if artifact.kind == ArtifactKind.plan_broll
    )
    assert portrait_payload["segments"][0]["asset_id"] == "portrait_a"
    assert broll_payload["overlays"][0]["window_id"] == "bwin_000"
    assert broll_payload["overlays"][0]["asset_id"] == "broll_a"
    assert broll_payload["overlays"][0]["timeline_start_frame"] == 30
    assert broll_payload["overlays"][0]["timeline_end_frame"] == 90


def test_deterministic_editing_planning_rejects_incomplete_default_portrait_fallback(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    retrieval = {"candidates_by_window": {}, "diagnostics": {}}
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.plan_window_material_retrieval: _artifact(
                ArtifactKind.plan_window_material_retrieval,
                retrieval,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.creative_intent: _artifact(ArtifactKind.creative_intent, {"intent": {}}),
        },
    )

    with pytest.raises(NodeExecutionError) as exc:
        nodes.deterministic_editing_planning.run(ctx)

    assert exc.value.error.code == ErrorCode.material_insufficient_portrait
    assert exc.value.error.details["missing_assignment_window_ids"] == ["pwin_000"]
    assert exc.value.error.details["materialized_portrait_segment_count"] == 0


def test_deterministic_editing_planning_accepts_complete_default_portrait_fallback(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    windows = _complete_default_portrait_windows()
    retrieval = {"candidates_by_window": {}, "diagnostics": {}}
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                windows,
            ),
            ArtifactKind.plan_window_material_retrieval: _artifact(
                ArtifactKind.plan_window_material_retrieval,
                retrieval,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.creative_intent: _artifact(ArtifactKind.creative_intent, {"intent": {}}),
        },
    )

    output = nodes.deterministic_editing_planning.run(ctx)

    portrait_payload = next(
        artifact.payload for artifact in output.artifacts if artifact.kind == ArtifactKind.plan_portrait
    )
    media_assignment = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_media_assignment
    )
    assert portrait_payload["segments"] == windows["default_assignment"]["portrait_plan_payload"][
        "segments"
    ]
    assert media_assignment["portrait"][0]["window_id"] == "pwin_000"
    assert media_assignment["diagnostics"]["portrait_assignment_source"] == (
        "timeline_window_default"
    )
    assert media_assignment["diagnostics"]["missing_retrieval_window_ids"] == ["pwin_000"]


def test_deterministic_editing_planning_falls_back_to_annotation_broll_when_retrieval_empty(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    _annotate_broll(adapter.repository)
    retrieval = {"candidates_by_window": {}, "diagnostics": {"missing_clip_embeddings": ["broll_a"]}}
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _complete_default_portrait_windows(),
            ),
            ArtifactKind.plan_window_material_retrieval: _artifact(
                ArtifactKind.plan_window_material_retrieval,
                retrieval,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.creative_intent: _artifact(ArtifactKind.creative_intent, {"intent": {}}),
        },
    )

    output = nodes.deterministic_editing_planning.run(ctx)

    broll_payload = next(
        artifact.payload for artifact in output.artifacts if artifact.kind == ArtifactKind.plan_broll
    )
    media_assignment = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_media_assignment
    )
    assert WarningCode.broll_skipped_no_material not in output.warnings
    assert broll_payload["overlays"][0]["asset_id"] == "broll_a"
    assert broll_payload["overlays"][0]["window_id"] == "bwin_000"
    assert broll_payload["overlays"][0]["matched_keywords"]
    assert media_assignment["diagnostics"]["broll_assignment_source"] == (
        "annotation_ranked_fallback"
    )
    assert media_assignment["diagnostics"]["missing_retrieval_broll"] is True


def test_deterministic_editing_planning_does_not_fallback_when_broll_topk_is_stale(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    _annotate_broll(adapter.repository)
    retrieval = {
        "candidates_by_window": {
            "bwin_000": [{"candidate_id": "bc_404", "retrieval_score": 0.9}]
        },
        "diagnostics": {},
    }
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _complete_default_portrait_windows(),
            ),
            ArtifactKind.plan_window_material_retrieval: _artifact(
                ArtifactKind.plan_window_material_retrieval,
                retrieval,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.creative_intent: _artifact(ArtifactKind.creative_intent, {"intent": {}}),
        },
    )

    output = nodes.deterministic_editing_planning.run(ctx)

    broll_payload = next(
        artifact.payload for artifact in output.artifacts if artifact.kind == ArtifactKind.plan_broll
    )
    media_assignment = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_media_assignment
    )
    assert WarningCode.broll_skipped_no_material in output.warnings
    assert broll_payload["overlays"] == []
    assert broll_payload["skipped_reason"] == WarningCode.broll_skipped_no_material.value
    assert media_assignment["broll"] == []
    assert "broll_assignment_source" not in media_assignment["diagnostics"]
    assert "missing_retrieval_broll" not in media_assignment["diagnostics"]


def test_agent_validator_rejects_broll_choice_outside_window_topk():
    candidates = index_candidates(_material())
    selection = EditingSelection(
        broll=[BrollChoice(slot_id="bwin_000", candidate_id="bc_001")]
    )
    errors = validate_selection(
        selection,
        boundary={
            "portrait_slots": [],
            "broll_slots": [
                {
                    "slot_id": "bwin_000",
                    "start_frame": 30,
                    "end_frame": 90,
                }
            ],
        },
        candidates=candidates,
        bgm_enabled=False,
        retrieval_topk_by_window={"bwin_000": ["bc_000"]},
    )

    assert any("retrieval_topk_candidate_ids" in error for error in errors)


def test_editing_agent_fallback_fails_when_retrieval_topk_cannot_cover_portrait_slots(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    windows = _windows()
    windows["portrait_windows"].append(
        {
            "window_id": "pwin_001",
            "start_frame": 120,
            "end_frame": 180,
            "unit_ids": ["unit_1"],
            "boundary_source": "semantic",
            "phase": "main",
        }
    )
    retrieval = {
        "candidates_by_window": {
            "pwin_000": [
                {
                    "candidate_id": "pc_000",
                    "retrieval_score": 1.0,
                    "source_frames_available": 240,
                    "required_frames": 120,
                }
            ],
            "pwin_001": [
                {
                    "candidate_id": "pc_000",
                    "retrieval_score": 1.0,
                    "source_frames_available": 240,
                    "required_frames": 60,
                }
            ],
        }
    }
    ctx = _ctx(
        adapter,
        "EditingAgentPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_narration_boundary: _artifact(
                ArtifactKind.plan_narration_boundary,
                {"fps": 30, "total_frames": 180, "safe_cut_boundaries": []},
            ),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                windows,
            ),
            ArtifactKind.plan_window_material_retrieval: _artifact(
                ArtifactKind.plan_window_material_retrieval,
                retrieval,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.creative_intent: _artifact(ArtifactKind.creative_intent, {"intent": {}}),
        },
    )

    with pytest.raises(NodeExecutionError) as exc:
        nodes.editing_agent_planning.run(ctx)

    assert exc.value.error.code == ErrorCode.material_insufficient_portrait
    assert any(
        "portrait slots not covered: pwin_001" in error
        for error in exc.value.error.details["errors"]
    )
