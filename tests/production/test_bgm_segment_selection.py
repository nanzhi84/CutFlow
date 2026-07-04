from __future__ import annotations

from packages.ai.gateway import ProviderGateway
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    AnnotationEditorVm,
    AnnotationMetaV4,
    AnnotationV4,
    Artifact,
    ArtifactKind,
    BgmSegmentV4,
    DigitalHumanVideoRequest,
    MediaAssetRecord,
    MediaInfo,
    NodeRun,
    NodeStatus,
    RunStatus,
    SelectionLedgerEntry,
    WorkflowRun,
    utcnow,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.production.pipeline import nodes
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter


def _adapter(object_store: LocalObjectStore) -> LocalRuntimeAdapter:
    repo = Repository()
    return LocalRuntimeAdapter(
        repo,
        provider_gateway=ProviderGateway(repo, object_store=object_store),
        prompt_registry=PromptRegistry(repo),
    )


def _request(**overrides) -> DigitalHumanVideoRequest:
    base = dict(
        case_id="case_demo",
        script="先讲开场活动，再讲产品亮点，最后提醒到店。",
        voice={"voice_id": "voice_sandbox"},
        broll={"enabled": True},
        bgm={"enabled": True},
        strictness={"strict_timestamps": False},
    )
    base.update(overrides)
    return DigitalHumanVideoRequest(**base)


def _ctx(adapter, request, node_id: str) -> NodeContext:
    state = RunState(request=request, artifacts={})
    run = WorkflowRun(
        id="run_bgm",
        job_id="job_bgm",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )
    node_run = NodeRun(
        id=f"nr_{node_id}",
        run_id=run.id,
        node_id=node_id,
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )
    return NodeContext(adapter=adapter, run=run, node_run=node_run, state=state)


def _artifact(kind: ArtifactKind, payload: dict) -> Artifact:
    return Artifact(
        id=f"art_{kind.value.replace('.', '_')}",
        run_id="run_bgm",
        kind=kind,
        payload_schema=f"{kind.value}.v1",
        payload=payload,
    )


def _inject_bgm_asset(repo: Repository, asset_id: str, segments: list[BgmSegmentV4]) -> None:
    duration = max((segment.end for segment in segments), default=0.0)
    source = repo.create_artifact(
        kind=ArtifactKind.uploaded_file,
        payload_schema="UploadedFileArtifact.v1",
        payload={"filename": f"{asset_id}.mp3", "object_uri": f"memory://{asset_id}"},
        case_id="case_demo",
        uri=f"memory://{asset_id}",
        media_info=MediaInfo(
            media_type="audio",
            codec="mp3",
            format="mp3",
            mime_type="audio/mpeg",
            duration_sec=duration,
        ),
    )
    asset = MediaAssetRecord(
        id=asset_id,
        case_id="case_demo",
        title="Segmented BGM",
        kind="bgm",
        source_artifact_id=source.id,
        usable=True,
    )
    repo.media_assets[asset_id] = asset
    repo.annotations[asset_id] = AnnotationEditorVm(
        asset=asset,
        etag="etag_bgm",
        projection={},
        canonical=AnnotationV4(
            meta=AnnotationMetaV4(
                asset_id=asset_id,
                case_id="case_demo",
                material_type="bgm",
                duration=duration,
            ),
            bgm_segments=segments,
        ),
    )


def _segment(segment_id: str, start: float, end: float, **overrides) -> BgmSegmentV4:
    return BgmSegmentV4(
        segment_id=segment_id,
        start=start,
        end=end,
        duration=end - start,
        role=overrides.pop("role", "general"),
        mood=overrides.pop("mood", "明快"),
        scene_fit=overrides.pop("scene_fit", ["短视频", "产品展示"]),
        reason=overrides.pop("reason", "适合铺底"),
        energy=overrides.pop("energy", 0.55),
        **overrides,
    )


def test_material_pack_offers_one_bgm_candidate_per_annotated_segment(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_bgm_asset(
        adapter.repository,
        "asset_bgm_song",
        [
            _segment("bgm_segment_1", 0.0, 58.0, role="hook", mood="轻快开场"),
            _segment("bgm_segment_2", 58.0, 118.0, role="climax", mood="高能推进"),
        ],
    )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    candidates = payload["bgm_candidates"]
    assert [(c["asset_id"], c["metadata"]["clip_id"]) for c in candidates] == [
        ("asset_bgm_song", "bgm_segment_1"),
        ("asset_bgm_song", "bgm_segment_2"),
    ]
    first = candidates[0]["metadata"]
    assert first["source_start"] == 0.0
    assert first["source_end"] == 58.0
    assert first["duration"] == 58.0
    assert first["role"] == "hook"
    assert first["mood"] == "轻快开场"
    assert first["scene_fit"] == ["短视频", "产品展示"]


def test_bgm_segment_recency_is_metadata_not_material_pack_ranking(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_bgm_asset(
        adapter.repository,
        "asset_bgm_song",
        [
            _segment("bgm_segment_1", 0.0, 58.0),
            _segment("bgm_segment_2", 58.0, 118.0),
        ],
    )
    adapter.repository.record_selection_ledger_entries(
        [
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_previous",
                medium="bgm",
                asset_id="asset_bgm_song",
                clip_id="bgm_segment_1",
                slot_phase="bgm",
                created_at=utcnow(),
            )
        ]
    )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    assert [c["metadata"]["clip_id"] for c in payload["bgm_candidates"][:2]] == [
        "bgm_segment_1",
        "bgm_segment_2",
    ]
    assert {c["score"] for c in payload["bgm_candidates"]} == {1.0}
    penalties = {
        c["metadata"]["clip_id"]: c["metadata"]["recency_penalty"]
        for c in payload["bgm_candidates"]
    }
    assert penalties["bgm_segment_1"] > penalties["bgm_segment_2"]


def test_material_pack_respects_active_bgm_reservations_from_parallel_run(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_bgm_asset(
        adapter.repository,
        "asset_bgm_locked",
        [_segment("locked_intro", 0.0, 45.0, mood="沉稳")],
    )
    _inject_bgm_asset(
        adapter.repository,
        "asset_bgm_open",
        [_segment("open_intro", 0.0, 45.0, mood="明快")],
    )
    adapter.repository.reserve_selections(
        case_id="case_demo",
        run_id="run_parallel",
        medium="bgm",
        asset_ids=["asset_bgm_locked"],
    )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    assert {c["asset_id"] for c in payload["bgm_candidates"]} == {"asset_bgm_open"}
    assert payload["diagnostics"]["bgm_active_reservations"] == 1
    owned = [
        reservation
        for reservation in adapter.repository.selection_reservations.values()
        if reservation.run_id == "run_bgm"
    ]
    assert {(reservation.medium, reservation.asset_id) for reservation in owned} == {
        ("bgm", "asset_bgm_open")
    }


def test_material_pack_offers_complete_bgm_segment_pool_without_top_k(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_bgm_asset(
        adapter.repository,
        "asset_bgm_long",
        [
            _segment(
                f"bgm_segment_{index}",
                float(index * 20),
                float(index * 20 + 20),
                energy=1.0 - index * 0.02,
            )
            for index in range(10)
        ],
    )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    assert len(payload["bgm_candidates"]) == 10
    assert {c["score"] for c in payload["bgm_candidates"]} == {1.0}
    assert [c["metadata"]["clip_id"] for c in payload["bgm_candidates"]] == [
        f"bgm_segment_{index}" for index in range(10)
    ]


def test_material_pack_complete_bgm_pool_includes_requested_asset(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    for index in range(8):
        _inject_bgm_asset(
            adapter.repository,
            f"asset_high_{index}",
            [_segment("segment_high", 0.0, 80.0, energy=0.9 - index * 0.01)],
        )
    _inject_bgm_asset(
        adapter.repository,
        "asset_requested_bgm",
        [_segment("requested_segment", 0.0, 80.0, energy=0.1)],
    )

    output = nodes.material_pack_planning.run(
        _ctx(
            adapter,
            _request(bgm={"enabled": True, "bgm_id": "asset_requested_bgm"}),
            "MaterialPackPlanning",
        )
    )
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    assert len(payload["bgm_candidates"]) == 9
    assert any(candidate["asset_id"] == "asset_requested_bgm" for candidate in payload["bgm_candidates"])
    owned = [
        reservation
        for reservation in adapter.repository.selection_reservations.values()
        if reservation.run_id == "run_bgm" and reservation.medium == "bgm"
    ]
    assert "asset_requested_bgm" in {reservation.asset_id for reservation in owned}


def test_style_planning_carries_selected_bgm_segment_into_style_plan(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    ctx = _ctx(adapter, _request(), "StylePlanning")
    ctx.state.artifacts[ArtifactKind.plan_material_pack] = _artifact(
        ArtifactKind.plan_material_pack,
        {
            "bgm_candidates": [
                {
                    "asset_id": "asset_bgm_song",
                    "score": 70.0,
                    "reason": "segment candidate",
                    "metadata": {
                        "clip_id": "bgm_segment_2",
                        "source_start": 58.0,
                        "source_end": 118.0,
                        "duration": 60.0,
                        "section_type": "chorus",
                        "section_label": "B",
                        "repeat_group": "main_hook",
                        "loopable": True,
                        "energy_profile": "peak",
                        "mood": "高能推进",
                        "scene_fit": ["产品展示"],
                        "script_fit": ["产品卖点强化"],
                        "avoid_script": ["沉浸睡眠"],
                        "reason": "适合高潮段落",
                    },
                }
            ],
            "font_candidates": [],
        },
    )

    output = nodes.style_planning.run(ctx)
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_style)

    assert payload["bgm_asset_id"] == "asset_bgm_song"
    assert payload["bgm"]["asset_id"] == "asset_bgm_song"
    assert payload["bgm"]["segment_id"] == "bgm_segment_2"
    assert payload["bgm"]["source_start"] == 58.0
    assert payload["bgm"]["source_end"] == 118.0
    assert payload["bgm"]["duration"] == 60.0
    assert payload["bgm"]["section_type"] == "chorus"
    assert payload["bgm"]["section_label"] == "B"
    assert payload["bgm"]["repeat_group"] == "main_hook"
    assert payload["bgm"]["loopable"] is True
    assert payload["bgm"]["energy_profile"] == "peak"
    assert payload["bgm"]["mood"] == "高能推进"
    assert payload["bgm"]["scene_fit"] == ["产品展示"]
    assert payload["bgm"]["script_fit"] == ["产品卖点强化"]
    assert payload["bgm"]["avoid_script"] == ["沉浸睡眠"]


def test_style_planning_consumes_subtitle_preset_defaults(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    ctx = _ctx(
        adapter,
        _request(subtitle={"enabled": True, "style_preset": "movie"}),
        "StylePlanning",
    )
    ctx.state.artifacts[ArtifactKind.plan_material_pack] = _artifact(
        ArtifactKind.plan_material_pack,
        {"bgm_candidates": [], "font_candidates": []},
    )

    output = nodes.style_planning.run(ctx)
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_style)

    assert payload["subtitle"]["font_size"] == 44
    assert payload["subtitle"]["position"] == {"x": 0.5, "y": 0.82}


def test_style_planning_preserves_explicit_subtitle_overrides(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    ctx = _ctx(
        adapter,
        _request(
            subtitle={
                "enabled": True,
                "style_preset": "movie",
                "font_size": 72,
                "position": {"x": 0.25, "y": 0.7},
            }
        ),
        "StylePlanning",
    )
    ctx.state.artifacts[ArtifactKind.plan_material_pack] = _artifact(
        ArtifactKind.plan_material_pack,
        {"bgm_candidates": [], "font_candidates": []},
    )

    output = nodes.style_planning.run(ctx)
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_style)

    assert payload["subtitle"]["font_size"] == 72
    assert payload["subtitle"]["position"] == {"x": 0.25, "y": 0.7}


def test_style_planning_requires_segmented_bgm_candidate(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    ctx = _ctx(adapter, _request(), "StylePlanning")
    ctx.state.artifacts[ArtifactKind.plan_material_pack] = _artifact(
        ArtifactKind.plan_material_pack,
        {
            "bgm_candidates": [
                {
                    "asset_id": "asset_unsegmented_bgm",
                    "score": 70.0,
                    "reason": "available bgm",
                    "metadata": {
                        "base_score": 70.0,
                        "recency_penalty": 0.0,
                    },
                }
            ],
            "font_candidates": [],
        },
    )

    output = nodes.style_planning.run(ctx)
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_style)

    assert output.status == NodeStatus.degraded
    assert payload["bgm_asset_id"] is None
    assert payload["bgm"]["asset_id"] is None
    assert payload["bgm"]["segment_id"] is None


def test_style_planning_chooses_bgm_clip_by_script_fit_over_raw_rank(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    ctx = _ctx(
        adapter,
        _request(script="开场先抛痛点，然后产品卖点强化，最后引导下单。"),
        "StylePlanning",
    )
    ctx.state.artifacts[ArtifactKind.plan_material_pack] = _artifact(
        ArtifactKind.plan_material_pack,
        {
            "bgm_candidates": [
                {
                    "asset_id": "asset_generic_high",
                    "score": 95.0,
                    "reason": "higher raw score",
                    "metadata": {
                        "clip_id": "generic_clip",
                        "source_start": 0.0,
                        "source_end": 80.0,
                        "duration": 80.0,
                        "mood": "轻快",
                        "script_fit": ["旅行Vlog"],
                    },
                },
                {
                    "asset_id": "asset_script_fit",
                    "score": 70.0,
                    "reason": "script matched",
                    "metadata": {
                        "clip_id": "selling_clip",
                        "source_start": 42.0,
                        "source_end": 126.0,
                        "duration": 84.0,
                        "mood": "推进感",
                        "script_fit": ["产品卖点强化", "硬广转化"],
                        "scene_fit": ["产品展示"],
                        "reason": "适合产品卖点强化",
                    },
                },
            ],
            "font_candidates": [],
        },
    )

    output = nodes.style_planning.run(ctx)
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_style)

    assert payload["bgm_asset_id"] == "asset_script_fit"
    assert payload["bgm"]["segment_id"] == "selling_clip"
    assert payload["bgm"]["source_start"] == 42.0
    assert payload["bgm"]["source_end"] == 126.0


def test_style_planning_demotes_short_non_loopable_bgm_for_single_clip_video(
    tmp_path, monkeypatch
):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    ctx = _ctx(
        adapter,
        _request(script="适合稳定铺底的品牌介绍和产品卖点讲解。"),
        "StylePlanning",
    )
    ctx.state.artifacts[ArtifactKind.plan_material_pack] = _artifact(
        ArtifactKind.plan_material_pack,
        {
            "bgm_candidates": [
                {
                    "asset_id": "asset_short_non_loop",
                    "score": 95.0,
                    "reason": "high score but too short",
                    "metadata": {
                        "clip_id": "short_intro",
                        "source_start": 0.0,
                        "source_end": 12.0,
                        "duration": 12.0,
                        "section_type": "intro",
                        "loopable": False,
                        "script_fit": ["品牌介绍", "产品卖点讲解"],
                    },
                },
                {
                    "asset_id": "asset_macro_bed",
                    "score": 60.0,
                    "reason": "usable macro section",
                    "metadata": {
                        "clip_id": "stable_main",
                        "source_start": 0.0,
                        "source_end": 72.0,
                        "duration": 72.0,
                        "section_type": "stable_bed",
                        "loopable": True,
                        "script_fit": ["品牌介绍"],
                    },
                },
            ],
            "font_candidates": [],
        },
    )

    output = nodes.style_planning.run(ctx)
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_style)

    assert payload["bgm_asset_id"] == "asset_macro_bed"
    assert payload["bgm"]["segment_id"] == "stable_main"


def test_style_planning_respects_requested_bgm_asset_even_when_not_top_scored(
    tmp_path, monkeypatch
):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    ctx = _ctx(
        adapter,
        _request(bgm={"enabled": True, "bgm_id": "asset_requested_bgm"}),
        "StylePlanning",
    )
    ctx.state.artifacts[ArtifactKind.plan_material_pack] = _artifact(
        ArtifactKind.plan_material_pack,
        {
            "bgm_candidates": [
                {
                    "asset_id": "asset_high_score_bgm",
                    "score": 90.0,
                    "reason": "higher score",
                    "metadata": {
                        "clip_id": "high_segment",
                        "source_start": 0.0,
                        "source_end": 60.0,
                        "duration": 60.0,
                        "mood": "高分但未指定",
                    },
                },
                {
                    "asset_id": "asset_requested_bgm",
                    "score": 55.0,
                    "reason": "requested asset",
                    "metadata": {
                        "clip_id": "requested_segment",
                        "source_start": 75.0,
                        "source_end": 135.0,
                        "duration": 60.0,
                        "mood": "用户指定",
                        "scene_fit": ["品牌统一"],
                        "reason": "requested bgm segment",
                    },
                },
            ],
            "font_candidates": [],
        },
    )

    output = nodes.style_planning.run(ctx)
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_style)

    assert payload["bgm_asset_id"] == "asset_requested_bgm"
    assert payload["bgm"]["asset_id"] == "asset_requested_bgm"
    assert payload["bgm"]["segment_id"] == "requested_segment"
    assert payload["bgm"]["source_start"] == 75.0
    assert payload["bgm"]["source_end"] == 135.0
    assert payload["bgm"]["mood"] == "用户指定"
    assert payload["bgm"]["scene_fit"] == ["品牌统一"]


def test_style_planning_does_not_select_bgm_when_disabled(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    ctx = _ctx(adapter, _request(bgm={"enabled": False}), "StylePlanning")
    ctx.state.artifacts[ArtifactKind.plan_material_pack] = _artifact(
        ArtifactKind.plan_material_pack,
        {
            "bgm_candidates": [
                {
                    "asset_id": "asset_bgm_song",
                    "score": 70.0,
                    "reason": "segment candidate",
                    "metadata": {
                        "clip_id": "bgm_segment_2",
                        "source_start": 58.0,
                        "source_end": 118.0,
                        "duration": 60.0,
                    },
                }
            ],
            "font_candidates": [],
        },
    )

    output = nodes.style_planning.run(ctx)
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_style)

    assert payload["bgm_asset_id"] is None
    assert payload["bgm"]["enabled"] is False
    assert payload["bgm"]["asset_id"] is None
    assert payload["bgm"]["segment_id"] is None
