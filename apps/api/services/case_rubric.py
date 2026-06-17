"""Case-rubric self-evolution service (case_rubric_v1).

Double-backend integration layer over the storage-agnostic ``rubric.py`` pure
functions: it wires blind scoring, reward-signal collection, lazy reward derivation,
calibration, and the one-confirmation bump flow.

Two storage paths:
- DB:    ``case_rubric_repository(request)`` is not None.
- memory: the in-memory ``repository(request)`` dicts.

Blind invariant (§6.2): a prediction's ``composite`` / ``band`` / ``dimension_scores``
are immutable after ``locked_at``; a ``performance_scored`` settlement is applied only
when the observation is later than ``locked_at`` (otherwise it is pollution and skipped).
"""

from __future__ import annotations

from fastapi import Request

from apps.api.common import (
    case_rubric_repository,
    get_case,
    repository,
    settings as app_settings,
)
from packages.core import contracts as c
from packages.core.contracts.state_machines import assert_transition
from packages.core.storage.repository import new_id
from packages.creative.cases import evolution, metrics_import
from packages.creative.cases import rubric as rubric_logic

# A reward sample shared with rubric.py: (feature vector, 0–1 actual reward).
RewardSample = tuple[c.CreativeFeatureVector, float]


def _learning_settings(request: Request):
    return app_settings(request).learning


# ---------------------------------------------------------------------------
# Feature reconstruction
# ---------------------------------------------------------------------------

def _features_from_script(script: c.ScriptVersion, *, case_id: str) -> c.CreativeFeatureVector:
    return evolution.extract_script_features(
        script,
        case_id=case_id,
        feature_id=f"cfv_{script.id}",
    )


def _features_from_draft(draft: c.ScriptDraft, *, case_id: str) -> c.CreativeFeatureVector:
    # A draft has no ScriptVersion yet; build a throwaway one so the same pure
    # extractor produces the feature vector we score (and later re-score) on.
    throwaway = c.ScriptVersion(
        id=draft.id,
        case_id=case_id,
        title=draft.title,
        script=draft.script,
    )
    return _features_from_script(throwaway, case_id=case_id)


def _features_for_prediction(
    request: Request, case_id: str, prediction: c.ScorePrediction
) -> c.CreativeFeatureVector | None:
    """Rebuild the feature vector a settled prediction was scored on, from its script."""
    script = _load_script_version(request, case_id, prediction.script_version_id)
    if script is None:
        return None
    return _features_from_script(script, case_id=case_id)


def _load_script_version(
    request: Request, case_id: str, script_version_id: str | None
) -> c.ScriptVersion | None:
    if script_version_id is None:
        return None
    repo = case_rubric_repository(request)
    if repo is not None:
        return repo.get_script_version(script_version_id)
    script = repository(request).scripts.get(script_version_id)
    if script is None or script.case_id != case_id:
        return None
    return script


# ---------------------------------------------------------------------------
# Rubric read + cold start
# ---------------------------------------------------------------------------

def get_rubric(request: Request, case_id: str) -> c.CaseRubric:
    get_case(request, case_id)
    repo = case_rubric_repository(request)
    if repo is not None:
        return repo.ensure_active_rubric(case_id)
    return _ensure_active_rubric_memory(request, case_id)


def _ensure_active_rubric_memory(request: Request, case_id: str) -> c.CaseRubric:
    repo = repository(request)
    active = _active_rubric_memory(repo, case_id)
    if active is not None:
        return active
    card = rubric_logic.cold_start_rubric(rubric_id=new_id("rubric"), case_id=case_id)
    repo.case_rubrics[card.id] = card
    return card


def _active_rubric_memory(repo, case_id: str) -> c.CaseRubric | None:
    candidates = [
        r for r in repo.case_rubrics.values() if r.case_id == case_id and r.status == "active"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.version)


# ---------------------------------------------------------------------------
# §6.2 blind scoring of freshly-created drafts
# ---------------------------------------------------------------------------

def score_drafts(
    request: Request, case_id: str, drafts: list[c.ScriptDraft]
) -> list[c.ScorePrediction]:
    """Blind-score each draft with the active rubric and persist a ScorePrediction.

    Called from ``case_agent.generate_script_with_memory`` after the draft is created.
    """
    if not drafts:
        return []
    rubric_card = get_rubric(request, case_id)
    repo = case_rubric_repository(request)
    predictions: list[c.ScorePrediction] = []
    for draft in drafts:
        features = _features_from_draft(draft, case_id=case_id)
        prediction = rubric_logic.predict(
            rubric_card,
            features,
            prediction_id=new_id("pred"),
            case_id=case_id,
            script_draft_id=draft.id,
        )
        if repo is not None:
            predictions.append(repo.add_prediction(prediction))
        else:
            repository(request).score_predictions[prediction.id] = prediction
            predictions.append(prediction)
    return predictions


def list_predictions(request: Request, case_id: str) -> list[c.ScorePrediction]:
    repo = case_rubric_repository(request)
    if repo is not None:
        return repo.list_predictions(case_id)
    predictions = [
        p for p in repository(request).score_predictions.values() if p.case_id == case_id
    ]
    return sorted(predictions, key=lambda p: p.created_at, reverse=True)


# ---------------------------------------------------------------------------
# §5 reward collection (搭车既有动作)
# ---------------------------------------------------------------------------

def record_adopt_reward(
    request: Request, case_id: str, draft_id: str, script_version_id: str
) -> None:
    """draft_adopted reward + settle the draft's prediction onto its ScriptVersion."""
    value, confidence = rubric_logic.reward_value("draft_adopted", _learning_settings(request))
    reward = c.RewardSignal(
        id=new_id("reward"),
        case_id=case_id,
        script_draft_id=draft_id,
        script_version_id=script_version_id,
        source_kind="draft_adopted",
        value=value,
        confidence=confidence,
        evidence_ref=draft_id,
    )
    repo = case_rubric_repository(request)
    if repo is not None:
        repo.add_reward(reward)
        prediction = repo.get_prediction_by_draft(draft_id)
        if prediction is not None and prediction.script_version_id != script_version_id:
            # Link the blind prediction to the adopted script (not a settlement;
            # composite/band/dimension_scores stay locked).
            repo.update_prediction(
                prediction.model_copy(update={"script_version_id": script_version_id})
            )
        return
    mem = repository(request)
    mem.reward_signals[reward.id] = reward
    prediction = _prediction_by_draft_memory(mem, draft_id)
    if prediction is not None:
        mem.score_predictions[prediction.id] = prediction.model_copy(
            update={"script_version_id": script_version_id}
        )


def record_discard_reward(
    request: Request, case_id: str, finished_video_id: str, reason: str | None
) -> None:
    """video_discarded reward; the reason drives the value (only ``script`` is负样本)."""
    script_version_id = _resolve_script_version_for_finished_video(
        request, case_id, finished_video_id
    )
    value, confidence = rubric_logic.reward_value(
        "video_discarded", _learning_settings(request), reason=reason
    )
    reason_value = reason if reason in {"script", "visual", "topic", "no_time"} else None
    reward = c.RewardSignal(
        id=new_id("reward"),
        case_id=case_id,
        script_version_id=script_version_id,
        source_kind="video_discarded",
        value=value,
        confidence=confidence,
        evidence_ref=finished_video_id,
        reason=reason_value,
    )
    repo = case_rubric_repository(request)
    if repo is not None:
        repo.add_reward(reward)
        return
    repository(request).reward_signals[reward.id] = reward


# ---------------------------------------------------------------------------
# §5.3 / §6.3 lazy reward derivation (idempotent by source_kind + evidence_ref)
# ---------------------------------------------------------------------------

def sync_rewards(request: Request, case_id: str) -> None:
    """Idempotently derive video_produced / published / performance_scored rewards
    from existing finished videos / publish records / performance scores, and settle
    matching blind predictions. Lazy (read-time); never hooks the worker."""
    repo = case_rubric_repository(request)
    if repo is not None:
        _sync_rewards_db(request, case_id, repo)
    else:
        _sync_rewards_memory(request, case_id)


def _sync_rewards_db(request: Request, case_id: str, repo) -> None:
    settings = _learning_settings(request)

    # video_produced: every finished video with a resolvable script version.
    for finished in repo.list_finished_videos(case_id):
        if repo.reward_exists("video_produced", finished.id):
            continue
        script_version_id = repo.resolve_script_version_for_finished_video(finished.id)
        if script_version_id is None:
            continue
        value, confidence = rubric_logic.reward_value("video_produced", settings)
        repo.add_reward(
            c.RewardSignal(
                id=new_id("reward"),
                case_id=case_id,
                script_version_id=script_version_id,
                source_kind="video_produced",
                value=value,
                confidence=confidence,
                evidence_ref=finished.id,
            )
        )

    # published: every published publish record (resolve script via video version).
    for record in repo.list_publish_records(case_id):
        if record.status != "published":
            continue
        if repo.reward_exists("published", record.id):
            continue
        script_version_id = _script_version_for_video_version_db(repo, record.video_version_id)
        value, confidence = rubric_logic.reward_value("published", settings)
        repo.add_reward(
            c.RewardSignal(
                id=new_id("reward"),
                case_id=case_id,
                script_version_id=script_version_id,
                source_kind="published",
                value=value,
                confidence=confidence,
                evidence_ref=record.id,
            )
        )

    # performance_scored: confident scores → reward + settle the matching prediction.
    predictions = repo.list_predictions(case_id)
    for score in repo.list_performance_scores(case_id):
        if score.excluded_reason is not None:
            continue
        if not repo.reward_exists("performance_scored", score.observation_id):
            script_version_id = score.video_version_id and _script_version_for_video_version_db(
                repo, score.video_version_id
            )
            repo.add_reward(
                c.RewardSignal(
                    id=new_id("reward"),
                    case_id=case_id,
                    script_version_id=script_version_id,
                    source_kind="performance_scored",
                    value=score.normalized_score,
                    confidence=score.confidence,
                    evidence_ref=score.observation_id,
                )
            )
        observation = _observation_by_id_db(repo, case_id, score.observation_id)
        target_script_version_id = _script_version_for_video_version_db(
            repo, score.video_version_id
        )
        settled = _settle_prediction_for_score(
            predictions, score, observation, target_script_version_id
        )
        if settled is not None:
            repo.update_prediction(settled)
            predictions = repo.list_predictions(case_id)


def _sync_rewards_memory(request: Request, case_id: str) -> None:
    repo = repository(request)
    settings = _learning_settings(request)

    def _reward_exists(source_kind: str, evidence_ref: str) -> bool:
        return any(
            r.case_id == case_id
            and r.source_kind == source_kind
            and r.evidence_ref == evidence_ref
            for r in repo.reward_signals.values()
        )

    def _add(reward: c.RewardSignal) -> None:
        repo.reward_signals[reward.id] = reward

    for finished in [f for f in repo.finished_videos.values() if f.case_id == case_id]:
        if _reward_exists("video_produced", finished.id):
            continue
        script_version_id = _resolve_script_version_for_finished_video(request, case_id, finished.id)
        if script_version_id is None:
            continue
        value, confidence = rubric_logic.reward_value("video_produced", settings)
        _add(
            c.RewardSignal(
                id=new_id("reward"),
                case_id=case_id,
                script_version_id=script_version_id,
                source_kind="video_produced",
                value=value,
                confidence=confidence,
                evidence_ref=finished.id,
            )
        )

    for record in [r for r in repo.publish_records.values() if r.case_id == case_id]:
        if record.status != "published" or _reward_exists("published", record.id):
            continue
        script_version_id = _script_version_for_video_version_memory(repo, record.video_version_id)
        value, confidence = rubric_logic.reward_value("published", settings)
        _add(
            c.RewardSignal(
                id=new_id("reward"),
                case_id=case_id,
                script_version_id=script_version_id,
                source_kind="published",
                value=value,
                confidence=confidence,
                evidence_ref=record.id,
            )
        )

    scores = [s for s in repo.performance_scores.values() if s.case_id == case_id]
    for score in scores:
        if score.excluded_reason is not None:
            continue
        if not _reward_exists("performance_scored", score.observation_id):
            script_version_id = _script_version_for_video_version_memory(
                repo, score.video_version_id
            )
            _add(
                c.RewardSignal(
                    id=new_id("reward"),
                    case_id=case_id,
                    script_version_id=script_version_id,
                    source_kind="performance_scored",
                    value=score.normalized_score,
                    confidence=score.confidence,
                    evidence_ref=score.observation_id,
                )
            )
        observation = repo.performance_observations.get(score.observation_id)
        predictions = [p for p in repo.score_predictions.values() if p.case_id == case_id]
        target_script_version_id = _script_version_for_video_version_memory(
            repo, score.video_version_id
        )
        settled = _settle_prediction_for_score(
            predictions, score, observation, target_script_version_id
        )
        if settled is not None:
            repo.score_predictions[settled.id] = settled


def _settle_prediction_for_score(
    predictions: list[c.ScorePrediction],
    score: c.PerformanceScore,
    observation: c.PerformanceObservation | None,
    target_script_version_id: str | None,
) -> c.ScorePrediction | None:
    """Settle the prediction linked to this score's script version — but only when the
    observation is later than the prediction's lock (blind invariant). Returns the
    updated prediction (settlement fields only) or None when nothing to do.

    Predictions are keyed on ``script_version_id``; the score carries
    ``video_version_id``, so the caller resolves the script version via the video
    lineage and passes it as ``target_script_version_id``.
    """
    if observation is None or target_script_version_id is None:
        return None
    candidates = [
        p
        for p in predictions
        if p.script_version_id == target_script_version_id and p.settled_reward is None
    ]
    if not candidates:
        return None
    prediction = candidates[0]
    if observation.observed_at <= prediction.locked_at:
        return None  # would break the blind invariant
    return prediction.model_copy(
        update={"settled_reward": score.normalized_score, "settled_at": c.utcnow()}
    )


# ---------------------------------------------------------------------------
# §5.3 single-row manual backfill
# ---------------------------------------------------------------------------

def backfill_metrics(
    request: Request,
    case_id: str,
    finished_video_id: str,
    payload: c.MetricsBackfillRequest,
) -> c.PerformanceObservation:
    """Operator backfills raw counts for one finished video (§5.3). Folds counts→canonical,
    builds the observation via the same canonical builder as batch import, scores it,
    persists both, then lazily syncs rewards (settling the blind prediction)."""
    get_case(request, case_id)
    publish_record_id, video_version_id = _resolve_publish_lineage_for_finished_video(
        request, case_id, finished_video_id
    )
    canonical = metrics_import.counts_to_canonical(
        {
            "views": payload.views,
            "impressions": payload.impressions,
            "likes": payload.likes,
            "comments": payload.comments,
            "shares": payload.shares,
            "follows": payload.follows,
            "conversions": payload.conversions,
            "avg_watch_sec": payload.avg_watch_sec,
        }
    )
    matched = metrics_import.MatchedRow(
        row_index=0,
        publish_record_id=publish_record_id,
        video_version_id=video_version_id,
        platform=payload.platform,
        account_id=payload.account_id,
        metric_name="views",
        metric_value=float(payload.views or 0),
        canonical_metrics=canonical,
        window=payload.window,
    )
    observation = metrics_import.observation_contract_from_match(case_id, matched)
    score = evolution.compute_performance_score(observation)

    repo = case_rubric_repository(request)
    if repo is not None:
        repo.add_performance(observation, score)
    else:
        mem = repository(request)
        mem.performance_observations[observation.id] = observation
        mem.performance_scores[score.id] = score

    sync_rewards(request, case_id)
    return observation


# ---------------------------------------------------------------------------
# §6.3 calibration + §6.4 bump
# ---------------------------------------------------------------------------

def calibration(request: Request, case_id: str) -> c.CalibrationReport:
    sync_rewards(request, case_id)
    rubric_card = get_rubric(request, case_id)
    settled = _reward_labeled_predictions(request, case_id)
    pending = len(_pending_retro_items(request, case_id))
    return rubric_logic.evaluate_calibration(
        settled,
        rubric=rubric_card,
        settings=_learning_settings(request),
        pending_retro_count=pending,
    )


def bump_proposal(request: Request, case_id: str) -> c.RubricBumpProposal | None:
    sync_rewards(request, case_id)
    repo = case_rubric_repository(request)
    existing = _open_bump_proposal(request, case_id)
    if existing is not None:
        return existing
    rubric_card = get_rubric(request, case_id)
    samples = _calibration_samples(request, case_id)
    proposal = rubric_logic.propose_bump(
        rubric_card,
        samples,
        settings=_learning_settings(request),
        proposal_id=new_id("bump"),
        candidate_id=new_id("rubric"),
    )
    if proposal is None:
        return None
    if repo is not None:
        return repo.add_bump_proposal(proposal)
    repository(request).rubric_bump_proposals[proposal.id] = proposal
    return proposal


def accept_bump(request: Request, case_id: str, proposal_id: str) -> c.CaseRubric:
    proposal = _get_bump_proposal(request, case_id, proposal_id)
    if proposal is None:
        raise _missing("Rubric bump proposal is missing.")
    assert_transition("rubric_bump", proposal.status, "accepted")
    repo = case_rubric_repository(request)
    new_active = proposal.candidate.model_copy(update={"status": "active"})
    if repo is not None:
        repo.supersede_active(case_id)
        repo.add_rubric(new_active)
        repo.update_bump_proposal(proposal.model_copy(update={"status": "accepted"}))
        return repo.get_active_rubric(case_id) or new_active
    mem = repository(request)
    active = _active_rubric_memory(mem, case_id)
    if active is not None:
        assert_transition("case_rubric", active.status, "superseded")
        mem.case_rubrics[active.id] = active.model_copy(
            update={"status": "superseded", "updated_at": c.utcnow()}
        )
    mem.case_rubrics[new_active.id] = new_active
    mem.rubric_bump_proposals[proposal.id] = proposal.model_copy(update={"status": "accepted"})
    return new_active


def reject_bump(
    request: Request, case_id: str, proposal_id: str, payload: c.RejectBumpRequest
) -> c.RubricBumpProposal:
    proposal = _get_bump_proposal(request, case_id, proposal_id)
    if proposal is None:
        raise _missing("Rubric bump proposal is missing.")
    assert_transition("rubric_bump", proposal.status, "rejected")
    rejected = proposal.model_copy(update={"status": "rejected"})
    repo = case_rubric_repository(request)
    if repo is not None:
        return repo.update_bump_proposal(rejected)
    repository(request).rubric_bump_proposals[proposal.id] = rejected
    return rejected


# ---------------------------------------------------------------------------
# §5.3 pending-retro list
# ---------------------------------------------------------------------------

def pending_retro(request: Request, case_id: str) -> c.PendingRetroResponse:
    get_case(request, case_id)
    return c.PendingRetroResponse(case_id=case_id, items=_pending_retro_items(request, case_id))


def _pending_retro_items(request: Request, case_id: str) -> list[c.PendingRetroItem]:
    """Published records whose window has elapsed but which carry no observation yet."""
    window = _learning_settings(request).retro_window_days
    now = c.utcnow()
    repo = case_rubric_repository(request)
    if repo is not None:
        records = [r for r in repo.list_publish_records(case_id) if r.status == "published"]
        observed_video_versions = {
            obs.video_version_id
            for obs in repo.list_performance_observations(case_id)
            if obs.video_version_id is not None
        }
        finished_by_version = {
            fv.id: fv
            for fv in repo.list_finished_videos(case_id)
        }
        video_versions = {vv_id: repo.resolve_video_version(vv_id) for vv_id in {
            r.video_version_id for r in records if r.video_version_id is not None
        }}
    else:
        mem = repository(request)
        records = [
            r for r in mem.publish_records.values()
            if r.case_id == case_id and r.status == "published"
        ]
        observed_video_versions = {
            obs.video_version_id
            for obs in mem.performance_observations.values()
            if obs.case_id == case_id and obs.video_version_id is not None
        }
        finished_by_version = {fv.id: fv for fv in mem.finished_videos.values() if fv.case_id == case_id}
        video_versions = {
            vv.id: vv for vv in mem.video_versions.values() if vv.case_id == case_id
        }

    items: list[c.PendingRetroItem] = []
    for record in records:
        if record.published_at is None:
            continue
        days_since = (now - record.published_at).days
        if days_since < window:
            continue
        if record.video_version_id in observed_video_versions:
            continue
        video_version = video_versions.get(record.video_version_id)
        finished_video_id = video_version.finished_video_id if video_version else None
        finished = finished_by_version.get(finished_video_id) if finished_video_id else None
        items.append(
            c.PendingRetroItem(
                id=new_id("retro"),
                case_id=case_id,
                finished_video_id=finished_video_id or "",
                publish_record_id=record.id,
                video_version_id=record.video_version_id,
                title=finished.title if finished else "",
                platform=record.platform,
                published_at=record.published_at,
                days_since_publish=days_since,
            )
        )
    return items


# ---------------------------------------------------------------------------
# Shared read helpers (symmetric across backends)
# ---------------------------------------------------------------------------

def _reward_labeled_predictions(request: Request, case_id: str) -> list[c.ScorePrediction]:
    rewards = _reward_signals(request, case_id)
    labeled: list[c.ScorePrediction] = []
    for prediction in list_predictions(request, case_id):
        reward = _latest_reward_for_prediction(prediction, rewards)
        if reward is not None:
            labeled.append(
                prediction.model_copy(
                    update={
                        "settled_reward": reward.value,
                        "settled_at": reward.occurred_at,
                    }
                )
            )
        elif prediction.settled_reward is not None:
            labeled.append(prediction)
    return labeled


def _latest_reward_for_prediction(
    prediction: c.ScorePrediction, rewards: list[c.RewardSignal]
) -> c.RewardSignal | None:
    candidates = [
        reward
        for reward in rewards
        if reward.occurred_at > prediction.locked_at
        and (
            (
                reward.script_draft_id is not None
                and reward.script_draft_id == prediction.script_draft_id
            )
            or (
                reward.script_version_id is not None
                and reward.script_version_id == prediction.script_version_id
            )
        )
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda reward: (reward.occurred_at, reward.created_at))


def _reward_signals(request: Request, case_id: str) -> list[c.RewardSignal]:
    repo = case_rubric_repository(request)
    if repo is not None:
        return repo.list_rewards(case_id)
    return [
        reward
        for reward in repository(request).reward_signals.values()
        if reward.case_id == case_id
    ]


def _calibration_samples(request: Request, case_id: str) -> list[RewardSample]:
    samples: list[RewardSample] = []
    for prediction in _reward_labeled_predictions(request, case_id):
        features = _features_for_prediction(request, case_id, prediction)
        if features is None:
            continue
        samples.append((features, float(prediction.settled_reward)))
    return samples


def _open_bump_proposal(request: Request, case_id: str) -> c.RubricBumpProposal | None:
    repo = case_rubric_repository(request)
    if repo is not None:
        return repo.get_open_bump_proposal(case_id)
    candidates = [
        p
        for p in repository(request).rubric_bump_proposals.values()
        if p.case_id == case_id and p.status == "proposed"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.created_at)


def _get_bump_proposal(
    request: Request, case_id: str, proposal_id: str
) -> c.RubricBumpProposal | None:
    repo = case_rubric_repository(request)
    if repo is not None:
        proposal = repo.get_bump_proposal(proposal_id)
    else:
        proposal = repository(request).rubric_bump_proposals.get(proposal_id)
    if proposal is None or proposal.case_id != case_id:
        return None
    return proposal


def _prediction_by_draft_memory(repo, draft_id: str) -> c.ScorePrediction | None:
    candidates = [p for p in repo.score_predictions.values() if p.script_draft_id == draft_id]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.created_at)


# -- lineage resolution -----------------------------------------------------

def _resolve_script_version_for_finished_video(
    request: Request, case_id: str, finished_video_id: str
) -> str | None:
    repo = case_rubric_repository(request)
    if repo is not None:
        return repo.resolve_script_version_for_finished_video(finished_video_id)
    version = _video_version_for_finished_video_memory(repository(request), finished_video_id)
    return version.script_version_id if version is not None else None


def _resolve_publish_lineage_for_finished_video(
    request: Request, case_id: str, finished_video_id: str
) -> tuple[str, str | None]:
    """Resolve (publish_record_id, video_version_id) for a finished video. Falls back
    to the finished_video_id as the publish_record_id when no publish record exists,
    so backfill works even before a record is created."""
    repo = case_rubric_repository(request)
    if repo is not None:
        if not any(fv.id == finished_video_id for fv in repo.list_finished_videos(case_id)):
            raise _missing_finished_video()
        for record in repo.list_publish_records(case_id):
            resolved = (
                repo.resolve_video_version(record.video_version_id)
                if record.video_version_id
                else None
            )
            if resolved is not None and resolved.finished_video_id == finished_video_id:
                return record.id, record.video_version_id
        version = repo.resolve_video_version_for_finished_video(finished_video_id)
        video_version_id = version.id if version is not None else None
        return finished_video_id, video_version_id
    mem = repository(request)
    finished = mem.finished_videos.get(finished_video_id)
    if finished is None or finished.case_id != case_id:
        raise _missing_finished_video()
    version = _video_version_for_finished_video_memory(mem, finished_video_id)
    video_version_id = version.id if version is not None else None
    if version is not None:
        for record in mem.publish_records.values():
            if record.case_id == case_id and record.video_version_id == version.id:
                return record.id, version.id
    return finished_video_id, video_version_id


def _video_version_for_finished_video_memory(repo, finished_video_id: str) -> c.VideoVersion | None:
    return next(
        (vv for vv in repo.video_versions.values() if vv.finished_video_id == finished_video_id),
        None,
    )


def _script_version_for_video_version_db(repo, video_version_id: str | None) -> str | None:
    if video_version_id is None:
        return None
    version = repo.resolve_video_version(video_version_id)
    return version.script_version_id if version is not None else None


def _script_version_for_video_version_memory(repo, video_version_id: str | None) -> str | None:
    if video_version_id is None:
        return None
    version = repo.video_versions.get(video_version_id)
    return version.script_version_id if version is not None else None


def _observation_by_id_db(repo, case_id: str, observation_id: str) -> c.PerformanceObservation | None:
    return next(
        (o for o in repo.list_performance_observations(case_id) if o.id == observation_id),
        None,
    )


def _missing(message: str):
    from packages.core.workflow import NodeExecutionError

    return NodeExecutionError(c.ErrorCode.validation_invalid_options, message)


def _missing_finished_video():
    from packages.core.workflow import NodeExecutionError

    return NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
