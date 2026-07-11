from __future__ import annotations

import ipaddress
import logging
import math
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Request
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from apps.api.common import request_id
from packages.ai.gateway import ProviderCall
from packages.core import contracts as c
from packages.core.contracts.artifacts import ClipEmbeddingRecord
from packages.core.storage.database import (
    AnnotationRow,
    ArtifactRow,
    ClipEmbeddingJobRow,
    ClipEmbeddingIndexRow,
    MediaAssetRow,
)
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.media.assets import local_object_path, store_file
from packages.media.sqlalchemy_repository import media_asset_row_to_contract
from packages.media.video.ffmpeg import (
    FfmpegCommandError,
    compress_video_to_budget,
    trim_to_valid_segments,
)
from packages.planning.editing.frame_grid import frame_index
from packages.planning.material import (
    CLIP_EMBEDDING_DIMENSION,
    CLIP_EMBEDDING_INSTRUCT,
    CLIP_EMBEDDING_MODEL,
    CLIP_EMBEDDING_NORMALIZATION,
    CLIP_INDEX_VERSION,
    avoid_intervals,
    build_clip_embedding_record,
    candidate_clip_embedding_key,
    clip_is_lip_sync_usable,
    clip_shows_person,
    subtract_bad_spans,
)
from packages.planning.material.portrait_source import clean_portrait_source_windows

_MIN_BROLL_CLEAN_SPAN_SECONDS = 1.0
_MAX_EMBEDDING_VIDEO_BYTES = 50 * 1024 * 1024
_EMBEDDING_VIDEO_BUDGET_MB = 49.0
_EMBEDDING_SIGNED_URL_TTL = timedelta(hours=1)

logger = logging.getLogger("apps.api.services.clip_embeddings")
_TERMINAL_JOB_STATUSES = {c.JobStatus.succeeded.value, c.JobStatus.failed.value}


@dataclass(frozen=True)
class _IndexCandidate:
    namespace: Literal["portrait", "broll"]
    candidate: dict[str, Any]
    asset_row: MediaAssetRow
    annotation: c.AnnotationV4
    clip: c.ClipV4
    source_uri: str
    text: str
    key: str


@dataclass(frozen=True)
class _PreparedEmbeddingInput:
    embedding: list[float]
    embedding_id: str
    input_ref: str


class _EmbeddingPreparationError(RuntimeError):
    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.fatal = fatal


def clip_embedding_status(
    request: Request,
    *,
    case_id: str,
    namespace: c.ClipEmbeddingNamespace = "all",
    asset_id: str | None = None,
    asset_ids: list[str] | None = None,
) -> c.ClipEmbeddingIndexStatusResponse:
    if asset_id and asset_ids:
        raise NodeExecutionError(
            c.ErrorCode.validation_invalid_options,
            "asset_id 与 asset_ids 不能同时传入。",
        )
    scoped_asset_ids = [asset_id] if asset_id else asset_ids
    session_factory = request.app.state.sqlalchemy_session_factory
    with session_factory() as session:
        candidates, skipped_assets = _index_candidates(
            session,
            case_id=case_id,
            namespace=namespace,
            asset_ids=scoped_asset_ids,
        )
        existing, last_indexed_at = _existing_index_snapshot(
            session, {candidate.key for candidate in candidates}
        )
    return _status_response(
        case_id=case_id,
        asset_id=asset_id,
        namespace=namespace,
        candidates=candidates,
        existing=existing,
        last_indexed_at=last_indexed_at,
        skipped_assets=skipped_assets,
    )


def enqueue_clip_embeddings(
    payload: c.ClipEmbeddingIndexRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> c.ClipEmbeddingIndexJobResponse:
    status = clip_embedding_status(
        request,
        case_id=payload.case_id,
        namespace=payload.namespace,
        asset_ids=payload.asset_ids,
    )
    eligible_count = status.candidate_count if payload.force else status.pending_count
    job = c.ClipEmbeddingJobStatusResponse(
        job_id=new_id("embjob"),
        case_id=payload.case_id,
        namespace=payload.namespace,
        status=c.JobStatus.queued,
        provider_profile_id=payload.provider_profile_id,
        limit=payload.limit,
        force=payload.force,
        queued_count=min(payload.limit, eligible_count),
        candidate_count=status.candidate_count,
        pending_count=status.pending_count,
        remaining_count=status.pending_count,
        request_id=request_id(),
    )
    _store_job(request.app, job)
    background_tasks.add_task(_run_clip_embedding_job, request.app, job.job_id, payload)
    return c.ClipEmbeddingIndexJobResponse(**job.model_dump(exclude={"schema_version"}))


def clip_embedding_job_status(request: Request, job_id: str) -> c.ClipEmbeddingJobStatusResponse:
    job = _read_job(request.app, job_id)
    if job is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Clip embedding job not found.")
    return job


def index_clip_embeddings(
    payload: c.ClipEmbeddingIndexRequest,
    request: Request,
    progress_callback: Callable[[c.ClipEmbeddingIndexResponse], None] | None = None,
) -> c.ClipEmbeddingIndexResponse:
    session_factory = request.app.state.sqlalchemy_session_factory
    with session_factory() as session:
        candidates, skipped_assets = _index_candidates(
            session,
            case_id=payload.case_id,
            namespace=payload.namespace,
            asset_ids=payload.asset_ids,
        )
        existing, last_indexed_at = _existing_index_snapshot(
            session, {candidate.key for candidate in candidates}
        )
        pending = [
            candidate for candidate in candidates if payload.force or candidate.key not in existing
        ][: payload.limit]
        results: list[c.ClipEmbeddingIndexResultItem] = []
        indexed_now = 0
        failed = 0

        def build_response() -> c.ClipEmbeddingIndexResponse:
            status = _status_response(
                case_id=payload.case_id,
                namespace=payload.namespace,
                candidates=candidates,
                existing=existing,
                skipped_assets=skipped_assets,
                last_indexed_at=last_indexed_at,
            )
            return c.ClipEmbeddingIndexResponse(
                **status.model_dump(),
                provider_profile_id=payload.provider_profile_id,
                processed_count=len(results),
                indexed_now_count=indexed_now,
                failed_count=failed,
                remaining_count=status.pending_count,
                results=list(results),
            )

        if progress_callback is not None:
            progress_callback(build_response())

        for candidate in pending:
            prepared, message, fatal = _call_embedding_provider(
                request,
                provider_profile_id=payload.provider_profile_id,
                case_id=payload.case_id,
                candidate=candidate,
            )
            if prepared is None:
                failed += 1
                results.append(
                    c.ClipEmbeddingIndexResultItem(
                        asset_id=candidate.asset_row.id,
                        clip_id=_candidate_clip_id(candidate),
                        namespace=candidate.namespace,
                        status="failed",
                        clip_embedding_key=candidate.key,
                        message=message or "Embedding provider failed.",
                    )
                )
                if progress_callback is not None:
                    progress_callback(build_response())
                if fatal:
                    break
                continue
            asset = media_asset_row_to_contract(candidate.asset_row)
            record = build_clip_embedding_record(
                candidate=candidate.candidate,
                asset=asset,
                namespace=candidate.namespace,
                provider_profile_id=payload.provider_profile_id,
                embedding=prepared.embedding,
                embedding_id=prepared.embedding_id or candidate.key,
                embedding_input_ref=prepared.input_ref,
            )
            _upsert_record(session, record)
            session.commit()
            last_indexed_at = c.utcnow()
            existing.add(record.clip_embedding_key)
            indexed_now += 1
            results.append(
                c.ClipEmbeddingIndexResultItem(
                    asset_id=record.asset_id,
                    clip_id=record.clip_id,
                    namespace=record.index_namespace,
                    status="indexed",
                    clip_embedding_key=record.clip_embedding_key,
                )
            )
            if progress_callback is not None:
                progress_callback(build_response())
        session.commit()
        return build_response()


def _index_candidates(
    session: Session,
    *,
    case_id: str,
    namespace: c.ClipEmbeddingNamespace,
    asset_ids: list[str] | None,
) -> tuple[list[_IndexCandidate], int]:
    assets = _asset_rows(session, case_id=case_id, asset_ids=asset_ids)
    annotations = _annotation_rows(session, {row.id for row in assets})
    artifacts = _artifact_rows(
        session,
        {row.source_artifact_id for row in assets if row.source_artifact_id},
    )
    candidates: list[_IndexCandidate] = []
    skipped_assets = 0
    for asset_row in assets:
        artifact_row = artifacts.get(asset_row.source_artifact_id or "")
        source_uri = _artifact_source_uri(artifact_row)
        if not source_uri:
            skipped_assets += 1
            continue
        row = annotations.get(asset_row.id)
        if row is None:
            skipped_assets += 1
            continue
        try:
            annotation = c.AnnotationV4.model_validate(row.canonical)
        except ValueError:
            skipped_assets += 1
            continue
        candidates.extend(
            _candidates_for_asset(
                asset_row,
                annotation,
                namespace=namespace,
                source_uri=source_uri,
            )
        )
    return _dedupe_candidates(candidates), skipped_assets


def _asset_rows(
    session: Session,
    *,
    case_id: str,
    asset_ids: list[str] | None,
) -> list[MediaAssetRow]:
    statement = (
        select(MediaAssetRow)
        .where(MediaAssetRow.case_id == case_id)
        .where(MediaAssetRow.kind == "video")
        .where(MediaAssetRow.annotation_status == "annotated")
        .where(MediaAssetRow.usable.is_(True))
        .order_by(MediaAssetRow.updated_at.desc(), MediaAssetRow.id.asc())
    )
    if asset_ids:
        statement = statement.where(MediaAssetRow.id.in_(asset_ids))
    return list(session.scalars(statement))


def _annotation_rows(session: Session, asset_ids: set[str]) -> dict[str, AnnotationRow]:
    if not asset_ids:
        return {}
    statement = (
        select(AnnotationRow)
        .where(AnnotationRow.asset_id.in_(asset_ids))
        .order_by(AnnotationRow.asset_id.asc(), AnnotationRow.updated_at.desc())
    )
    rows: dict[str, AnnotationRow] = {}
    for row in session.scalars(statement):
        rows.setdefault(row.asset_id, row)
    return rows


def _artifact_rows(session: Session, artifact_ids: set[str | None]) -> dict[str, ArtifactRow]:
    ids = {artifact_id for artifact_id in artifact_ids if artifact_id}
    if not ids:
        return {}
    rows = session.scalars(select(ArtifactRow).where(ArtifactRow.id.in_(ids)))
    return {row.id: row for row in rows}


def _artifact_source_uri(row: ArtifactRow | None) -> str:
    if row is None:
        return ""
    for value in (row.uri, row.oss_uri, row.local_path):
        if value:
            return str(value)
    return ""


def _candidates_for_asset(
    asset_row: MediaAssetRow,
    annotation: c.AnnotationV4,
    *,
    namespace: c.ClipEmbeddingNamespace,
    source_uri: str,
) -> list[_IndexCandidate]:
    candidates: list[_IndexCandidate] = []
    if namespace in {"all", "portrait"}:
        candidates.extend(_portrait_candidates(asset_row, annotation, source_uri=source_uri))
    if namespace in {"all", "broll"}:
        candidates.extend(_broll_candidates(asset_row, annotation, source_uri=source_uri))
    return candidates


def _portrait_candidates(
    asset_row: MediaAssetRow,
    annotation: c.AnnotationV4,
    *,
    source_uri: str,
) -> list[_IndexCandidate]:
    candidates: list[_IndexCandidate] = []
    bad_spans = avoid_intervals(annotation)
    source_duration = asset_row.duration_sec or annotation.meta.duration or None
    asset = media_asset_row_to_contract(asset_row)
    for clip in annotation.clips:
        if not clip_is_lip_sync_usable(clip):
            continue
        clean_windows = clean_portrait_source_windows(
            {
                "source_start": clip.start,
                "source_end": clip.end,
                "avoid_spans": bad_spans,
            },
            source_duration=source_duration,
        )
        for index, (start, end) in enumerate(clean_windows):
            metadata = {
                "clip_id": clip.segment_id,
                "source_window_id": clip.segment_id if index == 0 else f"{clip.segment_id}:m{index}",
                "source_start": round(float(start), 3),
                "source_end": round(float(end), 3),
                "lip_sync_confidence": float(clip.confidence),
            }
            candidate = {
                "asset_id": asset_row.id,
                "score": 1.0,
                "reason": "eligible portrait clip",
                "metadata": metadata,
            }
            key = candidate_clip_embedding_key(candidate=candidate, asset=asset, namespace="portrait")
            candidates.append(
                _IndexCandidate(
                    namespace="portrait",
                    candidate=candidate,
                    asset_row=asset_row,
                    annotation=annotation,
                    clip=clip,
                    source_uri=source_uri,
                    text=_embedding_text(asset_row, clip, namespace="portrait", metadata=metadata),
                    key=key,
                )
            )
    return candidates


def _broll_candidates(
    asset_row: MediaAssetRow,
    annotation: c.AnnotationV4,
    *,
    source_uri: str,
) -> list[_IndexCandidate]:
    candidates: list[_IndexCandidate] = []
    bad_spans = avoid_intervals(annotation)
    asset = media_asset_row_to_contract(asset_row)
    for clip in annotation.clips:
        if clip.usage.role.value == "avoid" or clip_is_lip_sync_usable(clip) or clip_shows_person(clip):
            continue
        clean_spans = subtract_bad_spans(
            clip.start,
            clip.end,
            bad_spans,
            min_len=_MIN_BROLL_CLEAN_SPAN_SECONDS,
        )
        for index, (start, end) in enumerate(clean_spans):
            # ``subtract_bad_spans`` intentionally preserves untouched short
            # spans. Keep provider-accepted sub-second clips, but never submit a
            # span that quantizes to zero frames on the production 30 fps grid.
            if frame_index(end) <= frame_index(start):
                continue
            metadata = {
                "clip_id": _clip_id_for_clean_span(clip.segment_id, index),
                "source_start": round(float(start), 3),
                "source_end": round(float(end), 3),
                "scene_name": _scene_name(clip),
                "matched_keywords": list(clip.retrieval.keywords),
                "diversity_key": _diversity_key(clip),
            }
            candidate = {
                "asset_id": asset_row.id,
                "score": 1.0,
                "reason": "eligible b-roll clip",
                "metadata": metadata,
            }
            key = candidate_clip_embedding_key(candidate=candidate, asset=asset, namespace="broll")
            candidates.append(
                _IndexCandidate(
                    namespace="broll",
                    candidate=candidate,
                    asset_row=asset_row,
                    annotation=annotation,
                    clip=clip,
                    source_uri=source_uri,
                    text=_embedding_text(asset_row, clip, namespace="broll", metadata=metadata),
                    key=key,
                )
            )
    return candidates


def _dedupe_candidates(candidates: list[_IndexCandidate]) -> list[_IndexCandidate]:
    seen: set[str] = set()
    deduped: list[_IndexCandidate] = []
    for candidate in candidates:
        if candidate.key in seen:
            continue
        seen.add(candidate.key)
        deduped.append(candidate)
    return deduped


def _existing_index_snapshot(
    session: Session, keys: set[str]
) -> tuple[set[str], datetime | None]:
    if not keys:
        return set(), None
    rows = session.scalars(
        select(ClipEmbeddingIndexRow.clip_embedding_key).where(
            ClipEmbeddingIndexRow.clip_embedding_key.in_(keys)
        )
    )
    last_indexed_at = session.scalar(
        select(func.max(ClipEmbeddingIndexRow.updated_at)).where(
            ClipEmbeddingIndexRow.clip_embedding_key.in_(keys)
        )
    )
    return set(rows), last_indexed_at


def _status_response(
    *,
    case_id: str,
    asset_id: str | None = None,
    namespace: c.ClipEmbeddingNamespace,
    candidates: list[_IndexCandidate],
    existing: set[str],
    skipped_assets: int,
    last_indexed_at: datetime | None = None,
) -> c.ClipEmbeddingIndexStatusResponse:
    indexed = sum(1 for candidate in candidates if candidate.key in existing)
    return c.ClipEmbeddingIndexStatusResponse(
        case_id=case_id,
        asset_id=asset_id,
        namespace=namespace,
        candidate_count=len(candidates),
        indexed_count=indexed,
        pending_count=max(0, len(candidates) - indexed),
        annotated_asset_count=len({candidate.asset_row.id for candidate in candidates}),
        skipped_asset_count=skipped_assets,
        embedding_model=CLIP_EMBEDDING_MODEL,
        embedding_dimension=CLIP_EMBEDDING_DIMENSION,
        index_version=CLIP_INDEX_VERSION,
        last_indexed_at=last_indexed_at,
        request_id=request_id(),
    )


def _store_job(app: Any, job: c.ClipEmbeddingJobStatusResponse) -> None:
    session_factory = app.state.sqlalchemy_session_factory
    with session_factory() as session:
        session.merge(
            ClipEmbeddingJobRow(
                id=job.job_id,
                case_id=job.case_id,
                namespace=job.namespace,
                status=job.status.value,
                payload=job.model_dump(mode="json"),
            )
        )
        session.commit()


def _read_job(app: Any, job_id: str) -> c.ClipEmbeddingJobStatusResponse | None:
    session_factory = app.state.sqlalchemy_session_factory
    with session_factory() as session:
        row = session.get(ClipEmbeddingJobRow, job_id)
        if row is None:
            return None
        return c.ClipEmbeddingJobStatusResponse.model_validate(row.payload)


def _update_job(
    app: Any,
    job_id: str,
    **updates: Any,
) -> c.ClipEmbeddingJobStatusResponse | None:
    session_factory = app.state.sqlalchemy_session_factory
    with session_factory() as session:
        row = session.scalar(
            select(ClipEmbeddingJobRow)
            .where(ClipEmbeddingJobRow.id == job_id)
            .with_for_update()
        )
        if row is None:
            return None
        current = c.ClipEmbeddingJobStatusResponse.model_validate(row.payload)
        # Terminal statuses are immutable: a restart reconcile may have already
        # failed this job, and a surviving background thread must not overwrite it.
        if current.status.value in _TERMINAL_JOB_STATUSES and "status" in updates:
            return current
        updated = current.model_copy(update={**updates, "updated_at": c.utcnow()})
        payload = updated.model_dump(mode="json")
        row.case_id = updated.case_id
        row.namespace = updated.namespace
        row.status = _job_status_value(updated.status)
        row.payload = payload
        session.commit()
        return c.ClipEmbeddingJobStatusResponse.model_validate(payload)


def reconcile_interrupted_clip_embedding_jobs(app: Any) -> None:
    session_factory = app.state.sqlalchemy_session_factory
    now = c.utcnow()
    now_payload = now.isoformat()
    message = "API 重启中断，请重新发起索引"
    with session_factory() as session:
        session.execute(
            text(
                """
                update clip_embedding_jobs
                set status = :failed_status,
                    payload = payload || jsonb_build_object(
                        'status', cast(:failed_payload as text),
                        'error_message', cast(:message as text),
                        'finished_at', cast(:finished_at as text),
                        'updated_at', cast(:updated_at as text)
                    ),
                    updated_at = :updated_at_ts
                where status in (:queued, :running)
                """
            ),
            {
                "failed_status": c.JobStatus.failed.value,
                "failed_payload": c.JobStatus.failed.value,
                "message": message,
                "finished_at": now_payload,
                "updated_at": now_payload,
                "updated_at_ts": now,
                "queued": c.JobStatus.queued.value,
                "running": c.JobStatus.running.value,
            },
        )
        session.commit()


def _job_status_value(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _run_clip_embedding_job(app: Any, job_id: str, payload: c.ClipEmbeddingIndexRequest) -> None:
    started_job = _update_job(
        app,
        job_id,
        status=c.JobStatus.running,
        started_at=c.utcnow(),
    )
    if started_job is None or started_job.status != c.JobStatus.running:
        return
    try:
        response = index_clip_embeddings(
            payload,
            SimpleNamespace(app=app),
            progress_callback=lambda progress: _publish_job_progress(app, job_id, progress),
        )
    except Exception as exc:  # pragma: no cover - defensive path for provider/runtime faults
        logger.exception("[clip-embeddings] job %s failed", job_id)
        _update_job(
            app,
            job_id,
            status=c.JobStatus.failed,
            finished_at=c.utcnow(),
            error_message=_exception_message(exc),
        )
        return
    status = c.JobStatus.failed if response.failed_count > 0 else c.JobStatus.succeeded
    _update_job(
        app,
        job_id,
        status=status,
        finished_at=c.utcnow(),
        result=response,
        processed_count=response.processed_count,
        indexed_now_count=response.indexed_now_count,
        failed_count=response.failed_count,
        remaining_count=response.remaining_count,
        pending_count=response.remaining_count,
    )


def _publish_job_progress(
    app: Any,
    job_id: str,
    response: c.ClipEmbeddingIndexResponse,
) -> None:
    _update_job(
        app,
        job_id,
        status=c.JobStatus.running,
        result=response,
        processed_count=response.processed_count,
        indexed_now_count=response.indexed_now_count,
        failed_count=response.failed_count,
        remaining_count=response.remaining_count,
        pending_count=response.remaining_count,
    )


def _exception_message(exc: Exception) -> str:
    if isinstance(exc, NodeExecutionError):
        return exc.error.message
    return str(exc) or exc.__class__.__name__


def _call_embedding_provider(
    request: Request,
    *,
    provider_profile_id: str,
    case_id: str,
    candidate: _IndexCandidate,
) -> tuple[_PreparedEmbeddingInput | None, str | None, bool]:
    try:
        video_uri, video_url = _prepare_clip_video(request, candidate)
    except _EmbeddingPreparationError as exc:
        return None, exc.message, exc.fatal
    # Clip index revisions bind to source media (source_artifact_id), not editable
    # annotation text. Keep index embeddings video-only; if text is added later, the
    # annotation revision must become part of the embedding key.
    invocation, result = request.app.state.provider_gateway.invoke(
        ProviderCall(
            case_id=case_id,
            provider_profile_id=provider_profile_id,
            capability_id="multimodal.embedding",
            input={
                "video_url": video_url,
                "model": CLIP_EMBEDDING_MODEL,
                "dimension": CLIP_EMBEDDING_DIMENSION,
                "normalization": CLIP_EMBEDDING_NORMALIZATION,
                "instruct": CLIP_EMBEDDING_INSTRUCT,
                "index_version": CLIP_INDEX_VERSION,
            },
            idempotency_key=f"clip-embedding:{candidate.key}",
        )
    )
    if result is None:
        error = invocation.error
        code = error.code.value if error and hasattr(error.code, "value") else str(error.code if error else "")
        fatal = code in {"provider.auth_failed", "provider.unsupported_option"}
        return None, error.message if error else "Embedding provider failed.", fatal
    embedding = result.output.get("embedding")
    if not isinstance(embedding, list):
        return None, "Embedding provider did not return a vector.", True
    embedding_id = str(result.output.get("embedding_id") or "")
    return (
        _PreparedEmbeddingInput(
            embedding=[float(value) for value in embedding],
            embedding_id=embedding_id,
            input_ref=video_uri,
        ),
        None,
        False,
    )


def _prepare_clip_video(request: Request, candidate: _IndexCandidate) -> tuple[str, str]:
    metadata = candidate.candidate.get("metadata")
    if not isinstance(metadata, dict):
        raise _EmbeddingPreparationError("Clip candidate metadata is missing.")
    start = _finite_float(metadata.get("source_start"), field_name="source_start")
    end = _finite_float(metadata.get("source_end"), field_name="source_end")
    if end <= start:
        raise _EmbeddingPreparationError("Clip candidate time span is invalid.")
    store = request.app.state.object_store
    with tempfile.TemporaryDirectory(prefix="cutflow-clip-embedding-") as temp_dir:
        temp_root = Path(temp_dir)
        source_path = _source_path_for_uri(store, candidate.source_uri)
        clip_id = _candidate_clip_id(candidate) or "clip"
        safe_clip_id = _safe_filename(clip_id)
        output_path = temp_root / f"{candidate.asset_row.id}_{safe_clip_id}_{start:.3f}_{end:.3f}.mp4"
        try:
            trim_to_valid_segments(
                source_path,
                [{"start": start, "end": end}],
                output_path,
            )
            output_path = _fit_embedding_video_budget(output_path, temp_root)
            stored = store_file(
                store,
                output_path,
                purpose="clip-embeddings",
                addressed=True,
                content_type="video/mp4",
            )
            signed = store.signed_url(stored.ref.uri, expires_in=_EMBEDDING_SIGNED_URL_TTL)
        except FfmpegCommandError as exc:
            detail = exc.stderr.strip() or str(exc)
            raise _EmbeddingPreparationError(
                f"Clip video extraction failed: {detail[:240]}",
            ) from exc
        except _EmbeddingPreparationError:
            raise
        except Exception as exc:
            raise _EmbeddingPreparationError(
                f"Clip video upload/signing failed: {exc}",
                fatal=True,
            ) from exc
    if not _is_public_http_url(signed.url):
        raise _EmbeddingPreparationError(
            "Clip video URL is not publicly fetchable by DashScope. "
            "Use an OSS-backed object store instead of local/localhost URLs.",
            fatal=True,
        )
    return stored.ref.uri, signed.url


def _source_path_for_uri(object_store: Any, uri: str) -> Path:
    if uri.startswith(("local://", "s3://")):
        try:
            return local_object_path(object_store, uri)
        except Exception as exc:
            raise _EmbeddingPreparationError(
                f"Source video URI cannot be read from object store: {uri}",
            ) from exc
    path = Path(uri)
    if path.exists():
        return path
    raise _EmbeddingPreparationError(
        f"Source video URI cannot be materialized for clipping: {uri}",
    )


def _fit_embedding_video_budget(path: Path, temp_root: Path) -> Path:
    if path.stat().st_size <= _MAX_EMBEDDING_VIDEO_BYTES:
        return path
    compressed = temp_root / f"{path.stem}_qwen3vl_budget.mp4"
    result = compress_video_to_budget(
        path,
        max_size_mb=_EMBEDDING_VIDEO_BUDGET_MB,
        output_path=compressed,
    )
    if result.size_bytes > _MAX_EMBEDDING_VIDEO_BYTES:
        raise _EmbeddingPreparationError(
            "Clip video remains above DashScope 50MB limit after compression.",
        )
    return result.path


def _is_public_http_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return False
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)


def _safe_filename(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return safe[:80] or "clip"


def _finite_float(value: Any, *, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise _EmbeddingPreparationError(f"{field_name} must be a finite number.") from exc
    if not math.isfinite(number) or number < 0:
        raise _EmbeddingPreparationError(f"{field_name} must be a non-negative finite number.")
    return number


def _upsert_record(session: Session, record: ClipEmbeddingRecord) -> None:
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
    update_values = {
        key: getattr(statement.excluded, key) for key in values if key != "clip_embedding_key"
    }
    update_values["updated_at"] = func.now()
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[ClipEmbeddingIndexRow.clip_embedding_key],
            set_=update_values,
        )
    )


def _embedding_text(
    asset_row: MediaAssetRow,
    clip: c.ClipV4,
    *,
    namespace: Literal["portrait", "broll"],
    metadata: dict[str, Any],
) -> str:
    parts = [
        f"namespace: {namespace}",
        f"asset_title: {asset_row.title}",
        f"asset_tags: {', '.join(asset_row.tags or [])}",
        f"clip_id: {metadata.get('clip_id')}",
        f"time_span_sec: {metadata.get('source_start')} - {metadata.get('source_end')}",
    ]
    if clip.retrieval.retrieval_sentence:
        parts.append(f"retrieval_sentence: {clip.retrieval.retrieval_sentence}")
    if clip.retrieval.summary:
        parts.append(f"summary: {clip.retrieval.summary}")
    if clip.retrieval.keywords:
        parts.append("keywords: " + ", ".join(clip.retrieval.keywords))
    visual = _flatten_dict(clip.visual.model_dump(mode="json"))
    semantics = _flatten_dict(clip.semantics.model_dump(mode="json"))
    usage = _flatten_dict(clip.usage.model_dump(mode="json"))
    if visual:
        parts.append(f"visual: {visual}")
    if semantics:
        parts.append(f"semantics: {semantics}")
    if usage:
        parts.append(f"usage: {usage}")
    return "\n".join(part for part in parts if part.strip())


def _flatten_dict(value: dict[str, Any]) -> str:
    return ", ".join(
        f"{key}={item}" for key, item in sorted(value.items()) if item not in (None, "", [])
    )


def _clip_id_for_clean_span(segment_id: str, span_index: int) -> str:
    return segment_id if span_index == 0 else f"{segment_id}-m{span_index}"


def _scene_name(clip: c.ClipV4) -> str:
    return (
        clip.semantics.narrative_role
        or clip.semantics.action
        or clip.semantics.scene_type
        or clip.retrieval.summary
        or "片段"
    ).strip()[:48] or "片段"


def _diversity_key(clip: c.ClipV4) -> str:
    return (clip.semantics.scene_type or clip.semantics.narrative_role or "").strip()


def _candidate_clip_id(candidate: _IndexCandidate) -> str:
    metadata = candidate.candidate.get("metadata")
    if isinstance(metadata, dict):
        return str(metadata.get("clip_id") or "")
    return ""
