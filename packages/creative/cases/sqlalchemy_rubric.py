"""SQLAlchemy persistence for the case-rubric self-evolution loop (case_rubric_v1).

Mirrors ``sqlalchemy_learning.py`` / ``sqlalchemy_learning_mappers.py``: each contract is
rebuilt with ``schema_version`` / ``created_at`` / ``updated_at`` from its row, and
JSONB columns store ``model_dump(mode="json")``. All scoring/calibration/fit logic
stays in the storage-agnostic ``rubric.py`` pure functions; this module only does IO.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
from packages.core.storage.base_repository import BaseRepository
from packages.core.storage.performance_mappers import (
    performance_observation_row_to_contract,
    performance_observation_to_row,
    performance_score_row_to_contract,
    performance_score_to_row,
)
from packages.core.storage.repository import new_id
import packages.creative.cases.rubric as rubric
from packages.creative.cases.sqlalchemy_learning_mappers import script_version_row_to_contract


# Row -> contract mappers

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


# Contract -> row mappers

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


# Repository

class SqlAlchemyCaseRubricRepository(BaseRepository):
    """DB-backed store for rubrics, blind predictions, reward signals & bumps."""

    # -- rubrics ------------------------------------------------------------

    def ensure_active_rubric(self, case_id: str) -> CaseRubric:
        with self.session_factory() as session:
            row = self._active_rubric_row(session, case_id)
            if row is not None:
                return case_rubric_row_to_contract(row)
            card = rubric.cold_start_rubric(rubric_id=new_id("rubric"), case_id=case_id)
            new_row = _case_rubric_to_row(card)
            session.add(new_row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                row = self._active_rubric_row(session, case_id)
                if row is None:
                    raise
                return case_rubric_row_to_contract(row)
            session.refresh(new_row)
            return case_rubric_row_to_contract(new_row)

    def get_active_rubric(self, case_id: str) -> CaseRubric | None:
        with self.session_factory() as session:
            row = self._active_rubric_row(session, case_id)
            return case_rubric_row_to_contract(row) if row is not None else None

    def _active_rubric_row(self, session: Session, case_id: str) -> CaseRubricRow | None:
        statement = (
            select(CaseRubricRow)
            .where(CaseRubricRow.case_id == case_id)
            .where(CaseRubricRow.status == "active")
            .order_by(CaseRubricRow.version.desc())
        )
        return session.scalars(statement).first()

    def accept_bump(self, case_id: str, proposal_id: str) -> CaseRubric:
        with self.session_factory() as session:
            proposal = session.get(RubricBumpProposalRow, proposal_id)
            if proposal is None or proposal.case_id != case_id:
                raise KeyError(proposal_id)
            assert_transition("rubric_bump", proposal.status, "accepted")
            active = self._active_rubric_row(session, case_id)
            if active is not None:
                assert_transition("case_rubric", active.status, "superseded")
                active.status = "superseded"
                active.updated_at = utcnow()
            new_active = CaseRubric.model_validate(proposal.candidate).model_copy(
                update={"status": "active"}
            )
            row = _case_rubric_to_row(new_active)
            session.add(row)
            proposal.status = "accepted"
            proposal.updated_at = utcnow()
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
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if reward.evidence_ref is None:
                    raise
                existing = self._reward_row_by_evidence(
                    session, reward.case_id, reward.source_kind, reward.evidence_ref
                )
                if existing is None:
                    raise
                return reward_signal_row_to_contract(existing)
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

    def reward_exists(
        self, case_id: str, source_kind: RewardSourceKind, evidence_ref: str | None
    ) -> bool:
        if evidence_ref is None:
            return False
        with self.session_factory() as session:
            statement = self._reward_by_evidence_statement(case_id, source_kind, evidence_ref).limit(1)
            return session.scalars(statement).first() is not None

    def _reward_row_by_evidence(
        self,
        session: Session,
        case_id: str,
        source_kind: RewardSourceKind,
        evidence_ref: str,
    ) -> RewardSignalRow | None:
        return session.scalars(
            self._reward_by_evidence_statement(case_id, source_kind, evidence_ref)
        ).first()

    def _reward_by_evidence_statement(
        self, case_id: str, source_kind: RewardSourceKind, evidence_ref: str
    ):
        return (
            select(RewardSignalRow)
            .where(RewardSignalRow.case_id == case_id)
            .where(RewardSignalRow.source_kind == source_kind)
            .where(RewardSignalRow.evidence_ref == evidence_ref)
        )

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
            session.add(performance_observation_to_row(observation))
            session.add(performance_score_to_row(score))
            session.commit()

    def list_performance_observations(self, case_id: str) -> list[PerformanceObservation]:
        with self.session_factory() as session:
            statement = select(PerformanceObservationRow).where(
                PerformanceObservationRow.case_id == case_id
            )
            return [performance_observation_row_to_contract(row) for row in session.scalars(statement)]

    def list_performance_scores(self, case_id: str) -> list[PerformanceScore]:
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

    def resolve_video_version(self, case_id: str, video_version_id: str):
        from packages.creative.cases.sqlalchemy_learning_mappers import video_version_row_to_contract

        with self.session_factory() as session:
            statement = (
                select(VideoVersionRow)
                .where(VideoVersionRow.id == video_version_id)
                .where(VideoVersionRow.case_id == case_id)
            )
            row = session.scalars(statement).first()
            return video_version_row_to_contract(row) if row is not None else None

    def resolve_video_version_for_finished_video(self, case_id: str, finished_video_id: str):
        from packages.creative.cases.sqlalchemy_learning_mappers import video_version_row_to_contract

        with self.session_factory() as session:
            statement = (
                select(VideoVersionRow)
                .where(VideoVersionRow.case_id == case_id)
                .where(VideoVersionRow.finished_video_id == finished_video_id)
                .order_by(VideoVersionRow.created_at.desc())
            )
            row = session.scalars(statement).first()
            return video_version_row_to_contract(row) if row is not None else None

    def get_script_version(self, case_id: str, script_version_id: str) -> ScriptVersion | None:
        with self.session_factory() as session:
            statement = (
                select(ScriptVersionRow)
                .where(ScriptVersionRow.id == script_version_id)
                .where(ScriptVersionRow.case_id == case_id)
            )
            row = session.scalars(statement).first()
            return script_version_row_to_contract(row) if row is not None else None

    # -- lineage ------------------------------------------------------------

    def resolve_script_version_for_finished_video(
        self, case_id: str, finished_video_id: str
    ) -> str | None:
        """FinishedVideo -> VideoVersion(by finished_video_id) -> script_version_id."""
        with self.session_factory() as session:
            statement = (
                select(FinishedVideoRow.id)
                .where(FinishedVideoRow.id == finished_video_id)
                .where(FinishedVideoRow.case_id == case_id)
            )
            finished = session.scalars(statement).first()
            if finished is None:
                return None
        version = self.resolve_video_version_for_finished_video(case_id, finished_video_id)
        return version.script_version_id if version is not None else None
