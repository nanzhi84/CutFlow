from __future__ import annotations


from fastapi import Request

from apps.api.common import (
    case_repository,
    get_case,
    request_id,
)
from packages.core import contracts as c
from packages.core.workflow import NodeExecutionError


def list_cases(
    request: Request,
    limit: int = 50,
    search: str | None = None,
    owner_user_id: str | None = None,
    industry: str | None = None,
) -> c.PageResponse[c.CaseListItem]:
    values = case_repository(request).list_cases(
        search=search,
        owner_user_id=owner_user_id,
        industry=industry,
        limit=limit,
    )
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_case(payload: c.CreateCaseRequest, request: Request, user: c.AuthUser) -> c.CaseDetail:
    return case_repository(request).create_case(payload, owner_user_id=user.id)


def case_detail(request: Request, case_id: str) -> c.CaseDetail:

    return get_case(request, case_id)


def patch_case(case_id: str, payload: c.PatchCaseRequest, request: Request) -> c.CaseDetail:
    case = case_repository(request).patch_case(case_id, payload)
    if case is None:
        raise NodeExecutionError(c.ErrorCode.validation_missing_case, f"Case {case_id} does not exist.")
    return case


def delete_case(case_id: str, request: Request) -> c.OkResponse:
    deleted = case_repository(request).delete_case(case_id)
    if deleted is None:
        raise NodeExecutionError(c.ErrorCode.validation_missing_case, f"Case {case_id} does not exist.")
    if not deleted:
        raise NodeExecutionError(
            c.ErrorCode.validation_conflict,
            "Case cannot be deleted while active runs or finished videos still reference it.",
        )
    return c.OkResponse(request_id=request_id())
