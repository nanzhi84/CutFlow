from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import CaseDetail, CaseListItem, CreateCaseRequest, PatchCaseRequest, utcnow
from packages.core.storage.database import CaseRow, FinishedVideoRow, JobRow, WorkflowRunRow
from packages.core.storage.repository import new_id

ACTIVE_RUN_STATUSES = {"created", "admitted", "running", "cancelling"}
ACTIVE_JOB_STATUSES = {"draft", "queued", "running"}


def case_row_to_detail(row: CaseRow) -> CaseDetail:
    return CaseDetail(
        id=row.id,
        name=row.name,
        owner_user_id=row.owner_user_id,
        active_memory_count=0,
        status=row.status,
        description=row.description,
        industry=row.industry,
        product=row.product,
        target_audience=row.target_audience,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def case_row_to_list_item(row: CaseRow) -> CaseListItem:
    return CaseListItem(
        id=row.id,
        name=row.name,
        owner_user_id=row.owner_user_id,
        active_memory_count=0,
        status=row.status,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyCaseRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def list_cases(
        self,
        *,
        search: str | None = None,
        owner_user_id: str | None = None,
        limit: int = 50,
    ) -> list[CaseListItem]:
        with self.session_factory() as session:
            statement = select(CaseRow)
            if search:
                statement = statement.where(CaseRow.name.ilike(f"%{search}%"))
            if owner_user_id:
                statement = statement.where(CaseRow.owner_user_id == owner_user_id)
            statement = statement.order_by(CaseRow.updated_at.desc()).limit(limit)
            return [case_row_to_list_item(row) for row in session.scalars(statement)]

    def get_case(self, case_id: str) -> CaseDetail | None:
        with self.session_factory() as session:
            row = session.get(CaseRow, case_id)
            return case_row_to_detail(row) if row is not None else None

    def create_case(self, payload: CreateCaseRequest, *, owner_user_id: str) -> CaseDetail:
        with self.session_factory() as session:
            row = CaseRow(
                id=new_id("case"),
                name=payload.name,
                owner_user_id=owner_user_id,
                status="active",
                description=payload.description,
                industry=payload.industry,
                product=payload.product,
                target_audience=payload.target_audience,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return case_row_to_detail(row)

    def delete_case(self, case_id: str) -> bool | None:
        with self.session_factory() as session:
            row = session.get(CaseRow, case_id)
            if row is None:
                return None
            if self._has_blocking_reference(session, case_id):
                return False
            session.delete(row)
            session.commit()
            return True

    def _has_blocking_reference(self, session: Session, case_id: str) -> bool:
        active_run = session.scalar(
            select(WorkflowRunRow.id)
            .where(WorkflowRunRow.case_id == case_id, WorkflowRunRow.status.in_(ACTIVE_RUN_STATUSES))
            .limit(1)
        )
        active_job = session.scalar(
            select(JobRow.id)
            .where(JobRow.case_id == case_id, JobRow.status.in_(ACTIVE_JOB_STATUSES))
            .limit(1)
        )
        finished_video = session.scalar(
            select(FinishedVideoRow.id).where(FinishedVideoRow.case_id == case_id).limit(1)
        )
        return active_run is not None or active_job is not None or finished_video is not None

    def patch_case(self, case_id: str, payload: PatchCaseRequest) -> CaseDetail | None:
        with self.session_factory() as session:
            row = session.get(CaseRow, case_id)
            if row is None:
                return None
            for key, value in payload.model_dump(exclude_none=True).items():
                setattr(row, key, value)
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return case_row_to_detail(row)
