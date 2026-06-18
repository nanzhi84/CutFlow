from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import Request

from apps.api.common import (
    object_store,
    production_repository,
    repository,
    request_id,
)
from packages.core import contracts as c
from packages.core.storage.repository import new_id
from packages.media.assets import local_object_path
from packages.media.video.ffmpeg import probe_media

def import_batch(payload: c.CreateImportBatchRequest, request: Request) -> c.ImportBatchReport:
    if production_repository(request) is not None:
        report = production_repository(request).create_import_batch(payload, request_id())
        if report is not None:
            return report
    rows = payload.rows or []
    results = []
    created = 0
    skipped = 0
    failed = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            failed += 1
            results.append(
                c.ImportRowResult(
                    row_index=index,
                    status="failed",
                    error=c.NodeError(
                        code=c.ErrorCode.validation_invalid_options,
                        message="Import row must be an object.",
                    ),
                )
            )
            continue
        internal_id = new_id(payload.import_type)
        if not payload.dry_run:
            if payload.import_type == "case":
                case = c.CaseDetail(
                    id=internal_id,
                    name=str(row.get("name", "Imported case")),
                    owner_user_id="usr_admin",
                    description=str(row.get("description", "")),
                )
                repository(request).cases[case.id] = case
            elif payload.import_type == "script":
                script = c.ScriptVersion(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    title=str(row.get("title", "Imported script")),
                    script=str(row.get("script", "")),
                )
                repository(request).scripts[script.id] = script
            elif payload.import_type == "media":
                case_id = str(row.get("case_id", "case_demo"))
                title = str(row.get("title", "Imported media"))
                kind = str(row.get("kind", "other"))
                uri = _optional_str(row.get("uri"))
                sha256 = _optional_str(row.get("sha256"))
                if uri:
                    existing_asset = _find_existing_imported_media_asset(
                        request,
                        case_id=case_id,
                        kind=kind,
                        sha256=sha256,
                        uri=uri,
                    )
                    if existing_asset is not None:
                        skipped += 1
                        results.append(
                            c.ImportRowResult(
                                row_index=index,
                                status="skipped",
                                external_id=str(row.get("external_id")) if row.get("external_id") else None,
                                internal_id=existing_asset.id,
                            )
                        )
                        continue
                    artifact = _create_imported_media_source_artifact(
                        request,
                        row=row,
                        case_id=case_id,
                        title=title,
                        kind=kind,
                        uri=uri,
                        sha256=sha256,
                    )
                    source_artifact_id = artifact.id
                else:
                    source_artifact_id = None
                asset = c.MediaAssetRecord(
                    id=internal_id,
                    case_id=case_id,
                    title=title,
                    kind=kind,
                    source_artifact_id=source_artifact_id,
                    annotation_status="pending",
                    thumbnail_url=_optional_str(row.get("thumbnail_uri"))
                    or _optional_str(row.get("thumbnail")),
                    duration_sec=_optional_float(row.get("duration_sec")),
                    width=_optional_int(row.get("width")),
                    height=_optional_int(row.get("height")),
                )
                repository(request).media_assets[asset.id] = asset
            elif payload.import_type == "finished_video":
                artifact = repository(request).create_artifact(
                    kind=c.ArtifactKind.video_finished,
                    payload_schema="ImportedFinishedVideoArtifact.v1",
                    payload={"external_id": row.get("external_id")},
                    uri=str(row.get("uri", f"sandbox://import/{internal_id}.mp4")),
                )
                finished = c.FinishedVideo(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    title=str(row.get("title", "Imported finished video")),
                    video_number=str(row.get("video_number")) if row.get("video_number") else None,
                    video_artifact=repository(request).artifact_ref(artifact.id),
                    duration_sec=float(row.get("duration_sec", 0)),
                    qc_status=str(row.get("qc_status", "pending")),
                )
                repository(request).finished_videos[finished.id] = finished
            elif payload.import_type == "video_version":
                version = c.VideoVersion(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    script_version_id=str(row.get("script_version_id")) if row.get("script_version_id") else None,
                    finished_video_id=str(row.get("finished_video_id")) if row.get("finished_video_id") else None,
                    timeline_plan_artifact_id=str(row.get("timeline_plan_artifact_id", "imported")),
                    style_plan_artifact_id=str(row.get("style_plan_artifact_id", "imported")),
                )
                repository(request).video_versions[version.id] = version
            elif payload.import_type == "publish_record":
                record = c.PublishRecord(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    video_version_id=str(row.get("video_version_id")) if row.get("video_version_id") else None,
                    platform=str(row.get("platform", "manual")),
                    status=str(row.get("status", "published")),
                    published_at=c.utcnow(),
                )
                repository(request).publish_records[record.id] = record
            elif payload.import_type == "performance":
                obs = c.PerformanceObservation(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    publish_record_id=str(row.get("publish_record_id", "manual")),
                    metric_name=str(row.get("metric_name", "views")),
                    metric_value=float(row.get("metric_value", 0)),
                )
                repository(request).performance_observations[obs.id] = obs
            elif payload.import_type == "prompt_seed":
                template = c.PromptTemplate(
                    id=internal_id,
                    name=str(row.get("name", "Imported prompt")),
                    purpose=str(row.get("purpose", "imported")),
                    variables_schema_ref=c.PromptSchemaRef(schema_id=str(row.get("variables_schema_id", "imported.variables"))),
                    output_schema_ref=c.PromptSchemaRef(schema_id=str(row.get("output_schema_id", "imported.output"))),
                    status="active",
                )
                version = c.PromptVersion(
                    id=new_id("pver"),
                    prompt_template_id=template.id,
                    content=str(row.get("content", "")),
                    status="published",
                    approved_at=c.utcnow(),
                    published_at=c.utcnow(),
                )
                repository(request).prompt_templates[template.id] = template
                repository(request).prompt_versions[version.id] = version
            elif payload.import_type == "provider_price":
                catalog = c.ProviderPriceCatalog(
                    id=internal_id,
                    provider_id=str(row.get("provider_id", "sandbox")),
                    status="published",
                    currency=str(row.get("currency", "CNY")),
                )
                repository(request).price_catalogs[catalog.id] = catalog
        created += 1
        results.append(
            c.ImportRowResult(
                row_index=index,
                status="created",
                external_id=str(row.get("external_id")) if row.get("external_id") else None,
                internal_id=internal_id,
            )
        )
    report = c.ImportBatchReport(
        batch_id=new_id("imp"),
        import_type=payload.import_type,
        status=c.ImportBatchStatus.completed if failed == 0 else c.ImportBatchStatus.partially_failed,
        created_count=created,
        skipped_count=skipped,
        failed_count=failed,
        results=results,
        request_id=request_id(),
    )
    repository(request).import_reports[report.batch_id] = report
    return report


def import_batch_detail(request: Request, batch_id: str) -> c.ImportBatchReport:

    if production_repository(request) is not None:
        report = production_repository(request).get_import_batch(batch_id)
        if report is not None:
            return report
    return repository(request).import_reports[batch_id]


def _create_imported_media_source_artifact(
    request: Request,
    *,
    row: dict,
    case_id: str,
    title: str,
    kind: str,
    uri: str,
    sha256: str | None,
) -> c.Artifact:
    probed = _probe_import_media_if_local(request, uri)
    content_type = (
        _optional_str(row.get("mime"))
        or (probed.mime_type if probed is not None else None)
        or mimetypes.guess_type(uri)[0]
        or "application/octet-stream"
    )
    duration_sec = (
        probed.duration_sec
        if probed is not None and probed.duration_sec is not None
        else _optional_float(row.get("duration_sec"))
    )
    width = probed.width if probed is not None and probed.width is not None else _optional_int(row.get("width"))
    height = probed.height if probed is not None and probed.height is not None else _optional_int(row.get("height"))
    media_info = probed or _media_info_from_import_metadata(
        uri=uri,
        kind=kind,
        content_type=content_type,
        duration_sec=duration_sec,
        width=width,
        height=height,
    )
    payload = {
        "upload_session_id": None,
        "filename": _filename_from_uri(uri, fallback=title),
        "content_type": content_type,
        "size_bytes": _optional_int(row.get("size_bytes")) or 0,
        "object_uri": uri,
        "sha256": sha256,
        "metadata": {
            "case_id": case_id,
            "title": title,
            "kind": kind,
            "duration_sec": duration_sec if duration_sec is not None else 0,
            "width": width,
            "height": height,
        },
    }
    return repository(request).create_artifact(
        kind=c.ArtifactKind.uploaded_file,
        payload_schema="UploadedFileArtifact.v1",
        payload=payload,
        case_id=case_id,
        uri=uri,
        sha256=sha256,
        media_info=media_info,
    )


def _find_existing_imported_media_asset(
    request: Request,
    *,
    case_id: str,
    kind: str,
    sha256: str | None,
    uri: str,
) -> c.MediaAssetRecord | None:
    for asset in repository(request).media_assets.values():
        if asset.case_id != case_id or asset.kind != kind or asset.source_artifact_id is None:
            continue
        artifact = repository(request).artifacts.get(asset.source_artifact_id)
        if artifact is None:
            continue
        if sha256 and artifact.sha256 == sha256:
            return asset
        if not sha256 and artifact.uri == uri:
            return asset
    return None


def _probe_import_media_if_local(request: Request, uri: str) -> c.MediaInfo | None:
    if uri.startswith("s3://"):
        return None
    try:
        return probe_media(local_object_path(object_store(request), uri))
    except Exception:
        return None


def _media_info_from_import_metadata(
    *,
    uri: str,
    kind: str,
    content_type: str,
    duration_sec: float | None,
    width: int | None,
    height: int | None,
) -> c.MediaInfo | None:
    media_type = _media_type_from_metadata(kind, content_type)
    if media_type is None:
        return None
    suffix = Path(urlsplit(uri).path).suffix.lstrip(".")
    return c.MediaInfo(
        media_type=media_type,
        codec="unknown",
        format=suffix or content_type.split("/")[-1] or "unknown",
        mime_type=content_type,
        duration_sec=None if media_type == "image" else duration_sec,
        width=width,
        height=height,
    )


def _media_type_from_metadata(kind: str, content_type: str) -> str | None:
    if content_type.startswith("video/") or kind in {"portrait", "broll", "video"}:
        return "video"
    if content_type.startswith("audio/") or kind in {"bgm", "voice", "voice_reference"}:
        return "audio"
    if content_type.startswith("image/") or kind in {"image", "cover_template"}:
        return "image"
    return None


def _filename_from_uri(uri: str, *, fallback: str) -> str:
    filename = Path(unquote(urlsplit(uri).path)).name
    return filename or fallback or "imported-media"


def _optional_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
