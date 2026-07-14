from __future__ import annotations


from fastapi import APIRouter, Body, Request

from apps.api.dependencies import require_role
from apps.api.services import cases as service
from packages.core import contracts as c

router = APIRouter()

@router.get("/api/cases", response_model=c.PageResponse[c.CaseListItem])
def list_cases(
    request: Request,
    limit: int = 50,
    search: str | None = None,
    owner_user_id: str | None = None,
    industry: str | None = None,
) -> c.PageResponse[c.CaseListItem]:
    return service.list_cases(request, limit, search, owner_user_id, industry)


@router.post("/api/cases", response_model=c.CaseDetail, status_code=201)
def create_case(payload: c.CreateCaseRequest, request: Request) -> c.CaseDetail:
    user = require_role(request, c.UserRole.operator)
    return service.create_case(payload, request, user=user)


@router.get("/api/cases/{case_id}", response_model=c.CaseDetail)
def case_detail(request: Request, case_id: str) -> c.CaseDetail:

    return service.case_detail(request, case_id)


@router.patch("/api/cases/{case_id}", response_model=c.CaseDetail)
def patch_case(case_id: str, payload: c.PatchCaseRequest, request: Request) -> c.CaseDetail:
    require_role(request, c.UserRole.operator)
    return service.patch_case(case_id, payload, request)


@router.delete("/api/cases/{case_id}", response_model=c.OkResponse)
def delete_case(
    case_id: str,
    request: Request,
    payload: c.DeleteCaseRequest | None = Body(default=None),
) -> c.OkResponse:
    require_role(request, c.UserRole.operator)
    return service.delete_case(case_id, request)
