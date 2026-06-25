
from packages.production.finished_video_numbering import next_finished_video_number


def test_next_finished_video_number_ignores_other_formats_and_increments() -> None:
    assert next_finished_video_number([None, "", "V-001", "draft", "V-010"]) == "V-011"
