"""P3 acceptance: a single unified ``video`` asset is split per-clip into the
A-roll (lip-sync portrait) and B-roll (cover) pools, and the planned portrait
track cuts the exact talking-head clip window — proving the end-to-end clip-level
material flow the unification is for.
"""

from __future__ import annotations

import pytest

from packages.ai.gateway import ProviderGateway
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    AnnotationEditorVm,
    AnnotationMetaV4,
    AnnotationV4,
    Artifact,
    ArtifactKind,
    ClipRetrievalV4,
    ClipSemanticsV4,
    ClipUsageV4,
    ClipV4,
    DigitalHumanVideoRequest,
    ErrorCode,
    Job,
    JobStatus,
    JobType,
    MediaAssetRecord,
    MediaInfo,
    NodeRun,
    NodeStatus,
    RunStatus,
    SelectionLedgerEntry,
    UsageRole,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline import nodes
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter

SCRIPT = "先讲解打磨工艺的细节非常重要。再展示补漆效果对比清晰可见。"


def _adapter(object_store: LocalObjectStore) -> LocalRuntimeAdapter:
    repo = Repository()
    return LocalRuntimeAdapter(
        repo,
        provider_gateway=ProviderGateway(repo, object_store=object_store),
        prompt_registry=PromptRegistry(repo),
    )


def _talk_clip(segment_id, start, end):
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


def _cover_clip(segment_id, start, end, keywords):
    return ClipV4(
        segment_id=segment_id,
        start=start,
        end=end,
        duration=end - start,
        semantics=ClipSemanticsV4(scene_type="工艺", narrative_role="process_proof"),
        usage=ClipUsageV4(
            role=UsageRole.cover, recommended_for_lip_sync=False, voiceover_only=True
        ),
        retrieval=ClipRetrievalV4(
            summary=" ".join(keywords),
            keywords=list(keywords),
            retrieval_sentence=" ".join(keywords),
        ),
        confidence=0.85,
    )


def _presenter_cover_clip(segment_id, start, end, keywords):
    # A presenter on camera but NOT lip-sync usable (cover role) — under the old
    # "non-A-roll == B-roll" split this leaked into b-roll; it must be excluded.
    return ClipV4(
        segment_id=segment_id,
        start=start,
        end=end,
        duration=end - start,
        semantics=ClipSemanticsV4(
            scene_type="luxury_store", subject_type="female_presenter", face_count_max=1
        ),
        usage=ClipUsageV4(role=UsageRole.cover, recommended_for_lip_sync=False, voiceover_only=True),
        retrieval=ClipRetrievalV4(
            summary=" ".join(keywords),
            keywords=list(keywords),
            retrieval_sentence=" ".join(keywords),
        ),
        confidence=0.85,
    )


def _inject_video_asset(
    repo: Repository,
    asset_id: str,
    clips,
    *,
    case_id="case_demo",
    kind="video",
    annotation_material_type: str | None = None,
) -> None:
    duration = max((float(clip.end) for clip in clips), default=0.0)
    source = repo.create_artifact(
        kind=ArtifactKind.uploaded_file,
        payload_schema="UploadedFileArtifact.v1",
        payload={"filename": f"{asset_id}.mp4", "object_uri": f"memory://{asset_id}"},
        case_id=case_id,
        uri=f"memory://{asset_id}",
        media_info=MediaInfo(
            media_type="video",
            codec="h264",
            format="mp4",
            mime_type="video/mp4",
            duration_sec=duration,
            width=320,
            height=568,
            fps=30,
        ),
    )
    asset = MediaAssetRecord(
        id=asset_id,
        case_id=case_id,
        title="mixed",
        kind=kind,
        source_artifact_id=source.id,
        usable=True,
    )
    repo.media_assets[asset_id] = asset
    annotation_case_id = case_id or "case_demo"
    annotation = AnnotationV4(
        meta=AnnotationMetaV4(
            asset_id=asset_id,
            case_id=annotation_case_id,
            material_type=annotation_material_type or kind,
            duration=duration,
        ),
        clips=clips,
    )
    repo.annotations[asset_id] = AnnotationEditorVm(
        asset=asset, etag="etag1", canonical=annotation, projection={}
    )


def _request(**overrides):
    base = dict(
        case_id="case_demo",
        script=SCRIPT,
        voice={"voice_id": "voice_sandbox"},
        broll={"enabled": True},
        strictness={"strict_timestamps": False},
    )
    base.update(overrides)
    return DigitalHumanVideoRequest(**base)


def _ctx(adapter, request, node_id):
    state = RunState(request=request, artifacts={})
    run = WorkflowRun(
        id="run_1",
        job_id="job_1",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )
    node_run = NodeRun(
        id="nr_1",
        run_id="run_1",
        node_id=node_id,
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )
    return NodeContext(adapter=adapter, run=run, node_run=node_run, state=state)


def test_material_pack_splits_one_video_into_portrait_and_broll(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    # Isolate from the adapter's seeded demo assets so the pools reflect only our video.
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(
        adapter.repository,
        "vid_mixed",
        [
            _talk_clip("talk", 2.0, 9.0),  # A-roll
            _cover_clip("cover", 9.0, 14.0, ["打磨", "工艺"]),  # B-roll, matches script
            _cover_clip("unrelated", 14.0, 19.0, ["门店", "咖啡"]),  # still eligible
        ],
    )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    # Portrait pool: the talking-head clip is a candidate carrying its exact source window.
    portrait = payload["portrait_candidates"]
    talk = next(c for c in portrait if (c["metadata"] or {}).get("clip_id") == "talk")
    assert talk["metadata"]["source_start"] == 2.0
    assert talk["metadata"]["source_end"] == 9.0

    # B-roll pool: the cover clip is offered; the talking-head clip never leaks in.
    broll_clip_ids = {(c["metadata"] or {}).get("clip_id") for c in payload["broll_candidates"]}
    assert "cover" in broll_clip_ids
    assert "unrelated" in broll_clip_ids
    assert "talk" not in broll_clip_ids
    assert {c["score"] for c in payload["broll_candidates"]} == {1.0}
    assert {
        (item["medium"], item["asset_id"], item["clip_id"], item["reason"])
        for item in payload["rejected_candidates"]
    } >= {
        ("broll", "vid_mixed", "talk", "broll_lipsync_routed_to_portrait"),
    }

    # Honest diagnostics for the unified bucket.
    assert payload["diagnostics"]["visual_eligibility_mode"] == "hard_gate_complete_pool"
    assert payload["diagnostics"]["visual_ranking_disabled"] == {"portrait": True, "broll": True}
    assert payload["diagnostics"]["portrait_from_video"] >= 1
    assert payload["diagnostics"]["video_no_lipsync"] is False


def test_material_pack_carries_portrait_recency_without_ranking_pool(tmp_path, monkeypatch):
    # MaterialPackPlanning is the ONE node that reads the selection ledger, but Stage 2
    # keeps it as hard eligibility only: a prior portrait use is stamped onto metadata
    # for TimelineWindowPlanning, never hidden in the MaterialPack score or order.
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(adapter.repository, "vid_a_used", [_talk_clip("talk_used", 0.0, 15.0)])
    _inject_video_asset(
        adapter.repository,
        "vid_z_fresh",
        [_talk_clip("talk_fresh", 0.0, 15.0)],
    )
    # vid_a_used opened a prior run for this case.
    adapter.repository.record_selection_ledger_entries(
        [
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_prev",
                medium="portrait",
                asset_id="vid_a_used",
                slot_phase="portrait_opening",
            )
        ]
    )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    by_asset = {c["asset_id"]: c for c in payload["portrait_candidates"]}
    used, fresh = by_asset["vid_a_used"], by_asset["vid_z_fresh"]

    # Scalar recency remains metadata only; eligibility score stays fixed.
    assert used["metadata"]["recency_penalty"] > 0.0
    assert fresh["metadata"]["recency_penalty"] == 0.0
    assert used["score"] == fresh["score"] == 1.0

    # The full weighted recency/opening context (recency_context.py) is stamped on each
    # candidate so TimelineWindowPlanning never needs the ledger.
    assert used["metadata"]["recent_usage"]["is_recently_used"] is True
    assert used["metadata"]["recent_usage"]["recent_opening_use_count"] >= 1
    assert fresh["metadata"]["recent_usage"]["is_recently_used"] is False

    # Deterministic complete-pool order is by source identity, not recency freshness.
    assert [c["asset_id"] for c in payload["portrait_candidates"]] == [
        "vid_a_used",
        "vid_z_fresh",
    ]


def test_material_pack_cache_reuses_stable_pool_but_refreshes_run_state(
    tmp_path,
    monkeypatch,
):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(
        adapter.repository,
        "vid_cache_fresh",
        [
            _talk_clip("talk_cache", 0.0, 8.0),
            _cover_clip("cover_cache", 8.0, 13.0, ["打磨", "工艺"]),
        ],
    )
    _inject_video_asset(
        adapter.repository,
        "vid_cache_locked",
        [
            _talk_clip("talk_locked", 0.0, 8.0),
            _cover_clip("cover_locked", 8.0, 13.0, ["补漆", "效果"]),
        ],
    )

    first = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    first_payload = next(a.payload for a in first.artifacts if a.kind == ArtifactKind.plan_material_pack)
    assert first_payload["diagnostics"]["materialpack_eligibility_cache"]["hit"] is False

    adapter.repository.release_run_reservations(run_id="run_1")
    adapter.repository.record_selection_ledger_entries(
        [
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_prev",
                medium="portrait",
                asset_id="vid_cache_fresh",
                slot_phase="portrait_opening",
            )
        ]
    )
    adapter.repository.reserve_selections(
        case_id="case_demo",
        run_id="run_parallel",
        medium="portrait",
        asset_ids=["vid_cache_locked"],
    )
    adapter.repository.reserve_selections(
        case_id="case_demo",
        run_id="run_parallel",
        medium="broll",
        asset_ids=["vid_cache_locked"],
    )

    second = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in second.artifacts if a.kind == ArtifactKind.plan_material_pack)

    assert payload["diagnostics"]["materialpack_eligibility_cache"]["hit"] is True
    assert {c["asset_id"] for c in payload["portrait_candidates"]} == {"vid_cache_fresh"}
    assert {c["asset_id"] for c in payload["broll_candidates"]} == {"vid_cache_fresh"}
    assert payload["portrait_candidates"][0]["metadata"]["recency_penalty"] > 0.0
    assert {
        (item["medium"], item["asset_id"], item.get("clip_id"), item["reason"])
        for item in payload["rejected_candidates"]
    } >= {
        ("portrait", "vid_cache_locked", None, "active_selection_reservation"),
        ("broll", "vid_cache_locked", None, "active_selection_reservation"),
    }


def test_material_pack_cache_key_changes_when_source_artifact_becomes_resolvable(
    tmp_path,
    monkeypatch,
):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(
        adapter.repository,
        "vid_cache_uri_repaired",
        [_talk_clip("talk_uri_repaired", 0.0, 15.0)],
    )
    asset = adapter.repository.media_assets["vid_cache_uri_repaired"]
    source = adapter.repository.artifacts[asset.source_artifact_id]
    source.uri = None
    source.media_info = source.media_info.model_copy(update={"duration_sec": 10.0})

    first = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    first_payload = next(a.payload for a in first.artifacts if a.kind == ArtifactKind.plan_material_pack)
    assert first_payload["diagnostics"]["materialpack_eligibility_cache"]["hit"] is False
    assert first_payload["portrait_candidates"][0]["metadata"]["source_end"] == 15.0

    adapter.repository.release_run_reservations(run_id="run_1")
    source.uri = "memory://vid_cache_uri_repaired"

    second = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    second_payload = next(
        a.payload for a in second.artifacts if a.kind == ArtifactKind.plan_material_pack
    )

    assert second_payload["diagnostics"]["materialpack_eligibility_cache"]["hit"] is False
    assert second_payload["portrait_candidates"][0]["metadata"]["source_end"] == 10.0


def test_material_pack_respects_active_reservations_from_parallel_run(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(
        adapter.repository,
        "vid_reserved",
        [
            _talk_clip("talk_reserved", 0.0, 12.0),
            _cover_clip("cover_reserved", 12.0, 18.0, ["打磨", "工艺", "补漆", "效果"]),
        ],
    )
    _inject_video_asset(
        adapter.repository,
        "vid_fresh",
        [
            _talk_clip("talk_fresh", 0.0, 7.0),
            _cover_clip("cover_fresh", 7.0, 11.0, ["打磨", "工艺"]),
        ],
    )
    adapter.repository.reserve_selections(
        case_id="case_demo",
        run_id="run_parallel",
        medium="portrait",
        asset_ids=["vid_reserved"],
    )
    adapter.repository.reserve_selections(
        case_id="case_demo",
        run_id="run_parallel",
        medium="broll",
        asset_ids=["vid_reserved"],
    )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    assert {c["asset_id"] for c in payload["portrait_candidates"]} == {"vid_fresh"}
    assert {c["asset_id"] for c in payload["broll_candidates"]} == {"vid_fresh"}
    assert payload["diagnostics"]["portrait_active_reservations"] == 1
    assert payload["diagnostics"]["broll_active_reservations"] == 1
    assert {
        (item["medium"], item["asset_id"], item.get("clip_id"), item["reason"])
        for item in payload["rejected_candidates"]
    } >= {
        ("portrait", "vid_reserved", None, "active_selection_reservation"),
        ("broll", "vid_reserved", None, "active_selection_reservation"),
    }
    owned = [
        reservation
        for reservation in adapter.repository.selection_reservations.values()
        if reservation.run_id == "run_1"
    ]
    assert {(reservation.medium, reservation.asset_id) for reservation in owned} == {
        ("portrait", "vid_fresh"),
        ("broll", "vid_fresh"),
    }


def test_material_pack_retries_when_candidate_is_reserved_during_reserve_call(
    tmp_path,
    monkeypatch,
):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(
        adapter.repository,
        "vid_race",
        [
            _talk_clip("talk_race", 0.0, 7.0),
            _cover_clip("cover_race", 7.0, 11.0, ["打磨", "工艺"]),
        ],
    )
    original_reserve = adapter.repository.reserve_selections
    injected = False

    def racing_reserve(*, case_id, run_id, medium, asset_ids, diversity_keys=None):
        nonlocal injected
        if not injected and run_id == "run_1" and medium == "portrait":
            injected = True
            original_reserve(
                case_id=case_id,
                run_id="run_parallel",
                medium=medium,
                asset_ids=["vid_race"],
            )
        return original_reserve(
            case_id=case_id,
            run_id=run_id,
            medium=medium,
            asset_ids=asset_ids,
            diversity_keys=diversity_keys,
        )

    monkeypatch.setattr(adapter.repository, "reserve_selections", racing_reserve)

    with pytest.raises(NodeExecutionError) as exc_info:
        nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))

    assert exc_info.value.error.code == ErrorCode.validation_conflict
    assert exc_info.value.error.retryable is True
    assert exc_info.value.error.details == {"conflicts": {"portrait": ["vid_race"]}}


def test_material_pack_reserves_every_eligible_broll_asset(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    for index in range(4):
        asset_id = f"vid_broll_{index}"
        _inject_video_asset(
            adapter.repository,
            asset_id,
            [_cover_clip(f"cover_{index}", 0.0, 5.0, ["工艺", str(index)])],
        )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    assert [c["asset_id"] for c in payload["broll_candidates"]] == [
        "vid_broll_0",
        "vid_broll_1",
        "vid_broll_2",
        "vid_broll_3",
    ]
    owned = [
        reservation
        for reservation in adapter.repository.selection_reservations.values()
        if reservation.run_id == "run_1" and reservation.medium == "broll"
    ]
    assert {reservation.asset_id for reservation in owned} == {
        "vid_broll_0",
        "vid_broll_1",
        "vid_broll_2",
        "vid_broll_3",
    }
    assert len(payload["reservations"]) == 4


def test_local_runtime_syncs_after_material_pack_reservations(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    snapshots: list[tuple[str, list[tuple[str, str, str, str]]]] = []
    repo = Repository()
    adapter = LocalRuntimeAdapter(
        repo,
        provider_gateway=ProviderGateway(repo, object_store=object_store),
        prompt_registry=PromptRegistry(repo),
        snapshot_sync=lambda job, run, repository: snapshots.append(
            (
                run.id,
                [
                    (reservation.run_id, reservation.medium, reservation.asset_id, reservation.status)
                    for reservation in repository.selection_reservations.values()
                ],
            )
        ),
    )
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(
        adapter.repository,
        "vid_sync",
        [
            _talk_clip("talk_sync", 0.0, 7.0),
            _cover_clip("cover_sync", 7.0, 11.0, ["打磨", "工艺"]),
        ],
    )
    request = _request()
    job = Job(
        id="job_sync",
        type=JobType.digital_human_video,
        status=JobStatus.running,
        case_id="case_demo",
        created_by="usr_admin",
        request_schema=request.schema_version,
        request=request,
    )
    run = WorkflowRun(
        id="run_sync",
        job_id=job.id,
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )
    adapter.repository.jobs[job.id] = job
    adapter.repository.runs[run.id] = run
    adapter.repository.node_runs[run.id] = []

    proceeded = adapter._execute_node("MaterialPackPlanning", run, RunState(request=request, artifacts={}))

    assert proceeded is True
    assert snapshots
    assert snapshots[-1] == (
        "run_sync",
        [
            ("run_sync", "portrait", "vid_sync", "reserved"),
            ("run_sync", "broll", "vid_sync", "reserved"),
        ],
    )


def test_material_pack_excludes_presenter_clip_from_broll_and_reports_it(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(
        adapter.repository,
        "vid_template",
        [
            # presenter on camera, matches the script -> must NOT become b-roll
            _presenter_cover_clip("presenter", 0.0, 6.0, ["打磨", "工艺"]),
            # clean scene cover -> the only legitimate b-roll
            _cover_clip("scenery", 6.0, 12.0, ["打磨", "工艺"]),
        ],
    )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    broll_clip_ids = {(c["metadata"] or {}).get("clip_id") for c in payload["broll_candidates"]}
    assert "presenter" not in broll_clip_ids
    assert "scenery" in broll_clip_ids
    assert {
        (item["medium"], item["asset_id"], item["clip_id"], item["reason"])
        for item in payload["rejected_candidates"]
    } >= {
        ("broll", "vid_template", "presenter", "broll_person_clip"),
    }
    # Honest visibility: the person clip excluded from b-roll is reported so a
    # near-empty b-roll plan is not mistaken for an annotation error.
    assert payload["diagnostics"]["broll_person_excluded"] == 1


def test_material_pack_reports_missing_annotation_as_rejected_candidate(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(adapter.repository, "vid_unannotated", [_cover_clip("cover", 0.0, 5.0, [])])
    adapter.repository.annotations.pop("vid_unannotated")

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    assert payload["portrait_candidates"] == []
    assert payload["broll_candidates"] == []
    assert {
        (item["medium"], item["asset_id"], item.get("clip_id"), item["reason"])
        for item in payload["rejected_candidates"]
    } >= {
        ("portrait", "vid_unannotated", None, "annotation_missing"),
        ("broll", "vid_unannotated", None, "annotation_missing"),
    }


def test_material_pack_video_without_talking_head_flags_no_lipsync(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    # A video with ONLY cover clips -> no A-roll candidate, but b-roll still works.
    _inject_video_asset(
        adapter.repository,
        "vid_scenery",
        [
            _cover_clip("c1", 0.0, 5.0, ["打磨", "工艺"]),
            _cover_clip("c2", 5.0, 9.0, ["补漆", "效果"]),
        ],
    )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    assert payload["diagnostics"]["portrait_from_video"] == 0
    assert payload["diagnostics"]["video_no_lipsync"] is True
    assert payload["diagnostics"]["portrait_missing"] is True
    # The cover clips still serve as b-roll — honest partial usefulness.
    assert payload["broll_candidates"]


def test_material_pack_excludes_ai_material_reference_assets(tmp_path, monkeypatch):
    """An AI素材 (Seedance reference) video tagged ai_material must NOT enter the
    digital-human portrait/b-roll material pools — it is a generation reference,
    not footage to cut into a normal run."""
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(
        adapter.repository,
        "vid_normal",
        [
            _talk_clip("talk_n", 0.0, 7.0),
            _cover_clip("cover_n", 7.0, 12.0, ["打磨", "工艺"]),
        ],
    )
    _inject_video_asset(
        adapter.repository,
        "vid_ai",
        [
            _talk_clip("talk_ai", 0.0, 7.0),
            _cover_clip("cover_ai", 7.0, 12.0, ["打磨", "工艺"]),
        ],
    )
    # Tag the second video as an AI素材 reference upload.
    adapter.repository.media_assets["vid_ai"] = adapter.repository.media_assets["vid_ai"].model_copy(
        update={"tags": ["ai_material"]}
    )

    output = nodes.material_pack_planning.run(_ctx(adapter, _request(), "MaterialPackPlanning"))
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    portrait_asset_ids = {c["asset_id"] for c in payload["portrait_candidates"]}
    broll_asset_ids = {c["asset_id"] for c in payload["broll_candidates"]}
    assert "vid_ai" not in portrait_asset_ids
    assert "vid_ai" not in broll_asset_ids
    # The normal (untagged) video still flows into the pools.
    assert "vid_normal" in broll_asset_ids


def test_material_pack_global_video_does_not_enter_case_scoped_broll(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    adapter.repository.media_assets.clear()
    adapter.repository.annotations.clear()
    _inject_video_asset(
        adapter.repository,
        "vid_global",
        [_cover_clip("cover", 0.0, 5.0, ["打磨", "工艺"])],
        case_id=None,
    )

    output = nodes.material_pack_planning.run(
        _ctx(
            adapter,
            _request(broll={"enabled": True, "case_id": "case_demo"}),
            "MaterialPackPlanning",
        )
    )
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_material_pack)

    assert payload["broll_candidates"] == []


def test_portrait_plan_cuts_only_the_talking_head_clip_window(tmp_path, monkeypatch):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)
    adapter = _adapter(object_store)
    # The seeded demo portrait asset has a real ~15s source; pin a [3,12] clip window.
    win_start, win_end = 3.0, 12.0
    material = {
        "portrait_candidates": [
            {
                "asset_id": "asset_portrait_demo",
                "score": 5.0,
                "metadata": {"clip_id": "talk", "source_start": win_start, "source_end": win_end},
            }
        ]
    }
    units = [
        {"unit_id": "u1", "text": "先讲解打磨工艺。", "start": 0.0, "end": 4.0, "confidence": 0.9},
        {"unit_id": "u2", "text": "再展示补漆效果。", "start": 4.0, "end": 8.0, "confidence": 0.9},
    ]
    ctx = _ctx(adapter, _request(), "TimelineWindowPlanning")
    ctx.state.artifacts[ArtifactKind.plan_material_pack] = Artifact(
        id="art_mp",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_mp",
        kind=ArtifactKind.plan_material_pack,
        payload=material,
        payload_schema="MaterialPackArtifact.v1",
    )
    ctx.state.artifacts[ArtifactKind.narration_units] = Artifact(
        id="art_nu",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_nu",
        kind=ArtifactKind.narration_units,
        payload={"source": "estimated", "units": units, "strict": False},
        payload_schema="NarrationUnitsArtifact.v1",
    )
    # NarrationBoundaryPlanning runs upstream now; no real pauses -> semantic-only.
    ctx.state.artifacts[ArtifactKind.plan_narration_boundary] = Artifact(
        id="art_nb",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_nb",
        kind=ArtifactKind.plan_narration_boundary,
        payload={"pause_windows": []},
        payload_schema="NarrationBoundaryPlan.v1",
    )

    output = nodes.timeline_window_planning.run(ctx)
    payload = next(a.payload for a in output.artifacts if a.kind == ArtifactKind.plan_portrait)
    assert payload["segments"]
    # Every planned source slice is drawn from INSIDE the pinned clip window.
    for seg in payload["segments"]:
        assert seg["source_start"] >= win_start - 0.05
        assert seg["source_end"] <= win_end + 0.05
