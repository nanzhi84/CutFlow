from __future__ import annotations

from packages.core.contracts import (
    CaseAgentRun,
    CaseAgentSourceBinding,
    CaseMemory,
    CaseMemoryScope,
    CreativeBrief,
    MemoryProposal,
    ReflectionRun,
    RunStatus,
    ScriptDraft,
    ScriptVersion,
    VideoVersion,
)
from packages.core.storage.database import (
    CaseAgentRunRow,
    CaseAgentSourceBindingRow,
    CaseMemoryRow,
    CreativeBriefRow,
    MemoryProposalRow,
    ReflectionRunRow,
    ScriptDraftRow,
    ScriptVersionRow,
    VideoVersionRow,
)


def source_binding_row_to_contract(row: CaseAgentSourceBindingRow) -> CaseAgentSourceBinding:
    return CaseAgentSourceBinding(
        id=row.id,
        case_id=row.case_id,
        source_type=row.source_type,
        source_ref=row.source_ref,
        title=row.title,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def case_agent_run_row_to_contract(row: CaseAgentRunRow) -> CaseAgentRun:
    return CaseAgentRun(
        id=row.id,
        case_id=row.case_id,
        goal=row.goal,
        status=RunStatus(row.status),
        source_binding_ids=list(row.source_binding_ids or []),
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def creative_brief_row_to_contract(row: CreativeBriefRow) -> CreativeBrief:
    return CreativeBrief(
        id=row.id,
        case_id=row.case_id,
        summary=row.summary,
        source_binding_ids=list(row.source_binding_ids or []),
        topic=row.topic,
        audience=row.audience,
        key_insights=list(row.key_insights or []),
        source_refs=list(row.source_refs or []),
        generated_by_run_id=row.generated_by_run_id,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def script_draft_row_to_contract(row: ScriptDraftRow) -> ScriptDraft:
    return ScriptDraft(
        id=row.id,
        case_id=row.case_id,
        title=row.title,
        script=row.script,
        status=row.status,
        memory_ids=list(row.memory_ids or []),
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def script_version_row_to_contract(row: ScriptVersionRow) -> ScriptVersion:
    return ScriptVersion(
        id=row.id,
        case_id=row.case_id,
        title=row.title,
        script=row.script,
        creative_intent_artifact_id=row.creative_intent_artifact_id,
        adopted_from_draft_id=row.adopted_from_draft_id,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _scope_from_row(scope: dict | None, scope_key: str | None) -> CaseMemoryScope:
    parsed = CaseMemoryScope.model_validate(scope or {})
    if scope_key and parsed.scope_key is None:
        parsed = parsed.model_copy(update={"scope_key": scope_key})
    return parsed


def case_memory_row_to_contract(row: CaseMemoryRow) -> CaseMemory:
    scope = _scope_from_row(row.scope, row.scope_key).model_copy(
        update={
            "valid_from": row.valid_from,
            "valid_until": row.valid_until,
        }
    )
    return CaseMemory(
        id=row.id,
        case_id=row.case_id,
        status=row.status,
        memory_type=row.memory_type,
        scope=scope,
        insight=row.insight,
        evidence=list(row.evidence or []),
        confidence=row.confidence,
        sample_size=row.sample_size,
        supersedes_memory_id=row.supersedes_memory_id,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def memory_proposal_row_to_contract(row: MemoryProposalRow) -> MemoryProposal:
    return MemoryProposal(
        id=row.id,
        case_id=row.case_id,
        status=row.status,
        memory_type=row.memory_type,
        scope=_scope_from_row(row.scope, row.scope_key),
        insight=row.insight,
        evidence=list(row.evidence or []),
        confidence=row.confidence,
        sample_size=row.sample_size,
        supersedes_memory_id=row.supersedes_memory_id,
        proposed_by_reflection_run_id=row.proposed_by_reflection_run_id,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def reflection_run_row_to_contract(row: ReflectionRunRow) -> ReflectionRun:
    return ReflectionRun(
        id=row.id,
        case_id=row.case_id,
        status=RunStatus(row.status),
        window=row.window,
        report_artifact_id=row.report_artifact_id,
        input_observation_ids=list(row.input_observation_ids or []),
        input_feature_vector_ids=list(row.input_feature_vector_ids or []),
        memory_proposal_ids=list(row.memory_proposal_ids or []),
        sample_size=row.sample_size,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def video_version_row_to_contract(row: VideoVersionRow) -> VideoVersion:
    return VideoVersion(
        id=row.id,
        case_id=row.case_id,
        script_version_id=row.script_version_id,
        finished_video_id=row.finished_video_id,
        timeline_plan_artifact_id=row.timeline_plan_artifact_id,
        style_plan_artifact_id=row.style_plan_artifact_id,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
