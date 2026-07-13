"""Contract-layer defect guards.

Covers two latent defects flagged in the architecture report:

* ``OutboxEvent.dedupe_key`` was declared twice — the second declaration
  (``str | None = None``) shadowed/loosened the intended required ``str``.
  The outbox DB column is ``NOT NULL`` and every producer always supplies a
  ``dedupe_key``, so the field must be required.
* A general scan asserting no Pydantic model in ``packages/core/contracts``
  declares the same field name twice (which Python/ruff silently accept,
  letting the later annotation win).
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from pydantic import ValidationError

import packages.core.contracts as contracts_pkg
from packages.core.contracts import (
    BgmOptions,
    BrollOptions,
    CoverOptions,
    EditPlanningOptions,
    LipSyncOptions,
    OutboxEvent,
    OutputOptions,
    StrictnessOptions,
    SubtitleOptions,
    VoiceOptions,
)
from packages.core.contracts.artifacts import (
    CaseContextArtifact,
    FontPlan,
    MaterialCandidate,
    NarrationUnit,
    StylePlanArtifact,
    SubtitleStylePlan,
)

_CONTRACTS_DIR = pathlib.Path(contracts_pkg.__file__).parent
_CONTRACT_FILES = sorted(_CONTRACTS_DIR.glob("*.py"))
_REPO_ROOT = _CONTRACTS_DIR.parents[2]

# Advanced LipSync request-layer fields removed in issue #115: they were exposed
# on the contract / OpenAPI / frontend but never wired into the digital_human_v2
# LipSync node's ProviderCall.input (only portrait_uri/audio_uri/duration_sec/
# timeout_minutes flow through), and ``query_face_threshold`` carried a unit
# mismatch (0..1 float on the contract vs the 120..200 int the videoretalk
# adapter expects). They must no longer be accepted on the user request layer.
_REMOVED_LIPSYNC_FIELDS = (
    "ref_image_artifact_id",
    "video_extension",
    "query_face_threshold",
)


def test_lipsync_options_drops_unwired_advanced_fields():
    for name in _REMOVED_LIPSYNC_FIELDS:
        assert name not in LipSyncOptions.model_fields, (
            f"{name} was removed from the LipSync request layer (#115) but is "
            "still declared on LipSyncOptions"
        )


@pytest.mark.parametrize("name", _REMOVED_LIPSYNC_FIELDS)
def test_lipsync_options_rejects_removed_field(name):
    # ContractModel is extra="forbid", so a stored/legacy request still carrying
    # one of these keys must now raise instead of silently round-tripping.
    with pytest.raises(ValidationError):
        LipSyncOptions.model_validate({name: None})


def test_lipsync_options_still_accepts_supported_fields():
    options = LipSyncOptions(enabled=True, timeout_minutes=45)
    assert options.enabled is True
    assert options.timeout_minutes == 45


# OutputOptions request-layer fields removed in issue #118: they were exposed on
# the contract / OpenAPI / frontend but never consumed by any production node
# (only ``width`` / ``height`` / ``fps`` reach the render/portrait/broll nodes;
# export/upload/keep/format toggles were dead request knobs). They must no longer
# be accepted on the user request layer.
_REMOVED_OUTPUT_FIELDS = (
    "export_jianying_draft",
    "export_editor_handoff",
    "upload_to_oss",
    "keep_local_originals",
    "format",
)

# StrictnessOptions request-layer fields removed in issue #118: ``strict_timestamps``
# (NarrationAlignment) and ``portrait_insufficient_policy`` (PortraitPlanning) are
# the only blocks a production node reads; the broll/bgm policies and the cost
# pricing flag never drove any node and are dropped from the request layer.
_REMOVED_STRICTNESS_FIELDS = (
    "broll_insufficient_policy",
    "bgm_unavailable_policy",
    "strict_cost_pricing",
)


def test_output_options_drops_unconsumed_fields():
    for name in _REMOVED_OUTPUT_FIELDS:
        assert name not in OutputOptions.model_fields, (
            f"{name} was removed from the Output request layer (#118) but is "
            "still declared on OutputOptions"
        )


@pytest.mark.parametrize("name", _REMOVED_OUTPUT_FIELDS)
def test_output_options_rejects_removed_field(name):
    # ContractModel is extra="forbid", so a stored/legacy request still carrying
    # one of these keys must now raise instead of silently round-tripping.
    with pytest.raises(ValidationError):
        OutputOptions.model_validate({name: None})


def test_output_options_still_accepts_supported_fields():
    options = OutputOptions(width=1280, height=720, fps=24)
    assert options.width == 1280
    assert options.height == 720
    assert options.fps == 24


def test_strictness_options_drops_unconsumed_fields():
    for name in _REMOVED_STRICTNESS_FIELDS:
        assert name not in StrictnessOptions.model_fields, (
            f"{name} was removed from the Strictness request layer (#118) but is "
            "still declared on StrictnessOptions"
        )


@pytest.mark.parametrize("name", _REMOVED_STRICTNESS_FIELDS)
def test_strictness_options_rejects_removed_field(name):
    with pytest.raises(ValidationError):
        StrictnessOptions.model_validate({name: None})


def test_strictness_options_still_accepts_supported_fields():
    options = StrictnessOptions(strict_timestamps=False, portrait_insufficient_policy="hard_fail")
    assert options.strict_timestamps is False
    assert options.portrait_insufficient_policy == "hard_fail"


# Request-option fields are allowed on the public job contract only when a production
# path consumes them (or the field is deliberately listed here with the consuming file).
# This keeps future knobs from repeating the historical "OpenAPI field exists but no
# node/provider/repository ever reads it" failures guarded above.
_REQUEST_OPTION_FIELD_CONSUMERS = {
    VoiceOptions: {
        "voice_id": (("packages/production/pipeline/nodes/tts.py", "request.voice.voice_id"),),
        "provider_profile_id": (
            ("packages/production/pipeline/_provider_profiles.py", "request.voice.provider_profile_id"),
        ),
        "speed": (("packages/production/pipeline/nodes/tts.py", "request.voice.speed"),),
        "emotion": (("packages/production/pipeline/nodes/tts.py", "request.voice.emotion"),),
        "volume": (("packages/production/pipeline/nodes/tts.py", "request.voice.volume"),),
    },
    BrollOptions: {
        "enabled": (
            (
                "packages/production/pipeline/nodes/deterministic_editing_planning.py",
                "request.broll.enabled",
            ),
        ),
        "mode": (
            (
                "packages/production/pipeline/nodes/timeline_window_planning.py",
                "request.broll.mode",
            ),
        ),
        "case_id": (
            ("packages/production/pipeline/nodes/material_pack_planning.py", "request.broll.case_id"),
        ),
        "max_inserts": (
            (
                "packages/production/pipeline/nodes/deterministic_editing_planning.py",
                "request.broll.max_inserts",
            ),
        ),
        "min_segment_duration": (
            (
                "packages/production/pipeline/nodes/timeline_window_planning.py",
                "request.broll.min_segment_duration",
            ),
        ),
        "allow_generic_coverage": (
            (
                "packages/production/pipeline/nodes/_broll_policy.py",
                "request.broll.allow_generic_coverage",
            ),
        ),
    },
    LipSyncOptions: {
        "enabled": (("packages/production/pipeline/nodes/lipsync.py", "request.lipsync.enabled"),),
        "provider_profile_id": (
            (
                "packages/production/pipeline/nodes/lipsync.py",
                "request.lipsync.provider_profile_id",
            ),
        ),
        "timeout_minutes": (
            ("packages/production/pipeline/nodes/lipsync.py", "request.lipsync.timeout_minutes"),
        ),
    },
    SubtitleOptions: {
        "enabled": (
            ("packages/production/pipeline/nodes/subtitle_and_bgm_mix.py", "request.subtitle.enabled"),
        ),
        "normal_enabled": (
            ("packages/production/pipeline/_materialize.py", "request.subtitle.normal_enabled"),
            (
                "packages/production/pipeline/nodes/subtitle_and_bgm_mix.py",
                "request.subtitle.normal_enabled",
            ),
        ),
        "emphasis_enabled": (
            ("packages/production/pipeline/_materialize.py", "request.subtitle.emphasis_enabled"),
            (
                "packages/production/pipeline/nodes/subtitle_and_bgm_mix.py",
                "request.subtitle.emphasis_enabled",
            ),
        ),
        "style_preset": (
            ("packages/production/pipeline/_materialize.py", "request.subtitle.style_preset"),
        ),
        "font_id": (
            ("packages/production/pipeline/_materialize.py", "request.subtitle.font_id"),
        ),
        "emphasis_font_id": (
            ("packages/production/pipeline/_materialize.py", "request.subtitle.emphasis_font_id"),
        ),
        "font_size": (
            ("packages/production/pipeline/_materialize.py", "request.subtitle.font_size"),
        ),
        "emphasis_font_size": (
            ("packages/production/pipeline/_materialize.py", "emphasis_font_size"),
            ("packages/production/pipeline/_subtitles.py", "emphasis_font_size"),
        ),
        "emphasis_primary_color": (
            ("packages/production/pipeline/_materialize.py", "request.subtitle.emphasis_primary_color"),
        ),
        "position": (
            ("packages/production/pipeline/_materialize.py", "request.subtitle.position"),
        ),
    },
    BgmOptions: {
        "enabled": (
            ("packages/production/pipeline/_materialize.py", "request.bgm.enabled"),
            ("packages/production/pipeline/nodes/material_pack_planning.py", "request.bgm.enabled"),
            (
                "packages/production/pipeline/nodes/postprocess_agent_planning.py",
                "state.request.bgm.enabled",
            ),
        ),
        "bgm_id": (
            ("packages/production/pipeline/_materialize.py", "request.bgm.bgm_id"),
        ),
        "volume": (
            ("packages/production/pipeline/_materialize.py", "request.bgm.volume"),
            ("packages/production/pipeline/nodes/subtitle_and_bgm_mix.py", "request.bgm.volume"),
        ),
        "auto_mix": (
            ("packages/production/pipeline/_materialize.py", "request.bgm.auto_mix"),
            ("packages/production/pipeline/nodes/subtitle_and_bgm_mix.py", "request.bgm.auto_mix"),
        ),
    },
    CoverOptions: {
        "mode": (("packages/production/pipeline/nodes/export_finished_video.py", "request.cover.mode"),),
        "template_id": (
            ("packages/production/pipeline/_provider_profiles.py", "request.cover.template_id"),
        ),
        "reference_asset_id": (
            (
                "packages/production/pipeline/nodes/export_finished_video.py",
                "request.cover.reference_asset_id",
            ),
        ),
    },
    OutputOptions: {
        "width": (
            ("packages/production/pipeline/nodes/subtitle_and_bgm_mix.py", "request.output.width"),
        ),
        "height": (
            ("packages/production/pipeline/nodes/subtitle_and_bgm_mix.py", "request.output.height"),
        ),
        "fps": (("packages/production/pipeline/nodes/render_final_timeline.py", "request.output.fps"),),
    },
    StrictnessOptions: {
        "strict_timestamps": (
            (
                "packages/production/pipeline/nodes/narration_alignment.py",
                "request.strictness.strict_timestamps",
            ),
        ),
        "portrait_insufficient_policy": (
            (
                "packages/production/pipeline/nodes/timeline_window_planning.py",
                "request.strictness.portrait_insufficient_policy",
            ),
        ),
    },
    EditPlanningOptions: {
        "instruction": (
            (
                "packages/production/pipeline/_media_selection_agent.py",
                "request.edit.instruction",
            ),
        ),
        "max_repair_attempts": (
            (
                "packages/production/pipeline/_media_selection_planning.py",
                "state.request.edit.max_repair_attempts",
            ),
        ),
    },
}


def test_request_option_fields_have_consumers_or_explicit_trace():
    problems: list[str] = []
    for model, consumers in _REQUEST_OPTION_FIELD_CONSUMERS.items():
        model_fields = set(model.model_fields)
        traced_fields = set(consumers)
        if model_fields != traced_fields:
            problems.append(
                f"{model.__name__}: missing={sorted(model_fields - traced_fields)} "
                f"extra={sorted(traced_fields - model_fields)}"
            )
            continue
        for field, checks in consumers.items():
            for relative_path, token in checks:
                path = _REPO_ROOT / relative_path
                if not path.exists():
                    problems.append(f"{model.__name__}.{field}: missing file {relative_path}")
                    continue
                if token not in path.read_text(encoding="utf-8"):
                    problems.append(f"{model.__name__}.{field}: token {token!r} missing in {relative_path}")
    assert not problems, "request option consumer trace is stale:\n" + "\n".join(problems)


# Artifact-layer dead fields removed in issue #118: each was written by an upstream
# node (StylePlanning) or merely defaulted, but no consumer ever read it, so the
# plan/candidate/context artifacts carried "looks-effective" config nothing honoured.
#   * SubtitleStylePlan.enabled / StylePlanArtifact.subtitle_enabled — the single
#     source of truth for "render subtitles?" is request.subtitle.enabled
#     (SubtitleAndBgmMix reads that, never the plan).
#   * SubtitleStylePlan.style_preset — write_ass_subtitles never maps a preset.
#   * FontPlan.fallback_family / FontPlan.size — font resolution walks
#     font_asset_id -> font.font_id -> subtitle.font_id; sizing comes from
#     subtitle.font_size, never these.
#   * StylePlanArtifact.selection_reservation_ids / MaterialCandidate.reservation_id
#     — reservations live on MaterialPackArtifact.reservations + the repository
#     reservation APIs; these duplicate slots had no producer/consumer.
#   * CaseContextArtifact.recent_video_versions / negative_lessons — LoadCaseContext
#     never populates them and nothing reads them.
# These models live on packages.core.contracts.artifacts, are dict-consumed (no
# model_validate re-check) and not on the public OpenAPI surface, so removal needs
# no migration and no schema regen.
_REMOVED_ARTIFACT_FIELDS = {
    MaterialCandidate: ("reservation_id",),
    SubtitleStylePlan: ("enabled", "style_preset"),
    FontPlan: ("fallback_family", "size"),
    StylePlanArtifact: ("subtitle_enabled", "selection_reservation_ids"),
    CaseContextArtifact: ("recent_video_versions", "negative_lessons"),
    # issue #100: written by the narration builders (end-start>=0.18) but never
    # consumed -- BrollPlanning converts NarrationUnit into ScriptSegment using
    # only text/start/end/keywords, and real inserts are
    # governed by plan_insertions()'s host window + _MIN_INSERT_SECONDS.
    NarrationUnit: ("broll_overlay_allowed",),
}

# Sibling fields on the same models that ARE wired and must survive the cleanup.
_RETAINED_ARTIFACT_FIELDS = {
    MaterialCandidate: ("asset_id", "score", "metadata"),
    SubtitleStylePlan: (
        "normal_enabled",
        "emphasis_enabled",
        "font_id",
        "emphasis_font_id",
        "font_size",
        "emphasis_font_size",
        "position",
        "font_weight",
        "emphasis_font_weight",
        "emphasis_outline",
        "default_emphasis_position_id",
        "default_emphasis_animation_id",
    ),
    FontPlan: ("font_id",),
    StylePlanArtifact: ("subtitle", "bgm", "font", "font_asset_id", "overlay_events"),
    CaseContextArtifact: (
        "case_id",
        "case_profile",
        "active_memories",
        "recent_script_versions",
    ),
    # Sibling boundary-planning fields on NarrationUnit that ARE read by the
    # editing-agent boundary planner and must survive.
    NarrationUnit: ("portrait_cut_allowed", "boundary_score", "boundary_reason"),
}


@pytest.mark.parametrize(
    ("model", "field"),
    [(model, field) for model, fields in _REMOVED_ARTIFACT_FIELDS.items() for field in fields],
)
def test_artifact_dead_field_removed(model, field):
    assert field not in model.model_fields, (
        f"{model.__name__}.{field} was removed as an un-consumed dead field (#118) "
        "but is still declared on the artifact model"
    )


@pytest.mark.parametrize(
    ("model", "field"),
    [(model, field) for model, fields in _RETAINED_ARTIFACT_FIELDS.items() for field in fields],
)
def test_artifact_wired_field_retained(model, field):
    assert field in model.model_fields, (
        f"{model.__name__}.{field} is still consumed by the production pipeline "
        "and must not be dropped"
    )


def test_outbox_event_dedupe_key_is_required_str():
    field = OutboxEvent.model_fields["dedupe_key"]
    assert field.annotation is str, (
        f"dedupe_key must be a required str, got {field.annotation!r}"
    )
    assert field.is_required(), "dedupe_key must be required (no default)"


def test_outbox_event_rejects_missing_dedupe_key():
    with pytest.raises(ValidationError):
        OutboxEvent(
            id="evt_1",
            topic="workflow.run.updated",
            aggregate_type="run",
            aggregate_id="run_1",
            payload_schema="run.updated.v1",
            payload={},
        )


def test_outbox_event_accepts_dedupe_key():
    event = OutboxEvent(
        id="evt_1",
        topic="workflow.run.updated",
        aggregate_type="run",
        aggregate_id="run_1",
        dedupe_key="run_1:running",
        payload_schema="run.updated.v1",
        payload={},
    )
    assert event.dedupe_key == "run_1:running"


def _duplicate_fields(path: pathlib.Path) -> list[str]:
    """Return human-readable descriptions of duplicate annotated fields."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        seen: dict[str, int] = {}
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name = stmt.target.id
                if name in seen:
                    problems.append(
                        f"{path.name}:{node.name}.{name} "
                        f"redeclared at lines {seen[name]} and {stmt.lineno}"
                    )
                else:
                    seen[name] = stmt.lineno
    return problems


def test_no_contract_model_has_duplicate_field_declarations():
    assert _CONTRACT_FILES, "expected to find contract source files to scan"
    problems: list[str] = []
    for path in _CONTRACT_FILES:
        problems.extend(_duplicate_fields(path))
    assert not problems, "duplicate field declarations found:\n" + "\n".join(problems)
