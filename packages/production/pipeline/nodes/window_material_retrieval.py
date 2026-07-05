"""WindowMaterialRetrieval: retrieve per-window clip topK from the offline index."""

from __future__ import annotations

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
    cosine_similarity,
    longest_clean_portrait_source_span,
    normalize_vector,
)
from packages.production.pipeline._editing_agent import index_candidates
from packages.production.pipeline._node_context import NodeContext

_TOP_K = 12


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
        "source": "offline_clip_embedding_index",
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
            provider_profile_id=profile.id,
            diagnostics=diagnostics,
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
    provider_profile_id: str,
    diagnostics: dict[str, Any],
) -> list[RetrievedWindowCandidate]:
    window_id = str(window.get("window_id") or "")
    required_frames = _required_frames(window)
    ranked: list[RetrievedWindowCandidate] = []
    for index, (candidate_id, candidate) in enumerate(candidate_pool.items()):
        source_frames = _source_frames_available(candidate, namespace=namespace)
        if source_frames < required_frames:
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
        record = ctx.repository.clip_embedding_index.get(key)
        if record is None:
            diagnostics["missing_clip_embeddings"].append(
                {
                    "window_id": window_id,
                    "candidate_id": candidate_id,
                    "clip_embedding_key": key,
                    "reason": "missing_clip_embedding",
                }
            )
            continue
        incompatibility = _record_incompatibility(
            record,
            namespace=namespace,
            provider_profile_id=provider_profile_id,
        )
        if incompatibility:
            diagnostics["rejected_candidates"].append(
                {
                    "window_id": window_id,
                    "candidate_id": candidate_id,
                    "clip_embedding_key": key,
                    "reason": incompatibility,
                }
            )
            continue
        semantic_similarity = cosine_similarity(query_embedding, record.embedding)
        recency_adjustment = _recency_adjustment(candidate)
        deterministic_tiebreaker = -float(index) / 1_000_000.0
        retrieval_score = semantic_similarity + recency_adjustment + deterministic_tiebreaker
        ranked.append(
            RetrievedWindowCandidate(
                candidate_id=candidate_id,
                clip_embedding_key=record.clip_embedding_key,
                asset_id=record.asset_id,
                clip_id=record.clip_id,
                source_start=record.source_start,
                source_end=record.source_end,
                source_frames_available=source_frames,
                required_frames=required_frames,
                semantic_similarity=round(semantic_similarity, 6),
                recency_adjustment=round(recency_adjustment, 6),
                deterministic_tiebreaker=round(deterministic_tiebreaker, 9),
                retrieval_score=round(retrieval_score, 6),
                retrieval_trace={
                    "source": "offline_clip_embedding_index",
                    "provider_profile_id": record.provider_profile_id,
                    "embedding_model": record.embedding_model,
                    "embedding_dimension": record.embedding_dimension,
                    "normalization": record.normalization,
                    "instruct": record.instruct,
                    "index_version": record.index_version,
                    "sample_policy": record.sample_policy,
                },
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


def _record_incompatibility(
    record: ClipEmbeddingRecord,
    *,
    namespace: str,
    provider_profile_id: str,
) -> str:
    if record.index_namespace != namespace:
        return "embedding_namespace_mismatch"
    if record.provider_profile_id != provider_profile_id:
        return "embedding_provider_profile_mismatch"
    if record.embedding_model != CLIP_EMBEDDING_MODEL:
        return "embedding_model_mismatch"
    if record.embedding_dimension != CLIP_EMBEDDING_DIMENSION:
        return "embedding_dimension_mismatch"
    if record.normalization != CLIP_EMBEDDING_NORMALIZATION:
        return "embedding_normalization_mismatch"
    if record.index_version != CLIP_INDEX_VERSION:
        return "embedding_index_version_mismatch"
    if len(record.embedding) != CLIP_EMBEDDING_DIMENSION:
        return "embedding_vector_dimension_mismatch"
    return ""


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
    degraded = bool(
        diagnostics.get("query_embedding_provider_missing")
        or diagnostics.get("missing_clip_embeddings")
        or diagnostics.get("rejected_candidates")
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
