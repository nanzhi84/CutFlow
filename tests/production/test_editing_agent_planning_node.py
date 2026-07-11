"""EditingAgentPlanning node integration (issue #136).

Drives the node through a real ``NodeContext`` on the sandbox path (no real
llm.chat provider is armed in tests, so ``first_available_provider_profile``
returns None and the node takes the deterministic fallback) and asserts it emits
the four downstream artifacts with complete frame fields — proving the new
``digital_human_editing_agent_v1`` chain feeds the unchanged render pipeline.
Also asserts the honest fail-fast when the sandbox gate is off.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.ai.gateway import ProviderGateway, ProviderResult
from packages.ai.prompts import PromptRegistry
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    NodeRun,
    NodeStatus,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    RunStatus,
    WarningCode,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline import nodes
from packages.production.pipeline._editing_agent import (
    BrollChoice,
    EditingSelection,
    PortraitChoice,
)
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter

SCRIPT = "今天带你看一下这套案例。第一步先看施工前的样子。"


class _FakeEditingLlmProvider:
    provider_id = "fake.llm"

    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)
        self.calls = []

    def invoke(self, call):
        self.calls.append(call)
        output = self.outputs.pop(0)
        return ProviderResult(output=output, input_tokens=100, output_tokens=20)


def _adapter(tmp_path) -> LocalRuntimeAdapter:
    repository = Repository()
    object_store = LocalObjectStore(root=tmp_path)
    return LocalRuntimeAdapter(
        repository,
        provider_gateway=ProviderGateway(repository, object_store=object_store),
        prompt_registry=PromptRegistry(repository),
    )


def _seed_fake_llm_profile(adapter: LocalRuntimeAdapter) -> None:
    adapter.repository.provider_profiles["fake.llm.prod"] = ProviderProfile(
        id="fake.llm.prod",
        provider_id="fake.llm",
        model_id="qwen3.7-plus",
        capability="llm.chat",
        display_name="Fake LLM",
        environment="prod",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.llm.options"),
    )


def _material() -> dict:
    return {
        "portrait_candidates": [
            {
                "asset_id": "portrait_a",
                "score": 90.0,
                "reason": "白色上衣",
                "metadata": {
                    "clip_id": "clip_a",
                    "source_start": 0.0,
                    "source_end": 15.0,
                    "description": "白色上衣稳定口播",
                    "keywords": ["白色上衣", "稳定口播"],
                },
            },
            {
                "asset_id": "portrait_b",
                "score": 70.0,
                "reason": "黑色上衣",
                "metadata": {"clip_id": "clip_b", "source_start": 0.0, "source_end": 15.0},
            },
        ],
        "broll_candidates": [
            {
                "asset_id": "broll_x",
                "score": 80.0,
                "reason": "施工前",
                "metadata": {
                    "clip_id": "clip_x",
                    "source_start": 0.0,
                    "source_end": 6.0,
                    "scene_name": "工地/施工前",
                    "matched_keywords": ["施工前"],
                    "description": "施工前墙面状态特写",
                },
            },
        ],
        "font_candidates": [{"asset_id": "font_yst", "score": 50.0, "reason": "清晰字体"}],
        "bgm_candidates": [
            {
                "asset_id": "bgm_001",
                "score": 75.0,
                "reason": "稳定",
                "metadata": {
                    "clip_id": "bgm_clip",
                    "source_start": 0.0,
                    "source_end": 60.0,
                    "duration": 60.0,
                    "section_type": "stable_bed",
                    "mood": "励志",
                    "energy_profile": "medium",
                    "loopable": True,
                },
            },
        ],
    }


def _boundary() -> dict:
    return {
        "fps": 30,
        "total_frames": 360,
        "safe_cut_boundaries": [
            {"cut_id": "cut_000", "time": 0.0, "frame": 0, "source": "semantic_only"},
            {"cut_id": "cut_001", "time": 6.0, "frame": 180, "source": "semantic_audio_pause"},
            {"cut_id": "cut_002", "time": 12.0, "frame": 360, "source": "semantic_only"},
        ],
        "portrait_slots": [
            {
                "slot_id": "pslot_000",
                "start_frame": 0,
                "end_frame": 180,
                "unit_ids": ["unit_1"],
                "boundary_source": "semantic_audio_pause",
            },
            {
                "slot_id": "pslot_001",
                "start_frame": 180,
                "end_frame": 360,
                "unit_ids": ["unit_2"],
                "boundary_source": "semantic_only",
            },
        ],
        "broll_slots": [
            {
                "slot_id": "bslot_000",
                "start_frame": 60,
                "end_frame": 120,
                "unit_ids": ["unit_1"],
                "text": "施工前",
            },
        ],
        "pause_windows": [],
    }


def _timeline_windows(boundary: dict) -> dict:
    portrait_plan_payload = _default_portrait_plan(boundary)
    return {
        "fps": int(boundary.get("fps") or 30),
        "total_frames": int(boundary.get("total_frames") or 0),
        "geometry_policy": {},
        "portrait_windows": [
            {
                "window_id": slot["slot_id"],
                "start_frame": slot["start_frame"],
                "end_frame": slot["end_frame"],
                "unit_ids": list(slot.get("unit_ids") or []),
                "boundary_source": slot.get("boundary_source"),
                "phase": "opening" if index == 0 else "main",
            }
            for index, slot in enumerate(boundary.get("portrait_slots") or [])
        ],
        "broll_windows": [
            {
                "window_id": slot["slot_id"],
                "start_frame": slot["start_frame"],
                "end_frame": slot["end_frame"],
                "length_frames": slot["end_frame"] - slot["start_frame"],
                "host_unit_ids": list(slot.get("unit_ids") or []),
                "host_portrait_window_ids": [],
                "text": slot.get("text") or "",
                "boundary_source": slot.get("boundary_source") or "narration_unit",
            }
            for slot in boundary.get("broll_slots") or []
        ],
        "default_assignment": {
            "portrait": [
                {
                    "window_id": f"{segment['asset_id']}:{segment['clip_id']}",
                    "segment_payload": segment,
                }
                for segment in portrait_plan_payload["segments"]
            ],
            "portrait_plan_payload": portrait_plan_payload,
            "engine": "compiler_default",
        },
        "compile_diagnostics": {},
    }


def _default_portrait_plan(boundary: dict) -> dict:
    segments = []
    asset_ids = ["portrait_a", "portrait_b"]
    clip_ids = ["clip_a", "clip_b"]
    for index, slot in enumerate(boundary.get("portrait_slots") or []):
        start_frame = int(slot.get("start_frame", 0) or 0)
        end_frame = int(slot.get("end_frame", 0) or 0)
        source_start_frame = 0
        source_end_frame = end_frame - start_frame
        segments.append(
            {
                "segment_id": f"portrait_{index + 1}",
                "asset_id": asset_ids[index % len(asset_ids)],
                "clip_id": clip_ids[index % len(clip_ids)],
                "start_sec": round(start_frame / 30, 3),
                "end_sec": round(end_frame / 30, 3),
                "source_start": round(source_start_frame / 30, 3),
                "source_end": round(source_end_frame / 30, 3),
                "role": "main",
                "source_mode": "lipsynced",
                "boundary_source": slot.get("boundary_source"),
                "boundary_reason": None,
                "unit_ids": list(slot.get("unit_ids") or []),
                "slot_phase": "portrait_opening" if index == 0 else "portrait_main",
                "recently_used_material": False,
                "timeline_start_frame": start_frame,
                "timeline_end_frame": end_frame,
                "source_start_frame": source_start_frame,
                "source_end_frame": source_end_frame,
            }
        )
    total_frames = segments[-1]["timeline_end_frame"] if segments else 0
    total_duration = round(total_frames / 30, 3)
    return {
        "fps": 30,
        "total_duration": total_duration,
        "asset_id": segments[0]["asset_id"] if segments else None,
        "duration_sec": total_duration,
        "segments": segments,
        "diagnostics": {"planner": "compiler_default", "segment_count": len(segments)},
    }


def _state() -> RunState:
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script=SCRIPT,
        voice={"voice_id": "voice_sandbox"},
        subtitle={"font_id": "font_yst", "emphasis_font_size": 52},
        edit={"instruction": "尽量用穿搭相近的人像"},
        strictness={"strict_timestamps": False},
    )
    narration = {
        "source": "estimated",
        "strict": False,
        "units": [
            {
                "unit_id": "unit_1",
                "text": "今天带你看一下这套案例。",
                "start": 0.0,
                "end": 6.0,
                "confidence": 0.8,
            },
            {
                "unit_id": "unit_2",
                "text": "第一步先看施工前的样子。",
                "start": 6.0,
                "end": 12.0,
                "confidence": 0.8,
            },
        ],
    }

    def _art(art_id: str, kind: ArtifactKind, payload: dict, schema: str) -> Artifact:
        return Artifact(
            id=art_id,
            case_id="case_demo",
            run_id="run_1",
            node_run_id="nr_up",
            kind=kind,
            payload=payload,
            payload_schema=schema,
        )

    boundary = _boundary()
    return RunState(
        request=request,
        artifacts={
            ArtifactKind.plan_material_pack: _art(
                "art_material",
                ArtifactKind.plan_material_pack,
                _material(),
                "MaterialPackArtifact.v1",
            ),
            ArtifactKind.narration_units: _art(
                "art_narration",
                ArtifactKind.narration_units,
                narration,
                "NarrationUnitsArtifact.v1",
            ),
            ArtifactKind.plan_narration_boundary: _art(
                "art_boundary",
                ArtifactKind.plan_narration_boundary,
                boundary,
                "NarrationBoundaryPlan.v1",
            ),
            ArtifactKind.plan_timeline_windows: _art(
                "art_windows",
                ArtifactKind.plan_timeline_windows,
                _timeline_windows(boundary),
                "TimelineWindowsPlan.v1",
            ),
        },
    )


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_1",
        job_id="job_1",
        case_id="case_demo",
        workflow_template_id="digital_human_editing_agent_v1",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run(node_id: str = "EditingAgentPlanning") -> NodeRun:
    return NodeRun(
        id=f"nr_{node_id.lower()}",
        run_id="run_1",
        node_id=node_id,
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def _node_ctx(adapter: LocalRuntimeAdapter, state: RunState) -> NodeContext:
    return NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)


def _run_node(adapter: LocalRuntimeAdapter, state: RunState):
    return nodes.editing_agent_planning.run(_node_ctx(adapter, state))


def _run_media_selection_node(adapter: LocalRuntimeAdapter, state: RunState):
    run = _run().model_copy(update={"workflow_template_id": "digital_human_editing_agent_v2"})
    ctx = NodeContext(
        adapter=adapter,
        run=run,
        node_run=_node_run("MediaSelectionAgentPlanning"),
        state=state,
    )
    return nodes.media_selection_agent_planning.run(ctx)


def _payload(output, kind: ArtifactKind) -> dict:
    return next(a.payload for a in output.artifacts if a.kind == kind)


def _agent_context(state: RunState):
    return nodes.editing_agent_planning.build_editing_agent_context(
        request=state.request,
        material=state.artifacts[ArtifactKind.plan_material_pack].payload,
        narration=state.artifacts[ArtifactKind.narration_units].payload,
        boundary=state.artifacts[ArtifactKind.plan_narration_boundary].payload,
        windows=state.artifacts[ArtifactKind.plan_timeline_windows].payload,
        creative_intent=SimpleNamespace(emphasis=[]),
    )


def _disable_llm_reprompt(state: RunState) -> None:
    state.request = state.request.model_copy(
        update={
            "edit": state.request.edit.model_copy(update={"max_repair_attempts": 0}),
        }
    )


def _attach_retrieval(state: RunState, topk_by_window: dict[str, list[str]]) -> None:
    state.artifacts[ArtifactKind.plan_window_material_retrieval] = Artifact(
        id="art_retrieval",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_retrieval",
        kind=ArtifactKind.plan_window_material_retrieval,
        payload={
            "candidates_by_window": {
                window_id: [{"candidate_id": candidate_id} for candidate_id in candidate_ids]
                for window_id, candidate_ids in topk_by_window.items()
            },
            "diagnostics": {},
        },
        payload_schema="WindowMaterialRetrievalArtifact.v1",
    )


def test_build_editing_agent_context_is_independent_from_provider():
    state = _state()
    context = _agent_context(state)

    assert context.agent_boundary["portrait_slots"][0]["slot_id"] == "pslot_000"
    assert context.agent_input["portrait_slots"][0]["legal_window_ids"] == ["pc_000", "pc_001"]
    assert context.shortlist_counts == {
        "portrait": {"raw": 2, "eligible": 2, "exposed": 2, "dropped": 0},
        "broll": {"raw": 1, "eligible": 1, "exposed": 1, "dropped": 0},
    }
    assert context.candidates.portrait_by_id


def test_build_context_limits_llm_input_to_retrieval_topk_candidates():
    state = _state()
    retrieval = {
        "candidates_by_window": {
            "pslot_000": [{"candidate_id": "pc_001"}],
            "pslot_001": [{"candidate_id": "pc_001"}],
            "bslot_000": [{"candidate_id": "bc_000"}],
        }
    }
    context = nodes.editing_agent_planning.build_editing_agent_context(
        request=state.request,
        material=state.artifacts[ArtifactKind.plan_material_pack].payload,
        narration=state.artifacts[ArtifactKind.narration_units].payload,
        boundary=state.artifacts[ArtifactKind.plan_narration_boundary].payload,
        windows=state.artifacts[ArtifactKind.plan_timeline_windows].payload,
        retrieval=retrieval,
        creative_intent=SimpleNamespace(emphasis=[]),
    )

    assert set(context.candidates.portrait_by_id) == {"pc_000", "pc_001"}
    assert [item["candidate_id"] for item in context.agent_input["portrait_candidates"]] == [
        "pc_001"
    ]
    assert [item["candidate_id"] for item in context.agent_input["broll_candidates"]] == ["bc_000"]
    assert context.agent_input["portrait_slots"][0]["retrieval_topk_candidate_ids"] == ["pc_001"]


def test_compact_prompt_input_keeps_only_llm_decision_fields():
    context = _agent_context(_state())
    agent_input = dict(context.agent_input)
    retrieval_ids = [f"pc_{index:03d}" for index in range(14)]
    broll_ids = [f"bc_{index:03d}" for index in range(14)]
    agent_input["safe_cut_boundaries"] = [{"cut_id": "cut_001", "frame": 120}]
    agent_input["portrait_slots"] = [
        {
            **context.agent_input["portrait_slots"][0],
            "legal_window_ids": retrieval_ids,
            "retrieval_topk_candidate_ids": retrieval_ids,
        }
    ]
    agent_input["broll_slots"] = [
        {
            **context.agent_input["broll_slots"][0],
            "retrieval_topk_candidate_ids": broll_ids,
        }
    ]
    agent_input["bgm_candidates"] = [
        {"bgm_id": f"bgm_{index:03d}", "score": float(index), "script_fit": ["a", "b", "c"]}
        for index in range(8)
    ]

    compact = nodes.editing_agent_planning._compact_prompt_input(agent_input)

    assert compact["safe_cut_boundaries"] == []
    assert compact["portrait_slots"][0]["retrieval_topk_candidate_ids"] == retrieval_ids[:12]
    assert compact["portrait_slots"][0]["legal_window_ids"] == retrieval_ids[:12]
    assert compact["broll_slots"][0]["retrieval_topk_candidate_ids"] == broll_ids[:12]
    portrait_lines = compact["portrait_candidates"].splitlines()
    assert portrait_lines[0] == (
        "candidate_id | asset_id | available_seconds | description | reason"
    )
    assert portrait_lines[1] == ("pc_000 | portrait_a | 15.0 | 白色上衣稳定口播 | 白色上衣")
    broll_lines = compact["broll_candidates"].splitlines()
    assert broll_lines[0] == (
        "candidate_id | asset_id | scene_name | allowed_slot_ids | matched_keywords | "
        "available_seconds | description"
    )
    assert broll_lines[1] == (
        "bc_000 | broll_x | 工地/施工前 | bslot_000 | 施工前 | 6.0 | 施工前墙面状态特写"
    )
    assert "source_start" not in compact["portrait_candidates"]
    assert "source_end" not in compact["broll_candidates"]
    assert "available_frames" not in compact["portrait_candidates"]
    assert "score" not in compact["broll_candidates"]
    assert "diversity_key" not in compact["broll_candidates"]
    assert [candidate["bgm_id"] for candidate in compact["bgm_candidates"]] == [
        "bgm_007",
        "bgm_006",
        "bgm_005",
        "bgm_004",
        "bgm_003",
        "bgm_002",
    ]


def test_local_broll_constraint_repair_replaces_invalid_candidate():
    state = _state()
    material = state.artifacts[ArtifactKind.plan_material_pack].payload
    material["broll_candidates"] = [
        {
            "asset_id": "broll_bad",
            "score": 100.0,
            "metadata": {
                "clip_id": "bad",
                "source_start": 0.0,
                "source_end": 4.0,
                "scene_name": "detail_showcase",
                "diversity_key": "measuring",
                "matched_keywords": ["measuring", "precision"],
            },
        },
        {
            "asset_id": "broll_ok",
            "score": 90.0,
            "metadata": {
                "clip_id": "ok",
                "source_start": 0.0,
                "source_end": 4.0,
                "scene_name": "detail_showcase",
                "diversity_key": "measuring_alt",
                "matched_keywords": ["measuring", "car"],
            },
        },
    ]
    context = _agent_context(state)
    selection = EditingSelection(
        portrait=[
            PortraitChoice(
                slot_id="pslot_000",
                window_id="pc_000",
            ),
            PortraitChoice(
                slot_id="pslot_001",
                window_id="pc_001",
            ),
        ],
        broll=[
            BrollChoice(
                slot_id="bslot_000",
                candidate_id="bc_000",
                reason="semantic best but not retrieved",
                confidence=0.9,
                matched_keywords=("measuring",),
            )
        ],
        font_id="font_yst",
        bgm_id="bgm_001",
    )

    repaired, actions, errors = nodes.editing_agent_planning._repair_broll_selection_to_constraints(
        selection=selection,
        boundary=context.agent_boundary,
        candidates=context.candidates,
        bgm_enabled=state.request.bgm.enabled,
        max_inserts=state.request.broll.max_inserts,
        retrieval_topk_by_window={"bslot_000": ["bc_001"]},
    )

    assert errors == []
    assert repaired.broll[0].candidate_id == "bc_001"
    assert actions == [
        {
            "slot_id": "bslot_000",
            "original_candidate_id": "bc_000",
            "repaired_candidate_id": "bc_001",
            "action": "replaced",
            "reason": "matched nearest legal retrieval/diversity candidate",
        }
    ]


def test_local_broll_constraint_repair_fills_missing_full_coverage_slot():
    state = _state()
    state.request = state.request.model_copy(
        update={
            "broll": state.request.broll.model_copy(
                update={"enabled": True, "mode": "full_coverage"}
            )
        }
    )
    boundary = state.artifacts[ArtifactKind.plan_narration_boundary].payload
    boundary["broll_slots"].append(
        {
            "slot_id": "bslot_001",
            "start_frame": 120,
            "end_frame": 180,
            "unit_ids": ["unit_1"],
            "text": "施工后",
        }
    )
    windows = state.artifacts[ArtifactKind.plan_timeline_windows].payload
    windows["broll_windows"].append(
        {
            "window_id": "bslot_001",
            "start_frame": 120,
            "end_frame": 180,
            "length_frames": 60,
            "host_unit_ids": ["unit_1"],
            "host_portrait_window_ids": [],
            "text": "施工后",
            "boundary_source": "narration_unit",
        }
    )
    material = state.artifacts[ArtifactKind.plan_material_pack].payload
    material["broll_candidates"].append(
        {
            "asset_id": "broll_y",
            "score": 70.0,
            "reason": "施工后",
            "metadata": {
                "clip_id": "clip_y",
                "source_start": 0.0,
                "source_end": 6.0,
                "scene_name": "施工后",
                "matched_keywords": ["施工后"],
            },
        }
    )
    context = _agent_context(state)
    selection = EditingSelection(
        portrait=[
            PortraitChoice(slot_id="pslot_000", window_id="pc_000"),
            PortraitChoice(slot_id="pslot_001", window_id="pc_001"),
        ],
        broll=[BrollChoice(slot_id="bslot_000", candidate_id="bc_000")],
        font_id="font_yst",
        bgm_id="bgm_001",
    )

    repaired, actions, errors = nodes.editing_agent_planning._repair_broll_selection_to_constraints(
        selection=selection,
        boundary=context.agent_boundary,
        candidates=context.candidates,
        bgm_enabled=state.request.bgm.enabled,
        max_inserts=2,
        retrieval_topk_by_window={"bslot_000": ["bc_000"], "bslot_001": ["bc_001"]},
        require_broll_coverage=True,
        allow_asset_diversity_reuse=True,
    )

    assert errors == []
    assert [(choice.slot_id, choice.candidate_id) for choice in repaired.broll] == [
        ("bslot_000", "bc_000"),
        ("bslot_001", "bc_001"),
    ]
    assert actions == [
        {
            "slot_id": "bslot_001",
            "repaired_candidate_id": "bc_001",
            "action": "filled",
            "reason": "filled missing full_coverage broll slot from legal retrieval candidate",
            "covered_frames": 60,
            "required_frames": 60,
        }
    ]


def test_local_portrait_constraint_repair_replaces_topk_hallucination(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    provider = _FakeEditingLlmProvider(
        [
            {
                "intent": {
                    "portrait_plan": [
                        {"slot_id": "pslot_000", "window_id": "pc_000"},
                        {"slot_id": "pslot_001", "window_id": "pc_001"},
                    ],
                    "broll_plan": [{"slot_id": "bslot_000", "candidate_id": "bc_000"}],
                    "bgm_plan": {"bgm_id": "bgm_001"},
                }
            }
        ]
    )
    adapter.provider_gateway.register(provider)
    state = _state()
    _disable_llm_reprompt(state)
    _attach_retrieval(
        state,
        {
            "pslot_000": ["pc_001"],
            "pslot_001": ["pc_000"],
            "bslot_000": ["bc_000"],
        },
    )

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.succeeded
    assert output.provider_invocation_ids and len(output.provider_invocation_ids) == 1
    assert provider.calls[0].input["response_format"] == {"type": "json_object"}
    assert WarningCode.editing_agent_local_constraint_repair in output.warnings
    assignment = _payload(output, ArtifactKind.plan_media_assignment)
    assert assignment["engine"] == "editing_agent_llm"
    assert assignment["portrait"] == [
        {
            "window_id": "pslot_000",
            "candidate_id": "pc_001",
            "source_mode": "lipsynced",
            "reason": "（本地约束修正：pc_000 -> pc_001）",
        },
        {
            "window_id": "pslot_001",
            "candidate_id": "pc_000",
            "source_mode": "lipsynced",
            "reason": "（本地约束修正：pc_001 -> pc_000）",
        },
    ]
    repair_trace = assignment["diagnostics"]["repair_trace"]
    assert repair_trace[0]["error_count"] > 0
    assert repair_trace[1]["attempt"] == "local_constraint_repair_portrait"
    assert repair_trace[1]["error_count"] == 0


def test_local_portrait_constraint_repair_replaces_second_asset_conflict(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    adapter.provider_gateway.register(
        _FakeEditingLlmProvider(
            [
                {
                    "intent": {
                        "portrait_plan": [
                            {"slot_id": "pslot_000", "window_id": "pc_000"},
                            {"slot_id": "pslot_001", "window_id": "pc_000"},
                        ],
                        "broll_plan": [{"slot_id": "bslot_000", "candidate_id": "bc_000"}],
                        "bgm_plan": {"bgm_id": "bgm_001"},
                    }
                }
            ]
        )
    )
    state = _state()
    _disable_llm_reprompt(state)
    _attach_retrieval(
        state,
        {
            "pslot_000": ["pc_000"],
            "pslot_001": ["pc_000", "pc_001"],
            "bslot_000": ["bc_000"],
        },
    )

    output = _run_node(adapter, state)

    assignment = _payload(output, ArtifactKind.plan_media_assignment)
    assert output.provider_invocation_ids and len(output.provider_invocation_ids) == 1
    assert WarningCode.editing_agent_local_constraint_repair in output.warnings
    assert [item["candidate_id"] for item in assignment["portrait"]] == ["pc_000", "pc_001"]
    assert assignment["diagnostics"]["repair_trace"][1]["actions"] == [
        {
            "slot_id": "pslot_001",
            "original_window_id": "pc_000",
            "repaired_window_id": "pc_001",
            "action": "replaced",
            "reason": "matched legal portrait retrieval candidate",
        }
    ]


def test_local_portrait_constraint_repair_without_replacement_keeps_failure_path(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK", "0")
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    adapter.provider_gateway.register(
        _FakeEditingLlmProvider(
            [
                {
                    "intent": {
                        "portrait_plan": [
                            {"slot_id": "pslot_000", "window_id": "pc_000"},
                            {"slot_id": "pslot_001", "window_id": "pc_000"},
                        ],
                        "broll_plan": [{"slot_id": "bslot_000", "candidate_id": "bc_000"}],
                        "bgm_plan": {"bgm_id": "bgm_001"},
                    }
                }
            ]
        )
    )
    state = _state()
    _disable_llm_reprompt(state)
    _attach_retrieval(
        state,
        {
            "pslot_000": ["pc_000"],
            "pslot_001": ["pc_000"],
            "bslot_000": ["bc_000"],
        },
    )

    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, state)

    assert exc.value.error.code == ErrorCode.prompt_output_invalid
    assert len(adapter.repository.provider_invocations) == 1


def test_llm_repair_path_is_visible(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    adapter.provider_gateway.register(
        _FakeEditingLlmProvider(
            [
                {"intent": {"portrait_plan": [{"slot_id": "pslot_000", "window_id": "pc_999"}]}},
                {
                    "intent": {
                        "portrait_plan": [
                            {"slot_id": "pslot_000", "window_id": "pc_000"},
                            {"slot_id": "pslot_001", "window_id": "pc_001"},
                        ],
                        "broll_plan": [{"slot_id": "bslot_000", "candidate_id": "bc_000"}],
                        "bgm_plan": {"bgm_id": "bgm_001"},
                    }
                },
            ]
        )
    )

    output = _run_node(adapter, _state())

    assert output.status == NodeStatus.succeeded
    assert WarningCode.editing_agent_llm_repair in output.warnings
    assert output.degradations == []
    assert len(output.provider_invocation_ids) == 2
    assignment = _payload(output, ArtifactKind.plan_media_assignment)
    assert assignment["engine"] == "editing_agent_llm"
    assert assignment["diagnostics"]["repair_trace"][0]["error_count"] > 0
    assert assignment["diagnostics"]["repair_trace"][1]["error_count"] == 0


def test_select_editing_assignment_runs_before_materialization(tmp_path):
    adapter = _adapter(tmp_path)
    state = _state()
    context = _agent_context(state)

    result = nodes.editing_agent_planning.select_editing_assignment(
        ctx=_node_ctx(adapter, state),
        agent_context=context,
    )

    assert result.engine == "deterministic_fallback"
    assert result.fallback_used is True
    assert result.fallback_reason == "no_provider"
    assert result.provider_invocation_ids == []
    assert result.selection.broll


def test_materialize_editing_outputs_runs_after_selection(tmp_path):
    adapter = _adapter(tmp_path)
    state = _state()
    context = _agent_context(state)
    selection_result = nodes.editing_agent_planning.select_editing_assignment(
        ctx=_node_ctx(adapter, state),
        agent_context=context,
    )
    default_portrait = state.artifacts[ArtifactKind.plan_timeline_windows].payload[
        "default_assignment"
    ]["portrait_plan_payload"]

    materialized = nodes.editing_agent_planning.materialize_editing_outputs(
        request=state.request,
        node_id="EditingAgentPlanning",
        agent_context=context,
        selection_result=selection_result,
        huazi_result=nodes.editing_agent_planning._empty_huazi_result("no_candidates"),
    )

    assert materialized.assignment_payload["engine"] == "deterministic_fallback"
    assert materialized.portrait_payload == default_portrait
    assert materialized.broll_payload["enabled"] is True
    assert materialized.diagnostics["fallback_used"] is True


def test_fallback_path_emits_five_frame_exact_artifacts(tmp_path):
    state = _state()
    default_portrait = state.artifacts[ArtifactKind.plan_timeline_windows].payload[
        "default_assignment"
    ]["portrait_plan_payload"]
    output = _run_node(_adapter(tmp_path), state)

    kinds = {a.kind for a in output.artifacts}
    assert kinds == {
        ArtifactKind.plan_media_assignment,
        ArtifactKind.plan_portrait,
        ArtifactKind.plan_broll,
        ArtifactKind.plan_style,
        ArtifactKind.plan_editing_diagnostics,
    }
    # deterministic fallback is an honest graded degradation, never silent
    assert output.status == NodeStatus.degraded
    assert WarningCode.editing_agent_deterministic_fallback in output.warnings
    assert output.provider_invocation_ids == []

    portrait = _payload(output, ArtifactKind.plan_portrait)
    assert portrait == default_portrait
    assert len(portrait["segments"]) == 2
    for seg in portrait["segments"]:
        for key in (
            "timeline_start_frame",
            "timeline_end_frame",
            "source_start_frame",
            "source_end_frame",
        ):
            assert isinstance(seg[key], int)
    assert portrait["segments"][0]["timeline_start_frame"] == 0
    assert portrait["segments"][-1]["timeline_end_frame"] == 360

    broll = _payload(output, ArtifactKind.plan_broll)
    assert broll["enabled"] is True
    for ov in broll["overlays"]:
        for key in (
            "timeline_start_frame",
            "timeline_end_frame",
            "source_start_frame",
            "source_end_frame",
        ):
            assert isinstance(ov[key], int)

    style = _payload(output, ArtifactKind.plan_style)
    assert style["font_asset_id"] == "font_yst"
    assert style["bgm"]["asset_id"] == "bgm_001"

    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["mode"] == "deterministic_fallback"
    assert diagnostics["fallback_used"] is True
    assert diagnostics["fallback_reason"] == "no_provider"
    assert diagnostics["instruction"] == "尽量用穿搭相近的人像"
    assert {c["slot_id"] for c in diagnostics["portrait_choices"]} == {"pslot_000", "pslot_001"}

    assignment = _payload(output, ArtifactKind.plan_media_assignment)
    assert assignment["engine"] == "deterministic_fallback"
    assert assignment["portrait"] == [
        {
            "window_id": "pslot_000",
            "candidate_id": "portrait_a:clip_a",
            "source_mode": "lipsynced",
            "reason": "compiler default",
        },
        {
            "window_id": "pslot_001",
            "candidate_id": "portrait_b:clip_b",
            "source_mode": "lipsynced",
            "reason": "compiler default",
        },
    ]


def test_v2_media_selection_emits_media_only_and_does_not_require_creative_intent(tmp_path):
    state = _state()

    output = _run_media_selection_node(_adapter(tmp_path), state)

    assert {artifact.kind for artifact in output.artifacts} == {
        ArtifactKind.plan_media_assignment,
        ArtifactKind.plan_portrait,
        ArtifactKind.plan_broll,
        ArtifactKind.plan_media_selection_diagnostics,
    }
    assert ArtifactKind.plan_style not in {artifact.kind for artifact in output.artifacts}
    assignment = _payload(output, ArtifactKind.plan_media_assignment)
    diagnostics = _payload(output, ArtifactKind.plan_media_selection_diagnostics)
    assert "bgm_id" not in assignment
    assert "font_id" not in assignment
    assert "bgm_id" not in diagnostics
    assert "huazi_choices" not in diagnostics
    assert diagnostics["candidate_counts"] == {"portrait": 2, "broll": 1}


def test_v2_media_selection_uses_its_own_media_only_prompt(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    provider = _FakeEditingLlmProvider(
        [
            {
                "content": "media selection",
                "intent": {
                    "portrait_plan": [
                        {"slot_id": "pslot_000", "candidate_id": "pc_000", "reason": "fit"},
                        {"slot_id": "pslot_001", "candidate_id": "pc_001", "reason": "fit"},
                    ],
                    "broll_plan": [
                        {"slot_id": "bslot_000", "candidate_id": "bc_000", "reason": "fit"}
                    ],
                    "analysis": "media only",
                }
            }
        ]
    )
    adapter.provider_gateway.register(provider)

    output = _run_media_selection_node(adapter, _state())

    assert output.status == NodeStatus.succeeded
    assert len(output.provider_invocation_ids) == 1
    assert "bgm_candidates" not in provider.calls[0].input["prompt"]
    assert {artifact.kind for artifact in output.artifacts} == {
        ArtifactKind.plan_media_assignment,
        ArtifactKind.plan_portrait,
        ArtifactKind.plan_broll,
        ArtifactKind.plan_media_selection_diagnostics,
    }


def test_v2_media_selection_repairs_duplicate_diversity_without_second_call(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    provider = _FakeEditingLlmProvider(
        [
            {
                "content": "media selection",
                "intent": {
                    "portrait_plan": [
                        {"slot_id": "pslot_000", "candidate_id": "pc_000", "reason": "fit"},
                        {"slot_id": "pslot_001", "candidate_id": "pc_001", "reason": "fit"},
                    ],
                    "broll_plan": [
                        {"slot_id": "bslot_000", "candidate_id": "bc_000", "reason": "fit"},
                        {"slot_id": "bslot_001", "candidate_id": "bc_001", "reason": "fit"},
                    ],
                    "analysis": "duplicate diversity",
                },
            }
        ]
    )
    adapter.provider_gateway.register(provider)
    state = _state()
    material = state.artifacts[ArtifactKind.plan_material_pack].payload
    material["broll_candidates"] = [
        {
            "asset_id": "broll_a",
            "score": 100.0,
            "metadata": {
                "clip_id": "broll_a_clip",
                "source_start": 0.0,
                "source_end": 6.0,
                "scene_name": "产品展示 A",
                "diversity_key": "product_demo",
            },
        },
        {
            "asset_id": "broll_b",
            "score": 90.0,
            "metadata": {
                "clip_id": "broll_b_clip",
                "source_start": 0.0,
                "source_end": 6.0,
                "scene_name": "产品展示 B",
                "diversity_key": "product_demo",
            },
        },
        {
            "asset_id": "broll_c",
            "score": 80.0,
            "metadata": {
                "clip_id": "broll_c_clip",
                "source_start": 0.0,
                "source_end": 6.0,
                "scene_name": "施工细节",
                "diversity_key": "construction_detail",
            },
        },
    ]
    boundary = state.artifacts[ArtifactKind.plan_narration_boundary].payload
    boundary["broll_slots"].append(
        {
            "slot_id": "bslot_001",
            "start_frame": 120,
            "end_frame": 180,
            "unit_ids": ["unit_1"],
            "text": "施工细节",
        }
    )
    state.artifacts[ArtifactKind.plan_timeline_windows].payload = _timeline_windows(boundary)

    output = _run_media_selection_node(adapter, state)

    assert output.status == NodeStatus.succeeded
    assert len(provider.calls) == 1
    assert len(output.provider_invocation_ids) == 1
    assert WarningCode.media_selection_agent_local_constraint_repair in output.warnings
    assert "diversity_key" in provider.calls[0].input["prompt"]
    assert "product_demo" in provider.calls[0].input["prompt"]
    assignment = _payload(output, ArtifactKind.plan_media_assignment)
    assert [choice["candidate_id"] for choice in assignment["broll"]] == [
        "bc_000",
        "bc_002",
    ]
    assert assignment["diagnostics"]["repair_trace"][-1]["attempt"] == (
        "local_media_constraint_repair"
    )
    assert assignment["diagnostics"]["repair_trace"][-1]["error_count"] == 0


def test_v2_media_selection_hard_rejects_postprocess_and_geometry_overreach(monkeypatch, tmp_path):
    monkeypatch.setenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK", "0")
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    provider = _FakeEditingLlmProvider(
        [
            {
                "content": "invalid overreach",
                "intent": {
                    "portrait_plan": [
                        {
                            "slot_id": "pslot_000",
                            "candidate_id": "pc_000",
                            "reason": "fit",
                            "start": 0,
                        },
                        {
                            "slot_id": "pslot_001",
                            "candidate_id": "pc_001",
                            "reason": "fit",
                        },
                    ],
                    "broll_plan": [
                        {
                            "slot_id": "bslot_000",
                            "candidate_id": "bc_000",
                            "reason": "fit",
                            "rect": [0, 0, 1, 1],
                        }
                    ],
                    "analysis": "overreach",
                    "bgm_id": "bgm_001",
                }
            }
        ]
    )
    adapter.provider_gateway.register(provider)
    state = _state()
    _disable_llm_reprompt(state)

    with pytest.raises(NodeExecutionError) as exc:
        _run_media_selection_node(adapter, state)

    assert exc.value.error.code == ErrorCode.prompt_output_invalid
    assert len(provider.calls) == 1


def test_editing_agent_artifacts_feed_timeline_assembly_validation(tmp_path):
    adapter = _adapter(tmp_path)
    state = _state()

    editing_output = _run_node(adapter, state)
    for artifact in editing_output.artifacts:
        state.artifacts[artifact.kind] = artifact

    timeline_ctx = NodeContext(
        adapter=adapter,
        run=_run(),
        node_run=_node_run("TimelineAssemblyValidation"),
        state=state,
    )
    timeline_output = nodes.timeline_assembly_validation.run(timeline_ctx)

    assert timeline_output.status == NodeStatus.succeeded
    assert {artifact.kind for artifact in timeline_output.artifacts} == {
        ArtifactKind.plan_timeline,
        ArtifactKind.plan_render,
    }
    timeline = _payload(timeline_output, ArtifactKind.plan_timeline)
    assert {track["track_id"] for track in timeline["tracks"]} == {"portrait", "broll"}


def test_no_provider_without_sandbox_fallback_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK", "0")
    with pytest.raises(NodeExecutionError) as exc:
        _run_node(_adapter(tmp_path), _state())
    assert exc.value.error.code == ErrorCode.provider_unsupported_option


def test_agent_portrait_infeasible_slot_fails_with_material_insufficient(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    provider = _FakeEditingLlmProvider(
        [
            {
                "intent": {
                    "portrait_plan": [
                        {"slot_id": "pslot_000", "window_id": "pc_000"},
                        {"slot_id": "pslot_001", "window_id": "pc_001"},
                    ]
                }
            }
        ]
    )
    adapter.provider_gateway.register(provider)
    state = _state()
    material = state.artifacts[ArtifactKind.plan_material_pack].payload
    for candidate in material["portrait_candidates"]:
        candidate["metadata"]["source_end"] = 2.0

    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, state)

    assert exc.value.error.code == ErrorCode.material_insufficient_portrait
    assert provider.outputs
    assert adapter.repository.provider_invocations == {}
    details = exc.value.error.details
    assert details == {
        "failed_slot_ids": ["pslot_000", "pslot_001"],
        "required_frames_by_slot": {"pslot_000": 180, "pslot_001": 180},
        "longest_available_source_frames": 60,
        "portrait_candidate_count": 2,
    }


def test_llm_path_records_broll_window_contract_drops(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    adapter.provider_gateway.register(
        _FakeEditingLlmProvider(
            [
                {
                    "intent": {
                        "portrait_plan": [
                            {"slot_id": "pslot_000", "window_id": "pc_000"},
                            {"slot_id": "pslot_001", "window_id": "pc_001"},
                        ],
                        "broll_plan": [
                            {
                                "slot_id": "bslot_000",
                                "candidate_id": "bc_000",
                                "reason": "展示问题细节",
                                "confidence": 0.9,
                            }
                        ],
                        "bgm_plan": {"bgm_id": "bgm_001"},
                    }
                }
            ]
        )
    )
    state = _state()
    boundary = state.artifacts[ArtifactKind.plan_narration_boundary].payload
    boundary["total_frames"] = 816
    boundary["safe_cut_boundaries"] = [
        {"cut_id": "cut_000", "time": 488 / 30, "frame": 488, "source": "semantic_only"},
        {"cut_id": "cut_001", "time": 724 / 30, "frame": 724, "source": "semantic_only"},
        {"cut_id": "cut_002", "time": 816 / 30, "frame": 816, "source": "semantic_only"},
    ]
    boundary["portrait_slots"] = [
        {
            "slot_id": "pslot_000",
            "start_frame": 488,
            "end_frame": 724,
            "unit_ids": ["unit_1"],
            "boundary_source": "semantic_only",
        },
        {
            "slot_id": "pslot_001",
            "start_frame": 724,
            "end_frame": 816,
            "unit_ids": ["unit_2"],
            "boundary_source": "semantic_only",
        },
    ]
    boundary["broll_slots"] = [
        {
            "slot_id": "bslot_000",
            "start_frame": 668,
            "end_frame": 715,
            "unit_ids": ["unit_1"],
            "text": "问题细节",
        }
    ]
    windows = _timeline_windows(boundary)
    windows["broll_windows"][0]["length_frames"] = 120
    state.artifacts[ArtifactKind.plan_timeline_windows].payload = windows

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.degraded
    assert output.provider_invocation_ids
    assert WarningCode.broll_insertions_dropped_geometry in output.warnings
    assert any(
        notice.code == WarningCode.broll_insertions_dropped_geometry and notice.affects_true_yield
        for notice in output.degradations
    )
    broll = _payload(output, ArtifactKind.plan_broll)
    assert broll["overlays"] == []
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["broll_drops"] == [
        {"slot_id": "bslot_000", "candidate_id": "bc_000", "reason": "window_length_mismatch"}
    ]


def test_llm_path_repairs_full_coverage_to_single_window_sized_candidate(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    provider = _FakeEditingLlmProvider(
        [
            {
                "intent": {
                    "portrait_plan": [
                        {"slot_id": "pslot_000", "window_id": "pc_000"},
                        {"slot_id": "pslot_001", "window_id": "pc_001"},
                    ],
                    "broll_plan": [
                        {
                            "slot_id": "bslot_000",
                            "candidate_id": "bc_000",
                            "reason": "展示施工前",
                            "confidence": 0.9,
                        }
                    ],
                    "bgm_plan": {"bgm_id": "bgm_001"},
                }
            }
        ]
    )
    adapter.provider_gateway.register(provider)
    state = _state()
    state.request = state.request.model_copy(
        update={
            "broll": state.request.broll.model_copy(
                update={"enabled": True, "mode": "full_coverage", "max_inserts": 1}
            ),
            "edit": state.request.edit.model_copy(update={"max_repair_attempts": 0}),
        }
    )
    material = state.artifacts[ArtifactKind.plan_material_pack].payload
    material["broll_candidates"][0]["metadata"]["source_end"] = 3.0
    material["broll_candidates"].append(
        {
            "asset_id": "broll_y",
            "score": 70.0,
            "reason": "完整施工过程",
            "metadata": {
                "clip_id": "clip_y",
                "source_start": 0.0,
                "source_end": 6.0,
                "scene_name": "施工过程",
                "matched_keywords": ["施工"],
                "description": "完整施工过程细节",
            },
        }
    )
    boundary = state.artifacts[ArtifactKind.plan_narration_boundary].payload
    boundary["broll_slots"] = [
        {
            "slot_id": "bslot_000",
            "start_frame": 0,
            "end_frame": 180,
            "unit_ids": ["unit_1", "unit_2"],
            "text": "施工前到施工过程",
        }
    ]
    windows = _timeline_windows(boundary)
    windows["geometry_policy"] = {
        "broll_window_contract": {
            "semantics": "authoritative_full_coverage_main_visual_track",
            "downstream_may_skip": False,
            "downstream_may_resize": False,
            "downstream_may_stitch": False,
        }
    }
    windows["broll_windows"][0]["source_length_frames"] = 180
    state.artifacts[ArtifactKind.plan_timeline_windows].payload = windows
    _attach_retrieval(
        state,
        {
            "pslot_000": ["pc_000"],
            "pslot_001": ["pc_001"],
            "bslot_000": ["bc_000", "bc_001"],
        },
    )

    output = _run_node(adapter, state)

    prompt = provider.calls[0].input["prompt"]
    broll = _payload(output, ArtifactKind.plan_broll)
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert "multi_clip_allowed" in prompt
    assert output.status == NodeStatus.succeeded
    assert WarningCode.editing_agent_local_constraint_repair in output.warnings
    assert [
        (
            overlay["window_id"],
            overlay["asset_id"],
            overlay["timeline_start_frame"],
            overlay["timeline_end_frame"],
        )
        for overlay in broll["overlays"]
    ] == [
        ("bslot_000", "broll_y", 0, 180),
    ]
    assert [choice["candidate_id"] for choice in diagnostics["broll_choices"]] == [
        "bc_001",
    ]
    assert diagnostics["repair_trace"][-1]["actions"][-1]["action"] == "replaced"


def test_agent_slots_come_from_compiled_windows_not_base_slots(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    adapter.provider_gateway.register(
        _FakeEditingLlmProvider(
            [
                {
                    "intent": {
                        "portrait_plan": [
                            {"slot_id": "pwin_000", "window_id": "pc_000"},
                            {"slot_id": "pwin_001", "window_id": "pc_001"},
                        ],
                        "broll_plan": [],
                        "bgm_plan": {"bgm_id": "bgm_001"},
                    }
                }
            ]
        )
    )
    state = _state()
    boundary = state.artifacts[ArtifactKind.plan_narration_boundary].payload
    boundary["portrait_slots"] = [
        {"slot_id": "pslot_000", "start_frame": 0, "end_frame": 120, "unit_ids": ["unit_1"]},
        {"slot_id": "pslot_001", "start_frame": 120, "end_frame": 240, "unit_ids": ["unit_1"]},
        {"slot_id": "pslot_002", "start_frame": 240, "end_frame": 360, "unit_ids": ["unit_2"]},
    ]
    state.artifacts[ArtifactKind.plan_timeline_windows].payload["portrait_windows"] = [
        {
            "window_id": "pwin_000",
            "start_frame": 0,
            "end_frame": 180,
            "unit_ids": ["unit_1"],
            "boundary_source": "semantic_audio_pause",
            "phase": "opening",
        },
        {
            "window_id": "pwin_001",
            "start_frame": 180,
            "end_frame": 360,
            "unit_ids": ["unit_2"],
            "boundary_source": "semantic_only",
            "phase": "main",
        },
    ]

    output = _run_node(adapter, state)

    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert len(boundary["portrait_slots"]) == 3
    assert [choice["slot_id"] for choice in diagnostics["portrait_choices"]] == [
        "pwin_000",
        "pwin_001",
    ]
    raw_requests = [
        artifact
        for artifact in adapter.repository.artifacts.values()
        if artifact.kind == ArtifactKind.provider_raw_request
    ]
    assert "pwin_000" in raw_requests[0].payload["prompt"]
    assert '"slot_id": "pslot_000", "start_frame": 0' not in raw_requests[0].payload["prompt"]


def test_shortlist_applies_budget_and_reports_counts(tmp_path):
    state = _state()
    material = state.artifacts[ArtifactKind.plan_material_pack].payload
    material["portrait_candidates"] = [
        {
            "asset_id": f"portrait_{index:02d}",
            "score": 100 - index,
            "metadata": {
                "clip_id": f"clip_{index:02d}",
                "source_start": 0.0,
                "source_end": 20.0,
            },
        }
        for index in range(14)
    ]
    material["broll_candidates"] = [
        {
            "asset_id": f"broll_{index:02d}",
            "score": 100 - index,
            "metadata": {
                "clip_id": f"bclip_{index:02d}",
                "source_start": 0.0,
                "source_end": 6.0,
            },
        }
        for index in range(8)
    ]

    output = _run_node(_adapter(tmp_path), state)

    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["shortlist_counts"] == {
        "portrait": {"raw": 14, "eligible": 14, "exposed": 12, "dropped": 2},
        "broll": {"raw": 8, "eligible": 8, "exposed": 6, "dropped": 2},
    }
    assert diagnostics["candidate_counts"]["portrait"] == 12
    assert diagnostics["candidate_counts"]["broll"] == 6


def test_strict_uniqueness_uses_full_pool_not_shortlisted_prompt_budget(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    slot_count = 13
    duplicate_selection = {
        "intent": {
            "portrait_plan": [
                {
                    "slot_id": f"pslot_{index:03d}",
                    "window_id": f"pc_{max(0, index - 1):03d}",
                }
                for index in range(slot_count)
            ],
            "broll_plan": [],
            "bgm_plan": {"bgm_id": "bgm_001"},
        }
    }
    repaired_selection = {
        "intent": {
            **duplicate_selection["intent"],
            "portrait_plan": [
                {"slot_id": f"pslot_{index:03d}", "window_id": f"pc_{index:03d}"}
                for index in range(slot_count)
            ],
        }
    }
    adapter.provider_gateway.register(
        _FakeEditingLlmProvider([duplicate_selection, repaired_selection])
    )
    state = _state()
    material = state.artifacts[ArtifactKind.plan_material_pack].payload
    material["portrait_candidates"] = [
        {
            "asset_id": f"portrait_{index:02d}",
            "score": 100 - index,
            "metadata": {
                "clip_id": f"clip_{index:02d}",
                "source_start": 0.0,
                "source_end": 30.0,
            },
        }
        for index in range(slot_count)
    ]
    boundary = state.artifacts[ArtifactKind.plan_narration_boundary].payload
    boundary["total_frames"] = slot_count * 60
    boundary["safe_cut_boundaries"] = [
        {
            "cut_id": f"cut_{index:03d}",
            "time": round(index * 2.0, 3),
            "frame": index * 60,
            "source": "semantic_only",
        }
        for index in range(slot_count + 1)
    ]
    boundary["portrait_slots"] = [
        {
            "slot_id": f"pslot_{index:03d}",
            "start_frame": index * 60,
            "end_frame": (index + 1) * 60,
            "unit_ids": [f"unit_{index:03d}"],
            "boundary_source": "semantic_only",
        }
        for index in range(slot_count)
    ]
    boundary["broll_slots"] = []
    state.artifacts[ArtifactKind.plan_timeline_windows].payload = _timeline_windows(boundary)

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.succeeded
    assert len(output.provider_invocation_ids) == 2
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["shortlist_counts"]["portrait"] == {
        "raw": slot_count,
        "eligible": slot_count,
        "exposed": slot_count,
        "dropped": 0,
    }
    assert diagnostics["repair_trace"][0]["error_count"] == 1
    assert "more than one slot" in diagnostics["repair_trace"][0]["errors"][0]
    assert diagnostics["candidate_counts"]["portrait"] == slot_count


def test_shortlist_keeps_capacity_for_restricted_long_slots(tmp_path):
    long_slot_count = 13
    short_slot_count = 2
    slot_count = long_slot_count + short_slot_count
    state = _state()
    material = state.artifacts[ArtifactKind.plan_material_pack].payload
    material["portrait_candidates"] = [
        {
            "asset_id": f"portrait_long_{index:02d}",
            "score": 60 - index,
            "metadata": {
                "clip_id": f"long_{index:02d}",
                "source_start": 0.0,
                "source_end": 30.0,
            },
        }
        for index in range(long_slot_count)
    ] + [
        {
            "asset_id": f"portrait_short_{index:02d}",
            "score": 100 - index,
            "metadata": {
                "clip_id": f"short_{index:02d}",
                "source_start": 0.0,
                "source_end": 1.0,
            },
        }
        for index in range(20)
    ]
    boundary = state.artifacts[ArtifactKind.plan_narration_boundary].payload
    boundary["total_frames"] = long_slot_count * 60 + short_slot_count * 30
    frames = [index * 60 for index in range(long_slot_count + 1)]
    short_base_frame = frames[-1]
    frames.extend(short_base_frame + (index + 1) * 30 for index in range(short_slot_count))
    boundary["safe_cut_boundaries"] = [
        {
            "cut_id": f"cut_{index:03d}",
            "time": round(frame / 30, 3),
            "frame": frame,
            "source": "semantic_only",
        }
        for index, frame in enumerate(frames)
    ]
    boundary["portrait_slots"] = [
        {
            "slot_id": f"pslot_{index:03d}",
            "start_frame": frames[index],
            "end_frame": frames[index + 1],
            "unit_ids": [f"unit_{index:03d}"],
            "boundary_source": "semantic_only",
        }
        for index in range(slot_count)
    ]
    boundary["broll_slots"] = []
    state.artifacts[ArtifactKind.plan_timeline_windows].payload = _timeline_windows(boundary)

    output = _run_node(_adapter(tmp_path), state)

    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["shortlist_counts"]["portrait"] == {
        "raw": 33,
        "eligible": 33,
        "exposed": 25,
        "dropped": 8,
    }
    assert diagnostics["candidate_counts"]["portrait"] == 25


def test_llm_path_repairs_reused_portrait_asset(monkeypatch, tmp_path):
    """Real provider path: DashScope-style output nests the selection under
    ``output['intent']``; the node must unwrap it, honour the LLM's ID choices, and
    NOT burn repair attempts. Regression for the intent-unwrap blocker that the
    sandbox-only tests could not catch."""
    adapter = _adapter(tmp_path)
    fake_profile = SimpleNamespace(id="dashscope.llm.prod")
    monkeypatch.setattr(
        adapter.provider_profiles,
        "first_available",
        lambda capability, *, include_sandbox=True: fake_profile,
    )
    duplicate_selection = {
        "portrait_plan": [
            {"slot_id": "pslot_000", "window_id": "pc_001"},
            {"slot_id": "pslot_001", "window_id": "pc_001"},
        ],
        "broll_plan": [
            {
                "slot_id": "bslot_000",
                "candidate_id": "bc_000",
                "reason": "施工前",
                "confidence": 0.9,
            }
        ],
        "bgm_plan": {"bgm_id": "bgm_001"},
        "analysis": "统一穿搭",
    }
    repaired_selection = {
        **duplicate_selection,
        "portrait_plan": [
            {"slot_id": "pslot_000", "window_id": "pc_001"},
            {"slot_id": "pslot_001", "window_id": "pc_000"},
        ],
    }
    calls = []
    outputs = iter([duplicate_selection, repaired_selection])

    def fake_invoke(call):
        calls.append(call)
        invocation_id = f"inv_{len(calls)}"
        return (
            SimpleNamespace(id=invocation_id, error=None),
            SimpleNamespace(output={"content": "...", "intent": next(outputs)}),
        )

    monkeypatch.setattr(adapter.provider_gateway, "invoke", fake_invoke)

    output = _run_node(adapter, _state())

    assert output.status == NodeStatus.succeeded  # real LLM path, no fallback degradation
    assert output.provider_invocation_ids == ["inv_1", "inv_2"]
    assert len(calls) == 2
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["mode"] == "editing_agent_llm"
    assert diagnostics["repair_trace"][0]["error_count"] == 1
    assert "more than one slot" in diagnostics["repair_trace"][0]["errors"][0]
    portrait = _payload(output, ArtifactKind.plan_portrait)
    assert [seg["asset_id"] for seg in portrait["segments"]] == ["portrait_b", "portrait_a"]


def test_llm_path_repairs_portrait_choice_using_clean_source_span(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    fake_profile = SimpleNamespace(id="dashscope.llm.prod")
    monkeypatch.setattr(
        adapter.provider_profiles,
        "first_available",
        lambda capability, *, include_sandbox=True: fake_profile,
    )
    state = _state()
    material = state.artifacts[ArtifactKind.plan_material_pack].payload
    material["portrait_candidates"] = [
        {
            "asset_id": "portrait_a",
            "score": 90.0,
            "reason": "raw long but motion tail",
            "metadata": {
                "clip_id": "clip_a",
                "source_start": 0.0,
                "source_end": 8.0,
                "avoid_spans": [[2.0, 8.0]],
            },
        },
        {
            "asset_id": "portrait_b",
            "score": 70.0,
            "reason": "clean middle",
            "metadata": {
                "clip_id": "clip_b",
                "source_start": 0.0,
                "source_end": 8.0,
                "avoid_spans": [[0.0, 1.0], [7.0, 8.0]],
            },
        },
    ]
    boundary = state.artifacts[ArtifactKind.plan_narration_boundary].payload
    boundary["total_frames"] = 240
    boundary["safe_cut_boundaries"] = [
        {"cut_id": "cut_000", "time": 0.0, "frame": 0, "source": "semantic_only"},
        {"cut_id": "cut_001", "time": 2.0, "frame": 60, "source": "semantic_audio_pause"},
        {"cut_id": "cut_002", "time": 8.0, "frame": 240, "source": "semantic_only"},
    ]
    boundary["portrait_slots"] = [
        {
            "slot_id": "pslot_000",
            "start_frame": 0,
            "end_frame": 60,
            "unit_ids": ["unit_1"],
            "boundary_source": "semantic_audio_pause",
        },
        {
            "slot_id": "pslot_001",
            "start_frame": 60,
            "end_frame": 240,
            "unit_ids": ["unit_2"],
            "boundary_source": "semantic_only",
        },
    ]
    boundary["broll_slots"] = []
    state.artifacts[ArtifactKind.plan_timeline_windows].payload = _timeline_windows(boundary)

    invalid_selection = {
        "portrait_plan": [
            {"slot_id": "pslot_000", "window_id": "pc_001"},
            {"slot_id": "pslot_001", "window_id": "pc_000"},
        ],
        "broll_plan": [],
        "bgm_plan": {"bgm_id": "bgm_001"},
    }
    repaired_selection = {
        **invalid_selection,
        "portrait_plan": [
            {"slot_id": "pslot_000", "window_id": "pc_000"},
            {"slot_id": "pslot_001", "window_id": "pc_001"},
        ],
    }
    outputs = iter([invalid_selection, repaired_selection])
    calls = []

    def fake_invoke(call):
        calls.append(call)
        invocation_id = f"inv_{len(calls)}"
        return (
            SimpleNamespace(id=invocation_id, error=None),
            SimpleNamespace(output={"content": "...", "intent": next(outputs)}),
        )

    monkeypatch.setattr(adapter.provider_gateway, "invoke", fake_invoke)

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.succeeded
    assert output.provider_invocation_ids == ["inv_1", "inv_2"]
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["repair_trace"][0]["error_count"] == 1
    assert "has 60 frames" in diagnostics["repair_trace"][0]["errors"][0]
    assert "choose one of legal_window_ids: pc_001" in diagnostics["repair_trace"][0]["errors"][0]
    portrait = _payload(output, ArtifactKind.plan_portrait)
    assert [seg["asset_id"] for seg in portrait["segments"]] == ["portrait_a", "portrait_b"]
    assert portrait["segments"][0]["source_start_frame"] == 0
    assert portrait["segments"][0]["source_end_frame"] == 60
    assert portrait["segments"][1]["source_start_frame"] == 30
    assert portrait["segments"][1]["source_end_frame"] == 210


def test_llm_invalid_selection_records_raw_artifacts_before_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK", "0")
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    adapter.provider_gateway.register(
        _FakeEditingLlmProvider(
            [
                {"intent": {"portrait_plan": [{"slot_id": "pslot_000", "window_id": "pc_999"}]}},
                {"intent": {"portrait_plan": [{"slot_id": "pslot_000", "window_id": "pc_999"}]}},
            ]
        )
    )

    state = _state()

    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, state)

    assert exc.value.error.code == ErrorCode.prompt_output_invalid
    assert "剪辑 Agent 的选择在" in exc.value.error.message
    invocations = list(adapter.repository.provider_invocations.values())
    assert len(invocations) == 2
    assert all(inv.request_artifact_id for inv in invocations)
    assert all(inv.response_artifact_id for inv in invocations)
    raw_requests = [
        artifact
        for artifact in adapter.repository.artifacts.values()
        if artifact.kind == ArtifactKind.provider_raw_request
    ]
    raw_responses = [
        artifact
        for artifact in adapter.repository.artifacts.values()
        if artifact.kind == ArtifactKind.provider_raw_response
    ]
    assert len(raw_requests) == 2
    assert len(raw_responses) == 2
    assert "legal_window_ids" in raw_requests[0].payload["prompt"]
    portrait_plan = raw_responses[-1].payload["output"]["intent"]["portrait_plan"]
    assert portrait_plan[0]["window_id"] == "pc_999"


# --------------------------------------------------------------------------- #
# HuaziPlanningSubagent — the second LLM pass that plans emphasis captions (#188)
# --------------------------------------------------------------------------- #
_MAIN_SELECTION = {
    "portrait_plan": [
        {"slot_id": "pslot_000", "window_id": "pc_000"},
        {"slot_id": "pslot_001", "window_id": "pc_001"},
    ],
    "broll_plan": [{"slot_id": "bslot_000", "candidate_id": "bc_000"}],
    "bgm_plan": {"bgm_id": "bgm_001"},
}


def _state_with_emphasis() -> RunState:
    # unit_1 contains "这套案例", unit_2 contains "施工前"; both are 2-10 visual chars.
    state = _state()
    state.artifacts[ArtifactKind.creative_intent] = Artifact(
        id="art_ci",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_ci",
        kind=ArtifactKind.creative_intent,
        payload={
            "intent": {"hook": "h", "beats": ["a"]},
            "emphasis": [{"phrase": "这套案例"}, {"phrase": "施工前"}],
        },
        payload_schema="CreativeIntentArtifact.v1",
    )
    return state


def _first_huazi_box_id(request, text: str) -> str:
    from packages.production.pipeline._huazi_candidates import normal_caption_top_y
    from packages.production.pipeline._huazi_layout import generate_layout_boxes
    from packages.production.pipeline._materialize import _subtitle_font_size, _subtitle_position

    font_size = _subtitle_font_size(request.subtitle.style_preset, request.subtitle.font_size)
    position = _subtitle_position(request.subtitle.style_preset, request.subtitle.position)
    top_y = normal_caption_top_y(
        position_y=position["y"], font_size=font_size, canvas_height=request.output.height
    )
    boxes = generate_layout_boxes(
        event_text=text,
        resolution=(request.output.width, request.output.height),
        normal_caption_top_y=top_y,
        neighbor_boxes=[],
    )
    return boxes[0]["layout_box_id"]


def test_huazi_subagent_makes_second_call_and_materializes_rect(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    state = _state_with_emphasis()
    _disable_llm_reprompt(state)
    box_id = _first_huazi_box_id(state.request, "这套案例")
    provider = _FakeEditingLlmProvider(
        [
            {"intent": _MAIN_SELECTION},
            {
                "intent": {
                    "huazi": [
                        {
                            "event_id": "hz_001",
                            "layout_box_id": box_id,
                            "animation_id": "pop_in",
                            "priority": 3,
                            "reason": "醒目",
                        }
                    ]
                }
            },
        ]
    )
    adapter.provider_gateway.register(provider)

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.succeeded
    # Two separate llm.chat passes happened: main editing agent + huazi subagent.
    assert len(provider.calls) == 2
    assert len(output.provider_invocation_ids) == 2
    assert provider.calls[1].idempotency_key.endswith(":huazi_agent:0")
    style = _payload(output, ArtifactKind.plan_style)
    overlays = style["overlay_events"]
    assert len(overlays) == 1
    assert overlays[0]["event_id"] == "hz_001"
    assert overlays[0]["layout_box_id"] == box_id
    assert overlays[0]["animation_id"] == "pop_in"
    assert overlays[0]["sfx_id"] == "none"
    assert overlays[0]["rect"] is not None and overlays[0]["text_align"] in {
        "left",
        "center",
        "right",
    }
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["huazi_choices"][0]["event_id"] == "hz_001"
    assert diagnostics["huazi_diagnostics"]["planned"] is True


def test_huazi_subagent_unwraps_intent_from_real_provider_shape(monkeypatch, tmp_path):
    # Regression for the intent-unwrap blocker: DashScope-style providers nest the
    # parsed JSON under output["intent"] with sibling content. The huazi call must
    # unwrap it exactly like the main agent, otherwise the selection is empty.
    adapter = _adapter(tmp_path)
    fake_profile = SimpleNamespace(id="dashscope.llm.prod")
    monkeypatch.setattr(
        adapter.provider_profiles,
        "first_available",
        lambda capability, *, include_sandbox=True: fake_profile,
    )
    state = _state_with_emphasis()
    _disable_llm_reprompt(state)
    huazi_box_id = _first_huazi_box_id(state.request, "施工前")
    outputs = iter(
        [
            {"content": "...", "intent": _MAIN_SELECTION},
            {
                "content": "...",
                "intent": {
                    "huazi": [
                        {
                            "event_id": "hz_002",
                            "layout_box_id": huazi_box_id,
                            "animation_id": "pop_in",
                            "priority": 2,
                            "reason": "强调",
                        }
                    ]
                },
            },
        ]
    )
    calls = []

    def fake_invoke(call):
        calls.append(call)
        return (
            SimpleNamespace(id=f"inv_{len(calls)}", error=None),
            SimpleNamespace(output=next(outputs)),
        )

    monkeypatch.setattr(adapter.provider_gateway, "invoke", fake_invoke)

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.succeeded
    assert output.provider_invocation_ids == ["inv_1", "inv_2"]
    style = _payload(output, ArtifactKind.plan_style)
    assert [ov["event_id"] for ov in style["overlay_events"]] == ["hz_002"]


def test_huazi_subagent_degrades_after_failed_repair(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    state = _state_with_emphasis()
    _disable_llm_reprompt(state)
    invalid = {"intent": {"huazi": [{"event_id": "hz_001", "layout_box_id": "no_such_box"}]}}
    provider = _FakeEditingLlmProvider([{"intent": _MAIN_SELECTION}, invalid, invalid])
    adapter.provider_gateway.register(provider)

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.degraded
    assert WarningCode.huazi_planning_failed in output.warnings
    assert any(
        notice.code == WarningCode.huazi_planning_failed and not notice.affects_true_yield
        for notice in output.degradations
    )
    # main + huazi + one huazi repair, then degrade to no huazi (never fails node).
    assert len(provider.calls) == 3
    assert len(output.provider_invocation_ids) == 3
    style = _payload(output, ArtifactKind.plan_style)
    assert style["overlay_events"] == []
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["huazi_diagnostics"]["degraded"] is True
    assert diagnostics["huazi_diagnostics"]["reason"] == "unrepairable"


def test_huazi_subagent_repairs_then_degrades_on_malformed_payload(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    state = _state_with_emphasis()
    _disable_llm_reprompt(state)
    malformed = {"intent": {}}
    provider = _FakeEditingLlmProvider([{"intent": _MAIN_SELECTION}, malformed, malformed])
    adapter.provider_gateway.register(provider)

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.degraded
    assert WarningCode.huazi_planning_failed in output.warnings
    assert len(provider.calls) == 3
    assert _payload(output, ArtifactKind.plan_style)["overlay_events"] == []
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    huazi_diagnostics = diagnostics["huazi_diagnostics"]
    assert huazi_diagnostics["reason"] == "unrepairable"
    assert len(huazi_diagnostics["repair_trace"]) == 2
    assert all(
        any("must include a 'huazi' array" in error for error in trace["errors"])
        for trace in huazi_diagnostics["repair_trace"]
    )


def test_huazi_subagent_skipped_when_emphasis_disabled(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    state = _state_with_emphasis()
    state.request = state.request.model_copy(
        update={"subtitle": state.request.subtitle.model_copy(update={"emphasis_enabled": False})}
    )
    _disable_llm_reprompt(state)
    provider = _FakeEditingLlmProvider([{"intent": _MAIN_SELECTION}])
    adapter.provider_gateway.register(provider)

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.succeeded
    assert len(provider.calls) == 1  # no huazi pass
    assert WarningCode.huazi_planning_failed not in output.warnings
    style = _payload(output, ArtifactKind.plan_style)
    assert style["overlay_events"] == []


def test_main_agent_huazi_plan_is_repaired_as_overreach(tmp_path):
    adapter = _adapter(tmp_path)
    _seed_fake_llm_profile(adapter)
    state = _state_with_emphasis()
    box_id = _first_huazi_box_id(state.request, "这套案例")
    provider = _FakeEditingLlmProvider(
        [
            {"intent": {**_MAIN_SELECTION, "huazi_plan": [{"event_id": "hz_001"}]}},
            {"intent": _MAIN_SELECTION},
            {
                "intent": {
                    "huazi": [
                        {
                            "event_id": "hz_001",
                            "layout_box_id": box_id,
                            "animation_id": "pop_in",
                            "priority": 1,
                        }
                    ]
                }
            },
        ]
    )
    adapter.provider_gateway.register(provider)

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.succeeded
    # main (overreach) -> main repair -> huazi call.
    assert len(provider.calls) == 3
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["repair_trace"][0]["error_count"] > 0
    assert "forbidden visual style fields" in " ".join(diagnostics["repair_trace"][0]["errors"])
