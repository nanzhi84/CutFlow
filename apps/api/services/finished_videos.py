from __future__ import annotations

from pathlib import Path

from fastapi import Request

from apps.api.common import (
    object_store,
    page,
    production_repository,
    repository,
    request_id,
    signed,
)
from packages.core import contracts as c
from packages.core.workflow import NodeExecutionError
from packages.creative.cases import evolution
from packages.media.assets import local_object_path
from packages.production.editor_handoff import EditorHandoffAsset, EditorHandoffBuilder, EditorHandoffInput
from packages.production.jianying_draft import JianyingDraftBuilder, JianyingDraftInput


def performance_attribution(request: Request, video_version_id: str) -> c.PerformanceAttributionResponse:
    if production_repository(request) is not None:
        attribution = production_repository(request).performance_attribution(video_version_id)
        if attribution is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Video version is missing.")
        return attribution
    repo = repository(request)
    video = repo.video_versions[video_version_id]
    feature_vector = _extract_feature_vector(repo, video)
    return c.PerformanceAttributionResponse(
        video_version_id=video_version_id,
        feature_vector=feature_vector,
        observations=[item for item in repo.performance_observations.values() if item.case_id == video.case_id],
        contributing_memories=[
            item
            for item in repo.memories.values()
            if item.case_id == video.case_id and item.status == "active"
        ],
    )


def _extract_feature_vector(repo, video: c.VideoVersion) -> c.CreativeFeatureVector:
    """§25.5 ScriptFeatureExtraction + VideoFeatureExtraction over in-memory state."""
    feature_id = f"cfv_{video.id}"
    partial = None
    if video.script_version_id and video.script_version_id in repo.scripts:
        partial = evolution.extract_script_features(
            repo.scripts[video.script_version_id],
            case_id=video.case_id,
            feature_id=feature_id,
        )
    return evolution.extract_video_features(video, feature_id=feature_id, partial=partial)


def case_finished_videos(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.FinishedVideo]:
    if production_repository(request) is not None:
        values = production_repository(request).list_finished_videos(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).finished_videos.values() if item.case_id == case_id], limit)


def finished_video_detail(request: Request, id: str) -> c.FinishedVideoDetail:
    if production_repository(request) is not None:
        detail = production_repository(request).finished_video_detail(id)
        if detail is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
        return detail
    finished = repository(request).finished_videos[id]
    version = next(
        (item for item in repository(request).video_versions.values() if item.finished_video_id == id),
        None,
    )
    records = [item for item in repository(request).publish_records.values() if item.video_version_id == (version.id if version else None)]
    return c.FinishedVideoDetail(finished_video=finished, video_version=version, publish_records=records)


def finished_video_preview(request: Request, id: str) -> c.SignedUrlResponse:
    if production_repository(request) is not None:
        uri = production_repository(request).artifact_uri_for_finished_video(id)
        if uri is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
        if uri:
            return object_store(request).signed_url(uri).model_copy(update={"request_id": request_id()})
        return signed(request, f"finished-videos/{id}/preview.mp4")
    finished = repository(request).finished_videos[id]
    artifact = repository(request).artifacts.get(finished.video_artifact.artifact_id)
    if artifact and artifact.uri:
        return object_store(request).signed_url(artifact.uri).model_copy(update={"request_id": request_id()})
    return signed(request, f"finished-videos/{id}/preview.mp4")


def finished_video_download(request: Request, id: str) -> c.SignedUrlResponse:
    return finished_video_preview(request, id)


def delete_finished_video(id: str, request: Request, reason: str | None = None) -> c.OkResponse:
    if production_repository(request) is not None:
        case_id = _finished_video_case_id_db(request, id)
        _record_discard_reward(request, case_id, id, reason)
        production_repository(request).delete_finished_video(id)
        return c.OkResponse(request_id=request_id())
    finished = repository(request).finished_videos.get(id)
    case_id = finished.case_id if finished is not None else None
    _record_discard_reward(request, case_id, id, reason)
    repository(request).finished_videos.pop(id, None)
    return c.OkResponse(request_id=request_id())


def _finished_video_case_id_db(request: Request, finished_video_id: str) -> str | None:
    detail = production_repository(request).finished_video_detail(finished_video_id)
    return detail.finished_video.case_id if detail is not None else None


def _record_discard_reward(
    request: Request, case_id: str | None, finished_video_id: str, reason: str | None
) -> None:
    """Reward搭车: emit a video_discarded RewardSignal before deletion (§5.2). The
    reason drives the value (only ``script`` is a negative signal). Best-effort: the
    learning layer must never block the existing delete flow."""
    if case_id is None:
        return
    from apps.api.services import case_rubric

    try:
        case_rubric.record_discard_reward(request, case_id, finished_video_id, reason)
    except Exception:  # pragma: no cover - learning side-channel is best-effort
        pass


def editor_handoff(
    id: str, payload: c.CreateEditorHandoffRequest, request: Request
) -> c.EditorHandoffPackageArtifact:
    if production_repository(request) is not None:
        return production_repository(request).create_editor_handoff(id, payload)
    finished = _finished_video_or_error(request, id)
    handoff = EditorHandoffBuilder(object_store(request)).build(
        EditorHandoffInput(
            finished_video_id=id,
            package_format=payload.format,
            assets=_handoff_assets(request, finished),
        )
    )
    artifact = repository(request).create_artifact(
        kind=c.ArtifactKind.editor_handoff,
        payload_schema="EditorHandoffPackageArtifact.v1",
        payload=handoff.manifest,
        uri=handoff.package_uri,
        sha256=handoff.sha256,
    )
    return c.EditorHandoffPackageArtifact(
        package_artifact=repository(request).artifact_ref(artifact.id),
        manifest=handoff.manifest,
    )


def jianying_draft(
    id: str, payload: c.CreateJianyingDraftRequest, request: Request
) -> c.JianyingDraftPackageArtifact:
    if production_repository(request) is not None:
        return production_repository(request).create_jianying_draft(id, payload)
    finished = _finished_video_or_error(request, id)
    jianying = JianyingDraftBuilder(object_store(request)).build(
        JianyingDraftInput(
            finished_video_id=id,
            title=finished.title,
            video_path=_artifact_local_path(request, finished.video_artifact),
            audio_path=_latest_run_artifact_path(request, finished.run_id, c.ArtifactKind.audio_tts),
            subtitle_path=_artifact_local_path(request, finished.subtitle_artifact) if finished.subtitle_artifact else None,
            duration_sec=finished.duration_sec,
            template_id=payload.template_id,
            timeline_plan=_timeline_plan_payload(request, id),
            narration_units=_narration_units(request, finished.run_id),
        )
    )
    artifact = repository(request).create_artifact(
        kind=c.ArtifactKind.jianying_draft,
        payload_schema="JianyingDraftPackageArtifact.v1",
        payload=jianying.manifest,
        uri=jianying.package_uri,
        sha256=jianying.sha256,
    )
    return c.JianyingDraftPackageArtifact(
        package_artifact=repository(request).artifact_ref(artifact.id),
        draft_manifest=jianying.manifest,
    )


def _finished_video_or_error(request: Request, finished_video_id: str) -> c.FinishedVideo:
    finished = repository(request).finished_videos.get(finished_video_id)
    if finished is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
    return finished


def _artifact_local_path(request: Request, artifact_ref: c.ArtifactRef) -> Path:
    artifact = repository(request).artifacts.get(artifact_ref.artifact_id)
    if artifact is None or not artifact.uri:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Artifact URI is missing.")
    try:
        return local_object_path(object_store(request), artifact.uri)
    except ValueError as exc:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Artifact URI is not locally readable.") from exc


def _latest_run_artifact_path(request: Request, run_id: str | None, kind: c.ArtifactKind) -> Path | None:
    if run_id is None:
        return None
    artifact = next(
        (item for item in repository(request).artifacts.values() if item.run_id == run_id and item.kind == kind and item.uri),
        None,
    )
    if artifact is None:
        return None
    try:
        return local_object_path(object_store(request), artifact.uri)
    except ValueError as exc:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, f"{kind.value} is not locally readable.") from exc


def _timeline_plan_payload(request: Request, finished_video_id: str) -> dict | None:
    version = next(
        (item for item in repository(request).video_versions.values() if item.finished_video_id == finished_video_id),
        None,
    )
    if version is None:
        return None
    artifact = repository(request).artifacts.get(version.timeline_plan_artifact_id)
    return artifact.payload if artifact is not None and isinstance(artifact.payload, dict) else None


def _narration_units(request: Request, run_id: str | None) -> list[dict]:
    if run_id is None:
        return []
    artifact = next(
        (
            item
            for item in repository(request).artifacts.values()
            if item.run_id == run_id and item.kind == c.ArtifactKind.narration_units and isinstance(item.payload, dict)
        ),
        None,
    )
    payload = artifact.payload if artifact is not None else {}
    units = payload.get("units") if isinstance(payload, dict) else None
    return list(units or [])


def _handoff_assets(request: Request, finished: c.FinishedVideo) -> list[EditorHandoffAsset]:
    assets = [_handoff_asset(request, "video", finished.video_artifact)]
    if finished.cover_artifact:
        assets.append(_handoff_asset(request, "cover", finished.cover_artifact))
    if finished.subtitle_artifact:
        assets.append(_handoff_asset(request, "subtitle", finished.subtitle_artifact))
    return assets


def _handoff_asset(request: Request, role: str, artifact_ref: c.ArtifactRef) -> EditorHandoffAsset:
    return EditorHandoffAsset(
        role=role,
        artifact_id=artifact_ref.artifact_id,
        kind=artifact_ref.kind.value,
        source_path=_artifact_local_path(request, artifact_ref),
    )
