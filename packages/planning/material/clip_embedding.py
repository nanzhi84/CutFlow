"""Offline clip-level embedding index helpers for window material retrieval.

Production nodes query this durable index; they do not bulk-embed the material
library during a run. The key includes the source span, asset revision, model,
dimension, index version, and sample policy so stale clip vectors cannot be
silently reused across incompatible indexing passes.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from packages.core.contracts import MediaAssetRecord
from packages.core.contracts.artifacts import ClipEmbeddingRecord
from packages.planning.editing.frame_grid import frame_index

CLIP_EMBEDDING_MODEL = "qwen3-vl-embedding"
CLIP_EMBEDDING_DIMENSION = 1024
CLIP_EMBEDDING_NORMALIZATION = "l2"
CLIP_EMBEDDING_INSTRUCT = "video_clip_retrieval_v1"
CLIP_INDEX_VERSION = "clip-video-qwen3-v3"
CLIP_SAMPLE_POLICY = {
    "policy_id": "deterministic-trim-or-frames-v1",
    "clip_scope": "source_span",
    "max_frames": 8,
    "frame_offsets": [0.08, 0.2, 0.36, 0.5, 0.64, 0.8, 0.92],
}


def sample_policy_hash(sample_policy: dict[str, Any] | None = None) -> str:
    payload = json.dumps(
        sample_policy or CLIP_SAMPLE_POLICY,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def asset_revision_token(asset: MediaAssetRecord | None) -> str:
    if asset is None:
        return "asset:unknown"
    source_artifact_id = getattr(asset, "source_artifact_id", None)
    if source_artifact_id:
        return (
            f"asset:{asset.id}:v{asset.version}:{asset.schema_version}:src:{source_artifact_id}"
        )
    updated = getattr(asset, "updated_at", None)
    updated_token = updated.isoformat() if updated is not None else ""
    return f"asset:{asset.id}:v{asset.version}:{asset.schema_version}:{updated_token}"


def clip_embedding_key(
    *,
    asset_id: str,
    clip_id: str,
    source_start: float,
    source_end: float,
    namespace: str,
    asset_revision: str,
    model: str = CLIP_EMBEDDING_MODEL,
    dimension: int = CLIP_EMBEDDING_DIMENSION,
    index_version: str = CLIP_INDEX_VERSION,
    sample_policy: dict[str, Any] | None = None,
) -> str:
    payload = {
        "asset_id": asset_id,
        "clip_id": clip_id,
        "source_start": round(float(source_start), 6),
        "source_end": round(float(source_end), 6),
        "namespace": namespace,
        "asset_revision": asset_revision,
        "model": model,
        "dimension": int(dimension),
        "index_version": index_version,
        "sample_policy_hash": sample_policy_hash(sample_policy),
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return f"clipemb_{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def candidate_source_span(candidate: dict) -> tuple[str, float, float]:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    clip_id = str(metadata.get("clip_id") or "").strip()
    if not clip_id:
        raise ValueError("clip_id is required for clip embedding index records")
    source_start = _required_finite_float(
        metadata.get("source_start"),
        field_name="source_start",
    )
    source_end = _required_finite_float(
        metadata.get("source_end"),
        field_name="source_end",
    )
    if source_end <= source_start:
        raise ValueError("source_end must be greater than source_start for clip embedding index records")
    return clip_id, source_start, source_end


def candidate_clip_embedding_key(
    *,
    candidate: dict,
    asset: MediaAssetRecord | None,
    namespace: str,
    model: str = CLIP_EMBEDDING_MODEL,
    dimension: int = CLIP_EMBEDDING_DIMENSION,
    index_version: str = CLIP_INDEX_VERSION,
    sample_policy: dict[str, Any] | None = None,
) -> str:
    clip_id, source_start, source_end = candidate_source_span(candidate)
    return clip_embedding_key(
        asset_id=str(candidate.get("asset_id") or ""),
        clip_id=clip_id,
        source_start=source_start,
        source_end=source_end,
        namespace=namespace,
        asset_revision=asset_revision_token(asset),
        model=model,
        dimension=dimension,
        index_version=index_version,
        sample_policy=sample_policy,
    )


def build_clip_embedding_record(
    *,
    candidate: dict,
    asset: MediaAssetRecord | None,
    namespace: str,
    provider_profile_id: str,
    embedding: list[float] | None = None,
    embedding_id: str | None = None,
    embedding_input_ref: str | None = None,
    model: str = CLIP_EMBEDDING_MODEL,
    dimension: int = CLIP_EMBEDDING_DIMENSION,
    index_version: str = CLIP_INDEX_VERSION,
    sample_policy: dict[str, Any] | None = None,
) -> ClipEmbeddingRecord:
    asset_id = str(candidate.get("asset_id") or "")
    clip_id, source_start, source_end = candidate_source_span(candidate)
    asset_revision = asset_revision_token(asset)
    key = clip_embedding_key(
        asset_id=asset_id,
        clip_id=clip_id,
        source_start=source_start,
        source_end=source_end,
        namespace=namespace,
        asset_revision=asset_revision,
        model=model,
        dimension=dimension,
        index_version=index_version,
        sample_policy=sample_policy,
    )
    vector = _validated_embedding_vector(embedding, dimension=dimension)
    return ClipEmbeddingRecord(
        clip_embedding_key=key,
        asset_id=asset_id,
        asset_revision=asset_revision,
        clip_id=clip_id,
        source_start=source_start,
        source_end=source_end,
        source_frames_available=max(0, frame_index(source_end) - frame_index(source_start)),
        index_namespace=namespace,  # type: ignore[arg-type]
        embedding_input_type="video_clip",
        embedding_input_ref=(
            embedding_input_ref or f"{asset_id}:{clip_id}:{source_start:.6f}:{source_end:.6f}"
        ),
        sample_policy=sample_policy or CLIP_SAMPLE_POLICY,
        embedding_id=embedding_id or key,
        embedding=normalize_vector(vector, dimension=dimension),
        provider_profile_id=provider_profile_id,
        embedding_model=model,
        embedding_dimension=dimension,
        normalization=CLIP_EMBEDDING_NORMALIZATION,
        instruct=CLIP_EMBEDDING_INSTRUCT,
        index_version=index_version,
    )


def deterministic_dense_embedding(seed: str, *, dimension: int = CLIP_EMBEDDING_DIMENSION) -> list[float]:
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).digest()
        for index in range(0, len(digest), 2):
            if len(values) >= dimension:
                break
            raw = int.from_bytes(digest[index : index + 2], "big")
            values.append((raw / 65535.0) * 2.0 - 1.0)
        counter += 1
    return normalize_vector(values, dimension=dimension)


def normalize_vector(values: list[float], *, dimension: int) -> list[float]:
    vector = [float(value) for value in values[:dimension]]
    if len(vector) < dimension:
        vector.extend([0.0] * (dimension - len(vector)))
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("vector must contain only finite values")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        raise ValueError("vector must have non-zero norm")
    return [value / norm for value in vector]


def _validated_embedding_vector(values: list[float] | None, *, dimension: int) -> list[float]:
    if values is None:
        raise ValueError("clip embedding is required")
    if len(values) != dimension:
        raise ValueError(f"clip embedding dimension mismatch: expected {dimension}, got {len(values)}")
    vector = [float(value) for value in values]
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("clip embedding must contain only finite values")
    if math.sqrt(sum(value * value for value in vector)) <= 0:
        raise ValueError("clip embedding must have non-zero norm")
    return vector


def _required_finite_float(value: Any, *, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a finite number for clip embedding index records") from None
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be a finite number for clip embedding index records")
    return result
