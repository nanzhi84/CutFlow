"""ExportFinishedVideo lipsync provider attribution.

The lipsync node (HeyGem-primary -> VideoReTalk-fallback) writes a
``LipSyncReportArtifact`` carrying the ``provider_profile_id`` that actually
produced the lipsynced video (and ``fallback_from`` when the primary failed).
ExportFinishedVideo must surface that on the FinishedVideo as a stable, profile-
agnostic ``lipsync_provider_id`` (the ProviderProfile.provider_id), plus a
``lipsync_fallback_used`` flag, so the UI can show "由 HeyGem 生成" vs the
VideoReTalk-fallback badge.

No network / no spend: the cover path is the existing frame cover and we seed the
lipsync report + provider profiles directly into the in-memory repository.
"""

from __future__ import annotations

from packages.ai.gateway.provider_gateway import ProviderGateway
from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    MediaInfo,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.contracts.artifacts import LipSyncReportArtifact
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore
from packages.media.assets import store_file
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter

# Repository() auto-seeds these real lipsync ProviderProfiles, so the export node
# can resolve profile_id -> provider_id without extra wiring.
HEYGEM_PROFILE = "runninghub.heygem.prod"
HEYGEM_PROVIDER = "runninghub.heygem"
VIDEORETALK_PROFILE = "dashscope.videoretalk.prod"
VIDEORETALK_PROVIDER = "dashscope.videoretalk"


def _adapter(tmp_path):
    repository = Repository()
    secret_store = LocalSecretStore(tmp_path / "secrets")
    object_store = LocalObjectStore(tmp_path / "objects")
    gateway = ProviderGateway(
        repository,
        secret_store=secret_store,
        object_store=object_store,
        auto_register_real_plugins=False,
    )
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    adapter.provider_gateway = gateway
    return adapter, object_store


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_attr",
        job_id="job_attr",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_export",
        run_id="run_attr",
        node_id="ExportFinishedVideo",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _seed_state(repository, object_store, factory, *, lipsync_report: dict | None, title: str | None = "归因测试") -> RunState:
    video_file = factory.video(duration_sec=1.0, filename="final.mp4")
    stored = store_file(object_store, video_file, purpose="final-video")
    final = repository.create_artifact(
        kind=ArtifactKind.video_final,
        payload_schema="uri-only",
        payload=None,
        case_id="case_demo",
        run_id="run_attr",
        uri=stored.ref.uri,
        sha256=stored.sha256,
        media_info=MediaInfo(media_type="video", codec="h264", format="mp4", duration_sec=1.0),
    )
    timeline = repository.create_artifact(
        kind=ArtifactKind.plan_timeline,
        payload_schema="TimelinePlanArtifact.v1",
        payload={"segments": []},
        case_id="case_demo",
        run_id="run_attr",
    )
    style = repository.create_artifact(
        kind=ArtifactKind.plan_style,
        payload_schema="StylePlanArtifact.v1",
        payload={"font": "default"},
        case_id="case_demo",
        run_id="run_attr",
    )
    artifacts = {
        ArtifactKind.video_final: final,
        ArtifactKind.plan_timeline: timeline,
        ArtifactKind.plan_style: style,
    }
    if lipsync_report is not None:
        report = repository.create_artifact(
            kind=ArtifactKind.lipsync_report,
            payload_schema="LipSyncReportArtifact.v1",
            payload=lipsync_report,
            case_id="case_demo",
            run_id="run_attr",
        )
        artifacts[ArtifactKind.lipsync_report] = report
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        title=title,
        publish_content="案例",
        script="第一句。第二句。",
        voice={"voice_id": "voice_sandbox"},
        cover={"mode": "frame"},
    )
    return RunState(request=request, artifacts=artifacts)


def _export(adapter, object_store, media_fixture_factory, monkeypatch, *, lipsync_report, title: str | None = "归因测试"):
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )
    state = _seed_state(adapter.repository, object_store, media_fixture_factory, lipsync_report=lipsync_report, title=title)
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    from packages.production.pipeline import nodes

    nodes.export_finished_video.run(ctx)
    return next(v for v in adapter.repository.finished_videos.values() if v.run_id == "run_attr")


def _report(**overrides) -> dict:
    base = LipSyncReportArtifact(
        provider_profile_id=HEYGEM_PROFILE,
        input_video_artifact_id="art_in_video",
        input_audio_artifact_id="art_in_audio",
        output_video_artifact_id="art_out_video",
    )
    return base.model_copy(update=overrides).model_dump(mode="json")


def test_primary_heygem_attribution(tmp_path, media_fixture_factory, monkeypatch):
    adapter, object_store = _adapter(tmp_path)
    finished = _export(
        adapter, object_store, media_fixture_factory, monkeypatch, lipsync_report=_report()
    )
    assert finished.lipsync_provider_id == HEYGEM_PROVIDER
    assert finished.lipsync_fallback_used is False
    assert finished.lipsync_fallback_reason is None


def test_blank_title_uses_generated_copy_headline(tmp_path, media_fixture_factory, monkeypatch):
    # A blank request title no longer falls back to the placeholder "未命名成片": the
    # finished video gets a generated headline. With no LLM armed the copy is derived
    # deterministically from the script ("第一句。第二句。" -> headline "第一句").
    adapter, object_store = _adapter(tmp_path)
    finished = _export(
        adapter, object_store, media_fixture_factory, monkeypatch, lipsync_report=None, title=None
    )
    assert finished.title == "第一句"


def test_fallback_videoretalk_attribution(tmp_path, media_fixture_factory, monkeypatch):
    adapter, object_store = _adapter(tmp_path)
    report = _report(
        provider_profile_id=VIDEORETALK_PROFILE,
        fallback_from=HEYGEM_PROFILE,
        fallback_to=VIDEORETALK_PROFILE,
        fallback_reason="HeyGem provider failed (HTTP 503).",
    )
    finished = _export(adapter, object_store, media_fixture_factory, monkeypatch, lipsync_report=report)
    # The provider that actually produced the video (VideoReTalk fallback) is attributed,
    # the fallback flag flips True, and the reason carries through for the UI tooltip.
    assert finished.lipsync_provider_id == VIDEORETALK_PROVIDER
    assert finished.lipsync_fallback_used is True
    assert finished.lipsync_fallback_reason == "HeyGem provider failed (HTTP 503)."


def test_no_report_leaves_defaults(tmp_path, media_fixture_factory, monkeypatch):
    adapter, object_store = _adapter(tmp_path)
    finished = _export(
        adapter, object_store, media_fixture_factory, monkeypatch, lipsync_report=None
    )
    assert finished.lipsync_provider_id is None
    assert finished.lipsync_fallback_used is False
    assert finished.lipsync_fallback_reason is None


def test_skipped_report_leaves_defaults(tmp_path, media_fixture_factory, monkeypatch):
    # Disabled / sandbox pass-through reports are ``skipped`` -> no real provider
    # produced the video, so no attribution is surfaced even if a profile id is present.
    adapter, object_store = _adapter(tmp_path)
    report = _report(skipped=True, skipped_reason="sandbox.pass_through")
    finished = _export(adapter, object_store, media_fixture_factory, monkeypatch, lipsync_report=report)
    assert finished.lipsync_provider_id is None
    assert finished.lipsync_fallback_used is False


def test_unresolvable_profile_leaves_defaults(tmp_path, media_fixture_factory, monkeypatch):
    # Report references a profile that is not in the repository -> do not crash,
    # leave attribution unset rather than leaking an unresolvable profile id.
    adapter, object_store = _adapter(tmp_path)
    report = _report(provider_profile_id="lipsync.profile.deleted")
    assert "lipsync.profile.deleted" not in adapter.repository.provider_profiles
    finished = _export(adapter, object_store, media_fixture_factory, monkeypatch, lipsync_report=report)
    assert finished.lipsync_provider_id is None
    assert finished.lipsync_fallback_used is False
