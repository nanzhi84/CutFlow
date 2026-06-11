import pytest
from pydantic import ValidationError

from packages.core.contracts import Artifact, ArtifactKind, ArtifactRef, MediaInfo
from packages.core.contracts.artifacts import ArtifactSchemaRegistry


JSON_ARTIFACT_KINDS = {
    ArtifactKind.uploaded_file,
    ArtifactKind.validated_production_spec,
    ArtifactKind.case_context,
    ArtifactKind.case_performance_analysis,
    ArtifactKind.case_reflection,
    ArtifactKind.script_strategy,
    ArtifactKind.creative_intent,
    ArtifactKind.audio_alignment_raw,
    ArtifactKind.audio_alignment,
    ArtifactKind.narration_units,
    ArtifactKind.material_pack,
    ArtifactKind.portrait_plan,
    ArtifactKind.broll_plan,
    ArtifactKind.style_plan,
    ArtifactKind.timeline_plan,
    ArtifactKind.render_plan,
    ArtifactKind.lipsync_report,
    ArtifactKind.editor_handoff_package,
    ArtifactKind.jianying_draft_package,
    ArtifactKind.publish_package,
    ArtifactKind.run_public_report,
    ArtifactKind.run_debug_report,
    ArtifactKind.provider_raw_request,
    ArtifactKind.provider_raw_response,
    ArtifactKind.import_mapping,
}


URI_ONLY_KINDS = {
    ArtifactKind.audio_tts,
    ArtifactKind.video_portrait_track,
    ArtifactKind.video_lipsync,
    ArtifactKind.video_rendered,
    ArtifactKind.video_final,
    ArtifactKind.video_finished,
    ArtifactKind.subtitle_ass,
    ArtifactKind.cover_image,
}


def test_every_json_artifact_kind_has_registry_model():
    registry = ArtifactSchemaRegistry.default()

    for kind in JSON_ARTIFACT_KINDS:
        model = registry.model_for(kind, "v1")
        assert model.__name__.endswith("Artifact")


def test_uri_only_artifact_requires_uri_sha256_and_media_info():
    registry = ArtifactSchemaRegistry.default()

    with pytest.raises(ValidationError):
        MediaInfo(media_type="video", codec="h264")

    media_info = MediaInfo(
        media_type="video",
        codec="h264",
        format="mp4",
        duration_sec=3.0,
        fps=30,
    )
    artifact = Artifact(
        id="art_video",
        kind=ArtifactKind.video_final,
        uri="local://video.mp4",
        sha256="abc123",
        media_info=media_info,
        payload_schema="uri-only",
        payload=None,
    )

    assert registry.validate_artifact(artifact) is artifact
    with pytest.raises(ValidationError):
        registry.validate_artifact(artifact.model_copy(update={"sha256": None}))
    with pytest.raises(ValidationError):
        registry.validate_artifact(artifact.model_copy(update={"media_info": None}))


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
