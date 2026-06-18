from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import (
    AdoptScriptDraftRequest,
    CaseMemory,
    GenerateScriptWithMemoryRequest,
    ScriptDraft,
    ScriptVersion,
    utcnow,
)
from packages.core.storage.database import CaseMemoryRow, ScriptDraftRow, ScriptVersionRow
from packages.core.storage.repository import new_id
from packages.creative.cases.sqlalchemy_learning_mappers import (
    case_memory_row_to_contract,
    script_draft_row_to_contract,
    script_version_row_to_contract,
)


class SqlAlchemyCaseLearningRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def list_drafts(self, *, case_id: str, limit: int = 50) -> list[ScriptDraft]:
        with self.session_factory() as session:
            statement = (
                select(ScriptDraftRow)
                .where(ScriptDraftRow.case_id == case_id)
                .order_by(ScriptDraftRow.updated_at.desc())
                .limit(limit)
            )
            return [script_draft_row_to_contract(row) for row in session.scalars(statement)]

    def adopt_draft(
        self, *, case_id: str, draft_id: str, payload: AdoptScriptDraftRequest
    ) -> ScriptVersion | None:
        with self.session_factory() as session:
            draft = session.get(ScriptDraftRow, draft_id)
            if draft is None or draft.case_id != case_id:
                return None
            script = ScriptVersionRow(
                id=new_id("script"),
                case_id=case_id,
                title=payload.title or draft.title,
                script=payload.publish_content or draft.script,
                adopted_from_draft_id=draft.id,
            )
            draft.status = "adopted"
            draft.updated_at = utcnow()
            session.add(script)
            session.commit()
            session.refresh(script)
            return script_version_row_to_contract(script)

    def list_memory(self, *, case_id: str, limit: int = 50) -> list[CaseMemory]:
        with self.session_factory() as session:
            statement = (
                select(CaseMemoryRow)
                .where(CaseMemoryRow.case_id == case_id)
                .where(CaseMemoryRow.status == "active")
                .order_by(CaseMemoryRow.updated_at.desc())
                .limit(limit)
            )
            return [case_memory_row_to_contract(row) for row in session.scalars(statement)]

    def recent_script_texts(self, *, case_id: str, limit: int = 8) -> list[str]:
        with self.session_factory() as session:
            draft_rows = session.scalars(
                select(ScriptDraftRow)
                .where(ScriptDraftRow.case_id == case_id)
                .order_by(ScriptDraftRow.updated_at.desc())
                .limit(limit)
            )
            script_rows = session.scalars(
                select(ScriptVersionRow)
                .where(ScriptVersionRow.case_id == case_id)
                .order_by(ScriptVersionRow.updated_at.desc())
                .limit(limit)
            )
            rows = [(row.updated_at, row.script) for row in draft_rows]
            rows.extend((row.updated_at, row.script) for row in script_rows)
            rows.sort(key=lambda item: item[0], reverse=True)
            return _dedupe_texts([text for _, text in rows], limit=limit)

    def generate_script_with_memory(
        self,
        *,
        case_id: str,
        payload: GenerateScriptWithMemoryRequest,
        script_override: str | None = None,
        title_override: str | None = None,
    ) -> ScriptDraft:
        with self.session_factory() as session:
            memories = []
            for memory_id in payload.memory_ids:
                memory = session.get(CaseMemoryRow, memory_id)
                if memory is not None and memory.case_id == case_id and memory.status == "active":
                    memories.append(memory.insight)
            draft = ScriptDraftRow(
                id=new_id("draft"),
                case_id=case_id,
                title=title_override or "AI 生成脚本",
                script=script_override or f"{payload.brief}\n\n参考记忆：{' / '.join(memories) if memories else '暂无'}",
                status="draft",
                memory_ids=payload.memory_ids,
            )
            session.add(draft)
            session.commit()
            session.refresh(draft)
            return script_draft_row_to_contract(draft)


def _dedupe_texts(texts: list[str], *, limit: int) -> list[str]:
    results: list[str] = []
    seen: set[str] = set()
    for text in texts:
        normalized = " ".join(text.split()).strip()
        if not normalized:
            continue
        key = normalized[:100]
        if key in seen:
            continue
        seen.add(key)
        results.append(normalized)
        if len(results) >= limit:
            break
    return results
