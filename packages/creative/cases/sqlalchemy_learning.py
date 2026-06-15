from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import (
    AdoptScriptDraftRequest, CaseAgentRun, CaseAgentRunDetail, CaseAgentSourceBinding, CaseInsightCard,
    CaseKnowledgeResponse, CaseMemory, CaseMemoryScope, CreateSourceBindingRequest, CreativePattern,
    GenerateScriptWithMemoryRequest, ImportCaseSourceRequest, MemoryProposal, MemoryRecallQuery,
    MemoryRecallResponse, PerformanceObservation, ReflectionRun, RunStatus, ScriptDraft, ScriptVersion,
    StartCaseAgentRunRequest, StartReflectionRunRequest, utcnow,
)
from packages.core.storage.database import (
    CaseAgentRunRow, CaseAgentSourceBindingRow, CaseMemoryRow, CreativeBriefRow, FinishedVideoRow,
    MemoryProposalRow, PerformanceObservationRow, PerformanceScoreRow, ReflectionRunRow, ScriptDraftRow,
    ScriptVersionRow, VideoVersionRow,
)
import packages.creative.cases.evolution as evolution
from packages.creative.cases.sqlalchemy_learning_mappers import (
    case_agent_run_row_to_contract, case_memory_row_to_contract, creative_brief_row_to_contract,
    memory_proposal_row_to_contract, reflection_run_row_to_contract, script_draft_row_to_contract,
    script_version_row_to_contract, source_binding_row_to_contract, video_version_row_to_contract,
)
from packages.core.storage.repository import new_id
from packages.core.contracts.state_machines import assert_transition


@dataclass
class BriefFields:
    """Synthesized CreativeBrief content for an imported case source (§32.4).

    The async service layer extracts these from the bound source content
    (via reference_extract) and hands them to the sync import_case_source method.
    """

    summary: str
    topic: str | None = None
    audience: str | None = None
    key_insights: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)


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

    def get_source_binding(self, *, case_id: str, binding_id: str) -> CaseAgentSourceBinding | None:
        with self.session_factory() as session:
            row = session.get(CaseAgentSourceBindingRow, binding_id)
            if row is None or row.case_id != case_id:
                return None
            return source_binding_row_to_contract(row)

    def delete_source_binding(self, *, case_id: str, binding_id: str) -> bool:
        with self.session_factory() as session:
            row = session.get(CaseAgentSourceBindingRow, binding_id)
            if row is None or row.case_id != case_id:
                return False
            session.delete(row)
            session.commit()
            return True

    def import_case_source(
        self,
        *,
        case_id: str,
        payload: ImportCaseSourceRequest,
        brief_fields: BriefFields | None = None,
    ) -> CaseAgentRun | None:
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
            fields = brief_fields or BriefFields(summary=binding.title or binding.source_ref)
            session.add(
                CreativeBriefRow(
                    id=new_id("brief"),
                    case_id=case_id,
                    summary=fields.summary,
                    source_binding_ids=[payload.source_binding_id],
                    topic=fields.topic,
                    audience=fields.audience,
                    key_insights=list(fields.key_insights),
                    source_refs=list(fields.source_refs),
                    generated_by_run_id=run.id,
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
                for proposal in self._derive_memory_proposals(session, case_id=case_id, run_id=run.id):
                    session.add(proposal)
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

    def recall_memory(self, *, case_id: str, query: MemoryRecallQuery) -> MemoryRecallResponse:
        """§25.8 memory recall with scope/validity-window filter and ranking."""
        with self.session_factory() as session:
            memories = [
                case_memory_row_to_contract(row)
                for row in session.scalars(
                    select(CaseMemoryRow)
                    .where(CaseMemoryRow.case_id == case_id)
                    .where(CaseMemoryRow.status == "active")
                )
            ]
            score_lookup = self._performance_scope_scores(session, case_id)
        recalled = evolution.filter_recall_memories(
            memories,
            mode=query.mode,
            topic=query.topic,
            platform=query.platform,
            memory_type=query.memory_type,
            scope_key=query.scope_key,
            limit=query.limit,
            score_lookup=score_lookup,
        )
        return MemoryRecallResponse(case_id=case_id, mode=query.mode, memories=recalled)

    def performance_scope_scores(self, *, case_id: str) -> dict[str, float]:
        with self.session_factory() as session:
            return self._performance_scope_scores(session, case_id)

    def _performance_scope_scores(self, session: Session, case_id: str) -> dict[str, float]:
        lookup: dict[str, float] = {}
        for row in session.scalars(
            select(PerformanceScoreRow)
            .where(PerformanceScoreRow.case_id == case_id)
            .where(PerformanceScoreRow.excluded_reason.is_(None))
        ):
            key = row.platform or row.video_version_id or row.observation_id
            lookup[key] = max(lookup.get(key, 0.0), row.normalized_score)
        return lookup

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
                memory_type=proposal.memory_type,
                scope=proposal.scope,
                scope_key=proposal.scope_key,
                insight=proposal.insight,
                evidence=proposal.evidence,
                confidence=proposal.confidence,
                sample_size=proposal.sample_size,
                supersedes_memory_id=proposal.supersedes_memory_id,
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
            observations = self._load_observations(session, case_id)
            reflection = ReflectionRunRow(
                id=new_id("refl"),
                case_id=case_id,
                status=RunStatus.succeeded.value,
                window=payload.window,
                input_observation_ids=[obs.id for obs in observations],
                input_feature_vector_ids=[],
                memory_proposal_ids=[],
                sample_size=len(observations),
            )
            session.add(reflection)
            session.flush()
            proposal_rows = self._derive_memory_proposals(
                session, case_id=case_id, run_id=reflection.id, observations=observations
            )
            for row in proposal_rows:
                session.add(row)
            reflection.memory_proposal_ids = [row.id for row in proposal_rows]
            session.commit()
            session.refresh(reflection)
            return reflection_run_row_to_contract(reflection)

    def _load_observations(self, session: Session, case_id: str) -> list[PerformanceObservation]:
        rows = session.scalars(
            select(PerformanceObservationRow).where(PerformanceObservationRow.case_id == case_id)
        )
        return [_performance_observation_row_to_contract(row) for row in rows]

    def _derive_memory_proposals(
        self,
        session: Session,
        *,
        case_id: str,
        run_id: str,
        observations: list[PerformanceObservation] | None = None,
    ) -> list[MemoryProposalRow]:
        """§8.4: derive data-driven proposals from performance analysis + briefs,
        dedup against existing active + proposed memories."""
        observations = observations if observations is not None else self._load_observations(session, case_id)
        scores = [evolution.compute_performance_score(obs) for obs in observations]
        analysis = evolution.analyze_historical_performance(observations, scores)
        briefs = [
            creative_brief_row_to_contract(row)
            for row in session.scalars(
                select(CreativeBriefRow).where(CreativeBriefRow.case_id == case_id)
            )
        ]
        existing_active = [
            case_memory_row_to_contract(row)
            for row in session.scalars(
                select(CaseMemoryRow)
                .where(CaseMemoryRow.case_id == case_id)
                .where(CaseMemoryRow.status == "active")
            )
        ]
        existing_proposed = [
            memory_proposal_row_to_contract(row)
            for row in session.scalars(
                select(MemoryProposalRow)
                .where(MemoryProposalRow.case_id == case_id)
                .where(MemoryProposalRow.status == "proposed")
            )
        ]
        proposals = evolution.build_memory_proposals(
            case_id=case_id,
            reflection_run_id=run_id,
            analysis=analysis,
            briefs=briefs,
            existing_active=existing_active,
            existing_proposed=existing_proposed,
            id_factory=lambda: new_id("mem"),
        )
        if not proposals:
            topic = briefs[0].topic if briefs else None
            summary = briefs[0].summary if briefs else None
            descriptor = topic or summary or "this case"
            proposals = [
                MemoryProposal(
                    id=new_id("mem"),
                    case_id=case_id,
                    status="proposed",
                    memory_type="script_pattern",
                    insight=(
                        f"Insufficient confident performance data for {descriptor}; "
                        "collect more published-metric samples before drawing conclusions."
                    ),
                    evidence=[run_id],
                    confidence=0.3,
                    sample_size=0,
                    proposed_by_reflection_run_id=run_id,
                )
            ]
        return [_memory_proposal_contract_to_row(proposal) for proposal in proposals]

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


def _performance_observation_row_to_contract(row: PerformanceObservationRow) -> PerformanceObservation:
    return PerformanceObservation(
        id=row.id,
        case_id=row.case_id,
        publish_record_id=row.publish_record_id,
        video_version_id=row.video_version_id,
        platform=row.platform,
        account_id=row.account_id,
        window=row.window,
        metric_name=row.metric_name,
        metric_value=row.metric_value,
        impressions=row.impressions,
        views=row.views,
        avg_watch_sec=row.avg_watch_sec,
        completion_rate=row.completion_rate,
        like_rate=row.like_rate,
        comment_rate=row.comment_rate,
        share_rate=row.share_rate,
        follow_rate=row.follow_rate,
        conversion_count=row.conversion_count,
        conversion_rate=row.conversion_rate,
        raw_metrics=dict(row.raw_metrics or {}),
        observed_at=row.observed_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _memory_proposal_contract_to_row(proposal: MemoryProposal) -> MemoryProposalRow:
    return MemoryProposalRow(
        id=proposal.id,
        case_id=proposal.case_id,
        status=proposal.status,
        memory_type=proposal.memory_type,
        scope=proposal.scope.model_dump(mode="json"),
        scope_key=proposal.scope.scope_key,
        insight=proposal.insight,
        evidence=list(proposal.evidence),
        confidence=proposal.confidence,
        sample_size=proposal.sample_size,
        supersedes_memory_id=proposal.supersedes_memory_id,
        proposed_by_reflection_run_id=proposal.proposed_by_reflection_run_id,
    )
