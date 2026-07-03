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
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import LocalRuntimeAdapter

SCRIPT = "今天带你看一下这套案例。第一步先看施工前的样子。"


class _FakeEditingLlmProvider:
    provider_id = "fake.llm"

    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = list(outputs)

    def invoke(self, _call):
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
                "metadata": {"clip_id": "clip_a", "source_start": 0.0, "source_end": 15.0},
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
                "host_unit_ids": list(slot.get("unit_ids") or []),
                "host_portrait_window_ids": [],
                "text": slot.get("text") or "",
                "boundary_source": slot.get("boundary_source") or "narration_unit",
            }
            for slot in boundary.get("broll_slots") or []
        ],
        "default_assignment": {},
        "compile_diagnostics": {},
    }


def _state() -> RunState:
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script=SCRIPT,
        voice={"voice_id": "voice_sandbox"},
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


def _run_node(adapter: LocalRuntimeAdapter, state: RunState):
    ctx = NodeContext(adapter=adapter, run=_run(), node_run=_node_run(), state=state)
    return nodes.editing_agent_planning.run(ctx)


def _payload(output, kind: ArtifactKind) -> dict:
    return next(a.payload for a in output.artifacts if a.kind == kind)


def test_fallback_path_emits_four_frame_exact_artifacts(tmp_path):
    output = _run_node(_adapter(tmp_path), _state())

    kinds = {a.kind for a in output.artifacts}
    assert kinds == {
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
    assert diagnostics["mode"] == "deterministic_fallback_no_provider"
    assert diagnostics["instruction"] == "尽量用穿搭相近的人像"
    assert {c["slot_id"] for c in diagnostics["portrait_choices"]} == {"pslot_000", "pslot_001"}


def test_editing_agent_artifacts_feed_timeline_planning(tmp_path):
    adapter = _adapter(tmp_path)
    state = _state()

    editing_output = _run_node(adapter, state)
    for artifact in editing_output.artifacts:
        state.artifacts[artifact.kind] = artifact

    timeline_ctx = NodeContext(
        adapter=adapter,
        run=_run(),
        node_run=_node_run("TimelinePlanning"),
        state=state,
    )
    timeline_output = nodes.timeline_planning.run(timeline_ctx)

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


def test_llm_path_records_broll_geometry_drops(tmp_path):
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
                        "font_plan": {"font_id": "font_yst"},
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
    state.artifacts[ArtifactKind.plan_timeline_windows].payload = _timeline_windows(boundary)

    output = _run_node(adapter, state)

    assert output.status == NodeStatus.degraded
    assert output.provider_invocation_ids
    assert WarningCode.broll_insertions_dropped_geometry in output.warnings
    assert any(
        notice.code == WarningCode.broll_insertions_dropped_geometry
        and notice.affects_true_yield
        for notice in output.degradations
    )
    broll = _payload(output, ArtifactKind.plan_broll)
    assert broll["overlays"] == []
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["broll_drops"] == [
        {"slot_id": "bslot_000", "candidate_id": "bc_000", "reason": "geometry_rejected"}
    ]


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
                        "font_plan": {"font_id": "font_yst"},
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
        "font_plan": {"font_id": "font_yst"},
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
    assert diagnostics["mode"] == "llm"
    assert diagnostics["repair_trace"][0]["error_count"] == 1
    assert "more than one slot" in diagnostics["repair_trace"][0]["errors"][0]
    portrait = _payload(output, ArtifactKind.plan_portrait)
    assert [seg["asset_id"] for seg in portrait["segments"]] == ["portrait_b", "portrait_a"]


def _make_portrait_scarce(state: RunState) -> None:
    # Shrink the portrait pool to a single distinct asset: 1 asset for 2 slots
    # (A < S), which drives the relaxed-uniqueness path in both selection modes.
    material = state.artifacts[ArtifactKind.plan_material_pack].payload
    material["portrait_candidates"] = material["portrait_candidates"][:1]


def test_fallback_relaxes_uniqueness_and_covers_all_slots_when_assets_scarce(tmp_path):
    state = _state()
    _make_portrait_scarce(state)

    output = _run_node(_adapter(tmp_path), state)

    # Portrait main track is fully covered (no hole) by reusing the one asset.
    portrait = _payload(output, ArtifactKind.plan_portrait)
    assert len(portrait["segments"]) == 2
    assert portrait["segments"][0]["timeline_start_frame"] == 0
    assert portrait["segments"][-1]["timeline_end_frame"] == 360
    assert {seg["asset_id"] for seg in portrait["segments"]} == {"portrait_a"}

    # The relaxation is surfaced as a graded degradation, never silent.
    assert output.status == NodeStatus.degraded
    assert WarningCode.portrait_asset_reuse_relaxed in output.warnings
    assert any(
        d.code == WarningCode.portrait_asset_reuse_relaxed for d in output.degradations
    )
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["portrait_asset_reuse_cap"] == 2


def test_llm_path_accepts_reused_asset_when_scarce_without_burning_repairs(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    fake_profile = SimpleNamespace(id="dashscope.llm.prod")
    monkeypatch.setattr(
        adapter.provider_profiles,
        "first_available",
        lambda capability, *, include_sandbox=True: fake_profile,
    )
    state = _state()
    _make_portrait_scarce(state)  # only pc_000 (portrait_a) remains; cap relaxes to 2

    selection = {
        "portrait_plan": [
            {"slot_id": "pslot_000", "window_id": "pc_000"},
            {"slot_id": "pslot_001", "window_id": "pc_000"},
        ],
        "broll_plan": [],
        "font_plan": {"font_id": "font_yst"},
        "bgm_plan": {"bgm_id": "bgm_001"},
        "analysis": "素材有限，复用同一人像",
    }
    calls = []

    def fake_invoke(call):
        calls.append(call)
        return (
            SimpleNamespace(id="inv_1", error=None),
            SimpleNamespace(output={"content": "...", "intent": selection}),
        )

    monkeypatch.setattr(adapter.provider_gateway, "invoke", fake_invoke)

    output = _run_node(adapter, state)

    # Reuse is legal under the relaxed cap -> accepted on the first attempt.
    assert len(calls) == 1
    assert output.provider_invocation_ids == ["inv_1"]
    # Still surfaced as a graded degradation.
    assert output.status == NodeStatus.degraded
    assert WarningCode.portrait_asset_reuse_relaxed in output.warnings
    portrait = _payload(output, ArtifactKind.plan_portrait)
    assert [seg["asset_id"] for seg in portrait["segments"]] == ["portrait_a", "portrait_a"]
    diagnostics = _payload(output, ArtifactKind.plan_editing_diagnostics)
    assert diagnostics["mode"] == "llm"
    assert diagnostics["portrait_asset_reuse_cap"] == 2


def test_llm_invalid_selection_records_raw_artifacts_before_fail_fast(tmp_path):
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

    with pytest.raises(NodeExecutionError) as exc:
        _run_node(adapter, _state())

    assert exc.value.error.code == ErrorCode.prompt_output_invalid
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
