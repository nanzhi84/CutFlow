"""SQLAlchemy persistence for the case-rubric self-evolution loop (case_rubric_v1).

Mirrors ``sqlalchemy_learning.py`` / ``sqlalchemy_learning_mappers.py``: each contract is
rebuilt with ``schema_version`` / ``created_at`` / ``updated_at`` from its row, and
JSONB columns store ``model_dump(mode="json")``. All scoring/calibration/fit logic
stays in the storage-agnostic ``rubric.py`` pure functions; this module only does IO.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import (
    CaseRubric,
    PerformanceObservation,
    PerformanceScore,
    RewardSignal,
    RewardSourceKind,
    RubricBumpProposal,
    RubricDimension,
    ScriptVersion,
    ScorePrediction,
    utcnow,
)
from packages.core.contracts.state_machines import assert_transition
from packages.core.storage.database import (
    CaseRubricRow,
    FinishedVideoRow,
    PerformanceObservationRow,
    PerformanceScoreRow,
    RewardSignalRow,
    RubricBumpProposalRow,
    ScriptVersionRow,
    ScorePredictionRow,
    VideoVersionRow,
)
from packages.core.storage.repository import new_id
import packages.creative.cases.rubric as rubric
from packages.creative.cases.sqlalchemy_learning_mappers import script_version_row_to_contract


# ---------------------------------------------------------------------------
# Row -> contract mappers
# ---------------------------------------------------------------------------

def case_rubric_row_to_contract(row: CaseRubricRow) -> CaseRubric:
    return CaseRubric(
        id=row.id,
        case_id=row.case_id,
        version=row.version,
        status=row.status,
        dimensions=[RubricDimension.model_validate(d) for d in (row.dimensions or [])],
        fitted_from_sample_size=row.fitted_from_sample_size,
        cold_start=row.cold_start,
        supersedes_version=row.supersedes_version,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def score_prediction_row_to_contract(row: ScorePredictionRow) -> ScorePrediction:
    return ScorePrediction(
        id=row.id,
        case_id=row.case_id,
        script_draft_id=row.script_draft_id,
        script_version_id=row.script_version_id,
        rubric_version=row.rubric_version,
        composite=row.composite,
        band=row.band,
        dimension_scores=dict(row.dimension_scores or {}),
        reason=row.reason,
        locked_at=row.locked_at,
        settled_reward=row.settled_reward,
        settled_at=row.settled_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def reward_signal_row_to_contract(row: RewardSignalRow) -> RewardSignal:
    return RewardSignal(
        id=row.id,
        case_id=row.case_id,
        script_version_id=row.script_version_id,
        script_draft_id=row.script_draft_id,
        source_kind=row.source_kind,
        value=row.value,
        confidence=row.confidence,
        evidence_ref=row.evidence_ref,
        reason=row.reason,
        occurred_at=row.occurred_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def rubric_bump_proposal_row_to_contract(row: RubricBumpProposalRow) -> RubricBumpProposal:
    return RubricBumpProposal(
        id=row.id,
        case_id=row.case_id,
        status=row.status,
        from_version=row.from_version,
        candidate=CaseRubric.model_validate(row.candidate),
        old_consistency=row.old_consistency,
        new_consistency=row.new_consistency,
        sample_size=row.sample_size,
        rationale=row.rationale,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
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


# ---------------------------------------------------------------------------
# Contract -> row mappers
# ---------------------------------------------------------------------------

def _case_rubric_to_row(rubric_card: CaseRubric) -> CaseRubricRow:
    return CaseRubricRow(
        id=rubric_card.id,
        case_id=rubric_card.case_id,
        version=rubric_card.version,
        status=rubric_card.status,
        dimensions=[d.model_dump(mode="json") for d in rubric_card.dimensions],
        fitted_from_sample_size=rubric_card.fitted_from_sample_size,
        cold_start=rubric_card.cold_start,
        supersedes_version=rubric_card.supersedes_version,
    )


def _score_prediction_to_row(pred: ScorePrediction) -> ScorePredictionRow:
    return ScorePredictionRow(
        id=pred.id,
        case_id=pred.case_id,
        script_draft_id=pred.script_draft_id,
        script_version_id=pred.script_version_id,
        rubric_version=pred.rubric_version,
        composite=pred.composite,
        band=pred.band,
        dimension_scores=dict(pred.dimension_scores or {}),
        reason=pred.reason,
        locked_at=pred.locked_at,
        settled_reward=pred.settled_reward,
        settled_at=pred.settled_at,
    )


def _reward_signal_to_row(reward: RewardSignal) -> RewardSignalRow:
    return RewardSignalRow(
        id=reward.id,
        case_id=reward.case_id,
        script_version_id=reward.script_version_id,
        script_draft_id=reward.script_draft_id,
        source_kind=reward.source_kind,
        value=reward.value,
        confidence=reward.confidence,
        evidence_ref=reward.evidence_ref,
        reason=reward.reason,
        occurred_at=reward.occurred_at,
    )


def _rubric_bump_proposal_to_row(proposal: RubricBumpProposal) -> RubricBumpProposalRow:
    return RubricBumpProposalRow(
        id=proposal.id,
        case_id=proposal.case_id,
        status=proposal.status,
        from_version=proposal.from_version,
        candidate=proposal.candidate.model_dump(mode="json"),
        old_consistency=proposal.old_consistency,
        new_consistency=proposal.new_consistency,
        sample_size=proposal.sample_size,
        rationale=proposal.rationale,
    )


def _performance_observation_to_row(observation: PerformanceObservation) -> PerformanceObservationRow:
    return PerformanceObservationRow(
        id=observation.id,
        case_id=observation.case_id,
        publish_record_id=observation.publish_record_id,
        video_version_id=observation.video_version_id,
        platform=observation.platform,
        account_id=observation.account_id,
        window=observation.window,
        metric_name=observation.metric_name,
        metric_value=observation.metric_value,
        impressions=observation.impressions,
        views=observation.views,
        avg_watch_sec=observation.avg_watch_sec,
        completion_rate=observation.completion_rate,
        like_rate=observation.like_rate,
        comment_rate=observation.comment_rate,
        share_rate=observation.share_rate,
        follow_rate=observation.follow_rate,
        conversion_count=observation.conversion_count,
        conversion_rate=observation.conversion_rate,
        raw_metrics=dict(observation.raw_metrics or {}),
        observed_at=observation.observed_at,
    )


def _performance_score_to_row(score: PerformanceScore) -> PerformanceScoreRow:
    return PerformanceScoreRow(
        id=score.id,
        observation_id=score.observation_id,
        case_id=score.case_id,
        video_version_id=score.video_version_id,
        platform=score.platform,
        account_id=score.account_id,
        window=score.window,
        primary_metric=score.primary_metric,
        normalized_score=score.normalized_score,
        confidence=score.confidence,
        sample_size=score.sample_size,
        excluded_reason=score.excluded_reason,
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class SqlAlchemyCaseRubricRepository:
    """DB-backed store for rubrics, blind predictions, reward signals & bumps."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    # -- rubrics ------------------------------------------------------------

    def ensure_active_rubric(self, case_id: str) -> CaseRubric:
        with self.session_factory() as session:
            row = self._active_rubric_row(session, case_id)
            if row is not None:
                return case_rubric_row_to_contract(row)
            card = rubric.cold_start_rubric(rubric_id=new_id("rubric"), case_id=case_id)
            new_row = _case_rubric_to_row(card)
            session.add(new_row)
            session.commit()
            session.refresh(new_row)
            return case_rubric_row_to_contract(new_row)

    def get_active_rubric(self, case_id: str) -> CaseRubric | None:
        with self.session_factory() as session:
            row = self._active_rubric_row(session, case_id)
            return case_rubric_row_to_contract(row) if row is not None else None

    def list_rubrics(self, case_id: str) -> list[CaseRubric]:
        with self.session_factory() as session:
            statement = (
                select(CaseRubricRow)
                .where(CaseRubricRow.case_id == case_id)
                .order_by(CaseRubricRow.version.desc())
            )
            return [case_rubric_row_to_contract(row) for row in session.scalars(statement)]

    def _active_rubric_row(self, session: Session, case_id: str) -> CaseRubricRow | None:
        statement = (
            select(CaseRubricRow)
            .where(CaseRubricRow.case_id == case_id)
            .where(CaseRubricRow.status == "active")
            .order_by(CaseRubricRow.version.desc())
        )
        return session.scalars(statement).first()

    def supersede_active(self, case_id: str) -> None:
        with self.session_factory() as session:
            row = self._active_rubric_row(session, case_id)
            if row is None:
                return
            assert_transition("case_rubric", row.status, "superseded")
            row.status = "superseded"
            row.updated_at = utcnow()
            session.commit()

    def add_rubric(self, card: CaseRubric) -> CaseRubric:
        with self.session_factory() as session:
            row = _case_rubric_to_row(card)
            session.add(row)
            session.commit()
            session.refresh(row)
            return case_rubric_row_to_contract(row)

    # -- predictions --------------------------------------------------------

    def add_prediction(self, pred: ScorePrediction) -> ScorePrediction:
        with self.session_factory() as session:
            row = _score_prediction_to_row(pred)
            session.add(row)
            session.commit()
            session.refresh(row)
            return score_prediction_row_to_contract(row)

    def update_prediction(self, pred: ScorePrediction) -> ScorePrediction:
        with self.session_factory() as session:
            row = session.get(ScorePredictionRow, pred.id)
            if row is None:
                row = _score_prediction_to_row(pred)
                session.add(row)
            else:
                # Honor the blind invariant: never mutate the locked composite /
                # band / dimension_scores; only the linkage + settlement fields.
                row.script_version_id = pred.script_version_id
                row.settled_reward = pred.settled_reward
                row.settled_at = pred.settled_at
                row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return score_prediction_row_to_contract(row)

    def get_prediction_by_draft(self, draft_id: str) -> ScorePrediction | None:
        with self.session_factory() as session:
            statement = (
                select(ScorePredictionRow)
                .where(ScorePredictionRow.script_draft_id == draft_id)
                .order_by(ScorePredictionRow.created_at.desc())
            )
            row = session.scalars(statement).first()
            return score_prediction_row_to_contract(row) if row is not None else None

    def list_predictions(self, case_id: str) -> list[ScorePrediction]:
        with self.session_factory() as session:
            statement = (
                select(ScorePredictionRow)
                .where(ScorePredictionRow.case_id == case_id)
                .order_by(ScorePredictionRow.created_at.desc())
            )
            return [score_prediction_row_to_contract(row) for row in session.scalars(statement)]

    # -- rewards ------------------------------------------------------------

    def add_reward(self, reward: RewardSignal) -> RewardSignal:
        with self.session_factory() as session:
            row = _reward_signal_to_row(reward)
            session.add(row)
            session.commit()
            session.refresh(row)
            return reward_signal_row_to_contract(row)

    def list_rewards(self, case_id: str) -> list[RewardSignal]:
        with self.session_factory() as session:
            statement = (
                select(RewardSignalRow)
                .where(RewardSignalRow.case_id == case_id)
                .order_by(RewardSignalRow.occurred_at.desc())
            )
            return [reward_signal_row_to_contract(row) for row in session.scalars(statement)]

    def reward_exists(self, source_kind: RewardSourceKind, evidence_ref: str | None) -> bool:
        if evidence_ref is None:
            return False
        with self.session_factory() as session:
            statement = (
                select(RewardSignalRow.id)
                .where(RewardSignalRow.source_kind == source_kind)
                .where(RewardSignalRow.evidence_ref == evidence_ref)
                .limit(1)
            )
            return session.scalars(statement).first() is not None

    # -- bump proposals -----------------------------------------------------

    def add_bump_proposal(self, proposal: RubricBumpProposal) -> RubricBumpProposal:
        with self.session_factory() as session:
            row = _rubric_bump_proposal_to_row(proposal)
            session.add(row)
            session.commit()
            session.refresh(row)
            return rubric_bump_proposal_row_to_contract(row)

    def get_open_bump_proposal(self, case_id: str) -> RubricBumpProposal | None:
        with self.session_factory() as session:
            statement = (
                select(RubricBumpProposalRow)
                .where(RubricBumpProposalRow.case_id == case_id)
                .where(RubricBumpProposalRow.status == "proposed")
                .order_by(RubricBumpProposalRow.created_at.desc())
            )
            row = session.scalars(statement).first()
            return rubric_bump_proposal_row_to_contract(row) if row is not None else None

    def get_bump_proposal(self, proposal_id: str) -> RubricBumpProposal | None:
        with self.session_factory() as session:
            row = session.get(RubricBumpProposalRow, proposal_id)
            return rubric_bump_proposal_row_to_contract(row) if row is not None else None

    def update_bump_proposal(self, proposal: RubricBumpProposal) -> RubricBumpProposal:
        with self.session_factory() as session:
            row = session.get(RubricBumpProposalRow, proposal.id)
            if row is None:
                row = _rubric_bump_proposal_to_row(proposal)
                session.add(row)
            else:
                row.status = proposal.status
                row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return rubric_bump_proposal_row_to_contract(row)

    # -- performance (reuse existing row shapes) ----------------------------

    def add_performance(self, observation: PerformanceObservation, score: PerformanceScore) -> None:
        with self.session_factory() as session:
            session.add(_performance_observation_to_row(observation))
            session.add(_performance_score_to_row(score))
            session.commit()

    def list_performance_observations(self, case_id: str) -> list[PerformanceObservation]:
        with self.session_factory() as session:
            statement = select(PerformanceObservationRow).where(
                PerformanceObservationRow.case_id == case_id
            )
            return [_performance_observation_row_to_contract(row) for row in session.scalars(statement)]

    def list_performance_scores(self, case_id: str) -> list[PerformanceScore]:
        # Reuse the production mapper (imported lazily to avoid a package-level
        # creative→production import edge).
        from packages.production.sqlalchemy_mappers import performance_score_row_to_contract

        with self.session_factory() as session:
            statement = select(PerformanceScoreRow).where(PerformanceScoreRow.case_id == case_id)
            return [performance_score_row_to_contract(row) for row in session.scalars(statement)]

    # -- finished videos / publish records (read; reuse existing mappers) ----

    def list_finished_videos(self, case_id: str):
        from packages.production.sqlalchemy_mappers import finished_video_row_to_contract

        with self.session_factory() as session:
            statement = select(FinishedVideoRow).where(FinishedVideoRow.case_id == case_id)
            return [finished_video_row_to_contract(row) for row in session.scalars(statement)]

    def list_publish_records(self, case_id: str):
        from packages.publishing.sqlalchemy_mappers import publish_record_row_to_contract
        from packages.core.storage.database import PublishRecordRow

        with self.session_factory() as session:
            statement = select(PublishRecordRow).where(PublishRecordRow.case_id == case_id)
            return [publish_record_row_to_contract(row) for row in session.scalars(statement)]

    def resolve_video_version(self, video_version_id: str):
        from packages.creative.cases.sqlalchemy_learning_mappers import video_version_row_to_contract

        with self.session_factory() as session:
            row = session.get(VideoVersionRow, video_version_id)
            return video_version_row_to_contract(row) if row is not None else None

    def resolve_video_version_for_finished_video(self, finished_video_id: str):
        from packages.creative.cases.sqlalchemy_learning_mappers import video_version_row_to_contract

        with self.session_factory() as session:
            statement = (
                select(VideoVersionRow)
                .where(VideoVersionRow.finished_video_id == finished_video_id)
                .order_by(VideoVersionRow.created_at.desc())
            )
            row = session.scalars(statement).first()
            return video_version_row_to_contract(row) if row is not None else None

    def get_script_version(self, script_version_id: str) -> ScriptVersion | None:
        with self.session_factory() as session:
            row = session.get(ScriptVersionRow, script_version_id)
            return script_version_row_to_contract(row) if row is not None else None

    # -- lineage ------------------------------------------------------------

    def resolve_script_version_for_finished_video(self, finished_video_id: str) -> str | None:
        """FinishedVideo -> VideoVersion(by finished_video_id) -> script_version_id."""
        with self.session_factory() as session:
            finished = session.get(FinishedVideoRow, finished_video_id)
            if finished is None:
                return None
        version = self.resolve_video_version_for_finished_video(finished_video_id)
        return version.script_version_id if version is not None else None
