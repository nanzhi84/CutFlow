from sqlalchemy import UniqueConstraint

from packages.core.storage.database import FinishedVideoRow
from packages.production.finished_video_numbering import next_finished_video_number


def test_next_finished_video_number_ignores_other_formats_and_increments() -> None:
    assert next_finished_video_number([None, "", "V-001", "legacy", "V-010"]) == "V-011"


def test_finished_video_number_is_unique_per_case() -> None:
    constraints = [
        constraint
        for constraint in FinishedVideoRow.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    ]

    assert any(
        constraint.name == "uq_finished_videos_case_video_number"
        and [column.name for column in constraint.columns] == ["case_id", "video_number"]
        for constraint in constraints
    )
