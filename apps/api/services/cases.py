from __future__ import annotations


from fastapi import Request

from apps.api.common import (
    case_repository,
    get_case,
    page,
    repository,
    request_id,
)
from packages.core import contracts as c
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError

ACTIVE_RUN_STATUSES = {
    c.RunStatus.created,
    c.RunStatus.admitted,
    c.RunStatus.running,
    c.RunStatus.cancelling,
}
# Media-asset kinds that count toward a case's reusable material library (mirrors
# MATERIAL_ASSET_KINDS in the SQLAlchemy repository).
MATERIAL_ASSET_KINDS = {"portrait", "broll", "video", "bgm", "font"}

def list_cases(
    request: Request,
    limit: int = 50,
    search: str | None = None,
    owner_user_id: str | None = None,
    industry: str | None = None,
) -> c.PageResponse[c.CaseListItem]:
    if case_repository(request) is not None:
        values = case_repository(request).list_cases(
            search=search,
            owner_user_id=owner_user_id,
            industry=industry,
            limit=limit,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    repo = repository(request)
    values = list(repo.cases.values())
    if search:
        values = [case for case in values if search.lower() in case.name.lower()]
    if owner_user_id:
        values = [case for case in values if case.owner_user_id == owner_user_id]
    if industry:
        values = [case for case in values if case.industry == industry]
    items = [_with_counts(repo, case) for case in values]
    return page(items, limit)


def _with_counts(repo, case: c.CaseDetail) -> c.CaseListItem:
    """Project an in-memory CaseDetail to a CaseListItem with per-case counts.

    Mirrors the SQLAlchemy R6 count semantics: material/voice from media assets,
    scripts from script versions, quality from QC'd finished videos (a terminal
    ``qc_status`` of passed/failed/warning — ``pending`` videos are not yet QC'd).
    """
    case_id = case.id
    material_count = sum(
        1
        for asset in repo.media_assets.values()
        if asset.case_id == case_id and asset.kind in MATERIAL_ASSET_KINDS
    )
    voice_count = sum(
        1 for asset in repo.media_assets.values() if asset.case_id == case_id and asset.kind == "voice"
    )
    script_count = sum(1 for script in repo.scripts.values() if script.case_id == case_id)
    quality_count = sum(
        1
        for video in repo.finished_videos.values()
        if video.case_id == case_id and video.qc_status not in ("", "pending")
    )
    return c.CaseListItem(
        id=case.id,
        name=case.name,
        owner_user_id=case.owner_user_id,
        active_memory_count=case.active_memory_count,
        status=case.status,
        industry=case.industry,
        material_count=material_count,
        script_count=script_count,
        voice_count=voice_count,
        quality_count=quality_count,
        schema_version=case.schema_version,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


def create_case(payload: c.CreateCaseRequest, request: Request, user: c.AuthUser) -> c.CaseDetail:

    if case_repository(request) is not None:
        return case_repository(request).create_case(payload, owner_user_id=user.id)
    case = c.CaseDetail(id=new_id("case"), owner_user_id=user.id, **payload.model_dump())
    repository(request).cases[case.id] = case
    return case


def case_detail(request: Request, case_id: str) -> c.CaseDetail:

    return get_case(request, case_id)


def patch_case(case_id: str, payload: c.PatchCaseRequest, request: Request) -> c.CaseDetail:
    if case_repository(request) is not None:
        case = case_repository(request).patch_case(case_id, payload)
        if case is None:
            raise NodeExecutionError(c.ErrorCode.validation_missing_case, f"Case {case_id} does not exist.")
        return case
    get_case(request, case_id)
    return repository(request).patch(repository(request).cases, case_id, payload.model_dump(exclude_none=True))


def delete_case(case_id: str, request: Request) -> c.OkResponse:
    if case_repository(request) is not None:
        deleted = case_repository(request).delete_case(case_id)
        if deleted is None:
            raise NodeExecutionError(c.ErrorCode.validation_missing_case, f"Case {case_id} does not exist.")
        if not deleted:
            raise NodeExecutionError(
                c.ErrorCode.validation_conflict,
                "Case cannot be deleted while active runs or finished videos still reference it.",
            )
        return c.OkResponse(request_id=request_id())

    get_case(request, case_id)
    repo = repository(request)
    has_active_run = any(
        run.case_id == case_id and run.status in ACTIVE_RUN_STATUSES for run in repo.runs.values()
    )
    has_finished_video = any(video.case_id == case_id for video in repo.finished_videos.values())
    if has_active_run or has_finished_video:
        raise NodeExecutionError(
            c.ErrorCode.validation_conflict,
            "Case cannot be deleted while active runs or finished videos still reference it.",
        )
    del repo.cases[case_id]
    return c.OkResponse(request_id=request_id())
