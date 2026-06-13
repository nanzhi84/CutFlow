"""Asset-annotation runner wiring (gated VLM -> AnnotationV4 artifact).

This is the API-side glue that drives the media-domain annotation runner
(:func:`packages.media.annotation.annotate_asset`) for a single media asset:

1. gate the paid VLM path behind a real ``vlm.annotation`` profile + active secret
   (explicit profile from the request, else the first usable one);
2. resolve a local video path from the asset's source artifact;
3. run sensors + (gated) per-window VLM -> :class:`AnnotationV4`;
4. persist the AnnotationV4 as an artifact via the existing artifact store;
5. project it into the annotation editor (canonical/projection) and update the asset.

Without a real profile (or without a readable source video) it DEGRADES: the run
still completes, but the annotation is sensor-only with ``vlm_status=vlm_unconfigured``
and empty semantics - it never fabricates labels.

Only the in-memory ``Repository`` path is wired here (the SQLAlchemy media repo keeps
its existing lightweight rerun); this keeps the gated runner unit-testable end to end
with a mocked gateway and no network.
"""

from __future__ import annotations

import logging

from fastapi import Request

from apps.api.common import object_store, repository
from packages.core import contracts as c
from packages.core.storage.repository import new_id
from packages.media.annotation import (
    GatedAnnotationResult,
    SensorDeps,
    V4Config,
    annotate_asset,
    resolve_vlm_profile,
)
from packages.media.assets import local_object_path

logger = logging.getLogger("apps.api.services.asset_annotation")


def run_inmemory_asset_annotation(
    request: Request,
    asset_id: str,
    payload: c.RerunAnnotationRequest,
    *,
    sensor_deps: SensorDeps | None = None,
) -> c.AnnotationRunResponse:
    """Run a gated AnnotationV4 for an in-memory asset and persist it.

    Returns ``completed`` (real or degraded) or ``failed`` (the VLM pipeline exhausted
    its retries). ``sensor_deps`` is injectable so tests run with mock sensors.
    """
    repo = repository(request)
    asset = repo.media_assets[asset_id]
    gateway = request.app.state.provider_gateway

    explicit = repo.provider_profiles.get(payload.provider_profile_id) if payload.provider_profile_id else None
    candidates = [p for p in repo.provider_profiles.values() if p.capability == "vlm.annotation"]
    vlm_profile = resolve_vlm_profile(gateway, candidate_profiles=candidates, explicit_profile=explicit)

    video_path = _local_video_path(request, repo, asset)
    if vlm_profile is not None and video_path is None:
        # A real profile exists but the source video is unreadable: we cannot run the
        # paid VLM path. Degrade rather than burn a call on a missing file.
        logger.warning("[annotation] asset %s has no readable source video; degrading", asset_id)
        vlm_profile = None

    duration = _asset_duration(repo, asset)
    result = annotate_asset(
        asset_id=asset.id,
        case_id=asset.case_id,
        material_type=asset.kind,
        video_path=str(video_path or ""),
        duration=duration,
        gateway=gateway,
        vlm_profile=vlm_profile,
        cfg=V4Config(),
        sensor_deps=sensor_deps,
    )

    _persist(request, repo, asset, result)
    status = "failed" if (result.vlm_configured and _is_failed(result)) else "completed"
    return c.AnnotationRunResponse(asset_id=asset_id, run_id=None, status=status)


def _persist(
    request: Request,
    repo,
    asset: c.MediaAssetRecord,
    result: GatedAnnotationResult,
) -> None:
    """Persist the AnnotationV4 artifact + project it into the editor + update the asset."""
    annotation = result.annotation
    canonical = annotation.model_dump(mode="json")

    artifact = repo.create_artifact(
        kind=c.ArtifactKind.material_annotation,
        payload_schema="AnnotationV4.v1",
        payload=canonical,
        case_id=asset.case_id,
    )

    is_failed = _is_failed(result)
    usable = not is_failed and result.vlm_configured and bool(annotation.usage_windows)
    repo.annotations[asset.id] = c.AnnotationEditorVm(
        asset=asset,
        etag=new_id("etag"),
        canonical=canonical,
        projection={
            "title": asset.title,
            "usable": usable,
            "annotation_artifact_id": artifact.id,
            "vlm_configured": result.vlm_configured,
            "annotation_status": annotation.meta.annotation_status.value,
        },
        editable_paths=["/labels", "/usable", "/title"],
    )

    # The typed MediaAssetRecord.annotation_status is constrained to the public
    # contract enum (pending/annotated/annotation_failed) and serializes to typed
    # API clients via GET /api/media/assets[/{id}]. The degraded "unconfigured"
    # case is a failed run for that field's purposes; the precise vlm_unconfigured
    # reason is preserved in AnnotationV4.quality_report["vlm_status"] and the
    # editor projection's vlm_configured flag above -- never in this enum field.
    if is_failed:
        annotation_status = "annotation_failed"
    else:
        annotation_status = "annotated"
    repo.media_assets[asset.id] = asset.model_copy(
        update={"annotation_status": annotation_status, "usable": usable, "updated_at": c.utcnow()}
    )


def _is_failed(result: GatedAnnotationResult) -> bool:
    return result.annotation.meta.annotation_status == c.AnnotationStatus.failed


def _local_video_path(request: Request, repo, asset: c.MediaAssetRecord):
    """Resolve a local filesystem path for the asset's source video, or None."""
    artifact_id = asset.source_artifact_id
    if not artifact_id:
        return None
    artifact = repo.artifacts.get(artifact_id)
    if artifact is None or not artifact.uri:
        return None
    try:
        path = local_object_path(object_store(request), artifact.uri)
    except ValueError:
        return None
    return path if path.exists() else None


def _asset_duration(repo, asset: c.MediaAssetRecord) -> float:
    """Best-effort duration from the source artifact's media_info (0.0 when unknown)."""
    artifact_id = asset.source_artifact_id
    if not artifact_id:
        return 0.0
    artifact = repo.artifacts.get(artifact_id)
    media_info = getattr(artifact, "media_info", None) if artifact is not None else None
    duration = getattr(media_info, "duration_sec", None) if media_info is not None else None
    try:
        return max(0.0, float(duration)) if duration is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
