from __future__ import annotations

import re

from fastapi import Request
from sqlalchemy import select

from apps.api.common import media_repository, object_store, repository, request_id
from packages.core import contracts as c
from packages.core.storage.database import AnnotationRow, ArtifactRow, MediaAssetRow, UploadSessionRow
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.media.annotation import (
    DEFAULT_DURATION_DRIFT_THRESHOLD,
    reclipped_or_validated,
)
from packages.media.assets import local_object_path, store_file
from packages.media.video.ffmpeg import FfmpegCommandError, probe_media, stabilize_video, trim_to_valid_segments


def batch_stabilize_assets(
    payload: c.BatchStabilizeMediaAssetsRequest, request: Request
) -> c.BatchMediaProcessResponse:
    results: list[c.MediaAssetProcessingResult] = []
    for asset_id in payload.asset_ids:
        try:
            artifact_id = _stabilize_asset(asset_id, request)
        except NodeExecutionError as exc:
            results.append(
                c.MediaAssetProcessingResult(
                    asset_id=asset_id, status="failed", error_code=exc.error.code, message=exc.error.message
                )
            )
        except FfmpegCommandError as exc:
            results.append(
                c.MediaAssetProcessingResult(
                    asset_id=asset_id, status="failed", error_code=exc.error_code, message="视频增稳失败。"
                )
            )
        else:
            results.append(c.MediaAssetProcessingResult(asset_id=asset_id, status="completed", artifact_id=artifact_id))
    return c.BatchMediaProcessResponse(results=results, request_id=request_id())


def trim_annotation(
    asset_id: str, payload: c.TrimAnnotationRequest, request: Request
) -> c.TrimAnnotationResponse:
    asset = _asset_record(request, asset_id)
    source = _asset_source_ref(request, asset_id)
    if source is None or source.uri is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset source artifact missing.")
    source_path = local_object_path(object_store(request), source.uri)
    duration = float(probe_media(source_path).duration_sec or 0)
    valid_segments = (
        [segment.model_dump() for segment in payload.valid_segments]
        if payload.valid_segments is not None
        else _valid_segments_from_annotation(_annotation_editor(request, asset), duration)
    )
    if not valid_segments:
        raise NodeExecutionError(_material_error_code(asset.kind), "素材没有可裁剪的有效片段。")
    try:
        output_path = trim_to_valid_segments(source_path, valid_segments)
        media_info = probe_media(output_path)
    except FfmpegCommandError as exc:
        raise NodeExecutionError(exc.error_code, "裁剪无效片段失败。") from exc
    stored = store_file(object_store(request), output_path, purpose="media-trimmed")
    artifact = _replace_asset_artifact(
        request,
        asset_id,
        source,
        uri=stored.ref.uri,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        media_info=media_info,
        payload={
            "source_artifact_id": _source_artifact_id(source),
            "media_asset_id": asset_id,
            "trimmed": True,
            "valid_segments": valid_segments,
        },
        tag="trimmed",
    )
    return c.TrimAnnotationResponse(
        asset_id=asset_id,
        artifact=artifact,
        valid_duration_sec=sum(item["end_sec"] - item["start_sec"] for item in valid_segments),
        request_id=request_id(),
    )


def replace_asset_source(
    asset_id: str, payload: c.MediaAssetReplaceSourceRequest, request: Request
) -> c.MediaAssetReplaceResponse:
    # Capture the existing source duration BEFORE swapping the source artifact, so the
    # duration-drift guard can compare against the replacement.
    old_duration = _annotated_old_duration(request, asset_id)
    artifact = _upload_artifact(request, payload.upload_session_id)
    new_duration = _artifact_duration(request, artifact)

    asset = _replace_with_existing_artifact(request, asset_id, artifact["artifact_id"], "replaced")

    # Duration-drift guard: replacing with a
    # differently-timed clip would leave the preserved annotation's clips /
    # usage_windows / quality_events pointing past or into the wrong frames. Within
    # the 0.15s threshold the annotation is preserved
    # as-is; beyond it we re-clip the canonical to the new duration (clamp time
    # layers). If re-clipping can't yield a valid annotation, the annotation is
    # invalidated (annotation_status=pending) and preserved_annotation=false -- we
    # never report a preserved annotation whose timestamps diverge from the media.
    preserved = _has_annotation(request, asset_id)
    if (
        preserved
        and old_duration is not None
        and new_duration is not None
        and old_duration > 0
        and new_duration > 0
        and abs(new_duration - old_duration) > DEFAULT_DURATION_DRIFT_THRESHOLD
    ):
        preserved = _reconcile_annotation_duration(
            request, asset_id, old_duration=old_duration, new_duration=new_duration
        )

    return c.MediaAssetReplaceResponse(
        asset=asset,
        artifact=_artifact_ref(artifact),
        preserved_annotation=preserved,
        request_id=request_id(),
    )


def _annotated_old_duration(request: Request, asset_id: str) -> float | None:
    """Existing media duration to compare against the replacement.

    Prefers the annotation canonical's meta.duration (what the annotation was timed
    against), then the current source artifact's media_info, then probing it. None when
    nothing is annotated / no readable old duration (the guard then no-ops)."""
    canonical = _annotation_canonical(request, asset_id)
    if isinstance(canonical, dict):
        meta = canonical.get("meta")
        if isinstance(meta, dict):
            try:
                duration = float(meta.get("duration") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration > 0:
                return duration
    source = _asset_source_ref(request, asset_id)
    if source is None or not getattr(source, "uri", None):
        return None
    try:
        return float(probe_media(local_object_path(object_store(request), source.uri)).duration_sec or 0.0)
    except (FfmpegCommandError, ValueError, OSError):
        return None


def _artifact_duration(request: Request, artifact: dict) -> float | None:
    """Probe the (replacement) artifact's source duration, or None when unreadable."""
    uri = artifact.get("uri")
    if not uri:
        return None
    try:
        return float(probe_media(local_object_path(object_store(request), uri)).duration_sec or 0.0)
    except (FfmpegCommandError, ValueError, OSError):
        return None


def _annotation_canonical(request: Request, asset_id: str) -> dict | None:
    sql_repo = media_repository(request)
    with sql_repo.session_factory() as session:
        row = session.scalar(
            select(AnnotationRow)
            .where(AnnotationRow.asset_id == asset_id)
            .order_by(AnnotationRow.updated_at.desc())
            .limit(1)
        )
        return dict(row.canonical) if row is not None and isinstance(row.canonical, dict) else None


def _reconcile_annotation_duration(
    request: Request, asset_id: str, *, old_duration: float, new_duration: float
) -> bool:
    """Re-clip the preserved annotation to the new duration, or invalidate it.

    Returns True when a valid re-clipped canonical was persisted (annotation
    preserved), False when the annotation had to be invalidated
    (annotation_status=pending) because it could not be safely re-clipped."""
    canonical = _annotation_canonical(request, asset_id)
    if canonical is None:
        return False
    reclipped = reclipped_or_validated(
        canonical, old_duration=old_duration, new_duration=new_duration
    )
    sql_repo = media_repository(request)
    if reclipped is None:
        # Not a V4 canonical or it cannot be re-clipped safely -> force re-annotation.
        sql_repo.invalidate_annotation(asset_id)
        return False
    sql_repo.set_annotation_canonical(asset_id, reclipped)
    return True


def auto_match_replace(
    payload: c.AutoMatchReplaceRequest, request: Request
) -> c.AutoMatchReplaceResponse:
    index: dict[str, list[c.MediaAssetRecord]] = {}
    for asset in _list_assets_for_match(request, payload.case_id, payload.kind):
        index.setdefault(_match_key(asset.title), []).append(asset)
    results: list[c.AutoMatchReplaceResult] = []
    for upload_id in payload.upload_session_ids:
        try:
            artifact = _upload_artifact(request, upload_id)
            matches = index.get(_match_key(artifact["filename"]), [])
            if len(matches) == 1:
                asset = _replace_with_existing_artifact(request, matches[0].id, artifact["artifact_id"], "replaced")
                results.append(
                    c.AutoMatchReplaceResult(
                        upload_session_id=upload_id, filename=artifact["filename"], status="matched",
                        asset_id=asset.id, artifact_id=artifact["artifact_id"]
                    )
                )
            elif not matches:
                results.append(c.AutoMatchReplaceResult(upload_session_id=upload_id, filename=artifact["filename"], status="unmatched", message="未找到同名模板"))
            else:
                results.append(c.AutoMatchReplaceResult(upload_session_id=upload_id, filename=artifact["filename"], status="ambiguous", message="匹配到多个同名模板"))
        except NodeExecutionError as exc:
            results.append(c.AutoMatchReplaceResult(upload_session_id=upload_id, filename="", status="failed", message=exc.error.message))
    return c.AutoMatchReplaceResponse(results=results, request_id=request_id())


def _stabilize_asset(asset_id: str, request: Request) -> str:
    source = _asset_source_ref(request, asset_id)
    if source is None or source.uri is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset source artifact missing.")
    output_path = stabilize_video(local_object_path(object_store(request), source.uri))
    media_info = probe_media(output_path)
    stored = store_file(object_store(request), output_path, purpose="media-stabilized")
    artifact = _replace_asset_artifact(
        request,
        asset_id,
        source,
        uri=stored.ref.uri,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
        media_info=media_info,
        payload={"source_artifact_id": _source_artifact_id(source), "media_asset_id": asset_id, "stabilized": True},
        tag="stabilized",
    )
    return artifact.artifact_id


def _upload_artifact(request: Request, upload_session_id: str) -> dict:
    sql_repo = media_repository(request)
    with sql_repo.session_factory() as session:
        upload = session.get(UploadSessionRow, upload_session_id)
        artifacts = session.scalars(
            select(ArtifactRow).where(ArtifactRow.kind == c.ArtifactKind.uploaded_file.value)
        )
        artifact = next(
            (
                item for item in artifacts
                if isinstance(item.payload, dict) and item.payload.get("id") == upload_session_id
            ),
            None,
        )
        if upload is None or artifact is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Replacement upload artifact missing.")
        return {"artifact_id": artifact.id, "kind": artifact.kind, "uri": artifact.uri, "sha256": artifact.sha256, "filename": upload.filename}


def _replace_with_existing_artifact(request: Request, asset_id: str, artifact_id: str, tag: str) -> c.MediaAssetRecord:
    sql_repo = media_repository(request)
    with sql_repo.session_factory() as session:
        asset = session.get(MediaAssetRow, asset_id)
        if asset is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        asset.source_artifact_id = artifact_id
        tags = list(asset.tags or [])
        if tag not in tags:
            tags.append(tag)
        asset.tags = tags
        asset.updated_at = c.utcnow()
        session.commit()
    detail = sql_repo.get_asset_detail(asset_id)
    if detail is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    return detail.asset


def _list_assets_for_match(request: Request, case_id: str | None, kind: str) -> list[c.MediaAssetRecord]:
    sql_repo = media_repository(request)
    return [item.asset for item in sql_repo.list_assets(limit=200, case_id=case_id, kind=kind)]


def _artifact_ref(artifact: dict) -> c.ArtifactRef:
    return c.ArtifactRef(
        artifact_id=artifact["artifact_id"],
        kind=c.ArtifactKind(artifact["kind"]),
        uri=artifact["uri"] or f"artifact://{artifact['artifact_id']}",
        sha256=artifact["sha256"],
    )


def _has_annotation(request: Request, asset_id: str) -> bool:
    sql_repo = media_repository(request)
    with sql_repo.session_factory() as session:
        annotation_id = session.scalar(select(AnnotationRow.id).where(AnnotationRow.asset_id == asset_id).limit(1))
        return annotation_id is not None


def _match_key(value: str) -> str:
    stem = value.rsplit(".", 1)[0]
    return re.sub(r"[\W_]+", "", stem, flags=re.UNICODE).lower()


def _asset_record(request: Request, asset_id: str) -> c.MediaAssetRecord:
    sql_repo = media_repository(request)
    detail = sql_repo.get_asset_detail(asset_id)
    if detail is not None:
        return detail.asset
    raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")


def _annotation_editor(request: Request, asset: c.MediaAssetRecord) -> c.AnnotationEditorVm:
    sql_repo = media_repository(request)
    if sql_repo is not None:
        editor = sql_repo.get_or_create_annotation(asset.id)
        if editor is not None:
            return editor
    repo = repository(request)
    if asset.id not in repo.annotations:
        repo.annotations[asset.id] = c.AnnotationEditorVm(
            asset=asset,
            etag=new_id("etag"),
            canonical={"labels": asset.tags, "kind": asset.kind},
            projection={"title": asset.title, "usable": asset.usable},
            editable_paths=["/labels", "/usable", "/title"],
        )
    return repo.annotations[asset.id]


def _asset_source_ref(request: Request, asset_id: str):
    sql_repo = media_repository(request)
    source = sql_repo.artifact_ref_for_asset(asset_id)
    if source is None:
        _asset_record(request, asset_id)
    return source


def _replace_asset_artifact(
    request: Request,
    asset_id: str,
    source,
    *,
    uri: str,
    size_bytes: int,
    sha256: str,
    media_info: c.MediaInfo,
    payload: dict,
    tag: str,
) -> c.ArtifactRef:
    sql_repo = media_repository(request)
    source_kind = c.ArtifactKind(source.kind)
    artifact_ref = sql_repo.replace_asset_source_artifact(
        asset_id, kind=source_kind, uri=uri, size_bytes=size_bytes, sha256=sha256,
        media_info=media_info, payload=payload, tag=tag
    )
    if artifact_ref is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    return artifact_ref


def _valid_segments_from_annotation(editor: c.AnnotationEditorVm, duration: float) -> list[dict[str, float]]:
    invalid = _read_annotation_segments(editor.projection.get("invalid_segments"), duration)
    valid: list[dict[str, float]] = []
    cursor = 0.0
    for start, end in invalid:
        if start > cursor:
            valid.append({"start_sec": cursor, "end_sec": start})
        cursor = max(cursor, end)
    if cursor < duration:
        valid.append({"start_sec": cursor, "end_sec": duration})
    return [segment for segment in valid if segment["end_sec"] - segment["start_sec"] > 0.03]


def _read_annotation_segments(value: object, duration: float) -> list[tuple[float, float]]:
    if not isinstance(value, list):
        return []
    windows: list[tuple[float, float]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        start = float(item.get("start_sec", item.get("start", 0)))
        end = float(item.get("end_sec", item.get("end", start)))
        if start < 0 or end < start or end > duration + 0.03:
            raise NodeExecutionError(c.ErrorCode.render_invalid_timeline, "无效片段越界。")
        windows.append((max(0.0, start), min(duration, end)))
    return sorted(windows)


def _material_error_code(kind: str) -> c.ErrorCode:
    return c.ErrorCode.material_insufficient_portrait if kind == "portrait" else c.ErrorCode.material_insufficient_broll


def _source_artifact_id(source) -> str:
    return source.artifact_id if hasattr(source, "artifact_id") else source.id
