from __future__ import annotations

import json

import pytest

from packages.ai.gateway import ProviderGateway, ProviderResult
from packages.ai.gateway.provider_gateway import _deterministic_embedding
from packages.ai.prompts import PromptRegistry
from apps.api.services.clip_embeddings import _upsert_record
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
    MediaAssetRecord,
    NodeRun,
    NodeStatus,
    ProviderError,
    ProviderInvocation,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    ProviderStatus,
    RunStatus,
    UsageRole,
    UsageWindowV4,
    WarningCode,
    WorkflowRun,
)
from packages.core.contracts.artifacts import ClipEmbeddingRecord
from packages.core.storage.database import MediaAssetRow
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.planning.material import CLIP_INDEX_VERSION, build_clip_embedding_record
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
from packages.production.sqlalchemy_repository import SqlAlchemyProductionRepository


def _adapter(tmp_path) -> LocalRuntimeAdapter:
    repository = Repository()
    object_store = LocalObjectStore(root=tmp_path)
    return LocalRuntimeAdapter(
        repository,
        provider_gateway=ProviderGateway(repository, object_store=object_store),
        prompt_registry=PromptRegistry(repository),
        seed_media=False,
    )


class _FakeWindowQueryLlmProvider:
    provider_id = "fake.window_query_llm"

    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)
        self.calls = []

    def invoke(self, call):
        self.calls.append(call)
        output = self.outputs.pop(0)
        return ProviderResult(output=output, input_tokens=100, output_tokens=20)


class _SpyPromptRegistry(PromptRegistry):
    def __init__(self, repository: Repository) -> None:
        super().__init__(repository)
        self.render_calls = []

    def render(self, **kwargs):
        self.render_calls.append(kwargs)
        return super().render(**kwargs)


def _seed_fake_window_query_llm(adapter: LocalRuntimeAdapter) -> _FakeWindowQueryLlmProvider:
    adapter.repository.provider_profiles["fake.window_query_llm.prod"] = ProviderProfile(
        id="fake.window_query_llm.prod",
        provider_id="fake.window_query_llm",
        model_id="qwen3.7-plus",
        capability="llm.chat",
        display_name="Fake Window Query LLM",
        environment="prod",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.llm.options"),
    )
    provider = _FakeWindowQueryLlmProvider([])
    adapter.provider_gateway.register(provider)
    return provider


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


def _request(
    *,
    script: str = "今天看施工前后变化。第一步先看施工前现场。",
    instruction: str | None = None,
    broll_mode: str = "insert",
) -> DigitalHumanVideoRequest:
    payload = {
        "case_id": "case_demo",
        "script": script,
        "title": "案例",
        "voice": {"voice_id": "voice_sandbox"},
        "broll": {"enabled": True, "mode": broll_mode, "max_inserts": 2},
    }
    if instruction is not None:
        payload["edit"] = {"instruction": instruction}
    return DigitalHumanVideoRequest(
        **payload,
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


def _complete_default_portrait_windows() -> dict:
    windows = _windows()
    default_segment = {
        "segment_id": "portrait_1",
        "asset_id": "portrait_a",
        "clip_id": "portrait_clip",
        "start_sec": 0.0,
        "end_sec": 4.0,
        "source_start": 0.0,
        "source_end": 4.0,
        "role": "main",
        "source_mode": "lipsynced",
        "boundary_source": "semantic",
        "boundary_reason": None,
        "unit_ids": ["unit_1"],
        "slot_phase": "portrait_opening",
        "recently_used_material": False,
        "timeline_start_frame": 0,
        "timeline_end_frame": 120,
        "source_start_frame": 0,
        "source_end_frame": 120,
    }
    windows["default_assignment"] = {
        "portrait": [
            {
                "window_id": "portrait_a:portrait_clip",
                "segment_payload": default_segment,
            }
        ],
        "portrait_plan_payload": {
            "fps": 30,
            "total_duration": 4.0,
            "asset_id": "portrait_a",
            "duration_sec": 4.0,
            "segments": [default_segment],
            "diagnostics": {"planner": "timeline_window_default", "segment_count": 1},
        },
        "engine": "compiler_default",
    }
    return windows


def _full_coverage_broll_windows() -> dict:
    return {
        "fps": 30,
        "total_frames": 120,
        "geometry_policy": {
            "broll_window_contract": {
                "semantics": "authoritative_full_coverage_main_visual_track",
                "downstream_may_skip": False,
                "downstream_may_resize": False,
                "downstream_may_stitch": True,
            }
        },
        "portrait_windows": [],
        "broll_windows": [
            {
                "window_id": "bwin_000",
                "start_frame": 0,
                "end_frame": 60,
                "length_frames": 60,
                "source_length_frames": 60,
                "host_unit_ids": ["unit_1"],
                "text": "施工前现场",
            },
            {
                "window_id": "bwin_001",
                "start_frame": 60,
                "end_frame": 120,
                "length_frames": 60,
                "source_length_frames": 60,
                "host_unit_ids": ["unit_1"],
                "text": "补漆后效果",
            },
        ],
        "default_assignment": {
            "portrait": [],
            "portrait_plan_payload": {
                "fps": 30,
                "total_duration": 4.0,
                "asset_id": None,
                "duration_sec": 4.0,
                "segments": [],
                "diagnostics": {"track_mode": "broll_full_coverage"},
            },
            "engine": "compiler_full_coverage",
        },
        "compile_diagnostics": {"track_mode": "broll_full_coverage"},
    }


def _stitched_full_coverage_windows() -> dict:
    return {
        "fps": 30,
        "total_frames": 180,
        "geometry_policy": {
            "broll_window_contract": {
                "semantics": "authoritative_full_coverage_main_visual_track",
                "downstream_may_skip": False,
                "downstream_may_resize": False,
                "downstream_may_stitch": True,
            }
        },
        "portrait_windows": [],
        "broll_windows": [
            {
                "window_id": "bwin_000",
                "start_frame": 0,
                "end_frame": 180,
                "length_frames": 180,
                "source_length_frames": 180,
                "host_unit_ids": ["unit_1"],
                "text": "施工前现场到补漆后效果",
                "text_assignment": "argmax_overlap",
                "scene_hint": "施工前现场到补漆后效果",
            }
        ],
        "default_assignment": {
            "portrait": [],
            "portrait_plan_payload": {
                "fps": 30,
                "total_duration": 6.0,
                "asset_id": None,
                "duration_sec": 6.0,
                "segments": [],
                "diagnostics": {"track_mode": "broll_full_coverage"},
            },
            "engine": "compiler_full_coverage",
        },
        "compile_diagnostics": {"track_mode": "broll_full_coverage"},
    }


def _stitched_material() -> dict:
    material = _material()
    material["broll_candidates"] = [
        {
            "asset_id": "broll_a",
            "score": 1.0,
            "reason": "eligible b-roll clip",
            "metadata": {
                "clip_id": "clip_a",
                "source_start": 0.0,
                "source_end": 3.0,
                "source_frames_available": 90,
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
                "source_end": 3.0,
                "source_frames_available": 90,
                "matched_keywords": ["补漆后"],
                "scene_name": "补漆后",
                "diversity_key": "scene:b",
            },
        },
    ]
    return material


def _annotate_broll(repository: Repository, *, asset_id: str = "broll_a") -> None:
    repository.media_assets[asset_id] = MediaAssetRecord(
        id=asset_id,
        case_id="case_demo",
        title=asset_id,
        kind="video",
        annotation_status="annotated",
        usable=True,
    )
    annotation = AnnotationV4(
        meta=AnnotationMetaV4(
            asset_id=asset_id,
            case_id="case_demo",
            material_type="broll",
            duration=4.0,
        ),
        clips=[
            ClipV4(
                segment_id="clip_a",
                start=0.0,
                end=4.0,
                duration=4.0,
                semantics=ClipSemanticsV4(scene_type="施工前", narrative_role="现场展示"),
                usage=ClipUsageV4(role=UsageRole.cover),
                retrieval=ClipRetrievalV4(
                    summary="施工前现场",
                    keywords=["施工前", "现场"],
                    retrieval_sentence="施工前现场",
                ),
            )
        ],
        usage_windows=[
            UsageWindowV4(start=0.0, end=4.0, role=UsageRole.cover, confidence=0.9)
        ],
        quality_report={"usable_ratio": 0.9},
    )
    repository.annotations[asset_id] = AnnotationEditorVm(
        asset=repository.media_assets[asset_id],
        etag="etag-broll",
        canonical=annotation.model_dump(mode="json"),
        projection={"usable": True},
    )


def _ctx(
    adapter: LocalRuntimeAdapter,
    node_id: str,
    artifacts: dict[ArtifactKind, Artifact],
    *,
    request: DigitalHumanVideoRequest | None = None,
):
    return NodeContext(
        adapter=adapter,
        run=_run(),
        node_run=_node_run(node_id),
        state=RunState(request=request or _request(), artifacts=artifacts),
    )


def _seed_clip_embedding_record(db_session_factory, record) -> None:
    with db_session_factory() as session:
        session.merge(
            MediaAssetRow(
                id=record.asset_id,
                case_id=None,
                title=record.asset_id,
                kind="video",
                tags=[],
                annotation_status="annotated",
                usable=True,
            )
        )
        session.flush()
        _upsert_record(session, record)
        session.commit()


def _clip_embedding_record(
    *,
    key: str,
    asset_id: str,
    clip_id: str,
    namespace: str = "broll",
) -> ClipEmbeddingRecord:
    return ClipEmbeddingRecord(
        clip_embedding_key=key,
        asset_id=asset_id,
        asset_revision=f"asset:{asset_id}:v1:v1:test",
        clip_id=clip_id,
        source_start=0.0,
        source_end=4.0,
        source_frames_available=120,
        index_namespace=namespace,
        embedding_input_ref=f"{asset_id}:{clip_id}:0.000000:4.000000",
        embedding_id=f"emb_{key}",
        embedding=[1.0, *([0.0] * 1023)],
        provider_profile_id="sandbox.embedding.default",
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
    assert all(set(item) == {"window_id", "retrieval_intent"} for item in payload["window_queries"])
    assert {item["window_id"] for item in payload["window_queries"]} == {
        "pwin_000",
        "bwin_000",
    }


def test_window_query_planning_keeps_instruction_when_narration_is_trimmed(tmp_path):
    adapter = _adapter(tmp_path)
    instruction = "必须保留这个检索指令"
    long_text = "超长旁白" * 260
    narration = _narration()
    narration["units"][0]["text"] = long_text
    windows = _windows()
    windows["broll_windows"][0]["text"] = long_text
    ctx = _ctx(
        adapter,
        "WindowQueryPlanning",
        {
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                windows,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, narration),
        },
        request=_request(instruction=instruction),
    )

    output = nodes.window_query_planning.run(ctx)

    query_by_window = {
        item["window_id"]: item["retrieval_intent"]
        for item in output.artifacts[0].payload["window_queries"]
    }
    assert instruction in query_by_window["pwin_000"]
    assert instruction in query_by_window["bwin_000"]
    assert len(query_by_window["pwin_000"]) <= 900
    assert len(query_by_window["bwin_000"]) <= 900


def test_window_query_planning_omits_portrait_narration_when_unit_text_missing(tmp_path):
    adapter = _adapter(tmp_path)
    instruction = "优先稳定正脸"
    script = "SCRIPT_SENTINEL_" + ("整段脚本" * 200)
    windows = _windows()
    windows["portrait_windows"][0]["unit_ids"] = ["missing_unit"]
    ctx = _ctx(
        adapter,
        "WindowQueryPlanning",
        {
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                windows,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
        },
        request=_request(script=script, instruction=instruction),
    )

    output = nodes.window_query_planning.run(ctx)

    portrait_query = next(
        item["retrieval_intent"]
        for item in output.artifacts[0].payload["window_queries"]
        if item["window_id"] == "pwin_000"
    )
    assert portrait_query.startswith("A-roll portrait talking-head source clip")
    assert instruction in portrait_query
    assert "SCRIPT_SENTINEL" not in portrait_query
    assert "Narration:" not in portrait_query


def test_window_query_planning_respects_full_coverage_text_assignment(tmp_path):
    adapter = _adapter(tmp_path)
    windows = _windows()
    windows["broll_windows"][0]["text"] = ""
    windows["broll_windows"][0]["text_assignment"] = "argmax_overlap"
    ctx = _ctx(
        adapter,
        "WindowQueryPlanning",
        {
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                windows,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
        },
    )

    output = nodes.window_query_planning.run(ctx)

    broll_query = next(
        item["retrieval_intent"]
        for item in output.artifacts[0].payload["window_queries"]
        if item["window_id"] == "bwin_000"
    )
    assert "Narration:" not in broll_query
    assert "施工前现场" not in broll_query


def test_window_query_planning_uses_llm_queries_and_unwraps_intent(tmp_path):
    adapter = _adapter(tmp_path)
    provider = _seed_fake_window_query_llm(adapter)
    provider.outputs.append(
        {
            "intent": {
                "window_queries": [
                    {
                        "window_id": "pwin_000",
                        "retrieval_intent": "稳定正脸口播，白色上衣，口型清晰",
                    },
                    {
                        "window_id": "bwin_000",
                        "retrieval_intent": "施工前现场墙面细节和工具摆放",
                    },
                    {"window_id": "extra_window", "retrieval_intent": "应被丢弃"},
                ]
            }
        }
    )
    spy_registry = _SpyPromptRegistry(adapter.repository)
    adapter.prompt_registry = spy_registry
    ctx = _ctx(
        adapter,
        "WindowQueryPlanning",
        {
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _windows(),
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.case_context: _artifact(
                ArtifactKind.case_context,
                {"case_profile": {"product": "旧房翻新", "target_audience": "业主"}},
            ),
            ArtifactKind.creative_intent: _artifact(
                ArtifactKind.creative_intent,
                {"intent": {"tone": "可信", "beats": ["施工前", "施工后"]}},
            ),
        },
        request=_request(instruction="人像穿搭尽量一致"),
    )

    output = nodes.window_query_planning.run(ctx)

    payload = output.artifacts[0].payload
    query_by_window = {
        item["window_id"]: item["retrieval_intent"] for item in payload["window_queries"]
    }
    assert output.status == NodeStatus.succeeded
    assert output.warnings == []
    assert output.degradations == []
    assert len(output.provider_invocation_ids) == 1
    assert set(query_by_window) == {"pwin_000", "bwin_000"}
    assert query_by_window["pwin_000"] == "稳定正脸口播，白色上衣，口型清晰"
    assert query_by_window["bwin_000"] == "施工前现场墙面细节和工具摆放"
    assert payload["diagnostics"]["source"] == "llm_window_queries"
    call = provider.calls[0]
    assert call.input["response_format"] == {"type": "json_object"}
    assert call.idempotency_key == "run_window_retrieval:nr_WindowQueryPlanning:window_query_llm"
    render_variables = spy_registry.render_calls[0]["variables"]
    assert set(render_variables) == {
        "script",
        "edit_instruction",
        "case_context",
        "creative_beats",
        "windows",
    }
    assert render_variables["script"] == ctx.state.request.script
    assert render_variables["edit_instruction"] == "人像穿搭尽量一致"
    assert "旧房翻新" in render_variables["case_context"]
    assert "施工前" in render_variables["creative_beats"]
    prompt_windows = json.loads(render_variables["windows"])
    assert prompt_windows == [
        {
            "window_id": "pwin_000",
            "kind": "portrait",
            "narration_text": "今天看施工前后变化，第一步先看施工前现场。",
            "scene_hint": "",
        },
        {
            "window_id": "bwin_000",
            "kind": "broll",
            "narration_text": "施工前现场",
            "scene_hint": "",
        },
    ]


def test_window_query_planning_template_uses_scene_hint(tmp_path):
    adapter = _adapter(tmp_path)
    windows = _windows()
    windows["broll_windows"][0]["scene_hint"] = "墙面破损、工具、施工前细节"
    ctx = _ctx(
        adapter,
        "WindowQueryPlanning",
        {
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                windows,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
        },
    )

    output = nodes.window_query_planning.run(ctx)

    query_by_window = {
        item["window_id"]: item["retrieval_intent"]
        for item in output.artifacts[0].payload["window_queries"]
    }
    assert "Scene hint: 墙面破损、工具、施工前细节" in query_by_window["bwin_000"]


def test_window_query_planning_backfills_missing_llm_window_with_template(tmp_path):
    adapter = _adapter(tmp_path)
    provider = _seed_fake_window_query_llm(adapter)
    provider.outputs.append(
        {
            "intent": {
                "window_queries": [
                    {
                        "window_id": "pwin_000",
                        "retrieval_intent": "稳定口播人像，正脸清楚",
                    }
                ]
            }
        }
    )
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
    query_by_window = {
        item["window_id"]: item["retrieval_intent"] for item in payload["window_queries"]
    }
    assert output.status == NodeStatus.degraded
    assert WarningCode.window_query_template_fallback in output.warnings
    assert [notice.code for notice in output.degradations] == [
        WarningCode.window_query_template_fallback
    ]
    assert query_by_window["pwin_000"] == "稳定口播人像，正脸清楚"
    assert query_by_window["bwin_000"].startswith("B-roll insert clip")
    assert "Narration: 施工前现场" in query_by_window["bwin_000"]
    assert payload["diagnostics"]["source"] == "llm_window_queries"
    assert payload["diagnostics"]["template_backfilled_windows"] == ["bwin_000"]


def test_window_query_planning_falls_back_when_gateway_fails(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    _seed_fake_window_query_llm(adapter)
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

    def fake_invoke(_call):
        return (
            ProviderInvocation(
                id="pinv_window_query_failed",
                provider_id="fake.window_query_llm",
                model_id="qwen3.7-plus",
                provider_profile_id="fake.window_query_llm.prod",
                capability_id="llm.chat",
                status=ProviderStatus.failed,
                error=ProviderError(code=ErrorCode.provider_timeout, message="timeout"),
            ),
            None,
        )

    monkeypatch.setattr(adapter.provider_gateway, "invoke", fake_invoke)

    output = nodes.window_query_planning.run(ctx)

    payload = output.artifacts[0].payload
    query_by_window = {
        item["window_id"]: item["retrieval_intent"] for item in payload["window_queries"]
    }
    assert output.status == NodeStatus.degraded
    assert output.warnings == [WarningCode.window_query_template_fallback]
    assert [notice.code for notice in output.degradations] == [
        WarningCode.window_query_template_fallback
    ]
    assert output.provider_invocation_ids == ["pinv_window_query_failed"]
    assert query_by_window["pwin_000"].startswith("A-roll portrait talking-head")
    assert query_by_window["bwin_000"].startswith("B-roll insert clip")
    assert payload["diagnostics"]["source"] == "template_fallback"
    assert payload["diagnostics"]["fallback_reason"] == "provider_error"
    assert payload["diagnostics"]["error"]["code"] == ErrorCode.provider_timeout.value


def test_window_query_planning_falls_back_without_provider(tmp_path):
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
    assert output.status == NodeStatus.degraded
    assert output.warnings == [WarningCode.window_query_template_fallback]
    assert [notice.code for notice in output.degradations] == [
        WarningCode.window_query_template_fallback
    ]
    assert output.provider_invocation_ids == []
    assert payload["diagnostics"]["source"] == "template_fallback"
    assert payload["diagnostics"]["fallback_reason"] == "no_provider"


def test_window_material_retrieval_uses_material_pack_pool_and_sql_hnsw_index(
    tmp_path,
    db_session_factory,
):
    adapter = _adapter(tmp_path)
    adapter.production_repository = SqlAlchemyProductionRepository(db_session_factory)
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
    _seed_clip_embedding_record(db_session_factory, record)
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
    assert trace["source"] == "postgres_hnsw_clip_embedding_index"
    assert trace["embedding_model"] == "qwen3-vl-embedding"
    assert trace["embedding_dimension"] == 1024
    assert payload["diagnostics"]["retrieval_backend"] == "postgres_hnsw"
    assert payload["diagnostics"]["rejected_candidates"][0]["reason"] == "source_too_short"
    assert adapter.repository.clip_embedding_index == {}


def test_window_material_retrieval_allows_partial_full_coverage_candidates(tmp_path):
    adapter = _adapter(tmp_path)
    material = _stitched_material()
    candidates = index_candidates(material)
    diagnostics = {"rejected_candidates": []}

    eligible = nodes.window_material_retrieval._eligible_candidates(
        ctx=_ctx(adapter, "WindowMaterialRetrieval", {}),
        namespace="broll",
        window_id="bwin_000",
        required_frames=180,
        candidate_pool=candidates.broll_by_id,
        diagnostics=diagnostics,
        allow_partial_source=True,
    )
    nodes.window_material_retrieval._record_full_coverage_window_capacity(
        diagnostics=diagnostics,
        window_id="bwin_000",
        required_frames=180,
        eligible=eligible,
    )

    assert [item.candidate_id for item in eligible] == ["bc_000", "bc_001"]
    assert diagnostics["rejected_candidates"] == []
    assert diagnostics["full_coverage_capacity_by_window"]["bwin_000"] == {
        "required_frames": 180,
        "eligible_candidate_count": 2,
        "total_source_frames": 180,
        "longest_source_frames": 90,
        "sufficient_by_sum": True,
    }


def test_window_material_retrieval_run_uses_partial_full_coverage_capacity(
    tmp_path,
    db_session_factory,
):
    adapter = _adapter(tmp_path)
    adapter.production_repository = SqlAlchemyProductionRepository(db_session_factory)
    material = _stitched_material()
    for asset_id in ("broll_a", "broll_b"):
        adapter.repository.media_assets[asset_id] = MediaAssetRecord(
            id=asset_id,
            case_id="case_demo",
            title=asset_id,
            kind="video",
            annotation_status="annotated",
            usable=True,
        )
    windows_artifact = _artifact(
        ArtifactKind.plan_timeline_windows,
        _stitched_full_coverage_windows(),
    )
    query_ctx = _ctx(
        adapter,
        "WindowQueryPlanning",
        {
            ArtifactKind.plan_timeline_windows: windows_artifact,
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
        },
    )
    query_output = nodes.window_query_planning.run(query_ctx)
    query_artifact = query_output.artifacts[0]
    broll_intent = query_artifact.payload["window_queries"][0]["retrieval_intent"]
    indexed = index_candidates(material)
    for candidate_id in ("bc_000", "bc_001"):
        candidate = indexed.broll_by_id[candidate_id]
        record = build_clip_embedding_record(
            candidate=candidate,
            asset=adapter.repository.media_assets[candidate["asset_id"]],
            namespace="broll",
            provider_profile_id="sandbox.embedding.default",
            embedding=_deterministic_embedding(
                f"sandbox.embedding.default:multimodal.embedding:{broll_intent}",
                dimension=1024,
            ),
        )
        _seed_clip_embedding_record(db_session_factory, record)
    retrieval_ctx = _ctx(
        adapter,
        "WindowMaterialRetrieval",
        {
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack,
                material,
            ),
            ArtifactKind.plan_timeline_windows: windows_artifact,
            ArtifactKind.plan_window_queries: query_artifact,
        },
    )

    output = nodes.window_material_retrieval.run(retrieval_ctx)

    payload = output.artifacts[0].payload
    diagnostics = payload["diagnostics"]
    assert output.status == NodeStatus.succeeded
    assert [item["candidate_id"] for item in payload["candidates_by_window"]["bwin_000"]] == [
        "bc_000",
        "bc_001",
    ]
    assert diagnostics["full_coverage_partial_clip_stitching"] is True
    assert diagnostics["rejected_candidates"] == []
    assert diagnostics["full_coverage_capacity_by_window"]["bwin_000"] == {
        "required_frames": 180,
        "eligible_candidate_count": 2,
        "total_source_frames": 180,
        "longest_source_frames": 90,
        "sufficient_by_sum": True,
    }


def test_window_material_retrieval_keyword_fusion_breaks_close_semantic_tie():
    candidate_plain = nodes.window_material_retrieval._RetrievalCandidate(
        candidate_id="bc_000",
        candidate={
            "asset_id": "broll_plain",
            "metadata": {"source_start": 0.0, "source_end": 4.0},
        },
        clip_embedding_key="clipemb_plain",
        source_frames=120,
        index=0,
    )
    candidate_keyword = nodes.window_material_retrieval._RetrievalCandidate(
        candidate_id="bc_001",
        candidate={
            "asset_id": "broll_keyword",
            "metadata": {
                "source_start": 0.0,
                "source_end": 4.0,
                "matched_keywords": ["施工前"],
            },
        },
        clip_embedding_key="clipemb_keyword",
        source_frames=120,
        index=1,
    )

    class FakeProductionRepository:
        def nearest_clip_embeddings(self, **_kwargs):
            return [
                (
                    _clip_embedding_record(
                        key="clipemb_plain",
                        asset_id="broll_plain",
                        clip_id="plain",
                    ),
                    0.2,
                ),
                (
                    _clip_embedding_record(
                        key="clipemb_keyword",
                        asset_id="broll_keyword",
                        clip_id="keyword",
                    ),
                    0.2,
                ),
            ]

    ranked = nodes.window_material_retrieval._retrieve_for_window_from_sql(
        production_repository=FakeProductionRepository(),
        namespace="broll",
        eligible=[candidate_plain, candidate_keyword],
        query_embedding=[1.0, *([0.0] * 1023)],
        query_keywords=["施工前", "现场"],
        provider_profile_id="sandbox.embedding.default",
        required_frames=60,
    )

    assert [candidate.candidate_id for candidate in ranked[:2]] == ["bc_001", "bc_000"]
    assert ranked[0].retrieval_trace["keyword_adjustment"] == 0.075
    assert ranked[0].retrieval_trace["keyword_matched"] == ["施工前"]


def test_window_material_retrieval_uses_portrait_metadata_keywords_for_fusion():
    candidate_plain = nodes.window_material_retrieval._RetrievalCandidate(
        candidate_id="pc_000",
        candidate={
            "asset_id": "portrait_plain",
            "metadata": {"source_start": 0.0, "source_end": 4.0},
        },
        clip_embedding_key="clipemb_plain",
        source_frames=120,
        index=0,
    )
    candidate_keyword = nodes.window_material_retrieval._RetrievalCandidate(
        candidate_id="pc_001",
        candidate={
            "asset_id": "portrait_keyword",
            "metadata": {
                "source_start": 0.0,
                "source_end": 4.0,
                "keywords": ["稳定口播"],
            },
        },
        clip_embedding_key="clipemb_keyword",
        source_frames=120,
        index=1,
    )

    class FakeProductionRepository:
        def nearest_clip_embeddings(self, **_kwargs):
            return [
                (
                    _clip_embedding_record(
                        key="clipemb_plain",
                        asset_id="portrait_plain",
                        clip_id="plain",
                        namespace="portrait",
                    ),
                    0.2,
                ),
                (
                    _clip_embedding_record(
                        key="clipemb_keyword",
                        asset_id="portrait_keyword",
                        clip_id="keyword",
                        namespace="portrait",
                    ),
                    0.2,
                ),
            ]

    ranked = nodes.window_material_retrieval._retrieve_for_window_from_sql(
        production_repository=FakeProductionRepository(),
        namespace="portrait",
        eligible=[candidate_plain, candidate_keyword],
        query_embedding=[1.0, *([0.0] * 1023)],
        query_keywords=["稳定口播", "现场"],
        provider_profile_id="sandbox.embedding.default",
        required_frames=60,
    )

    assert [candidate.candidate_id for candidate in ranked[:2]] == ["pc_001", "pc_000"]
    assert ranked[0].retrieval_trace["keyword_adjustment"] == 0.075
    assert ranked[0].retrieval_trace["keyword_matched"] == ["稳定口播"]


def test_window_material_retrieval_without_keywords_preserves_legacy_score_formula():
    item = nodes.window_material_retrieval._RetrievalCandidate(
        candidate_id="bc_000",
        candidate={
            "asset_id": "broll_plain",
            "metadata": {"source_start": 0.0, "source_end": 4.0, "recency_penalty": 0.2},
        },
        clip_embedding_key="clipemb_plain",
        source_frames=120,
        index=3,
    )

    candidate = nodes.window_material_retrieval._retrieved_candidate(
        item=item,
        record=_clip_embedding_record(key="clipemb_plain", asset_id="broll_plain", clip_id="plain"),
        semantic_similarity=0.8,
        query_keywords=["施工前"],
        required_frames=60,
        source="postgres_hnsw_clip_embedding_index",
    )

    assert candidate.retrieval_score == round(0.8 - 0.02 - 0.000003, 6)
    assert candidate.retrieval_trace["keyword_adjustment"] == 0.0
    assert candidate.retrieval_trace["keyword_matched"] == []


def test_window_material_retrieval_status_ignores_normal_source_too_short_filtering():
    assert (
        nodes.window_material_retrieval._is_retrieval_degraded(
            diagnostics={
                "rejected_candidates": [],
                "missing_clip_embeddings": ["clipemb_missing"],
            },
            candidates_by_window={"bwin_000": [object()]},
        )
        is True
    )
    assert (
        nodes.window_material_retrieval._is_retrieval_degraded(
            diagnostics={
                "rejected_candidates": [{"reason": "source_too_short"}],
                "missing_clip_embeddings": [],
            },
            candidates_by_window={"pwin_000": [object()], "bwin_000": [object()]},
        )
        is False
    )
    assert (
        nodes.window_material_retrieval._is_retrieval_degraded(
            diagnostics={
                "rejected_candidates": [{"reason": "source_too_short"}],
                "missing_clip_embeddings": [],
            },
            candidates_by_window={"pwin_000": []},
        )
        is True
    )
    assert (
        nodes.window_material_retrieval._is_retrieval_degraded(
            diagnostics={
                "rejected_candidates": [{"reason": "query_embedding_failed"}],
                "missing_clip_embeddings": [],
            },
            candidates_by_window={"pwin_000": [object()]},
        )
        is True
    )


def test_window_material_retrieval_query_embedding_validation_edges():
    valid = nodes.window_material_retrieval._valid_query_embedding(
        {
            "dimension": "1024",
            "embedding": [2.0, *([0.0] * 1023)],
        }
    )
    assert valid is not None
    assert valid[0] == 1.0
    bad_outputs = [
        None,
        {"model": "other", "dimension": 1024, "embedding": [1.0, *([0.0] * 1023)]},
        {"dimension": True, "embedding": [1.0, *([0.0] * 1023)]},
        {"dimension": "1024x", "embedding": [1.0, *([0.0] * 1023)]},
        {"dimension": 1024, "normalization": "raw", "embedding": [1.0, *([0.0] * 1023)]},
        {"dimension": 1024, "index_version": "old", "embedding": [1.0, *([0.0] * 1023)]},
        {"dimension": 1024, "embedding": "not-a-list"},
        {"dimension": 1024, "embedding": ["oops", *([0.0] * 1023)]},
        {"dimension": 1024, "embedding": [1.0, 0.0]},
        {"dimension": 1024, "embedding": [0.0] * 1024},
    ]
    assert all(nodes.window_material_retrieval._valid_query_embedding(item) is None for item in bad_outputs)
    assert nodes.window_material_retrieval._embedding_output_metadata("bad") == {"type": "str"}
    assert nodes.window_material_retrieval._source_frames_available(
        {"metadata": {}},
        namespace="portrait",
    ) == 0


def test_window_material_retrieval_degrades_when_provider_profile_missing(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    monkeypatch.setattr(adapter.provider_profiles, "first_available", lambda *_args, **_kwargs: None)
    ctx = _ctx(
        adapter,
        "WindowMaterialRetrieval",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(ArtifactKind.plan_timeline_windows, _windows()),
            ArtifactKind.plan_window_queries: _artifact(
                ArtifactKind.plan_window_queries,
                {"window_queries": [{"window_id": "bwin_000", "retrieval_intent": "施工前现场"}]},
            ),
        },
    )

    output = nodes.window_material_retrieval.run(ctx)

    assert output.status == NodeStatus.degraded
    payload = output.artifacts[0].payload
    assert payload["diagnostics"]["query_embedding_provider_missing"] is True
    assert payload["candidates_by_window"] == {}


def test_window_material_retrieval_degrades_missing_queries_without_provider_call(tmp_path):
    adapter = _adapter(tmp_path)
    ctx = _ctx(
        adapter,
        "WindowMaterialRetrieval",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(ArtifactKind.plan_timeline_windows, _windows()),
            ArtifactKind.plan_window_queries: _artifact(
                ArtifactKind.plan_window_queries,
                {"window_queries": []},
            ),
        },
    )

    output = nodes.window_material_retrieval.run(ctx)

    assert output.status == NodeStatus.degraded
    payload = output.artifacts[0].payload
    assert payload["candidates_by_window"] == {"pwin_000": [], "bwin_000": []}
    assert {item["reason"] for item in payload["diagnostics"]["rejected_candidates"]} == {
        "missing_window_query"
    }


def _failed_provider_invocation(message: str = "timeout") -> ProviderInvocation:
    return ProviderInvocation(
        id="pinv_failed",
        provider_id="sandbox",
        model_id="qwen3-vl-embedding",
        provider_profile_id="sandbox.embedding.default",
        capability_id="multimodal.embedding",
        status=ProviderStatus.failed,
        error=ProviderError(code=ErrorCode.provider_timeout, message=message),
    )


def test_window_material_retrieval_degrades_provider_error_and_bad_output(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    ctx = _ctx(
        adapter,
        "WindowMaterialRetrieval",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(ArtifactKind.plan_timeline_windows, _windows()),
            ArtifactKind.plan_window_queries: _artifact(
                ArtifactKind.plan_window_queries,
                {
                    "window_queries": [
                        {"window_id": "pwin_000", "retrieval_intent": "口播主轨"},
                        {"window_id": "bwin_000", "retrieval_intent": "施工前现场"},
                    ]
                },
            ),
        },
    )
    calls = {"count": 0}

    def fake_invoke(_call):
        calls["count"] += 1
        if calls["count"] == 1:
            return _failed_provider_invocation(), None
        return (
            ProviderInvocation(
                id="pinv_bad_output",
                provider_id="sandbox",
                model_id="qwen3-vl-embedding",
                provider_profile_id="sandbox.embedding.default",
                capability_id="multimodal.embedding",
                status=ProviderStatus.succeeded,
            ),
            ProviderResult(output={"dimension": 1024, "embedding": "not-a-vector"}),
        )

    monkeypatch.setattr(adapter.provider_gateway, "invoke", fake_invoke)

    output = nodes.window_material_retrieval.run(ctx)

    assert output.status == NodeStatus.degraded
    payload = output.artifacts[0].payload
    assert [item["reason"] for item in payload["diagnostics"]["rejected_candidates"]] == [
        "query_embedding_failed",
        "query_embedding_incompatible",
    ]
    assert output.provider_invocation_ids == ["pinv_failed", "pinv_bad_output"]


def test_window_material_retrieval_sql_results_ignore_stale_embedding_keys():
    known = nodes.window_material_retrieval._RetrievalCandidate(
        candidate_id="bc_known",
        candidate={
            "asset_id": "broll_a",
            "metadata": {"recency_penalty": 2.0, "recent_usage": {"recency_penalty": 3.0}},
        },
        clip_embedding_key="clipemb_known",
        source_frames=90,
        index=4,
    )
    known_record = ClipEmbeddingRecord(
        clip_embedding_key="clipemb_known",
        asset_id="broll_a",
        asset_revision="asset:broll_a:v1:v1:test",
        clip_id="clip_a",
        source_start=0.0,
        source_end=3.0,
        source_frames_available=90,
        index_namespace="broll",
        embedding_input_ref="s3://bucket/clip.mp4",
        embedding_id="emb_known",
        embedding=[1.0, *([0.0] * 1023)],
        provider_profile_id="sandbox.embedding.default",
    )
    stale_record = known_record.model_copy(
        update={
            "clip_embedding_key": "clipemb_stale",
            "embedding_id": "emb_stale",
        }
    )

    class FakeRepository:
        def nearest_clip_embeddings(self, **kwargs):
            assert kwargs["limit"] == 1
            return [(stale_record, 0.01), (known_record, 0.2)]

    ranked = nodes.window_material_retrieval._retrieve_for_window_from_sql(
        production_repository=FakeRepository(),
        namespace="broll",
        eligible=[known],
        query_embedding=[1.0, *([0.0] * 1023)],
        query_keywords=[],
        provider_profile_id="sandbox.embedding.default",
        required_frames=60,
    )

    assert [candidate.candidate_id for candidate in ranked] == ["bc_known"]
    assert ranked[0].semantic_similarity == 0.8
    assert ranked[0].recency_adjustment == -0.3


def test_window_material_retrieval_uses_shared_clip_index_version():
    candidate_payload = {
        "asset_id": "broll_a",
        "metadata": {"clip_id": "clip_a", "source_start": 0.0, "source_end": 4.0},
    }
    record = build_clip_embedding_record(
        candidate=candidate_payload,
        asset=MediaAssetRecord(
            id="broll_a",
            title="施工现场",
            kind="video",
            source_artifact_id="artifact_broll_a",
        ),
        namespace="broll",
        provider_profile_id="sandbox.embedding.default",
        embedding=[1.0, *([0.0] * 1023)],
    )
    eligible = nodes.window_material_retrieval._RetrievalCandidate(
        candidate_id="bc_000",
        candidate=candidate_payload,
        clip_embedding_key=record.clip_embedding_key,
        source_frames=120,
        index=0,
    )

    class FakeRepository:
        def __init__(self) -> None:
            self.kwargs = {}

        def nearest_clip_embeddings(self, **kwargs):
            self.kwargs = kwargs
            return [(record, 0.1)]

    repository = FakeRepository()

    ranked = nodes.window_material_retrieval._retrieve_for_window_from_sql(
        production_repository=repository,
        namespace="broll",
        eligible=[eligible],
        query_embedding=[1.0, *([0.0] * 1023)],
        query_keywords=[],
        provider_profile_id="sandbox.embedding.default",
        required_frames=60,
    )

    assert record.index_version == CLIP_INDEX_VERSION
    assert repository.kwargs["index_version"] == CLIP_INDEX_VERSION
    assert ranked[0].retrieval_trace["index_version"] == CLIP_INDEX_VERSION


def test_window_material_retrieval_requires_sql_hnsw_repository(tmp_path):
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
    query_artifact = _artifact(
        ArtifactKind.plan_window_queries,
        {
            "window_queries": [
                {"window_id": "pwin_000", "retrieval_intent": "口播主轨"},
                {"window_id": "bwin_000", "retrieval_intent": "施工前现场"},
            ]
        },
    )
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

    with pytest.raises(NodeExecutionError) as exc:
        nodes.window_material_retrieval.run(retrieval_ctx)

    assert exc.value.error.code == ErrorCode.validation_invalid_options
    assert exc.value.error.details["required_backend"] == "postgres_hnsw"


def test_deterministic_editing_planning_consumes_window_retrieval_topk(tmp_path):
    adapter = _adapter(tmp_path)
    material = _material()
    material["broll_candidates"][1]["score"] = 2.0
    material["broll_candidates"][1]["metadata"]["source_end"] = 4.0
    material["broll_candidates"][1]["metadata"]["source_frames_available"] = 120
    retrieval = {
        "candidates_by_window": {
            "pwin_000": [
                {
                    "candidate_id": "pc_000",
                    "retrieval_score": 1.0,
                    "source_frames_available": 240,
                    "required_frames": 120,
                }
            ],
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

    portrait_payload = next(
        artifact.payload for artifact in output.artifacts if artifact.kind == ArtifactKind.plan_portrait
    )
    broll_payload = next(
        artifact.payload for artifact in output.artifacts if artifact.kind == ArtifactKind.plan_broll
    )
    assert portrait_payload["segments"][0]["asset_id"] == "portrait_a"
    assert broll_payload["overlays"][0]["window_id"] == "bwin_000"
    assert broll_payload["overlays"][0]["asset_id"] == "broll_a"
    assert broll_payload["overlays"][0]["timeline_start_frame"] == 30
    assert broll_payload["overlays"][0]["timeline_end_frame"] == 90


def test_deterministic_editing_planning_rejects_incomplete_default_portrait_fallback(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    retrieval = {"candidates_by_window": {}, "diagnostics": {}}
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
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

    with pytest.raises(NodeExecutionError) as exc:
        nodes.deterministic_editing_planning.run(ctx)

    assert exc.value.error.code == ErrorCode.material_insufficient_portrait
    assert exc.value.error.details["missing_assignment_window_ids"] == ["pwin_000"]
    assert exc.value.error.details["materialized_portrait_segment_count"] == 0


def test_deterministic_editing_planning_accepts_complete_default_portrait_fallback(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    windows = _complete_default_portrait_windows()
    retrieval = {"candidates_by_window": {}, "diagnostics": {}}
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                windows,
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

    portrait_payload = next(
        artifact.payload for artifact in output.artifacts if artifact.kind == ArtifactKind.plan_portrait
    )
    media_assignment = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_media_assignment
    )
    assert portrait_payload["segments"] == windows["default_assignment"]["portrait_plan_payload"][
        "segments"
    ]
    assert media_assignment["portrait"][0]["window_id"] == "pwin_000"
    assert media_assignment["diagnostics"]["portrait_assignment_source"] == (
        "timeline_window_default"
    )
    assert media_assignment["diagnostics"]["missing_retrieval_window_ids"] == ["pwin_000"]


def test_deterministic_editing_planning_falls_back_to_annotation_broll_when_retrieval_empty(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    _annotate_broll(adapter.repository)
    retrieval = {"candidates_by_window": {}, "diagnostics": {"missing_clip_embeddings": ["broll_a"]}}
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _complete_default_portrait_windows(),
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
    media_assignment = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_media_assignment
    )
    assert WarningCode.broll_skipped_no_material not in output.warnings
    assert broll_payload["overlays"][0]["asset_id"] == "broll_a"
    assert broll_payload["overlays"][0]["window_id"] == "bwin_000"
    assert broll_payload["overlays"][0]["matched_keywords"]
    assert media_assignment["diagnostics"]["broll_assignment_source"] == (
        "annotation_ranked_fallback"
    )
    assert media_assignment["diagnostics"]["missing_retrieval_broll"] is True


def test_deterministic_editing_planning_does_not_fallback_when_broll_topk_is_stale(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    _annotate_broll(adapter.repository)
    retrieval = {
        "candidates_by_window": {
            "bwin_000": [{"candidate_id": "bc_404", "retrieval_score": 0.9}]
        },
        "diagnostics": {},
    }
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _complete_default_portrait_windows(),
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
    media_assignment = next(
        artifact.payload
        for artifact in output.artifacts
        if artifact.kind == ArtifactKind.plan_media_assignment
    )
    assert WarningCode.broll_skipped_no_material in output.warnings
    assert broll_payload["overlays"] == []
    assert broll_payload["skipped_reason"] == WarningCode.broll_skipped_no_material.value
    assert media_assignment["broll"] == []
    assert "broll_assignment_source" not in media_assignment["diagnostics"]
    assert "missing_retrieval_broll" not in media_assignment["diagnostics"]


def test_deterministic_editing_planning_hard_fails_full_coverage_missing_window(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    retrieval = {
        "candidates_by_window": {
            "bwin_000": [{"candidate_id": "bc_000", "retrieval_score": 0.9}],
            "bwin_001": [],
        },
        "diagnostics": {},
    }
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                _full_coverage_broll_windows(),
            ),
            ArtifactKind.plan_window_material_retrieval: _artifact(
                ArtifactKind.plan_window_material_retrieval,
                retrieval,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.creative_intent: _artifact(ArtifactKind.creative_intent, {"intent": {}}),
        },
        request=_request(broll_mode="full_coverage"),
    )

    with pytest.raises(NodeExecutionError) as exc:
        nodes.deterministic_editing_planning.run(ctx)

    assert exc.value.error.code == ErrorCode.material_insufficient_broll
    assert exc.value.error.details["missing_broll_window_ids"] == ["bwin_001"]


def test_deterministic_editing_planning_stitches_full_coverage_window(tmp_path):
    adapter = _adapter(tmp_path)
    retrieval = {
        "candidates_by_window": {
            "bwin_000": [
                {
                    "candidate_id": "bc_000",
                    "retrieval_score": 0.99,
                    "source_frames_available": 90,
                    "required_frames": 180,
                },
                {
                    "candidate_id": "bc_001",
                    "retrieval_score": 0.98,
                    "source_frames_available": 90,
                    "required_frames": 180,
                },
            ]
        },
        "diagnostics": {
            "full_coverage_capacity_by_window": {
                "bwin_000": {
                    "required_frames": 180,
                    "eligible_candidate_count": 2,
                    "total_source_frames": 180,
                    "longest_source_frames": 90,
                    "sufficient_by_sum": True,
                }
            }
        },
    }
    windows_artifact = _artifact(
        ArtifactKind.plan_timeline_windows,
        _stitched_full_coverage_windows(),
    )
    ctx = _ctx(
        adapter,
        "DeterministicEditingPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(
                ArtifactKind.plan_material_pack,
                _stitched_material(),
            ),
            ArtifactKind.plan_timeline_windows: windows_artifact,
            ArtifactKind.plan_window_material_retrieval: _artifact(
                ArtifactKind.plan_window_material_retrieval,
                retrieval,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.creative_intent: _artifact(ArtifactKind.creative_intent, {"intent": {}}),
        },
        request=_request(broll_mode="full_coverage"),
    )

    output = nodes.deterministic_editing_planning.run(ctx)

    artifacts = {artifact.kind: artifact for artifact in output.artifacts}
    broll_payload = artifacts[ArtifactKind.plan_broll].payload
    media_assignment = artifacts[ArtifactKind.plan_media_assignment].payload
    overlays = broll_payload["overlays"]
    assert WarningCode.broll_skipped_no_material not in output.warnings
    assert WarningCode.broll_insertions_dropped_geometry not in output.warnings
    assert [
        (
            overlay["window_id"],
            overlay["asset_id"],
            overlay["timeline_start_frame"],
            overlay["timeline_end_frame"],
            overlay["source_start_frame"],
            overlay["source_end_frame"],
        )
        for overlay in overlays
    ] == [
        ("bwin_000", "broll_a", 0, 90, 0, 90),
        ("bwin_000", "broll_b", 90, 180, 0, 90),
    ]
    assert [choice["candidate_id"] for choice in media_assignment["broll"]] == [
        "bc_000",
        "bc_001",
    ]
    assert media_assignment["diagnostics"]["broll_drops"] == []
    for artifact in (
        artifacts[ArtifactKind.plan_portrait],
        artifacts[ArtifactKind.plan_broll],
        windows_artifact,
    ):
        adapter.repository.artifacts[artifact.id] = artifact

    timeline_state = RunState(
        request=_request(broll_mode="full_coverage"),
        artifacts={
            ArtifactKind.plan_portrait: artifacts[ArtifactKind.plan_portrait],
            ArtifactKind.plan_broll: artifacts[ArtifactKind.plan_broll],
            ArtifactKind.plan_timeline_windows: windows_artifact,
        },
    )
    timeline_ctx = NodeContext(
        adapter=adapter,
        run=_run(),
        node_run=_node_run("TimelinePlanning"),
        state=timeline_state,
    )
    timeline_output = nodes.timeline_planning.run(timeline_ctx)
    timeline = next(
        artifact.payload
        for artifact in timeline_output.artifacts
        if artifact.kind == ArtifactKind.plan_timeline
    )
    broll_track = [track for track in timeline["tracks"] if track["track_id"] == "broll"]
    assert timeline_output.status == NodeStatus.succeeded
    assert [
        (segment["timeline_start_frame"], segment["timeline_end_frame"])
        for segment in broll_track
    ] == [(0, 90), (90, 180)]
    assert timeline["validation"]["valid"] is True


def test_timeline_planning_rejects_full_coverage_stitching_gaps(tmp_path):
    adapter = _adapter(tmp_path)
    windows = _stitched_full_coverage_windows()
    portrait = _artifact(
        ArtifactKind.plan_portrait,
        windows["default_assignment"]["portrait_plan_payload"],
    )
    broll = _artifact(
        ArtifactKind.plan_broll,
        {
            "enabled": True,
            "overlays": [
                {
                    "overlay_id": "broll_1",
                    "window_id": "bwin_000",
                    "asset_id": "broll_a",
                    "clip_id": "clip_a",
                    "timeline_start": 0.0,
                    "timeline_end": 3.0,
                    "source_start": 0.0,
                    "source_end": 3.0,
                    "timeline_start_frame": 0,
                    "timeline_end_frame": 90,
                    "source_start_frame": 0,
                    "source_end_frame": 90,
                    "reason": "partial full coverage",
                    "confidence": 0.9,
                }
            ],
        },
    )
    ctx = _ctx(
        adapter,
        "TimelinePlanning",
        {
            ArtifactKind.plan_portrait: portrait,
            ArtifactKind.plan_broll: broll,
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                windows,
            ),
        },
        request=_request(broll_mode="full_coverage"),
    )
    for artifact in ctx.state.artifacts.values():
        adapter.repository.artifacts[artifact.id] = artifact

    with pytest.raises(NodeExecutionError) as exc:
        nodes.timeline_planning.run(ctx)

    assert exc.value.error.code == ErrorCode.render_invalid_timeline
    assert exc.value.error.details["coverage_gaps"] == [
        {"window_id": "bwin_000", "gaps": [{"start_frame": 90, "end_frame": 180}]}
    ]


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


def test_editing_agent_fallback_fails_when_retrieval_topk_cannot_cover_portrait_slots(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    windows = _windows()
    windows["portrait_windows"].append(
        {
            "window_id": "pwin_001",
            "start_frame": 120,
            "end_frame": 180,
            "unit_ids": ["unit_1"],
            "boundary_source": "semantic",
            "phase": "main",
        }
    )
    retrieval = {
        "candidates_by_window": {
            "pwin_000": [
                {
                    "candidate_id": "pc_000",
                    "retrieval_score": 1.0,
                    "source_frames_available": 240,
                    "required_frames": 120,
                }
            ],
            "pwin_001": [
                {
                    "candidate_id": "pc_000",
                    "retrieval_score": 1.0,
                    "source_frames_available": 240,
                    "required_frames": 60,
                }
            ],
        }
    }
    ctx = _ctx(
        adapter,
        "EditingAgentPlanning",
        {
            ArtifactKind.plan_material_pack: _artifact(ArtifactKind.plan_material_pack, _material()),
            ArtifactKind.plan_narration_boundary: _artifact(
                ArtifactKind.plan_narration_boundary,
                {"fps": 30, "total_frames": 180, "safe_cut_boundaries": []},
            ),
            ArtifactKind.plan_timeline_windows: _artifact(
                ArtifactKind.plan_timeline_windows,
                windows,
            ),
            ArtifactKind.plan_window_material_retrieval: _artifact(
                ArtifactKind.plan_window_material_retrieval,
                retrieval,
            ),
            ArtifactKind.narration_units: _artifact(ArtifactKind.narration_units, _narration()),
            ArtifactKind.creative_intent: _artifact(ArtifactKind.creative_intent, {"intent": {}}),
        },
    )

    with pytest.raises(NodeExecutionError) as exc:
        nodes.editing_agent_planning.run(ctx)

    assert exc.value.error.code == ErrorCode.material_insufficient_portrait
    assert any(
        "portrait slots not covered: pwin_001" in error
        for error in exc.value.error.details["errors"]
    )
