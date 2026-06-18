from packages.core import contracts as c


def test_finished_video_contract_serializes_stable_video_number() -> None:
    video = c.FinishedVideo(
        id="fv_demo",
        case_id="case_demo",
        title="产品介绍",
        video_number="V-007",
        video_artifact=c.ArtifactRef(
            artifact_id="art_video",
            kind=c.ArtifactKind.video_finished,
            uri="local://video.mp4",
        ),
    )

    assert video.model_dump(mode="json")["video_number"] == "V-007"
