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

Both the in-memory ``Repository`` path and the production SQLAlchemy media-repo path
are wired here, so '重新分析/rerun' actually runs the gated V4 pipeline (sensors +
gated VLM -> AnnotationV4 canonical) on every deployment - the canonical is what
material planning consumes (Spec §12.2). Both keep the gated runner unit-testable
end to end with a mocked gateway and no network.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import Request

from apps.api.common import (
    media_repository,
    object_store,
    provider_repository,
    repository,
)
from apps.api.services.annotation_patch import build_projection
from packages.core import contracts as c
from packages.core.storage.repository import new_id
from packages.media.annotation import (
    BgmAnnotationResult,
    GatedAnnotationResult,
    SensorDeps,
    V4Config,
    annotate_asset,
    annotate_bgm,
    resolve_vlm_profile,
)
from packages.media.annotation.bgm import resolve_audio_profile
from packages.media.assets import local_object_path, store_file
from packages.media.video.ffmpeg import extract_audio_segment

logger = logging.getLogger("apps.api.services.asset_annotation")

# Asset kinds annotated through the audio (BGM) path rather than the visual VLM path.
_AUDIO_ANNOTATION_KINDS = frozenset({"bgm"})


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

    # BGM / audio assets are annotated through the audio path (objective features +
    # gated LLM semantics); the visual VLM path cannot annotate an audio asset.
    if asset.kind in _AUDIO_ANNOTATION_KINDS:
        return _run_bgm_annotation(request, repo, asset, payload)

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


def _run_bgm_annotation(
    request: Request,
    repo,
    asset: c.MediaAssetRecord,
    payload: c.RerunAnnotationRequest,
    *,
    feature_extractor=None,
) -> c.AnnotationRunResponse:
    """Annotate a BGM/audio asset: objective features + gated LLM semantics.

    Gates the paid ``llm.chat`` path behind a real profile + active secret. Without
    one (or with an unreadable source) it degrades to a features-only annotation and
    never fabricates semantics. ``feature_extractor`` is injectable for tests so no
    real ffmpeg / librosa runs.
    """
    gateway = request.app.state.provider_gateway
    explicit = (
        repo.provider_profiles.get(payload.provider_profile_id)
        if payload.provider_profile_id
        else None
    )
    candidates = [
        p for p in repo.provider_profiles.values() if p.capability == "audio.understanding"
    ]
    audio_profile = resolve_audio_profile(
        gateway,
        candidate_profiles=candidates,
        explicit_profile=explicit,
    )

    audio_path = _local_audio_path(request, repo, asset)
    duration = _asset_duration(repo, asset)
    audio_url_for_window = _bgm_audio_urlizer(request, audio_path)
    result = annotate_bgm(
        asset_id=asset.id,
        case_id=asset.case_id or "",
        audio_path=str(audio_path or ""),
        duration=duration,
        asset_title=asset.title,
        gateway=gateway,
        audio_profile=audio_profile,
        audio_url_for_window=audio_url_for_window,
        feature_extractor=feature_extractor,
    )

    _persist_bgm(repo, asset, result)
    status = "failed" if (result.llm_configured and _bgm_is_failed(result)) else "completed"
    return c.AnnotationRunResponse(asset_id=asset.id, run_id=None, status=status)


def _persist_bgm(repo, asset: c.MediaAssetRecord, result: BgmAnnotationResult) -> None:
    """Persist the BGM AnnotationV4 artifact + editor projection + asset status."""
    annotation = result.annotation
    canonical = annotation.model_dump(mode="json")
    artifact = repo.create_artifact(
        kind=c.ArtifactKind.material_annotation,
        payload_schema="AnnotationV4.v1",
        payload=canonical,
        case_id=asset.case_id,
    )
    is_failed = _bgm_is_failed(result)
    # A BGM annotation is usable when the real audio path produced at least one window.
    bgm_report = (
        canonical.get("quality_report", {}).get("bgm", {}) if isinstance(canonical, dict) else {}
    )
    usable = not is_failed and result.llm_configured and (
        bool(annotation.bgm_usage_windows) or bool(bgm_report.get("mood"))
    )
    projection = build_projection(
        annotation,
        asset,
        annotation_artifact_id=artifact.id,
        llm_configured=result.llm_configured,
    )
    projection["usable"] = usable
    projection["bgm"] = bgm_report
    repo.annotations[asset.id] = c.AnnotationEditorVm(
        asset=asset,
        etag=new_id("etag"),
        canonical=canonical,
        projection=projection,
        editable_paths=["/labels", "/usable", "/title", "/canonical/bgm_usage_windows"],
    )
    annotation_status = "annotation_failed" if is_failed else "annotated"
    repo.media_assets[asset.id] = asset.model_copy(
        update={"annotation_status": annotation_status, "usable": usable, "updated_at": c.utcnow()}
    )


def _bgm_is_failed(result: BgmAnnotationResult) -> bool:
    return result.annotation.meta.annotation_status == c.AnnotationStatus.failed


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
    # canonical-owns-projection (Spec §12.1): rebuild the editor projection from the
    # canonical AnnotationV4 so segments/quality_events are visible to the editor.
    projection = build_projection(
        annotation,
        asset,
        annotation_artifact_id=artifact.id,
        vlm_configured=result.vlm_configured,
    )
    projection["usable"] = usable
    repo.annotations[asset.id] = c.AnnotationEditorVm(
        asset=asset,
        etag=new_id("etag"),
        canonical=canonical,
        projection=projection,
        editable_paths=["/labels", "/usable", "/title"],
    )

    # Public asset status is deliberately coarser than VLM failure detail; keep
    # the specific VLM state in quality_report and the editor projection.
    if is_failed:
        annotation_status = "annotation_failed"
    else:
        annotation_status = "annotated"
    repo.media_assets[asset.id] = asset.model_copy(
        update={"annotation_status": annotation_status, "usable": usable, "updated_at": c.utcnow()}
    )


def run_sqlalchemy_asset_annotation(
    request: Request,
    asset_id: str,
    payload: c.RerunAnnotationRequest,
    *,
    sensor_deps: SensorDeps | None = None,
) -> c.AnnotationRunResponse | None:
    """Run a gated AnnotationV4 for a DB-backed asset and persist canonical + projection.

    This is the production '重新分析/rerun' path: it drives the SAME gated runner the
    in-memory path uses, then writes the AnnotationV4 canonical into AnnotationRow.canonical
    (schema ``AnnotationV4.v1``) + a ``material_annotation`` artifact, so material planning
    reads a real V4 annotation via ``annotation_v4_for_asset``. Without a real
    ``vlm.annotation`` profile + active secret (or without a readable source video) it
    degrades to a sensor-only ``vlm_unconfigured`` result (never fabricated semantics).

    Returns ``None`` when the asset is missing (router maps to 404).
    """
    media_repo = media_repository(request)
    asset = media_repo.asset_record(asset_id)
    if asset is None:
        return None
    gateway = request.app.state.provider_gateway

    # BGM / audio assets are annotated through the audio path (objective features +
    # gated LLM semantics); the visual VLM path cannot annotate an audio asset and
    # would clobber a real BGM annotation with a degraded/empty visual V4.
    if asset.kind in _AUDIO_ANNOTATION_KINDS:
        return _run_sqlalchemy_bgm_annotation(request, media_repo, asset, payload)

    provider_repo = provider_repository(request)
    candidates: list[c.ProviderProfile] = []
    explicit: c.ProviderProfile | None = None
    if provider_repo is not None:
        candidates = provider_repo.list_profiles(capability="vlm.annotation", limit=100)
        if payload.provider_profile_id:
            explicit = next((p for p in candidates if p.id == payload.provider_profile_id), None)
            if explicit is None:
                explicit = gateway.get_profile(payload.provider_profile_id)
    vlm_profile = resolve_vlm_profile(gateway, candidate_profiles=candidates, explicit_profile=explicit)

    video_path = _sqlalchemy_local_video_path(request, media_repo, asset_id)
    if vlm_profile is not None and video_path is None:
        logger.warning("[annotation] asset %s has no readable source video; degrading", asset_id)
        vlm_profile = None

    duration = media_repo.asset_source_duration(asset_id)
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

    annotation = result.annotation
    canonical = annotation.model_dump(mode="json")
    is_failed = _is_failed(result)
    usable = (not is_failed) and result.vlm_configured and bool(annotation.usage_windows)
    projection = build_projection(annotation, asset, vlm_configured=result.vlm_configured)
    projection["usable"] = usable
    annotation_status = "annotation_failed" if is_failed else "annotated"

    editor = media_repo.persist_annotation_v4(
        asset_id,
        canonical=canonical,
        projection=projection,
        annotation_status=annotation_status,
        usable=usable,
        case_id=asset.case_id,
    )
    if editor is None:
        return None
    status = "failed" if (result.vlm_configured and is_failed) else "completed"
    return c.AnnotationRunResponse(asset_id=asset_id, run_id=None, status=status)


def _run_sqlalchemy_bgm_annotation(
    request: Request,
    media_repo,
    asset: c.MediaAssetRecord,
    payload: c.RerunAnnotationRequest,
    *,
    feature_extractor=None,
) -> c.AnnotationRunResponse:
    """Annotate a DB-backed BGM/audio asset: objective features + gated LLM semantics.

    Mirrors the in-memory ``_run_bgm_annotation`` but resolves the source from the
    SQLAlchemy media repo and persists via the SAME ``persist_annotation_v4`` writer the
    visual rerun uses (AnnotationV4 canonical + ``material_annotation`` artifact + flip
    ``media_assets.annotation_status``). Gates the paid ``llm.chat`` path behind a real
    profile + active secret; without one (or with an unreadable source) it degrades to a
    features-only annotation and never fabricates semantics. Does NOT fall through to the
    visual ``annotate_asset`` path. ``feature_extractor`` is injectable for tests.
    """
    gateway = request.app.state.provider_gateway

    provider_repo = provider_repository(request)
    candidates: list[c.ProviderProfile] = []
    explicit: c.ProviderProfile | None = None
    if provider_repo is not None:
        candidates = provider_repo.list_profiles(capability="audio.understanding", limit=100)
        if payload.provider_profile_id:
            explicit = next((p for p in candidates if p.id == payload.provider_profile_id), None)
            if explicit is None:
                explicit = gateway.get_profile(payload.provider_profile_id)
    audio_profile = resolve_audio_profile(
        gateway,
        candidate_profiles=candidates,
        explicit_profile=explicit,
    )

    audio_path = _sqlalchemy_local_audio_path(request, media_repo, asset.id)
    duration = media_repo.asset_source_duration(asset.id)
    audio_url_for_window = _bgm_audio_urlizer(request, audio_path)
    result = annotate_bgm(
        asset_id=asset.id,
        case_id=asset.case_id or "",
        audio_path=str(audio_path or ""),
        duration=duration,
        asset_title=asset.title,
        gateway=gateway,
        audio_profile=audio_profile,
        audio_url_for_window=audio_url_for_window,
        feature_extractor=feature_extractor,
    )

    annotation = result.annotation
    canonical = annotation.model_dump(mode="json")
    is_failed = _bgm_is_failed(result)
    # A BGM annotation is usable when the real audio path produced at least one window.
    bgm_report = (
        canonical.get("quality_report", {}).get("bgm", {}) if isinstance(canonical, dict) else {}
    )
    usable = (not is_failed) and result.llm_configured and (
        bool(annotation.bgm_usage_windows) or bool(bgm_report.get("mood"))
    )
    # canonical-owns-projection (Spec §12.1): rebuild the editor projection from the
    # canonical AnnotationV4, then surface the BGM-specific fields the editor reads.
    projection = build_projection(annotation, asset, llm_configured=result.llm_configured)
    projection["usable"] = usable
    projection["bgm"] = bgm_report
    annotation_status = "annotation_failed" if is_failed else "annotated"

    editor = media_repo.persist_annotation_v4(
        asset.id,
        canonical=canonical,
        projection=projection,
        annotation_status=annotation_status,
        usable=usable,
        case_id=asset.case_id,
        editable_paths=["/labels", "/usable", "/title", "/canonical/bgm_usage_windows"],
    )
    if editor is None:
        return c.AnnotationRunResponse(asset_id=asset.id, run_id=None, status="failed")
    status = "failed" if (result.llm_configured and is_failed) else "completed"
    return c.AnnotationRunResponse(asset_id=asset.id, run_id=None, status=status)


def _bgm_audio_urlizer(request: Request, local_audio_path):
    def audio_url_for_window(start: float, end: float) -> str | None:
        if local_audio_path is None:
            return None
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
                tmp_path = Path(tmp_file.name)
            extract_audio_segment(local_audio_path, start, end, tmp_path)
            store = object_store(request)
            stored = store_file(store, tmp_path, purpose="bgm-clip")
            return store.signed_url(stored.ref.uri).url
        except Exception:
            return None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)

    return audio_url_for_window


def _sqlalchemy_local_video_path(request: Request, media_repo, asset_id: str):
    """Resolve a local filesystem path for the DB asset's source video, or None."""
    source = media_repo.media_source_for_asset(asset_id)
    if source is None:
        return None
    uri, _media_info = source
    if not uri:
        return None
    try:
        path = local_object_path(object_store(request), uri)
    except ValueError:
        return None
    return path if path.exists() else None


# BGM source resolution shares the same artifact-uri -> local-path resolution as
# video; the only difference is intent (audio vs. video), so it reuses the helper.
def _sqlalchemy_local_audio_path(request: Request, media_repo, asset_id: str):
    """Resolve a local filesystem path for the DB BGM/audio asset's source, or None."""
    return _sqlalchemy_local_video_path(request, media_repo, asset_id)


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


# BGM source resolution shares the same artifact-uri -> local-path resolution as
# video; the only difference is intent (audio vs. video), so it reuses the helper.
def _local_audio_path(request: Request, repo, asset: c.MediaAssetRecord):
    """Resolve a local filesystem path for the BGM/audio asset's source, or None."""
    return _local_video_path(request, repo, asset)


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
