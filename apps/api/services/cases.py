from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Request, Response, UploadFile
from fastapi.responses import JSONResponse

from apps.api.common import (
    auth,
    case_learning_repository,
    case_repository,
    ensure_artifact_ref,
    get_case,
    media_repository,
    object_store,
    ops_repository,
    page,
    production_repository,
    prompt_repository,
    provider_repository,
    publishing_repository,
    repository,
    request_id,
    secret_repository,
    secret_store,
    signed,
    upload_repository,
    workflow_runtime,
)
from apps.api.dependencies import SESSION_COOKIE, current_user, not_found_response
from packages.core import contracts as c
from packages.core.auth import SqlAlchemyAuthService
from packages.core.contracts.state_machines import assert_transition
from packages.core.observability import metric_snapshot
from packages.core.registration_codes import hash_registration_code
from packages.core.storage.object_store import parse_local_uri
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError

ACTIVE_RUN_STATUSES = {
    c.RunStatus.created,
    c.RunStatus.admitted,
    c.RunStatus.running,
    c.RunStatus.cancelling,
}

def list_cases(
    request: Request,
    limit: int = 50,
    search: str | None = None,
    owner_user_id: str | None = None,
) -> c.PageResponse[c.CaseListItem]:
    if case_repository(request) is not None:
        values = case_repository(request).list_cases(
            search=search,
            owner_user_id=owner_user_id,
            limit=limit,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    values = list(repository(request).cases.values())
    if search:
        values = [case for case in values if search.lower() in case.name.lower()]
    if owner_user_id:
        values = [case for case in values if case.owner_user_id == owner_user_id]
    return page(values, limit)


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
