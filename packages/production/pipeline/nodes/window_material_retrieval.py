"""WindowMaterialRetrieval: retrieve per-window clip topK from the offline index."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.ai.gateway import ProviderCall
from packages.core.config.settings import sandbox_fallback_allowed
from packages.core.contracts import ArtifactKind, ErrorCode, NodeStatus
from packages.core.contracts.artifacts import (
    ClipEmbeddingRecord,
    RetrievedWindowCandidate,
    WindowMaterialRetrievalArtifact,
)
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.planning.editing.frame_grid import frame_index
from packages.planning.material import (
    CLIP_EMBEDDING_DIMENSION,
    CLIP_EMBEDDING_MODEL,
    CLIP_EMBEDDING_NORMALIZATION,
    CLIP_INDEX_VERSION,
    candidate_clip_embedding_key,
    extract_keywords,
    longest_clean_portrait_source_span,
    normalize_vector,
)
from packages.production.pipeline._editing_agent import index_candidates
from packages.production.pipeline._node_context import NodeContext

_TOP_K = 12
_SQL_RECALL_MULTIPLIER = 10
_SQL_RECALL_MIN_EXTRA = 50
_KEYWORD_FUSION_WEIGHT = 0.15


@dataclass(frozen=True)
class _RetrievalCandidate:
    candidate_id: str
    candidate: dict
    clip_embedding_key: str
    source_frames: int
    index: int


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    windows = state.require(ArtifactKind.plan_timeline_windows).payload or {}
    material = state.require(ArtifactKind.plan_material_pack).payload or {}
    queries = state.require(ArtifactKind.plan_window_queries).payload or {}
    query_by_window = {
        str(item.get("window_id") or ""): str(item.get("retrieval_intent") or "")
        for item in (queries.get("window_queries") or [])
        if isinstance(item, dict)
    }
    indexed = index_candidates(material)
    allow_sandbox_fallback = sandbox_fallback_allowed()
    profile = ctx.first_available_provider_profile(
        "multimodal.embedding",
        include_sandbox=allow_sandbox_fallback,
    )
    diagnostics: dict[str, Any] = {
        "source": "postgres_hnsw_clip_embedding_index",
        "top_k": _TOP_K,
        "provider_profile_id": getattr(profile, "id", None),
        "embedding_model": CLIP_EMBEDDING_MODEL,
        "embedding_dimension": CLIP_EMBEDDING_DIMENSION,
        "normalization": CLIP_EMBEDDING_NORMALIZATION,
        "index_version": CLIP_INDEX_VERSION,
        "rejected_candidates": [],
        "missing_clip_embeddings": [],
        "window_types": {},
    }
    full_coverage_broll = _full_coverage_broll_windows(windows)
    if full_coverage_broll:
        diagnostics["full_coverage_single_clip_required"] = True
    provider_invocation_ids: list[str] = []
    candidates_by_window: dict[str, list[RetrievedWindowCandidate]] = {}

    if profile is None:
        if not allow_sandbox_fallback:
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                "未配置可用的真实多模态 embedding 供应商（multimodal.embedding）。"
                "请在「设置」中配置并启用 qwen3-vl-embedding 供应商及密钥。",
            )
        diagnostics["query_embedding_provider_missing"] = True
        return _output(ctx, diagnostics=diagnostics, candidates_by_window=candidates_by_window)

    window_specs = [
        *(
            ("portrait", window)
            for window in (windows.get("portrait_windows") or [])
            if isinstance(window, dict)
        ),
        *(
            ("broll", window)
            for window in (windows.get("broll_windows") or [])
            if isinstance(window, dict)
        ),
    ]
    for namespace, window in window_specs:
        window_id = str(window.get("window_id") or "")
        if not window_id:
            continue
        diagnostics["window_types"][window_id] = namespace
        retrieval_intent = query_by_window.get(window_id, "")
        if not retrieval_intent:
            diagnostics["rejected_candidates"].append(
                {"window_id": window_id, "reason": "missing_window_query"}
            )
            candidates_by_window[window_id] = []
            continue
        query_keywords = extract_keywords(retrieval_intent)
        invocation, result = ctx.provider_gateway.invoke(
            ProviderCall(
                case_id=ctx.run.case_id,
                run_id=ctx.run.id,
                node_run_id=ctx.node_run.id,
                provider_profile_id=profile.id,
                capability_id="multimodal.embedding",
                input={
                    "retrieval_intent": retrieval_intent,
                    "text": retrieval_intent,
                    "model": CLIP_EMBEDDING_MODEL,
                    "dimension": CLIP_EMBEDDING_DIMENSION,
                    "normalization": CLIP_EMBEDDING_NORMALIZATION,
                    "index_version": CLIP_INDEX_VERSION,
                },
                idempotency_key=f"{ctx.run.id}:{ctx.node_run.id}:{window_id}:query_embedding",
            )
        )
        provider_invocation_ids.append(invocation.id)
        if result is None or invocation.error:
            diagnostics["rejected_candidates"].append(
                {
                    "window_id": window_id,
                    "reason": "query_embedding_failed",
                    "provider_invocation_id": invocation.id,
                    "error": invocation.error.model_dump(mode="json") if invocation.error else None,
                }
            )
            candidates_by_window[window_id] = []
            continue
        query_embedding = _valid_query_embedding(result.output)
        if query_embedding is None:
            diagnostics["rejected_candidates"].append(
                {
                    "window_id": window_id,
                    "reason": "query_embedding_incompatible",
                    "provider_invocation_id": invocation.id,
                    "output": _embedding_output_metadata(result.output),
                }
            )
            candidates_by_window[window_id] = []
            continue

        candidate_pool = (
            indexed.portrait_by_id if namespace == "portrait" else indexed.broll_by_id
        )
        candidates_by_window[window_id] = _retrieve_for_window(
            ctx=ctx,
            namespace=namespace,
            window=window,
            candidate_pool=candidate_pool,
            query_embedding=query_embedding,
            query_keywords=query_keywords,
            provider_profile_id=profile.id,
            diagnostics=diagnostics,
            record_full_coverage_capacity=full_coverage_broll and namespace == "broll",
        )

    return _output(
        ctx,
        diagnostics=diagnostics,
        candidates_by_window=candidates_by_window,
        provider_invocation_ids=provider_invocation_ids,
    )


def _retrieve_for_window(
    *,
    ctx: NodeContext,
    namespace: str,
    window: dict,
    candidate_pool: dict[str, dict],
    query_embedding: list[float],
    query_keywords: list[str],
    provider_profile_id: str,
    diagnostics: dict[str, Any],
    allow_partial_source: bool = False,
    record_full_coverage_capacity: bool = False,
) -> list[RetrievedWindowCandidate]:
    window_id = str(window.get("window_id") or "")
    required_frames = _required_frames(window)
    eligible = _eligible_candidates(
        ctx=ctx,
        namespace=namespace,
        window_id=window_id,
        required_frames=required_frames,
        candidate_pool=candidate_pool,
        diagnostics=diagnostics,
        allow_partial_source=allow_partial_source,
    )
    if record_full_coverage_capacity:
        _record_full_coverage_window_capacity(
            diagnostics=diagnostics,
            window_id=window_id,
            required_frames=required_frames,
            eligible=eligible,
        )
    if not eligible:
        return []
    production_repository = ctx.production_repository
    if production_repository is None or not callable(
        getattr(production_repository, "nearest_clip_embeddings", None)
    ):
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "WindowMaterialRetrieval requires a SQL-backed clip embedding repository "
            "with pgvector HNSW nearest-neighbor search.",
            details={"required_backend": "postgres_hnsw"},
        )
    diagnostics["retrieval_backend"] = "postgres_hnsw"
    return _retrieve_for_window_from_sql(
        production_repository=production_repository,
        namespace=namespace,
        eligible=eligible,
        query_embedding=query_embedding,
        query_keywords=query_keywords,
        provider_profile_id=provider_profile_id,
        required_frames=required_frames,
        min_source_frames=1 if allow_partial_source else required_frames,
    )


def _eligible_candidates(
    *,
    ctx: NodeContext,
    namespace: str,
    window_id: str,
    required_frames: int,
    candidate_pool: dict[str, dict],
    diagnostics: dict[str, Any],
    allow_partial_source: bool = False,
) -> list[_RetrievalCandidate]:
    eligible: list[_RetrievalCandidate] = []
    seen_keys: set[str] = set()
    for index, (candidate_id, candidate) in enumerate(candidate_pool.items()):
        source_frames = _source_frames_available(candidate, namespace=namespace)
        if source_frames <= 0:
            diagnostics["rejected_candidates"].append(
                {
                    "window_id": window_id,
                    "candidate_id": candidate_id,
                    "reason": "source_empty",
                    "source_frames_available": source_frames,
                    "required_frames": required_frames,
                }
            )
            continue
        if not allow_partial_source and source_frames < required_frames:
            diagnostics["rejected_candidates"].append(
                {
                    "window_id": window_id,
                    "candidate_id": candidate_id,
                    "reason": "source_too_short",
                    "source_frames_available": source_frames,
                    "required_frames": required_frames,
                }
            )
            continue
        asset = ctx.repository.media_assets.get(str(candidate.get("asset_id") or ""))
        key = candidate_clip_embedding_key(
            candidate=candidate,
            asset=asset,
            namespace=namespace,
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        eligible.append(
            _RetrievalCandidate(
                candidate_id=candidate_id,
                candidate=candidate,
                clip_embedding_key=key,
                source_frames=source_frames,
                index=index,
            )
        )
    return eligible


def _retrieve_for_window_from_sql(
    *,
    production_repository: Any,
    namespace: str,
    eligible: list[_RetrievalCandidate],
    query_embedding: list[float],
    query_keywords: list[str],
    provider_profile_id: str,
    required_frames: int,
    min_source_frames: int | None = None,
) -> list[RetrievedWindowCandidate]:
    candidate_by_key = {item.clip_embedding_key: item for item in eligible}
    recall_limit = min(
        len(eligible),
        max(_TOP_K * _SQL_RECALL_MULTIPLIER, _TOP_K + _SQL_RECALL_MIN_EXTRA),
    )
    nearest = production_repository.nearest_clip_embeddings(
        clip_embedding_keys=[item.clip_embedding_key for item in eligible],
        query_embedding=query_embedding,
        namespace=namespace,
        provider_profile_id=provider_profile_id,
        embedding_model=CLIP_EMBEDDING_MODEL,
        embedding_dimension=CLIP_EMBEDDING_DIMENSION,
        normalization=CLIP_EMBEDDING_NORMALIZATION,
        index_version=CLIP_INDEX_VERSION,
        min_source_frames_available=max(
            1,
            int(min_source_frames if min_source_frames is not None else required_frames),
        ),
        limit=recall_limit,
    )
    ranked: list[RetrievedWindowCandidate] = []
    for record, distance in nearest:
        candidate = candidate_by_key.get(record.clip_embedding_key)
        if candidate is None:
            continue
        semantic_similarity = max(-1.0, min(1.0, 1.0 - float(distance)))
        ranked.append(
            _retrieved_candidate(
                item=candidate,
                record=record,
                semantic_similarity=semantic_similarity,
                query_keywords=query_keywords,
                required_frames=required_frames,
                source="postgres_hnsw_clip_embedding_index",
            )
        )
    ranked.sort(
        key=lambda candidate: (
            -candidate.retrieval_score,
            -candidate.semantic_similarity,
            candidate.candidate_id,
        )
    )
    return ranked[:_TOP_K]


def _retrieved_candidate(
    *,
    item: _RetrievalCandidate,
    record: ClipEmbeddingRecord,
    semantic_similarity: float,
    query_keywords: list[str],
    required_frames: int,
    source: str,
) -> RetrievedWindowCandidate:
    recency_adjustment = _recency_adjustment(item.candidate)
    keyword_adjustment, keyword_matched = _keyword_adjustment(
        item.candidate,
        query_keywords=query_keywords,
    )
    deterministic_tiebreaker = -float(item.index) / 1_000_000.0
    retrieval_score = (
        semantic_similarity
        + recency_adjustment
        + keyword_adjustment
        + deterministic_tiebreaker
    )
    return RetrievedWindowCandidate(
        candidate_id=item.candidate_id,
        clip_embedding_key=record.clip_embedding_key,
        asset_id=record.asset_id,
        clip_id=record.clip_id,
        source_start=record.source_start,
        source_end=record.source_end,
        source_frames_available=item.source_frames,
        required_frames=required_frames,
        semantic_similarity=round(semantic_similarity, 6),
        recency_adjustment=round(recency_adjustment, 6),
        deterministic_tiebreaker=round(deterministic_tiebreaker, 9),
        retrieval_score=round(retrieval_score, 6),
        retrieval_trace={
            "source": source,
            "provider_profile_id": record.provider_profile_id,
            "embedding_model": record.embedding_model,
            "embedding_dimension": record.embedding_dimension,
            "normalization": record.normalization,
            "instruct": record.instruct,
            "index_version": record.index_version,
            "sample_policy": record.sample_policy,
            "keyword_adjustment": round(keyword_adjustment, 6),
            "keyword_matched": keyword_matched,
        },
    )


def _valid_query_embedding(output: Any) -> list[float] | None:
    if not isinstance(output, dict):
        return None
    if str(output.get("model") or CLIP_EMBEDDING_MODEL) != CLIP_EMBEDDING_MODEL:
        return None
    if _embedding_dimension(output.get("dimension")) != CLIP_EMBEDDING_DIMENSION:
        return None
    if str(output.get("normalization") or CLIP_EMBEDDING_NORMALIZATION) != CLIP_EMBEDDING_NORMALIZATION:
        return None
    if str(output.get("index_version") or CLIP_INDEX_VERSION) != CLIP_INDEX_VERSION:
        return None
    embedding = output.get("embedding")
    if not isinstance(embedding, list):
        return None
    try:
        vector = [float(value) for value in embedding]
    except (TypeError, ValueError):
        return None
    if len(vector) != CLIP_EMBEDDING_DIMENSION:
        return None
    try:
        return normalize_vector(vector, dimension=CLIP_EMBEDDING_DIMENSION)
    except (TypeError, ValueError):
        return None


def _embedding_dimension(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _output(
    ctx: NodeContext,
    *,
    diagnostics: dict[str, Any],
    candidates_by_window: dict[str, list[RetrievedWindowCandidate]],
    provider_invocation_ids: list[str] | None = None,
) -> NodeOutput:
    payload = WindowMaterialRetrievalArtifact(
        candidates_by_window=candidates_by_window,
        diagnostics=diagnostics,
    )
    degraded = _is_retrieval_degraded(
        diagnostics=diagnostics,
        candidates_by_window=candidates_by_window,
    )
    return NodeOutput(
        status=NodeStatus.degraded if degraded else NodeStatus.succeeded,
        artifacts=[
            ctx.artifact(
                ArtifactKind.plan_window_material_retrieval,
                payload.model_dump(mode="json"),
                "WindowMaterialRetrievalArtifact.v1",
            )
        ],
        provider_invocation_ids=provider_invocation_ids or [],
    )


def _is_retrieval_degraded(
    *,
    diagnostics: dict[str, Any],
    candidates_by_window: dict[str, list[RetrievedWindowCandidate]],
) -> bool:
    if diagnostics.get("query_embedding_provider_missing") or diagnostics.get(
        "missing_clip_embeddings"
    ):
        return True
    if any(not candidates for candidates in candidates_by_window.values()):
        return True
    return any(
        item.get("reason") != "source_too_short"
        for item in diagnostics.get("rejected_candidates") or []
        if isinstance(item, dict)
    )


def _full_coverage_broll_windows(windows: dict) -> bool:
    contract = (
        (windows.get("geometry_policy") or {}).get("broll_window_contract")
        if isinstance(windows.get("geometry_policy"), dict)
        else {}
    )
    return (
        isinstance(contract, dict)
        and contract.get("semantics") == "authoritative_full_coverage_main_visual_track"
    )


def _record_full_coverage_window_capacity(
    *,
    diagnostics: dict[str, Any],
    window_id: str,
    required_frames: int,
    eligible: list[_RetrievalCandidate],
) -> None:
    source_frames = [item.source_frames for item in eligible]
    total_source_frames = sum(source_frames)
    diagnostics.setdefault("full_coverage_capacity_by_window", {})[window_id] = {
        "required_frames": required_frames,
        "eligible_candidate_count": len(eligible),
        "total_source_frames": total_source_frames,
        "longest_source_frames": max(source_frames or [0]),
        "sufficient_by_single": max(source_frames or [0]) >= required_frames,
        "sufficient_by_sum": total_source_frames >= required_frames,
    }


def _required_frames(window: dict) -> int:
    start = int(window.get("start_frame", 0) or 0)
    end = int(window.get("end_frame", 0) or 0)
    return max(0, int(window.get("length_frames") or end - start))


def _source_frames_available(candidate: dict, *, namespace: str) -> int:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    if namespace == "portrait":
        clean_span = longest_clean_portrait_source_span(metadata)
        if clean_span is None:
            return 0
        source_start, source_end = clean_span
        return max(0, frame_index(source_end) - frame_index(source_start))
    source_start = _as_float(metadata.get("source_start"))
    source_end = _as_float(metadata.get("source_end"))
    return max(0, frame_index(source_end) - frame_index(source_start))


def _recency_adjustment(candidate: dict) -> float:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    penalty = _as_float(metadata.get("recency_penalty"))
    recent_usage = metadata.get("recent_usage") if isinstance(metadata.get("recent_usage"), dict) else {}
    penalty = max(penalty, _as_float(recent_usage.get("recency_penalty")))
    return -0.1 * penalty


def _keyword_adjustment(candidate: dict, *, query_keywords: list[str]) -> tuple[float, list[str]]:
    query = [_clean_keyword(item) for item in query_keywords]
    query = [item for item in query if item]
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    raw_candidate_keywords = metadata.get("keywords") or metadata.get("matched_keywords") or []
    if isinstance(raw_candidate_keywords, list):
        candidate_keywords = {_clean_keyword(item) for item in raw_candidate_keywords}
    else:
        candidate_keywords = {_clean_keyword(raw_candidate_keywords)}
    candidate_keywords.discard("")
    matched = [keyword for keyword in query if keyword in candidate_keywords]
    keyword_score = len(matched) / max(len(query), 1)
    keyword_score = max(0.0, min(1.0, keyword_score))
    return _KEYWORD_FUSION_WEIGHT * keyword_score, matched


def _clean_keyword(value: Any) -> str:
    return str(value or "").strip()


def _embedding_output_metadata(output: Any) -> dict[str, Any]:
    if not isinstance(output, dict):
        return {"type": type(output).__name__}
    return {
        "model": output.get("model"),
        "dimension": output.get("dimension"),
        "normalization": output.get("normalization"),
        "index_version": output.get("index_version"),
    }


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
