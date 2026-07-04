from __future__ import annotations

from packages.ai.gateway import ProviderGateway
from packages.ai.gateway.provider_gateway import _deterministic_embedding
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    MediaAssetRecord,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.planning.material import build_clip_embedding_record
from packages.production.pipeline import nodes
from packages.production.pipeline._editing_agent import (
    BrollChoice,
    EditingSelection,
    index_candidates,
    validate_selection,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter


def _adapter(tmp_path) -> LocalRuntimeAdapter:
    repository = Repository()
    object_store = LocalObjectStore(root=tmp_path)
    return LocalRuntimeAdapter(
        repository,
        provider_gateway=ProviderGateway(repository, object_store=object_store),
        prompt_registry=PromptRegistry(repository),
        seed_media=False,
    )


def _run(template_id: str = "digital_human_v2") -> WorkflowRun:
    return WorkflowRun(
        id="run_window_retrieval",
        job_id="job_window_retrieval",
        case_id="case_demo",
        workflow_template_id=template_id,
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run(node_id: str) -> NodeRun:
    return NodeRun(
        id=f"nr_{node_id}",
        run_id="run_window_retrieval",
        node_id=node_id,
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _artifact(kind: ArtifactKind, payload: dict) -> Artifact:
    return Artifact(
        id=f"art_{kind.value.replace('.', '_')}",
        case_id="case_demo",
        run_id="run_window_retrieval",
        node_run_id="nr_input",
        kind=kind,
        payload=payload,
        payload_schema=f"{kind.value}.v1",
    )


def _request() -> DigitalHumanVideoRequest:
    return DigitalHumanVideoRequest(
        case_id="case_demo",
        script="今天看施工前后变化。第一步先看施工前现场。",
        title="案例",
        voice={"voice_id": "voice_sandbox"},
        broll={"enabled": True, "max_inserts": 2},
    )


def _windows() -> dict:
    return {
        "fps": 30,
        "total_frames": 180,
        "portrait_windows": [
            {
                "window_id": "pwin_000",
                "start_frame": 0,
                "end_frame": 120,
                "unit_ids": ["unit_1"],
                "boundary_source": "semantic",
                "phase": "opening",
            }
        ],
        "broll_windows": [
            {
                "window_id": "bwin_000",
                "start_frame": 30,
                "end_frame": 90,
                "length_frames": 60,
                "host_unit_ids": ["unit_1"],
                "host_portrait_window_ids": ["pwin_000"],
                "text": "施工前现场",
                "boundary_source": "narration_unit",
            }
        ],
        "default_assignment": {},
        "compile_diagnostics": {},
    }


def _narration() -> dict:
    return {
        "units": [
            {
                "unit_id": "unit_1",
                "text": "今天看施工前后变化，第一步先看施工前现场。",
                "start": 0.0,
                "end": 4.0,
                "confidence": 0.9,
            }
        ]
    }


def _material() -> dict:
    return {
        "case_id": "case_demo",
        "portrait_candidates": [
            {
                "asset_id": "portrait_a",
                "score": 1.0,
                "reason": "eligible portrait clip",
                "metadata": {
                    "clip_id": "portrait_clip",
                    "source_start": 0.0,
                    "source_end": 8.0,
                    "source_frames_available": 240,
                },
            }
        ],
        "broll_candidates": [
            {
                "asset_id": "broll_a",
                "score": 1.0,
                "reason": "eligible b-roll clip",
                "metadata": {
                    "clip_id": "clip_a",
                    "source_start": 0.0,
                    "source_end": 4.0,
                    "source_frames_available": 120,
                    "matched_keywords": ["施工前"],
                    "scene_name": "施工前",
                    "diversity_key": "scene:a",
                },
            },
            {
                "asset_id": "broll_b",
                "score": 1.0,
                "reason": "eligible b-roll clip",
                "metadata": {
                    "clip_id": "clip_b",
                    "source_start": 0.0,
                    "source_end": 1.0,
                    "source_frames_available": 30,
                    "matched_keywords": ["现场"],
                    "scene_name": "现场",
                    "diversity_key": "scene:b",
                },
            },
        ],
        "font_candidates": [],
        "bgm_candidates": [],
    }


def _ctx(adapter: LocalRuntimeAdapter, node_id: str, artifacts: dict[ArtifactKind, Artifact]):
    return NodeContext(
        adapter=adapter,
        run=_run(),
        node_run=_node_run(node_id),
        state=RunState(request=_request(), artifacts=artifacts),
    )


def test_window_query_planning_emits_only_window_id_and_intent(tmp_path):
    adapter = _adapter(tmp_path)
    ctx = _ctx(
        adapter,
        "WindowQueryPlanning",
        {
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
        },
    )

    output = nodes.window_query_planning.run(ctx)

    payload = output.artifacts[0].payload
    assert len(payload["window_queries"]) == 2
    assert set(payload["window_queries"][0]) == {"window_id", "retrieval_intent"}
    assert {item["window_id"] for item in payload["window_queries"]} == {
        "pwin_000",
        "bwin_000",
    }


def test_window_material_retrieval_uses_material_pack_pool_and_offline_index(tmp_path):
    adapter = _adapter(tmp_path)
    material = _material()
    for asset_id in ("portrait_a", "broll_a", "broll_b"):
        adapter.repository.media_assets[asset_id] = MediaAssetRecord(
            id=asset_id,
            case_id="case_demo",
            title=asset_id,
            kind="video",
            annotation_status="annotated",
            usable=True,
        )
    query_ctx = _ctx(
        adapter,
        "WindowQueryPlanning",
        {
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
        },
    )
    query_output = nodes.window_query_planning.run(query_ctx)
    query_artifact = query_output.artifacts[0]
    broll_intent = next(
        item["retrieval_intent"]
        for item in query_artifact.payload["window_queries"]
        if item["window_id"] == "bwin_000"
    )
    indexed = index_candidates(material)
    broll_a = indexed.broll_by_id["bc_000"]
    record = build_clip_embedding_record(
        candidate=broll_a,
        asset=adapter.repository.media_assets["broll_a"],
        namespace="broll",
        provider_profile_id="sandbox.embedding.default",
        embedding=_deterministic_embedding(
            f"sandbox.embedding.default:multimodal.embedding:{broll_intent}",
            dimension=1024,
        ),
    )
    adapter.repository.clip_embedding_index[record.clip_embedding_key] = record
    retrieval_ctx = _ctx(
        adapter,
        "WindowMaterialRetrieval",
        {
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack,
                material,
            ),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.plan_window_queries: query_artifact,
        },
    )

    output = nodes.window_material_retrieval.run(retrieval_ctx)

    payload = output.artifacts[0].payload
    assert output.provider_invocation_ids
    assert payload["candidates_by_window"]["bwin_000"][0]["candidate_id"] == "bc_000"
    assert "why_retrieved" not in payload["candidates_by_window"]["bwin_000"][0]
    trace = payload["candidates_by_window"]["bwin_000"][0]["retrieval_trace"]
    assert trace["embedding_model"] == "qwen3-vl-embedding"
    assert trace["embedding_dimension"] == 1024
    assert payload["diagnostics"]["rejected_candidates"][0]["reason"] == "source_too_short"
    assert payload["diagnostics"]["missing_clip_embeddings"]


def test_deterministic_editing_planning_consumes_window_retrieval_topk(tmp_path):
    adapter = _adapter(tmp_path)
    material = _material()
    retrieval = {
        "candidates_by_window": {
            "bwin_000": [
                {
                    "candidate_id": "bc_000",
                    "retrieval_score": 0.99,
                    "source_frames_available": 120,
                    "required_frames": 60,
                }
            ]
        },
        "diagnostics": {},
    }
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, material),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.plan_window_material_retrieval: _artifact(
                ArtifactKind.plan_window_material_retrieval,
                retrieval,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.creative_intent: _artifact(ArtifactKind.creative_intent, {"intent": {}}),
        },
    )

    output = nodes.deterministic_editing_planning.run(ctx)

    broll_payload = next(
        artifact.payload for artifact in output.artifacts if artifact.kind == ArtifactKind.plan_broll
    )
    assert broll_payload["overlays"][0]["window_id"] == "bwin_000"
    assert broll_payload["overlays"][0]["asset_id"] == "broll_a"
    assert broll_payload["overlays"][0]["timeline_start_frame"] == 30
    assert broll_payload["overlays"][0]["timeline_end_frame"] == 90


def test_agent_validator_rejects_broll_choice_outside_window_topk():
    candidates = index_candidates(_material())
    selection = EditingSelection(
        broll=[BrollChoice(slot_id="bwin_000", candidate_id="bc_001")]
    )
    errors = validate_selection(
        selection,
        boundary={
            "portrait_slots": [],
            "broll_slots": [
                {
                    "slot_id": "bwin_000",
                    "start_frame": 30,
                    "end_frame": 90,
                }
            ],
        },
        candidates=candidates,
        bgm_enabled=False,
        retrieval_topk_by_window={"bwin_000": ["bc_000"]},
    )

    assert any("retrieval_topk_candidate_ids" in error for error in errors)
