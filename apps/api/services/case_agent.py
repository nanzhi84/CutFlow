from __future__ import annotations

from fastapi import Request

from apps.api.common import (
    case_learning_repository,
    get_case,
    object_store,
    page,
    production_repository,
    provider_repository,
    repository,
    request_id,
    secret_store,
)
from apps.api.services.case_agent_llm import generate_script_with_llm
from packages.core import contracts as c
from packages.core.contracts.state_machines import assert_transition
from packages.core.storage.repository import new_id
from packages.core.config.settings import sandbox_fallback_allowed
from packages.core.workflow import NodeExecutionError
from packages.creative.cases import BriefFields, evolution, metrics_import
from packages.creative.reference_extract import ReferenceExtractError, extract_reference

def source_bindings(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseAgentSourceBinding]:

    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).list_source_bindings(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).source_bindings.values() if item.case_id == case_id], limit)


def create_source_binding(
    case_id: str, payload: c.CreateSourceBindingRequest, request: Request
) -> c.CaseAgentSourceBinding:
    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        return case_learning_repository(request).create_source_binding(case_id=case_id, payload=payload)
    binding = c.CaseAgentSourceBinding(id=new_id("src"), case_id=case_id, **payload.model_dump())
    repository(request).source_bindings[binding.id] = binding
    return binding


def delete_source_binding(case_id: str, binding_id: str, request: Request) -> c.OkResponse:
    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        deleted = case_learning_repository(request).delete_source_binding(case_id=case_id, binding_id=binding_id)
        if not deleted:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Source binding is missing.")
        return c.OkResponse(ok=True, request_id=request_id())
    binding = repository(request).source_bindings.get(binding_id)
    if binding is None or binding.case_id != case_id:
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Source binding is missing.")
    del repository(request).source_bindings[binding_id]
    return c.OkResponse(ok=True, request_id=request_id())


async def import_case_source(
    case_id: str, payload: c.ImportCaseSourceRequest, request: Request
) -> c.CaseAgentRun:
    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        learning = case_learning_repository(request)
        binding = learning.get_source_binding(case_id=case_id, binding_id=payload.source_binding_id)
        if binding is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Source binding is missing.")
        brief_fields = await _synthesize_brief_fields(binding, request)
        run = learning.import_case_source(case_id=case_id, payload=payload, brief_fields=brief_fields)
        if run is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Source binding is missing.")
        return run
    binding = repository(request).source_bindings.get(payload.source_binding_id)
    if binding is None or binding.case_id != case_id:
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Source binding is missing.")
    run = c.CaseAgentRun(
        id=new_id("agent_run"),
        case_id=case_id,
        goal="brief",
        status=c.RunStatus.succeeded,
        source_binding_ids=[payload.source_binding_id],
    )
    repository(request).case_agent_runs[run.id] = run
    brief_fields = await _synthesize_brief_fields(binding, request)
    brief = c.CreativeBrief(
        id=new_id("brief"),
        case_id=case_id,
        summary=brief_fields.summary,
        source_binding_ids=[payload.source_binding_id],
        topic=brief_fields.topic,
        audience=brief_fields.audience,
        key_insights=list(brief_fields.key_insights),
        source_refs=list(brief_fields.source_refs),
        generated_by_run_id=run.id,
    )
    repository(request).briefs[brief.id] = brief
    return run


async def _synthesize_brief_fields(binding: c.CaseAgentSourceBinding, request: Request) -> BriefFields:
    """Build a real CreativeBrief from a bound source instead of a stub summary (F/#2).

    - text / manual_note: the source_ref is the content; use it inline (no extraction).
    - url: extract the reference script via reference_extract and summarize it.
    - file: out-of-scope this round; fall back to the binding title (or ref).
    """
    source_ref = (binding.source_ref or "").strip()
    title = (binding.title or "").strip() or None
    if binding.source_type in {"text", "manual_note"}:
        summary = _shorten(source_ref) if source_ref else (title or "Imported source.")
        return BriefFields(
            summary=summary,
            topic=title,
            key_insights=_insights_from_text(source_ref),
            source_refs=[source_ref] if source_ref else [],
        )
    if binding.source_type == "url":
        try:
            result = await extract_reference(
                source_ref,
                "zh",
                asr_invoke=lambda audio_url, language: _invoke_asr(request, audio_url, language),
                object_store=object_store(request),
                secret_store=secret_store(request),
            )
        except ReferenceExtractError as exc:
            raise NodeExecutionError(exc.code, exc.message, details=exc.details) from exc
        reference_script = (result.reference_script or "").strip()
        return BriefFields(
            summary=_shorten(reference_script) if reference_script else (result.title or source_ref),
            topic=result.title or title,
            key_insights=_insights_from_text(reference_script),
            source_refs=[result.resolved_url or source_ref],
        )
    # file (and any other type): no content extraction this round.
    return BriefFields(
        summary=title or source_ref or "Imported source.",
        topic=title,
        source_refs=[source_ref] if source_ref else [],
    )


def _shorten(text: str, *, limit: int = 280) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _insights_from_text(text: str, *, limit: int = 5) -> list[str]:
    """Split source content into a few candidate key-insight lines for the brief."""
    raw_lines = [line.strip() for line in text.splitlines()]
    insights = [line for line in raw_lines if line]
    if len(insights) <= 1 and text.strip():
        # Single block: fall back to sentence-ish segmentation on Chinese/Latin stops.
        import re

        insights = [seg.strip() for seg in re.split(r"[。!?\n.!?]", text) if seg.strip()]
    return insights[:limit]


def _invoke_asr(request: Request, audio_url: str, language: str) -> str:
    from packages.ai.gateway import ProviderCall

    profile = _first_asr_profile(request)
    if profile is None:
        raise ReferenceExtractError(c.ErrorCode.reference_asr_failed, "ASR provider profile is not configured.")
    invocation, result = request.app.state.provider_gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="asr.transcribe",
            input={"audio_uri": audio_url, "language_hints": [language]},
        )
    )
    if result is None or invocation.error:
        raise ReferenceExtractError(
            c.ErrorCode.reference_asr_failed,
            invocation.error.message if invocation.error else "ASR provider failed.",
        )
    text = result.output.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ReferenceExtractError(c.ErrorCode.reference_asr_failed, "ASR response did not include text.")
    return text.strip()


def _first_asr_profile(request: Request) -> c.ProviderProfile | None:
    db_repo = provider_repository(request)
    if db_repo is not None:
        profiles = db_repo.list_profiles(capability="asr.transcribe", limit=20)
    else:
        profiles = [
            profile
            for profile in repository(request).provider_profiles.values()
            if profile.capability == "asr.transcribe"
        ]
    for profile in profiles:
        if profile.enabled:
            return profile
    return None


def start_case_agent_run(
    case_id: str, payload: c.StartCaseAgentRunRequest, request: Request
) -> c.CaseAgentRun:
    get_case(request, case_id)
    if not sandbox_fallback_allowed():
        raise NodeExecutionError(
            c.ErrorCode.provider_unsupported_option,
            "案例智能体（自动跑稿 / 记忆提案）尚未接入真实模型，暂不可用。",
        )
    if case_learning_repository(request) is not None:
        return case_learning_repository(request).start_agent_run(case_id=case_id, payload=payload)
    run = c.CaseAgentRun(
        id=new_id("agent_run"),
        case_id=case_id,
        goal=payload.goal,
        status=c.RunStatus.succeeded,
        source_binding_ids=payload.source_binding_ids,
    )
    repository(request).case_agent_runs[run.id] = run
    if payload.goal == "script_draft":
        draft = c.ScriptDraft(
            id=new_id("draft"),
            case_id=case_id,
            title="Agent generated draft",
            script="开场提出痛点。展示解决方案。收束到行动建议。",
        )
        repository(request).drafts[draft.id] = draft
    if payload.goal == "memory_proposal":
        proposal = _agent_memory_proposal(request, case_id, run.id)
        repository(request).memory_proposals[proposal.id] = proposal
    return run


def _agent_memory_proposal(request: Request, case_id: str, run_id: str) -> c.MemoryProposal:
    """Derive a data-driven memory proposal from the case's run evidence (§8.4).

    Uses the latest brief + any confident performance analysis instead of a
    hardcoded literal, and dedups against existing active/proposed memories.
    """
    repo = repository(request)
    observations = [obs for obs in repo.performance_observations.values() if obs.case_id == case_id]
    scores = [evolution.compute_performance_score(obs) for obs in observations]
    analysis = evolution.analyze_historical_performance(observations, scores)
    briefs = [b for b in repo.briefs.values() if b.case_id == case_id]
    existing_active = [m for m in repo.memories.values() if m.case_id == case_id and m.status == "active"]
    existing_proposed = [
        m for m in repo.memory_proposals.values() if m.case_id == case_id and m.status == "proposed"
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
    if proposals:
        return proposals[0]
    return _fallback_proposal(case_id, run_id, briefs)


def case_agent_runs(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseAgentRun]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).list_agent_runs(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).case_agent_runs.values() if item.case_id == case_id], limit)


def case_agent_run_detail(request: Request, case_id: str, run_id: str) -> c.CaseAgentRunDetail:

    if case_learning_repository(request) is not None:
        detail = case_learning_repository(request).agent_run_detail(case_id=case_id, run_id=run_id)
        if detail is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Agent run is missing.")
        return detail
    run = repository(request).case_agent_runs[run_id]
    return c.CaseAgentRunDetail(
        run=run,
        briefs=[item for item in repository(request).briefs.values() if item.case_id == case_id],
        drafts=[item for item in repository(request).drafts.values() if item.case_id == case_id],
        memory_proposals=[item for item in repository(request).memory_proposals.values() if item.case_id == case_id],
    )


def script_drafts(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.ScriptDraft]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).list_drafts(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).drafts.values() if item.case_id == case_id], limit)


def adopt_script_draft(
    case_id: str, draft_id: str, payload: c.AdoptScriptDraftRequest, request: Request
) -> c.ScriptVersion:
    if case_learning_repository(request) is not None:
        script = case_learning_repository(request).adopt_draft(
            case_id=case_id,
            draft_id=draft_id,
            payload=payload,
        )
        if script is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Script draft is missing.")
        return script
    draft = repository(request).drafts[draft_id]
    script = c.ScriptVersion(
        id=new_id("script"),
        case_id=case_id,
        title=payload.title or draft.title,
        script=payload.publish_content or draft.script,
        adopted_from_draft_id=draft.id,
    )
    repository(request).scripts[script.id] = script
    repository(request).drafts[draft.id] = draft.model_copy(update={"status": "adopted", "updated_at": c.utcnow()})
    return script


def memory_proposals(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.MemoryProposal]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).list_memory_proposals(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).memory_proposals.values() if item.case_id == case_id], limit)


def case_knowledge(request: Request, case_id: str) -> c.CaseKnowledgeResponse:

    get_case(request, case_id)
    if case_learning_repository(request) is not None:
        return case_learning_repository(request).knowledge(case_id=case_id)
    return c.CaseKnowledgeResponse(
        case_id=case_id,
        memories=[item for item in repository(request).memories.values() if item.case_id == case_id],
        recent_script_versions=[item for item in repository(request).scripts.values() if item.case_id == case_id][-10:],
        recent_video_versions=[item for item in repository(request).video_versions.values() if item.case_id == case_id][-10:],
    )


def case_memory(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseMemory]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).list_memory(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository(request).memories.values() if item.case_id == case_id], limit)


def approve_memory(
    case_id: str, memory_id: str, payload: c.ApproveMemoryRequest, request: Request
) -> c.CaseMemory:
    if case_learning_repository(request) is not None:
        memory = case_learning_repository(request).approve_memory(case_id=case_id, memory_id=memory_id)
        if memory is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Memory proposal is missing.")
        return memory
    proposal = repository(request).memory_proposals.get(memory_id) or repository(request).memories[memory_id]
    next_status = proposal.status
    if next_status == "proposed":
        assert_transition("case_memory", next_status, "approved")
        next_status = "approved"
    assert_transition("case_memory", next_status, "active")
    memory = c.CaseMemory.model_validate(
        proposal.model_dump(exclude={"proposed_by_reflection_run_id"})
    ).model_copy(update={"status": "active"})
    if memory_id in repository(request).memory_proposals:
        repository(request).memory_proposals[memory_id] = repository(request).memory_proposals[memory_id].model_copy(
            update={"status": "approved", "updated_at": c.utcnow()}
        )
    repository(request).memories[memory.id] = memory
    return memory


def reject_memory(
    case_id: str, memory_id: str, payload: c.RejectMemoryRequest, request: Request
) -> c.MemoryProposal:
    if case_learning_repository(request) is not None:
        proposal = case_learning_repository(request).reject_memory(case_id=case_id, memory_id=memory_id)
        if proposal is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Memory proposal is missing.")
        return proposal
    proposal = repository(request).memory_proposals[memory_id].model_copy(update={"status": "rejected"})
    repository(request).memory_proposals[memory_id] = proposal
    return proposal


def case_performance(request: Request, case_id: str, window: str = "7d") -> c.CasePerformanceResponse:

    if production_repository(request) is not None:
        return production_repository(request).case_performance(case_id=case_id, window=window)
    observations = [item for item in repository(request).performance_observations.values() if item.case_id == case_id]
    metrics = c.PerformanceMetricView(
        impressions=int(sum(item.metric_value for item in observations if item.metric_name == "impressions")),
        views=int(sum(item.metric_value for item in observations if item.metric_name == "views")),
        likes=int(sum(item.metric_value for item in observations if item.metric_name == "likes")),
    )
    obs_ids = {obs.id for obs in observations}
    scores = [
        score
        for score in repository(request).performance_scores.values()
        if score.case_id == case_id and score.observation_id in obs_ids
    ]
    return c.CasePerformanceResponse(metrics=metrics, observations=observations, scores=scores)


def import_metrics(case_id: str, payload: c.MetricsImportRequest, request: Request) -> c.ImportBatchReport:
    if production_repository(request) is not None:
        return production_repository(request).import_metrics(
            case_id=case_id,
            payload=payload,
            request_id=request_id(),
        )
    repo = repository(request)
    records = [
        metrics_import.PublishRecordIndex(
            publish_record_id=record.id,
            video_version_id=record.video_version_id,
            platform=record.platform,
        )
        for record in repo.publish_records.values()
        if record.case_id == case_id
    ]
    result = metrics_import.match_metrics_rows(
        payload.rows,
        policy=payload.matching_policy,
        records=records,
        default_platform=payload.platform,
        default_account_id=payload.account_id,
    )
    results: list[c.ImportRowResult] = []
    for matched in result.matched:
        obs = _observation_from_match(case_id, matched)
        if not payload.dry_run:
            repo.performance_observations[obs.id] = obs
            score = evolution.compute_performance_score(obs)
            repo.performance_scores[score.id] = score
        results.append(c.ImportRowResult(row_index=matched.row_index, status="created", internal_id=obs.id))
    for unmatched in result.unmatched:
        results.append(
            c.ImportRowResult(
                row_index=unmatched.row_index,
                status="skipped",
                error=c.NodeError(code=c.ErrorCode.validation_invalid_options, message=unmatched.reason),
            )
        )
    results.sort(key=lambda item: item.row_index)
    report = c.ImportBatchReport(
        batch_id=new_id("imp"),
        import_type="performance",
        status=c.ImportBatchStatus.completed
        if not result.unmatched
        else c.ImportBatchStatus.partially_failed,
        created_count=len(result.matched),
        skipped_count=len(result.unmatched),
        failed_count=0,
        results=results,
        request_id=request_id(),
    )
    repo.import_reports[report.batch_id] = report
    return report


def _observation_from_match(
    case_id: str, matched: metrics_import.MatchedRow
) -> c.PerformanceObservation:
    canonical = matched.canonical_metrics
    return c.PerformanceObservation(
        id=new_id("perf"),
        case_id=case_id,
        publish_record_id=matched.publish_record_id,
        video_version_id=matched.video_version_id,
        platform=matched.platform,
        account_id=matched.account_id,
        window=matched.window,
        metric_name=matched.metric_name,
        metric_value=matched.metric_value,
        impressions=int(canonical["impressions"]) if "impressions" in canonical else None,
        views=int(canonical["views"]) if "views" in canonical else None,
        avg_watch_sec=canonical.get("avg_watch_sec"),
        completion_rate=canonical.get("completion_rate"),
        like_rate=canonical.get("like_rate"),
        comment_rate=canonical.get("comment_rate"),
        share_rate=canonical.get("share_rate"),
        follow_rate=canonical.get("follow_rate"),
        conversion_count=int(canonical["conversion_count"]) if "conversion_count" in canonical else None,
        conversion_rate=canonical.get("conversion_rate"),
        raw_metrics=dict(canonical),
    )


def recall_memory(
    request: Request, case_id: str, query: c.MemoryRecallQuery
) -> c.MemoryRecallResponse:
    """§25.8 memory recall: scope + validity-window filtered, confidence/score ranked."""
    get_case(request, case_id)
    learning = case_learning_repository(request)
    if learning is not None:
        return learning.recall_memory(case_id=case_id, query=query)
    memories = [item for item in repository(request).memories.values() if item.case_id == case_id]
    score_lookup = _memory_score_lookup(request, case_id)
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
    return c.MemoryRecallResponse(case_id=case_id, mode=query.mode, memories=recalled)


def _memory_score_lookup(request: Request, case_id: str) -> dict[str, float]:
    """Map memory scope_key -> best normalized performance score for high/low recall."""
    lookup: dict[str, float] = {}
    for score in repository(request).performance_scores.values():
        if score.case_id != case_id or score.excluded_reason is not None:
            continue
        key = score.platform or score.video_version_id or score.observation_id
        lookup[key] = max(lookup.get(key, 0.0), score.normalized_score)
    return lookup


def start_reflection(case_id: str, payload: c.StartReflectionRunRequest, request: Request) -> c.ReflectionRun:
    if not sandbox_fallback_allowed():
        raise NodeExecutionError(
            c.ErrorCode.provider_unsupported_option,
            "案例反思尚未接入真实模型，暂不可用。",
        )
    if case_learning_repository(request) is not None:
        get_case(request, case_id)
        return case_learning_repository(request).start_reflection(case_id=case_id, payload=payload)
    repo = repository(request)
    observations = [
        obs for obs in repo.performance_observations.values() if obs.case_id == case_id
    ]
    scores_by_obs = {s.observation_id: s for s in repo.performance_scores.values()}
    scores = [
        scores_by_obs.get(obs.id) or evolution.compute_performance_score(obs)
        for obs in observations
    ]
    analysis = evolution.analyze_historical_performance(observations, scores)
    briefs = [b for b in repo.briefs.values() if b.case_id == case_id]
    existing_active = [m for m in repo.memories.values() if m.case_id == case_id and m.status == "active"]
    existing_proposed = [
        m for m in repo.memory_proposals.values() if m.case_id == case_id and m.status == "proposed"
    ]
    reflection = c.ReflectionRun(
        id=new_id("refl"),
        case_id=case_id,
        status=c.RunStatus.succeeded,
        window=payload.window,
        input_observation_ids=[obs.id for obs in observations],
        sample_size=len(observations),
    )
    proposals = evolution.build_memory_proposals(
        case_id=case_id,
        reflection_run_id=reflection.id,
        analysis=analysis,
        briefs=briefs,
        existing_active=existing_active,
        existing_proposed=existing_proposed,
        id_factory=lambda: new_id("mem"),
    )
    if not proposals:
        # No-silent-degrade still needs a reviewable artifact: emit a low-confidence
        # proposal grounded in the brief so the reflection run is never empty.
        proposals = [_fallback_proposal(case_id, reflection.id, briefs)]
    for proposal in proposals:
        repo.memory_proposals[proposal.id] = proposal
    reflection = reflection.model_copy(
        update={"memory_proposal_ids": [proposal.id for proposal in proposals]}
    )
    repo.reflection_runs[reflection.id] = reflection
    return reflection


def _fallback_proposal(
    case_id: str, reflection_run_id: str, briefs: list[c.CreativeBrief]
) -> c.MemoryProposal:
    topic = briefs[0].topic if briefs else None
    summary = briefs[0].summary if briefs else None
    descriptor = topic or summary or "this case"
    return c.MemoryProposal(
        id=new_id("mem"),
        case_id=case_id,
        status="proposed",
        memory_type="script_pattern",
        insight=(
            f"Insufficient confident performance data for {descriptor}; "
            "collect more published-metric samples before drawing conclusions."
        ),
        evidence=[reflection_run_id],
        confidence=0.3,
        sample_size=0,
        proposed_by_reflection_run_id=reflection_run_id,
    )


def case_insights(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CaseInsightCard]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).insights(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    cards = [
        c.CaseInsightCard(
            id=new_id("insight"),
            case_id=case_id,
            title="Memory proposals",
            body=f"{len([item for item in repository(request).memory_proposals.values() if item.case_id == case_id])} proposal(s) waiting for review.",
        )
    ]
    performance_count = len(
        [item for item in repository(request).performance_observations.values() if item.case_id == case_id]
    )
    if performance_count:
        cards.append(
            c.CaseInsightCard(
                id=new_id("insight"),
                case_id=case_id,
                title="Performance imports",
                body=f"{performance_count} performance observation(s) available for analysis.",
                severity="success",
            )
        )
    return page(cards, limit)


def creative_patterns(request: Request, case_id: str, limit: int = 50) -> c.PageResponse[c.CreativePattern]:

    if case_learning_repository(request) is not None:
        values = case_learning_repository(request).creative_patterns(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    patterns = [item for item in repository(request).creative_patterns.values() if item.case_id == case_id]
    if not patterns:
        patterns = [
            c.CreativePattern(
                id=new_id("pattern"),
                case_id=case_id,
                label="Concrete hook + short CTA",
                lift=None,
                evidence_count=len(repository(request).finished_videos),
            )
        ]
    return page(patterns, limit)


def generate_script_with_memory(
    case_id: str, payload: c.GenerateScriptWithMemoryRequest, request: Request
) -> c.ScriptDraft:
    repo = repository(request)
    if case_learning_repository(request) is not None:
        get_case(request, case_id)
        knowledge = case_learning_repository(request).knowledge(case_id=case_id)
        memories = [memory.insight for memory in knowledge.memories if memory.id in payload.memory_ids]
        provider_script = generate_script_with_llm(
            case_id,
            payload.brief,
            payload.memory_ids,
            memories,
            request,
            persona_mode=payload.persona_mode,
            operation=payload.operation,
            strategy_tags=payload.strategy_tags,
            reference_script=payload.reference_script,
            duration=payload.duration,
        )
        return case_learning_repository(request).generate_script_with_memory(
            case_id=case_id,
            payload=payload,
            script_override=provider_script,
        )
    memories = [repo.memories[mid].insight for mid in payload.memory_ids if mid in repo.memories]
    provider_script = generate_script_with_llm(
        case_id,
        payload.brief,
        payload.memory_ids,
        memories,
        request,
        persona_mode=payload.persona_mode,
        operation=payload.operation,
    )
    draft = c.ScriptDraft(
        id=new_id("draft"),
        case_id=case_id,
        title="Memory-guided draft",
        script=provider_script or f"{payload.brief}\n\n参考记忆：{' / '.join(memories) if memories else '暂无'}",
        memory_ids=payload.memory_ids,
    )
    repo.drafts[draft.id] = draft
    return draft
