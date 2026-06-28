from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.core.contracts import (
    CASE_MATERIAL_ASSET_KINDS,
    CaseDetail,
    CaseListItem,
    CreateCaseRequest,
    PatchCaseRequest,
    utcnow,
)
from packages.core.storage.database import (
    CaseRow,
    FinishedVideoRow,
    JobRow,
    MediaAssetRow,
    ScriptVersionRow,
    WorkflowRunRow,
)
from packages.core.storage.base_repository import BaseRepository
from packages.core.storage.repository import new_id

ACTIVE_RUN_STATUSES = {"created", "admitted", "running", "cancelling"}
ACTIVE_JOB_STATUSES = {"draft", "queued", "running"}
MATERIAL_ASSET_KINDS = CASE_MATERIAL_ASSET_KINDS


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
        key_selling_points=list(row.key_selling_points or []),
        ip_persona=row.ip_persona,
        brand_voice=row.brand_voice,
        strategy_tags=list(row.strategy_tags or []),
        brand_keywords=list(row.brand_keywords or []),
        competitor_names=list(row.competitor_names or []),
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def case_row_to_list_item(row: CaseRow, counts: dict[str, int] | None = None) -> CaseListItem:
    counts = counts or {}
    return CaseListItem(
        id=row.id,
        name=row.name,
        owner_user_id=row.owner_user_id,
        active_memory_count=0,
        status=row.status,
        industry=row.industry,
        material_count=counts.get("material_count", 0),
        script_count=counts.get("script_count", 0),
        voice_count=counts.get("voice_count", 0),
        quality_count=counts.get("quality_count", 0),
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyCaseRepository(BaseRepository):
    def list_cases(
        self,
        *,
        search: str | None = None,
        owner_user_id: str | None = None,
        industry: str | None = None,
        limit: int = 50,
    ) -> list[CaseListItem]:
        with self.session_factory() as session:
            statement = select(CaseRow)
            if search:
                statement = statement.where(CaseRow.name.ilike(f"%{search}%"))
            if owner_user_id:
                statement = statement.where(CaseRow.owner_user_id == owner_user_id)
            if industry:
                statement = statement.where(CaseRow.industry == industry)
            statement = statement.order_by(CaseRow.updated_at.desc()).limit(limit)
            rows = list(session.scalars(statement))
            counts = self._counts_for_cases(session, [row.id for row in rows])
            return [case_row_to_list_item(row, counts.get(row.id)) for row in rows]

    @staticmethod
    def _counts_for_cases(session: Session, case_ids: list[str]) -> dict[str, dict[str, int]]:
        """Per-case material/script/voice/quality counts keyed by case id.

        FKs are uneven across the schema, so counts are derived per R6:
        - material_count: media assets whose kind is a reusable library kind.
        - voice_count: media assets whose kind == 'voice' (VoiceProfileRow has no case_id).
        - script_count: ScriptVersionRow rows for the case.
        - quality_count: QC'd finished videos — a terminal FinishedVideoRow.qc_status
          (passed/failed/warning); ``pending`` videos are not yet QC'd and excluded.
        """
        counts: dict[str, dict[str, int]] = {
            case_id: {"material_count": 0, "script_count": 0, "voice_count": 0, "quality_count": 0}
            for case_id in case_ids
        }
        if not case_ids:
            return counts

        material_rows = session.execute(
            select(MediaAssetRow.case_id, func.count())
            .where(
                MediaAssetRow.case_id.in_(case_ids),
                MediaAssetRow.kind.in_(MATERIAL_ASSET_KINDS),
            )
            .group_by(MediaAssetRow.case_id)
        )
        for case_id, count in material_rows:
            if case_id in counts:
                counts[case_id]["material_count"] = int(count)

        voice_rows = session.execute(
            select(MediaAssetRow.case_id, func.count())
            .where(MediaAssetRow.case_id.in_(case_ids), MediaAssetRow.kind == "voice")
            .group_by(MediaAssetRow.case_id)
        )
        for case_id, count in voice_rows:
            if case_id in counts:
                counts[case_id]["voice_count"] = int(count)

        script_rows = session.execute(
            select(ScriptVersionRow.case_id, func.count())
            .where(ScriptVersionRow.case_id.in_(case_ids))
            .group_by(ScriptVersionRow.case_id)
        )
        for case_id, count in script_rows:
            if case_id in counts:
                counts[case_id]["script_count"] = int(count)

        quality_rows = session.execute(
            select(FinishedVideoRow.case_id, func.count())
            .where(
                FinishedVideoRow.case_id.in_(case_ids),
                FinishedVideoRow.qc_status.notin_(("", "pending")),
            )
            .group_by(FinishedVideoRow.case_id)
        )
        for case_id, count in quality_rows:
            if case_id in counts:
                counts[case_id]["quality_count"] = int(count)

        return counts

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
                key_selling_points=list(payload.key_selling_points),
                ip_persona=payload.ip_persona,
                brand_voice=payload.brand_voice,
                strategy_tags=list(payload.strategy_tags),
                brand_keywords=list(payload.brand_keywords),
                competitor_names=list(payload.competitor_names),
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
