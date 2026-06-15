"""Publishing node orchestration shared by the publish service.

Bridges the publishing domain nodes (copy / cover / platform adapter) to the API
runtime repository + object store. Keeps the route handlers thin and the heavy
wiring (script resolution, artifact registration, ffmpeg-backed cover) in one place.
"""

from __future__ import annotations

from fastapi import Request

from apps.api.common import object_store, repository
from packages.core import contracts as c
from packages.publishing import (
    PublishCopyContext,
    PublishPayload,
    derive_publish_copy,
    generate_publish_copy,
    generate_publish_cover,
    normalize_publish_tags,
    preview_cover_frame,
    select_adapter,
)
from packages.publishing.cover_node import CoverArtifact


def resolve_copy_context(repo, package: c.PublishPackage | None, item) -> PublishCopyContext:
    """Resolve the script + case context for the Publishing Copy Node from the
    item's publish package -> finished video -> run -> adopted ScriptVersion,
    falling back to the package title/description and the item's own title."""
    script = ""
    case_name = None
    description = item.description or ""
    case_id = getattr(package, "case_id", None)
    if package is not None:
        finished = (
            repo.finished_videos.get(package.source_finished_video_id)
            if package.source_finished_video_id
            else None
        )
        if finished is not None:
            run = repo.runs.get(finished.run_id) if getattr(finished, "run_id", None) else None
            script_version = None
            for candidate in repo.scripts.values():
                if candidate.case_id == finished.case_id:
                    script_version = candidate
            if script_version is not None:
                script = script_version.script or ""
            elif run is not None:
                script = getattr(getattr(run, "request", None), "script", "") or ""
            if not script:
                script = finished.title or ""
        case = repo.cases.get(case_id) if case_id else None
        if case is not None:
            case_name = getattr(case, "name", None)
            description = description or getattr(case, "description", "") or ""
    if not script:
        script = item.title or item.description or ""
    return PublishCopyContext(
        script=script,
        case_name=case_name,
        description=description,
    )


def run_copy_node(repo, package, item, *, title_limit: int | None = None):
    """Run the Publishing Copy Node for an item.

    No real ``llm.chat`` provider is wired into the API request path, so this uses
    the deterministic derivation (origin parity, honest non-fabricated copy). The
    LLM path + §2.3 schema hard-fail live in ``packages.publishing.copy_node`` and
    are used by the worker/production node + unit-tested directly.
    """
    context = resolve_copy_context(repo, package, item)
    if title_limit is not None:
        context = PublishCopyContext(
            script=context.script,
            case_name=context.case_name,
            description=context.description,
            title_limit=title_limit,
        )
    copy, source, invocation_id = generate_publish_copy(context, llm_chat=None)
    return copy, source, invocation_id


def _cover_artifact_writer(request: Request, *, run_id: str | None = None):
    repo = repository(request)

    def _write(*, uri: str, sha256: str, case_id: str | None) -> c.ArtifactRef:
        artifact = repo.create_artifact(
            kind=c.ArtifactKind.cover_image,
            payload_schema="uri-only",
            payload=None,
            case_id=case_id,
            run_id=run_id,
            uri=uri,
            sha256=sha256,
        )
        return repo.artifact_ref(artifact.id)

    return _write


def run_cover_node(
    request: Request,
    *,
    video_uri: str,
    mode: str,
    frame_time_sec: float,
    item,
    case_id: str | None,
) -> CoverArtifact:
    """Run the publishing Cover Node. No image provider is armed in the API path,
    so AI cover is unavailable and the node produces an honest frame cover, flagging
    cover.frame_fallback (§2.2) when mode='ai'."""
    return generate_publish_cover(
        object_store=object_store(request),
        video_uri=video_uri,
        write_artifact=_cover_artifact_writer(request),
        mode=mode,
        frame_time_sec=frame_time_sec,
        title=item.cover_title or item.title,
        description=item.publish_content or item.description,
        cover_subtitle=item.cover_subtitle,
        tags=tuple(item.tags or []),
        case_id=case_id,
        ai_cover=None,
    )


def run_preview_frame(
    request: Request,
    *,
    video_uri: str,
    frame_time_sec: float,
    case_id: str | None,
) -> c.ArtifactRef:
    return preview_cover_frame(
        object_store=object_store(request),
        video_uri=video_uri,
        frame_time_sec=frame_time_sec,
        write_artifact=_cover_artifact_writer(request),
        case_id=case_id,
    )


def build_publish_payload(
    item,
    *,
    package: c.PublishPackage | None,
    case_name: str | None,
    scheduled_at,
    manual_review: bool,
    simulate_failure: bool,
) -> PublishPayload:
    video_uri = getattr(getattr(package, "video_artifact", None), "uri", None)
    cover_uri = getattr(getattr(package, "cover_artifact", None), "uri", None)
    return PublishPayload(
        title=item.title,
        description=item.publish_content or item.description,
        platforms=(item.platform,),
        tags=tuple(normalize_publish_tags(item.tags or [])),
        location=item.location,
        account_group=item.account_group,
        case_name=case_name,
        scheduled_at=scheduled_at,
        video_uri=video_uri,
        cover_uri=cover_uri,
        manual_review=manual_review,
        simulate_failure=simulate_failure,
    )


def resolve_publish_adapter(explicit: str | None = None):
    return select_adapter(explicit)
