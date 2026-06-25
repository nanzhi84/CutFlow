import pytest
from pydantic import ValidationError

from packages.core.contracts import ArtifactKind, ArtifactRef
from packages.core.contracts.artifacts import BrollOverlay


def test_artifact_ref_requires_uri():
    ref = ArtifactRef(
        artifact_id="art_1",
        kind=ArtifactKind.video_final,
        uri="local://video.mp4",
        schema_version="v1",
        sha256="abc123",
    )

    assert ref.uri == "local://video.mp4"
    with pytest.raises(ValidationError):
        ArtifactRef(artifact_id="art_1", kind=ArtifactKind.video_final)


def test_broll_overlay_accepts_clip_id():
    overlay = BrollOverlay(
        overlay_id="broll_1",
        asset_id="asset_broll_demo",
        clip_id="cover_a",
        timeline_start=0,
        timeline_end=2,
        source_start=0,
        source_end=2,
        reason="matched",
        confidence=0.8,
    )

    assert overlay.clip_id == "cover_a"
