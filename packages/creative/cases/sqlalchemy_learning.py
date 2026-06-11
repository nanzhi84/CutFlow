from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import (
    AdoptScriptDraftRequest, CaseAgentRun, CaseAgentRunDetail, CaseAgentSourceBinding, CaseInsightCard,
    CaseKnowledgeResponse, CaseMemory, CaseMemoryScope, CreateSourceBindingRequest, CreativePattern,
    GenerateScriptWithMemoryRequest, ImportCaseSourceRequest, MemoryProposal, ReflectionRun, RunStatus,
    ScriptDraft, ScriptVersion, StartCaseAgentRunRequest, StartReflectionRunRequest, utcnow,
)
from packages.core.storage.database import (
    CaseAgentRunRow, CaseAgentSourceBindingRow, CaseMemoryRow, CreativeBriefRow, FinishedVideoRow,
    MemoryProposalRow, ReflectionRunRow, ScriptDraftRow, ScriptVersionRow, VideoVersionRow,
)
from packages.creative.cases.sqlalchemy_learning_mappers import (
    case_agent_run_row_to_contract, case_memory_row_to_contract, creative_brief_row_to_contract,
    memory_proposal_row_to_contract, reflection_run_row_to_contract, script_draft_row_to_contract,
    script_version_row_to_contract, source_binding_row_to_contract, video_version_row_to_contract,
)
from packages.core.storage.repository import new_id
from packages.core.contracts.state_machines import assert_transition


class SqlAlchemyCaseLearningRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def list_source_bindings(self, *, case_id: str, limit: int = 50) -> list[CaseAgentSourceBinding]:
        with self.session_factory() as session:
            statement = (
                select(CaseAgentSourceBindingRow)
                .where(CaseAgentSourceBindingRow.case_id == case_id)
                .order_by(CaseAgentSourceBindingRow.updated_at.desc())
                .limit(limit)
            )
            return [source_binding_row_to_contract(row) for row in session.scalars(statement)]

    def create_source_binding(
        self, *, case_id: str, payload: CreateSourceBindingRequest
    ) -> CaseAgentSourceBinding:
        with self.session_factory() as session:
            row = CaseAgentSourceBindingRow(
                id=new_id("src"),
                case_id=case_id,
                source_type=payload.source_type,
                source_ref=payload.source_ref,
                title=payload.title,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return source_binding_row_to_contract(row)

    def delete_source_binding(self, *, case_id: str, binding_id: str) -> bool:
        with self.session_factory() as session:
            row = session.get(CaseAgentSourceBindingRow, binding_id)
            if row is None or row.case_id != case_id:
                return False
            session.delete(row)
            session.commit()
            return True

    def import_case_source(self, *, case_id: str, payload: ImportCaseSourceRequest) -> CaseAgentRun | None:
        with self.session_factory() as session:
            binding = session.get(CaseAgentSourceBindingRow, payload.source_binding_id)
            if binding is None or binding.case_id != case_id:
                return None
            run = self._create_run_row(
                case_id=case_id,
                goal="brief",
                source_binding_ids=[payload.source_binding_id],
            )
            session.add(run)
            session.add(
                CreativeBriefRow(
                    id=new_id("brief"),
                    case_id=case_id,
                    summary="Imported source summary.",
                    source_binding_ids=[payload.source_binding_id],
                )
            )
            session.commit()
            session.refresh(run)
            return case_agent_run_row_to_contract(run)

    def start_agent_run(self, *, case_id: str, payload: StartCaseAgentRunRequest) -> CaseAgentRun:
        with self.session_factory() as session:
            run = self._create_run_row(
                case_id=case_id,
                goal=payload.goal,
                source_binding_ids=payload.source_binding_ids,
            )
            session.add(run)
            if payload.goal == "script_draft":
                session.add(
                    ScriptDraftRow(
                        id=new_id("draft"),
                        case_id=case_id,
                        title="Agent generated draft",
                        script="开场提出痛点。展示解决方案。收束到行动建议。",
                        status="draft",
                        memory_ids=[],
                    )
                )
            elif payload.goal == "memory_proposal":
                session.add(
                    MemoryProposalRow(
                        id=new_id("mem"),
                        case_id=case_id,
                        status="proposed",
                        scope=CaseMemoryScope().model_dump(mode="json"),
                        insight="Short hooks with concrete outcomes perform better for this case.",
                        evidence=[],
                        confidence=0.5,
                        proposed_by_reflection_run_id=run.id,
                    )
                )
            session.commit()
            session.refresh(run)
            return case_agent_run_row_to_contract(run)

    def list_agent_runs(self, *, case_id: str, limit: int = 50) -> list[CaseAgentRun]:
        with self.session_factory() as session:
            statement = (
                select(CaseAgentRunRow)
                .where(CaseAgentRunRow.case_id == case_id)
                .order_by(CaseAgentRunRow.updated_at.desc())
                .limit(limit)
            )
            return [case_agent_run_row_to_contract(row) for row in session.scalars(statement)]

    def agent_run_detail(self, *, case_id: str, run_id: str) -> CaseAgentRunDetail | None:
        with self.session_factory() as session:
            run = session.get(CaseAgentRunRow, run_id)
            if run is None or run.case_id != case_id:
                return None
            return CaseAgentRunDetail(
                run=case_agent_run_row_to_contract(run),
                briefs=[
                    creative_brief_row_to_contract(row)
                    for row in session.scalars(
                        select(CreativeBriefRow).where(CreativeBriefRow.case_id == case_id)
                    )
                ],
                drafts=[
                    script_draft_row_to_contract(row)
                    for row in session.scalars(select(ScriptDraftRow).where(ScriptDraftRow.case_id == case_id))
                ],
                memory_proposals=[
                    memory_proposal_row_to_contract(row)
                    for row in session.scalars(
                        select(MemoryProposalRow).where(MemoryProposalRow.case_id == case_id)
                    )
                ],
            )

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

    def list_memory_proposals(self, *, case_id: str, limit: int = 50) -> list[MemoryProposal]:
        with self.session_factory() as session:
            statement = (
                select(MemoryProposalRow)
                .where(MemoryProposalRow.case_id == case_id)
                .order_by(MemoryProposalRow.updated_at.desc())
                .limit(limit)
            )
            return [memory_proposal_row_to_contract(row) for row in session.scalars(statement)]

    def knowledge(self, *, case_id: str) -> CaseKnowledgeResponse:
        with self.session_factory() as session:
            memories = [
                case_memory_row_to_contract(row)
                for row in session.scalars(
                    select(CaseMemoryRow)
                    .where(CaseMemoryRow.case_id == case_id)
                    .where(CaseMemoryRow.status == "active")
                    .order_by(CaseMemoryRow.updated_at.desc())
                )
            ]
            scripts = [
                script_version_row_to_contract(row)
                for row in session.scalars(
                    select(ScriptVersionRow)
                    .where(ScriptVersionRow.case_id == case_id)
                    .order_by(ScriptVersionRow.updated_at.desc())
                    .limit(10)
                )
            ]
            videos = [
                video_version_row_to_contract(row)
                for row in session.scalars(
                    select(VideoVersionRow)
                    .where(VideoVersionRow.case_id == case_id)
                    .order_by(VideoVersionRow.updated_at.desc())
                    .limit(10)
                )
            ]
            return CaseKnowledgeResponse(
                case_id=case_id,
                memories=memories,
                recent_script_versions=scripts,
                recent_video_versions=videos,
            )

    def list_memory(self, *, case_id: str, limit: int = 50) -> list[CaseMemory]:
        with self.session_factory() as session:
            statement = (
                select(CaseMemoryRow)
                .where(CaseMemoryRow.case_id == case_id)
                .order_by(CaseMemoryRow.updated_at.desc())
                .limit(limit)
            )
            return [case_memory_row_to_contract(row) for row in session.scalars(statement)]

    def approve_memory(self, *, case_id: str, memory_id: str) -> CaseMemory | None:
        with self.session_factory() as session:
            memory = session.get(CaseMemoryRow, memory_id)
            if memory is not None:
                if memory.case_id != case_id:
                    return None
                if memory.status == "proposed":
                    assert_transition("case_memory", memory.status, "approved")
                    memory.status = "approved"
                assert_transition("case_memory", memory.status, "active")
                memory.status = "active"
                memory.updated_at = utcnow()
                session.commit()
                session.refresh(memory)
                return case_memory_row_to_contract(memory)

            proposal = session.get(MemoryProposalRow, memory_id)
            if proposal is None or proposal.case_id != case_id:
                return None
            assert_transition("case_memory", proposal.status, "approved")
            proposal.status = "approved"
            assert_transition("case_memory", proposal.status, "active")
            proposal.updated_at = utcnow()
            memory = CaseMemoryRow(
                id=proposal.id,
                case_id=case_id,
                status="active",
                scope=proposal.scope,
                insight=proposal.insight,
                evidence=proposal.evidence,
                confidence=proposal.confidence,
            )
            session.add(memory)
            session.commit()
            session.refresh(memory)
            return case_memory_row_to_contract(memory)

    def reject_memory(self, *, case_id: str, memory_id: str) -> MemoryProposal | None:
        with self.session_factory() as session:
            proposal = session.get(MemoryProposalRow, memory_id)
            if proposal is None or proposal.case_id != case_id:
                return None
            proposal.status = "rejected"
            proposal.updated_at = utcnow()
            session.commit()
            session.refresh(proposal)
            return memory_proposal_row_to_contract(proposal)

    def start_reflection(self, *, case_id: str, payload: StartReflectionRunRequest) -> ReflectionRun:
        with self.session_factory() as session:
            reflection = ReflectionRunRow(
                id=new_id("refl"),
                case_id=case_id,
                status=RunStatus.succeeded.value,
                window=payload.window,
            )
            session.add(reflection)
            session.flush()
            session.add(
                MemoryProposalRow(
                    id=new_id("mem"),
                    case_id=case_id,
                    status="proposed",
                    scope=CaseMemoryScope().model_dump(mode="json"),
                    insight="Reuse the best performing hook style from recent videos.",
                    evidence=[reflection.id],
                    confidence=0.65,
                    proposed_by_reflection_run_id=reflection.id,
                )
            )
            session.commit()
            session.refresh(reflection)
            return reflection_run_row_to_contract(reflection)

    def insights(self, *, case_id: str, limit: int = 50) -> list[CaseInsightCard]:
        with self.session_factory() as session:
            proposals = list(
                session.scalars(
                    select(MemoryProposalRow)
                    .where(MemoryProposalRow.case_id == case_id)
                    .where(MemoryProposalRow.status == "proposed")
                )
            )
        return [
            CaseInsightCard(
                id=new_id("insight"),
                case_id=case_id,
                title="Memory proposals",
                body=f"{len(proposals)} proposal(s) waiting for review.",
            )
        ][:limit]

    def creative_patterns(self, *, case_id: str, limit: int = 50) -> list[CreativePattern]:
        with self.session_factory() as session:
            evidence_count = len(
                list(session.scalars(select(FinishedVideoRow).where(FinishedVideoRow.case_id == case_id)))
            )
        return [
            CreativePattern(
                id=new_id("pattern"),
                case_id=case_id,
                label="Concrete hook + short CTA",
                lift=None,
                evidence_count=evidence_count,
            )
        ][:limit]

    def generate_script_with_memory(
        self,
        *,
        case_id: str,
        payload: GenerateScriptWithMemoryRequest,
        script_override: str | None = None,
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
                title="Memory-guided draft",
                script=script_override or f"{payload.brief}\n\n参考记忆：{' / '.join(memories) if memories else '暂无'}",
                status="draft",
                memory_ids=payload.memory_ids,
            )
            session.add(draft)
            session.commit()
            session.refresh(draft)
            return script_draft_row_to_contract(draft)

    def _create_run_row(
        self,
        *,
        case_id: str,
        goal: str,
        source_binding_ids: list[str],
    ) -> CaseAgentRunRow:
        return CaseAgentRunRow(
            id=new_id("agent_run"),
            case_id=case_id,
            goal=goal,
            status=RunStatus.succeeded.value,
            source_binding_ids=source_binding_ids,
        )
