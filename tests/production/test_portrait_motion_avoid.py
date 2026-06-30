from __future__ import annotations

from types import SimpleNamespace

from packages.ai.gateway import ProviderGateway
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    AnnotationEditorVm,
    AnnotationMetaV4,
    AnnotationV4,
    ArtifactKind,
    ClipRetrievalV4,
    ClipSemanticsV4,
    ClipUsageV4,
    ClipV4,
    DigitalHumanVideoRequest,
    MediaAssetRecord,
    MediaInfo,
    NodeRun,
    NodeStatus,
    QualityEventType,
    QualityEventV4,
    RunStatus,
    UsageRole,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.production.pipeline import nodes
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter


def _window_ctx(*, duration: float = 10.0):
    source = SimpleNamespace(media_info=SimpleNamespace(duration_sec=duration))
    return SimpleNamespace(source_artifact_for_asset=lambda asset_id: source)


def _portrait_item(*, start: float = 0.0, end: float = 8.0, avoid_spans=None, recent_usage=None) -> dict:
    metadata = {"clip_id": "talk", "source_start": start, "source_end": end}
    if avoid_spans is not None:
        metadata["avoid_spans"] = avoid_spans
    if recent_usage is not None:
        metadata["recent_usage"] = recent_usage
    return {"asset_id": "asset_portrait", "score": 1.0, "metadata": metadata}


def test_portrait_window_candidates_carries_material_pack_recent_usage_without_ledger():
    # The node no longer reads the ledger: it consumes the ``recent_usage`` context
    # MaterialPackPlanning already stamped onto the candidate metadata, carrying it
    # verbatim and deriving the planner ``recency_penalty`` from it.
    recent_usage = {
        "is_recently_used": True,
        "recency_penalty": 0.42,
        "exact_recency_penalty": 0.42,
        "similarity_penalty": 0.0,
        "summary": "最近 3 条同案例选择中出现 1 次（1 条视频）。",
        "history_task_count": 1,
        "history_segment_count": 1,
        "recent_task_use_count": 1,
        "recent_segment_use_count": 1,
        "recent_opening_use_count": 1,
        "similar_recent_task_use_count": 0,
        "similar_recent_segment_use_count": 0,
        "similar_recent_opening_use_count": 0,
    }
    candidates = nodes.portrait_planning._portrait_window_candidates(
        _window_ctx(duration=10.0),
        [_portrait_item(start=1.25, end=5.75, recent_usage=recent_usage)],
    )

    assert candidates == [
        {
            "window_id": "asset_portrait:talk",
            "template_id": "asset_portrait",
            "template_name": "asset_portrait",
            "start": 1.25,
            "end": 5.75,
            "duration": 4.5,
            "role": "main",
            "confidence": 0.9,
            "source_mode_hint": "lipsynced",
            "recent_usage": recent_usage,
            "recency_penalty": 0.42,
            "diversity_key": None,
        }
    ]


def test_portrait_window_candidates_defaults_recent_usage_when_metadata_omits_it():
    # Missing ``recent_usage`` (e.g. an older material pack) degrades to "fresh" —
    # no demotion — rather than the node reaching back to the ledger.
    candidates = nodes.portrait_planning._portrait_window_candidates(
        _window_ctx(duration=10.0),
        [_portrait_item(start=1.25, end=5.75)],
    )

    assert candidates[0]["recent_usage"] == {}
    assert candidates[0]["recency_penalty"] == 0.0


def test_portrait_window_candidates_uses_clean_head_before_tail_bad_span():
    candidates = nodes.portrait_planning._portrait_window_candidates(
        _window_ctx(duration=10.0),
        [_portrait_item(start=0.0, end=8.0, avoid_spans=[[4.2, 8.0]])],
    )

    assert len(candidates) == 1
    assert candidates[0]["window_id"] == "asset_portrait:talk"
    assert candidates[0]["start"] == 0.0
    assert candidates[0]["end"] == 4.2
    assert candidates[0]["duration"] == 4.2


def test_portrait_window_candidates_splits_middle_bad_span_into_multiple_windows():
    candidates = nodes.portrait_planning._portrait_window_candidates(
        _window_ctx(duration=10.0),
        [_portrait_item(start=0.0, end=6.0, avoid_spans=[[2.0, 4.0]])],
    )

    assert [(c["window_id"], c["start"], c["end"], c["duration"]) for c in candidates] == [
        ("asset_portrait:talk", 0.0, 2.0, 2.0),
        ("asset_portrait:talk:m1", 4.0, 6.0, 2.0),
    ]


def _quality_event(event_id: str, event_type: QualityEventType, start: float, end: float):
    return QualityEventV4(
        event_id=event_id,
        event_type=event_type,
        start=start,
        end=end,
        risk_tier="hard",
        confidence=0.9,
        severity=0.8,
        source="motion_guard",
    )


def _talk_clip(segment_id: str, start: float, end: float) -> ClipV4:
    return ClipV4(
        segment_id=segment_id,
        start=start,
        end=end,
        duration=end - start,
        semantics=ClipSemanticsV4(subject_type="person", face_count_max=1),
        usage=ClipUsageV4(role=UsageRole.main, recommended_for_lip_sync=True),
        retrieval=ClipRetrievalV4(summary="口播", keywords=["口播"], retrieval_sentence="口播"),
        confidence=0.9,
    )


def _adapter(object_store: LocalObjectStore) -> LocalRuntimeAdapter:
    repository = Repository()
    return LocalRuntimeAdapter(
        repository,
        provider_gateway=ProviderGateway(repository, object_store=object_store),
        prompt_registry=PromptRegistry(repository),
        seed_media=False,
    )


def _request() -> DigitalHumanVideoRequest:
    return DigitalHumanVideoRequest(
        case_id="case_demo",
        script="展示打磨工艺。",
        voice={"voice_id": "voice_sandbox"},
        portrait={"template_mode": "agent"},
        broll={"enabled": False},
        strictness={"strict_timestamps": False},
    )


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_1",
        job_id="job_1",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_material",
        run_id="run_1",
        node_id="MaterialPackPlanning",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _material_ctx(adapter: LocalRuntimeAdapter) -> NodeContext:
    return NodeContext(
        adapter=adapter,
        run=_run(),
        node_run=_node_run(),
        state=RunState(request=_request(), artifacts={}),
    )


def _inject_video_asset(repository: Repository) -> None:
    asset_id = "asset_motion_portrait"
    clip = _talk_clip("talk", 0.0, 8.0)
    source = repository.create_artifact(
        kind=ArtifactKind.uploaded_file,
        payload_schema="UploadedFileArtifact.v1",
        payload={"filename": "portrait.mp4", "object_uri": "memory://portrait"},
        case_id="case_demo",
        uri="memory://portrait",
        media_info=MediaInfo(
            media_type="video",
            codec="h264",
            format="mp4",
            mime_type="video/mp4",
            duration_sec=8.0,
            width=320,
            height=568,
            fps=30,
        ),
    )
    asset = MediaAssetRecord(
        id=asset_id,
        case_id="case_demo",
        title="motion portrait",
        kind="video",
        source_artifact_id=source.id,
        usable=True,
    )
    annotation = AnnotationV4(
        meta=AnnotationMetaV4(
            asset_id=asset_id,
            case_id="case_demo",
            material_type="video",
            duration=8.0,
        ),
        clips=[clip],
        quality_events=[
            _quality_event("shake_tail", QualityEventType.shake, 6.0, 8.0),
        ],
    )
    repository.media_assets[asset_id] = asset
    repository.annotations[asset_id] = AnnotationEditorVm(
        asset=asset,
        etag="etag1",
        canonical=annotation,
        projection={},
    )


def test_material_pack_adds_portrait_avoid_spans_and_motion_diagnostic(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(adapter.repository)

    output = nodes.material_pack_planning.run(_material_ctx(adapter))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    portrait = payload["portrait_candidates"]
    assert len(portrait) == 1
    assert portrait[0]["metadata"]["clip_id"] == "talk"
    assert portrait[0]["metadata"]["avoid_spans"] == [[6.0, 8.0]]
    assert payload["diagnostics"]["portrait_motion_excluded"] == 1
