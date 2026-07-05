#!/usr/bin/env python3
"""Inspect pending clip embedding candidates used by WindowMaterialRetrieval.

The write path has moved to the API service because true qwen3-vl-embedding
requires cutting each source span into a video clip, uploading it to OSS, and
passing that clip URL to DashScope. This script stays as a dry-run inspector for
legacy material_pack artifacts.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.ai.gateway import (  # noqa: E402
    ProviderCall,
    ProviderGateway,
    SqlAlchemyProviderRuntimeRepository,
)
from packages.core.contracts.artifacts import ClipEmbeddingRecord  # noqa: E402
from packages.core.storage import Repository  # noqa: E402
from packages.core.storage.database import (  # noqa: E402
    AnnotationRow,
    ArtifactRow,
    ClipEmbeddingIndexRow,
    MediaAssetRow,
    create_database_engine,
    create_session_factory,
)
from packages.core.storage.secret_store import LocalSecretStore  # noqa: E402
from packages.core.storage.sqlalchemy_secrets import SqlAlchemySecretStore  # noqa: E402
from packages.media.sqlalchemy_repository import media_asset_row_to_contract  # noqa: E402
from packages.planning.material import (  # noqa: E402
    CLIP_EMBEDDING_DIMENSION,
    CLIP_EMBEDDING_MODEL,
    CLIP_EMBEDDING_NORMALIZATION,
    CLIP_INDEX_VERSION,
    build_clip_embedding_record,
    candidate_clip_embedding_key,
)

Namespace = Literal["portrait", "broll"]


@dataclass(frozen=True)
class ClipCandidate:
    namespace: Namespace
    candidate: dict[str, Any]
    key: str


def log(message: str) -> None:
    print(message, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-id", help="Workflow run containing a plan.material_pack artifact.")
    source.add_argument("--artifact-id", help="Specific plan.material_pack artifact id.")
    parser.add_argument(
        "--namespace",
        choices=["all", "portrait", "broll"],
        default="all",
        help="Limit indexing to one candidate namespace.",
    )
    parser.add_argument(
        "--profile-id",
        default="dashscope.multimodal_embedding.prod",
        help="Provider profile to call for multimodal.embedding.",
    )
    parser.add_argument("--limit", type=int, help="Maximum pending candidates to call.")
    parser.add_argument("--force", action="store_true", help="Rebuild existing index rows.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Disabled: use the API clip-embeddings/index endpoint for writes.",
    )
    return parser


def material_pack_artifact(session: Session, *, run_id: str | None, artifact_id: str | None) -> ArtifactRow:
    if artifact_id:
        row = session.get(ArtifactRow, artifact_id)
        if row is None:
            raise SystemExit(f"material pack artifact not found: {artifact_id}")
        return row
    statement = (
        select(ArtifactRow)
        .where(ArtifactRow.run_id == run_id)
        .where(ArtifactRow.kind == "plan.material_pack")
        .order_by(ArtifactRow.updated_at.desc())
        .limit(1)
    )
    row = session.scalars(statement).first()
    if row is None:
        raise SystemExit(f"plan.material_pack artifact not found for run: {run_id}")
    return row


def collect_candidates(
    payload: dict[str, Any],
    *,
    namespace_filter: str,
    assets: dict[str, MediaAssetRow],
) -> tuple[list[ClipCandidate], list[str]]:
    candidates: list[ClipCandidate] = []
    errors: list[str] = []
    namespaces: list[tuple[Namespace, str]] = [
        ("portrait", "portrait_candidates"),
        ("broll", "broll_candidates"),
    ]
    for namespace, key in namespaces:
        if namespace_filter not in {"all", namespace}:
            continue
        raw_candidates = payload.get(key)
        if not isinstance(raw_candidates, list):
            continue
        for index, candidate in enumerate(raw_candidates):
            if not isinstance(candidate, dict):
                errors.append(f"{key}[{index}] is not an object")
                continue
            asset_id = str(candidate.get("asset_id") or "")
            asset_row = assets.get(asset_id)
            asset = media_asset_row_to_contract(asset_row) if asset_row is not None else None
            try:
                embedding_key = candidate_clip_embedding_key(
                    candidate=candidate,
                    asset=asset,
                    namespace=namespace,
                )
            except ValueError as exc:
                errors.append(f"{key}[{index}] invalid span: {exc}")
                continue
            candidates.append(ClipCandidate(namespace=namespace, candidate=candidate, key=embedding_key))
    return dedupe_candidates(candidates), errors


def dedupe_candidates(candidates: list[ClipCandidate]) -> list[ClipCandidate]:
    seen: set[str] = set()
    deduped: list[ClipCandidate] = []
    for candidate in candidates:
        if candidate.key in seen:
            continue
        seen.add(candidate.key)
        deduped.append(candidate)
    return deduped


def load_asset_rows(session: Session, payload: dict[str, Any]) -> dict[str, MediaAssetRow]:
    asset_ids: set[str] = set()
    for key in ("portrait_candidates", "broll_candidates"):
        raw_candidates = payload.get(key)
        if not isinstance(raw_candidates, list):
            continue
        for candidate in raw_candidates:
            if isinstance(candidate, dict) and candidate.get("asset_id"):
                asset_ids.add(str(candidate["asset_id"]))
    if not asset_ids:
        return {}
    rows = session.scalars(select(MediaAssetRow).where(MediaAssetRow.id.in_(asset_ids))).all()
    return {row.id: row for row in rows}


def load_latest_annotations(session: Session, asset_ids: set[str]) -> dict[str, AnnotationRow]:
    if not asset_ids:
        return {}
    statement = (
        select(AnnotationRow)
        .where(AnnotationRow.asset_id.in_(asset_ids))
        .order_by(AnnotationRow.asset_id.asc(), AnnotationRow.updated_at.desc())
    )
    annotations: dict[str, AnnotationRow] = {}
    for row in session.scalars(statement):
        annotations.setdefault(row.asset_id, row)
    return annotations


def existing_keys(session: Session, keys: set[str]) -> set[str]:
    if not keys:
        return set()
    rows = session.scalars(
        select(ClipEmbeddingIndexRow.clip_embedding_key).where(
            ClipEmbeddingIndexRow.clip_embedding_key.in_(keys)
        )
    )
    return set(rows)


def embedding_input_text(
    *,
    candidate: dict[str, Any],
    namespace: Namespace,
    asset: MediaAssetRow | None,
    annotation: AnnotationRow | None,
) -> str:
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    clip_id = str(metadata.get("clip_id") or "")
    segment = matching_segment(annotation, metadata)
    parts: list[str] = [
        f"namespace: {namespace}",
        f"clip_id: {clip_id}",
        f"time_span_sec: {metadata.get('source_start')} - {metadata.get('source_end')}",
    ]
    if asset is not None:
        parts.append(f"asset_title: {asset.title}")
        if asset.tags:
            parts.append(f"asset_tags: {', '.join(asset.tags)}")
        if asset.duration_sec is not None:
            parts.append(f"asset_duration_sec: {asset.duration_sec:.3f}")
    reason = str(candidate.get("reason") or "").strip()
    if reason:
        parts.append(f"candidate_reason: {reason}")
    for field in ("scene_name", "diversity_key", "lip_sync_confidence"):
        if metadata.get(field) is not None:
            parts.append(f"{field}: {metadata[field]}")
    keywords = metadata.get("matched_keywords")
    if isinstance(keywords, list) and keywords:
        parts.append("matched_keywords: " + ", ".join(str(item) for item in keywords))
    if isinstance(segment, dict):
        append_segment_text(parts, segment)
    return "\n".join(parts)


def matching_segment(annotation: AnnotationRow | None, metadata: dict[str, Any]) -> dict[str, Any] | None:
    if annotation is None:
        return None
    clip_id = str(metadata.get("clip_id") or "")
    segments = _segments(annotation.projection) + _segments(annotation.canonical)
    for segment in segments:
        segment_id = str(segment.get("segment_id") or segment.get("clip_id") or "")
        if clip_id and segment_id == clip_id:
            return segment
    start = _float_or_none(metadata.get("source_start"))
    end = _float_or_none(metadata.get("source_end"))
    if start is None or end is None:
        return None
    best: tuple[float, dict[str, Any]] | None = None
    for segment in segments:
        seg_start = _float_or_none(segment.get("start"))
        seg_end = _float_or_none(segment.get("end"))
        if seg_start is None or seg_end is None:
            continue
        overlap = max(0.0, min(end, seg_end) - max(start, seg_start))
        if overlap > 0 and (best is None or overlap > best[0]):
            best = (overlap, segment)
    return best[1] if best else None


def _segments(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    values = payload.get("segments")
    if values is None:
        values = payload.get("clips")
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, dict)]


def append_segment_text(parts: list[str], segment: dict[str, Any]) -> None:
    retrieval = segment.get("retrieval")
    if isinstance(retrieval, dict):
        for field in ("retrieval_sentence", "summary"):
            value = str(retrieval.get(field) or "").strip()
            if value:
                parts.append(f"{field}: {value}")
        keywords = retrieval.get("keywords")
        if isinstance(keywords, list) and keywords:
            parts.append("retrieval_keywords: " + ", ".join(str(item) for item in keywords))
    for group in ("visual", "semantics", "usage"):
        value = segment.get(group)
        if isinstance(value, dict):
            flattened = ", ".join(
                f"{key}={item}" for key, item in sorted(value.items()) if item not in (None, "")
            )
            if flattened:
                parts.append(f"{group}: {flattened}")


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def call_embedding_provider(
    gateway: ProviderGateway,
    *,
    profile_id: str,
    case_id: str | None,
    run_id: str | None,
    candidate: ClipCandidate,
    text: str,
) -> list[float]:
    invocation, result = gateway.invoke(
        ProviderCall(
            case_id=case_id,
            run_id=run_id,
            provider_profile_id=profile_id,
            capability_id="multimodal.embedding",
            input={
                "text": text,
                "retrieval_intent": text,
                "model": CLIP_EMBEDDING_MODEL,
                "dimension": CLIP_EMBEDDING_DIMENSION,
                "normalization": CLIP_EMBEDDING_NORMALIZATION,
                "index_version": CLIP_INDEX_VERSION,
            },
            idempotency_key=f"clip-embedding:{candidate.key}",
        )
    )
    if result is None:
        error = invocation.error.message if invocation.error else "provider call failed"
        raise RuntimeError(error)
    embedding = result.output.get("embedding")
    if not isinstance(embedding, list):
        raise RuntimeError("provider did not return an embedding vector")
    return [float(value) for value in embedding]


def upsert_record(session: Session, record: ClipEmbeddingRecord) -> None:
    values = {
        "clip_embedding_key": record.clip_embedding_key,
        "asset_id": record.asset_id,
        "asset_revision": record.asset_revision,
        "clip_id": record.clip_id,
        "source_start": record.source_start,
        "source_end": record.source_end,
        "source_frames_available": record.source_frames_available,
        "index_namespace": record.index_namespace,
        "embedding_scope": record.embedding_scope,
        "embedding_input_type": record.embedding_input_type,
        "embedding_input_ref": record.embedding_input_ref,
        "sample_policy": record.sample_policy,
        "embedding_id": record.embedding_id,
        "embedding": record.embedding,
        "provider_profile_id": record.provider_profile_id,
        "embedding_model": record.embedding_model,
        "embedding_dimension": record.embedding_dimension,
        "normalization": record.normalization,
        "instruct": record.instruct,
        "index_version": record.index_version,
    }
    statement = pg_insert(ClipEmbeddingIndexRow).values(**values)
    update_values = {key: getattr(statement.excluded, key) for key in values if key != "clip_embedding_key"}
    update_values["updated_at"] = func.now()
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[ClipEmbeddingIndexRow.clip_embedding_key],
            set_=update_values,
        )
    )


def main() -> int:
    args = build_parser().parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    if args.apply:
        raise SystemExit(
            "--apply is disabled here. Use POST /api/media/assets/clip-embeddings/index "
            "so the service can build real OSS-backed clip video embeddings."
        )

    session_factory = create_session_factory(create_database_engine())
    with session_factory() as session:
        artifact = material_pack_artifact(
            session,
            run_id=args.run_id,
            artifact_id=args.artifact_id,
        )
        payload = artifact.payload
        if not isinstance(payload, dict):
            raise SystemExit(f"artifact payload is not an object: {artifact.id}")
        assets = load_asset_rows(session, payload)
        candidates, candidate_errors = collect_candidates(
            payload,
            namespace_filter=args.namespace,
            assets=assets,
        )
        keys = {candidate.key for candidate in candidates}
        already_indexed = existing_keys(session, keys)
        pending = [
            candidate for candidate in candidates if args.force or candidate.key not in already_indexed
        ]
        if args.limit is not None:
            pending = pending[: args.limit]

        by_namespace = {
            namespace: sum(1 for candidate in candidates if candidate.namespace == namespace)
            for namespace in ("portrait", "broll")
        }
        log(
            f"material_pack={artifact.id} run={artifact.run_id} case={artifact.case_id} "
            f"portrait={by_namespace['portrait']} broll={by_namespace['broll']} "
            f"existing={len(already_indexed)} pending={len(pending)}"
        )
        if candidate_errors:
            log(f"candidate_errors={len(candidate_errors)}")
            for error in candidate_errors[:10]:
                log(f"  {error}")
        if not args.apply:
            log("dry-run only; add --apply to call the provider and write clip_embedding_index rows.")
            return 0
        if not pending:
            log("nothing to index.")
            return 0

        asset_ids = {str(candidate.candidate.get("asset_id") or "") for candidate in pending}
        annotations = load_latest_annotations(session, asset_ids)
        gateway = ProviderGateway(
            Repository(),
            provider_reader=SqlAlchemyProviderRuntimeRepository(session_factory),
            secret_store=SqlAlchemySecretStore(session_factory, fallback=LocalSecretStore()),
        )
        indexed = 0
        for index, pending_candidate in enumerate(pending, start=1):
            asset_id = str(pending_candidate.candidate.get("asset_id") or "")
            asset_row = assets.get(asset_id)
            asset = media_asset_row_to_contract(asset_row) if asset_row is not None else None
            text = embedding_input_text(
                candidate=pending_candidate.candidate,
                namespace=pending_candidate.namespace,
                asset=asset_row,
                annotation=annotations.get(asset_id),
            )
            embedding = call_embedding_provider(
                gateway,
                profile_id=args.profile_id,
                case_id=artifact.case_id,
                run_id=artifact.run_id,
                candidate=pending_candidate,
                text=text,
            )
            record = build_clip_embedding_record(
                candidate=pending_candidate.candidate,
                asset=asset,
                namespace=pending_candidate.namespace,
                provider_profile_id=args.profile_id,
                embedding=embedding,
            )
            upsert_record(session, record)
            indexed += 1
            log(
                f"[{index}/{len(pending)}] indexed {pending_candidate.namespace} "
                f"{asset_id}:{record.clip_id}"
            )
        session.commit()
        log(f"indexed={indexed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
