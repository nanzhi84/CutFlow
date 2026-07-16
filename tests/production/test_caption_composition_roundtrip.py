from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    MediaAssetRecord,
    MediaInfo,
    NodeRun,
    NodeStatus,
    RunStatus,
    WarningCode,
    WorkflowRun,
)
from packages.core.contracts.artifacts import (
    AlignmentArtifact,
    CaptionBand,
    CaptionCompositionPlanArtifact,
    CaptionCue,
    CaptionFrameSpan,
    CaptionLine,
    CaptionRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.media.assets import store_file
from packages.media.video.ffmpeg import FfmpegCommandError
from packages.production.pipeline import digital_human as digital_human_module
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._fonts import DEFAULT_EMPHASIS_FONT_ASSET_ID, ResolvedFont
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline._speech_timing import proportional_tokens
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
from packages.production.pipeline.nodes import (
    bgm_agent_planning,
    caption_composition_planning,
    subtitle_and_bgm_mix,
)
from packages.production.pipeline.nodes.subtitle_and_bgm_mix import (
    _resolve_planned_font,
    _select_emphasis_sfx_asset_id,
)
from packages.production.pipeline._sfx_events import plan_emphasis_sfx_events


def _build_font(path: Path, family: str, *, characters: str = "A") -> None:
    codepoints = sorted({ord(char) for char in characters})
    glyph_names = {codepoint: f"uni{codepoint:04X}" for codepoint in codepoints}
    glyph_order = [".notdef", *glyph_names.values()]
    empty = TTGlyphPen(None).glyph()
    font = FontBuilder(1000, isTTF=True)
    font.setupGlyphOrder(glyph_order)
    font.setupCharacterMap(glyph_names)
    font.setupGlyf({name: empty for name in glyph_order})
    font.setupHorizontalMetrics({name: (500, 0) for name in glyph_order})
    font.setupHorizontalHeader(ascent=800, descent=-200)
    font.setupNameTable({"familyName": family, "styleName": "Regular"})
    font.setupOS2(sTypoAscender=800, sTypoDescender=-200)
    font.setupPost()
    font.save(str(path))


def _stored_artifact(
    repository: Repository,
    object_store: LocalObjectStore,
    *,
    path: Path,
    kind: ArtifactKind,
    media_type: str,
):
    stored = store_file(object_store, path, purpose="roundtrip-fixtures")
    return repository.create_artifact(
        kind=kind,
        payload_schema="uri-only",
        payload=None,
        case_id="case_caption_roundtrip",
        uri=stored.ref.uri,
        sha256=stored.sha256,
        media_info=MediaInfo(
            media_type=media_type,
            codec="fixture",
            format=path.suffix.lstrip(".") or "bin",
            duration_sec=2.0,
        ),
    )


def _register_font(
    repository: Repository,
    object_store: LocalObjectStore,
    *,
    tmp_path: Path,
    asset_id: str,
    family: str,
    characters: str = "A",
) -> None:
    path = tmp_path / f"{asset_id}.ttf"
    _build_font(path, family, characters=characters)
    source = _stored_artifact(
        repository,
        object_store,
        path=path,
        kind=ArtifactKind.uploaded_file,
        media_type="json",
    )
    repository.media_assets[asset_id] = MediaAssetRecord(
        id=asset_id,
        case_id="case_caption_roundtrip",
        title=family,
        kind="font",
        source_artifact_id=source.id,
        usable=True,
    )


def test_emphasis_sfx_selection_requires_an_explicit_light_caption_tag() -> None:
    assets = [
        MediaAssetRecord(id="sfx_a", title="Heavy impact", kind="sfx", tags=["impact"]),
        MediaAssetRecord(id="sfx_b", title="Whoosh", kind="sfx", tags=["whoosh"]),
    ]

    assert _select_emphasis_sfx_asset_id(assets) is None

    assets.append(
        MediaAssetRecord(
            id="sfx_c",
            title="Light pop",
            kind="sfx",
            tags=["caption_emphasis", "light_pop"],
        )
    )
    assert _select_emphasis_sfx_asset_id(assets) == "sfx_c"


def test_emphasis_sfx_events_are_frame_synchronous_cooled_down_and_bounded() -> None:
    runs = [
        CaptionRun(
            run_id=f"run_{index:02d}",
            text="字",
            role="emphasis" if index != 1 else "normal",
            hint_id=f"hint_{index:02d}" if index != 1 else None,
            char_span=(index, index + 1),
            enter_frame=frame,
            exit_frame=60,
            effect_id=("none" if index == 2 else "soft_in" if index == 1 else "pop"),
            advance_px=10,
            baseline_offset_px=0,
        )
        for index, frame in enumerate([0, 1, 2, 6, 12, 18, 24, 30, 36, 42])
    ]
    runs.append(
        CaptionRun(
            run_id="run_cooldown",
            text="字",
            role="emphasis",
            hint_id="hint_cooldown",
            char_span=(10, 11),
            enter_frame=1,
            exit_frame=60,
            effect_id="pop",
            advance_px=10,
            baseline_offset_px=0,
        )
    )
    composition = CaptionCompositionPlanArtifact(
        fps=30,
        width=1080,
        height=1920,
        normal_enabled=True,
        emphasis_enabled=True,
        band=CaptionBand(),
        normal_font_asset_id="font_normal",
        emphasis_font_asset_id="font_emphasis",
        normal_font_size=64,
        emphasis_font_size=72,
        cues=[
            CaptionCue(
                cue_id="cue_1",
                text="字" * len(runs),
                start_frame=0,
                end_frame=60,
                spoken_span=CaptionFrameSpan(start_frame=0, end_frame=60),
                display_span=CaptionFrameSpan(start_frame=0, end_frame=60),
                source_unit_ids=["unit_1"],
                lines=[CaptionLine(runs=runs, advance_px=10 * len(runs))],
            )
        ],
    )

    assert (
        plan_emphasis_sfx_events(
            caption_composition=composition,
            duration=10,
            sfx_asset_id=None,
        )
        == []
    )
    events = plan_emphasis_sfx_events(
        caption_composition=composition,
        duration=0,
        sfx_asset_id="sfx_light",
    )

    assert len(events) == 4
    assert [event.start_ms for event in events] == [0, 200, 400, 600]
    assert all(event.asset_id == "sfx_light" for event in events)
    assert all(event.volume == 0.48 for event in events)


def test_planned_font_fails_closed(tmp_path, monkeypatch) -> None:
    ctx = SimpleNamespace(
        source_artifact_for_asset=lambda _asset_id: object(),
        artifact_path=lambda _artifact: tmp_path / "font.ttf",
    )
    monkeypatch.setattr(subtitle_and_bgm_mix, "resolve_font_asset", lambda **_kwargs: (None, "bad"))
    with pytest.raises(NodeExecutionError, match="无法加载"):
        _resolve_planned_font(
            ctx,
            font_asset_id="bad",
            runtime_dir=tmp_path / "runtime",
            label="普通字幕",
        )

    collection = ResolvedFont("Collection", tmp_path, tmp_path / "font.ttc")
    monkeypatch.setattr(
        subtitle_and_bgm_mix,
        "resolve_font_asset",
        lambda **_kwargs: (collection, None),
    )
    with pytest.raises(NodeExecutionError, match="缺少唯一可读字形度量"):
        _resolve_planned_font(
            ctx,
            font_asset_id="collection",
            runtime_dir=tmp_path / "runtime",
            label="强调字幕",
        )

    assert (
        caption_composition_planning._timing_source(
            AlignmentArtifact(audio_artifact_id="audio", segments=[])
        )
        == "interpolated"
    )
    assert caption_composition_planning._baseline_offset(None, 10) == 8
    assert (
        caption_composition_planning._baseline_offset(
            SimpleNamespace(cell_height=1000, ascender=750),
            20,
        )
        == 15
    )
    monkeypatch.setattr(
        caption_composition_planning,
        "resolve_font_asset",
        lambda **_kwargs: (collection, None),
    )
    with pytest.raises(NodeExecutionError, match="TTC 集合"):
        caption_composition_planning._resolve_required_font(
            ctx,
            font_asset_id="collection",
            runtime_dir=tmp_path / "runtime",
            label="强调字幕",
            defaulted=False,
        )


def test_caption_composition_rejects_an_invalid_input_artifact() -> None:
    state = SimpleNamespace(
        require=lambda _kind: SimpleNamespace(payload={}),
    )

    with pytest.raises(NodeExecutionError) as exc:
        caption_composition_planning.run(SimpleNamespace(state=state))

    assert exc.value.error.code == ErrorCode.artifact_schema_mismatch
    assert exc.value.error.retryable is False


def test_caption_node_ignores_creative_hints_when_emphasis_is_disabled(
    tmp_path,
    monkeypatch,
) -> None:
    script = "普通字幕"
    request = DigitalHumanVideoRequest(
        case_id="case_caption_disabled",
        script=script,
        voice={"voice_id": "voice_sandbox"},
        subtitle={
            "enabled": True,
            "normal_enabled": True,
            "emphasis_enabled": False,
            "font_id": "font_normal",
        },
        bgm={"enabled": False},
        output={"width": 1080, "height": 1920, "fps": 30},
    )
    state = RunState(
        request=request,
        artifacts={
            ArtifactKind.plan_timeline: SimpleNamespace(
                payload={
                    "fps": 30,
                    "total_frames": 60,
                    "tracks": [],
                    "validation": {"valid": True},
                }
            ),
            ArtifactKind.narration_units: SimpleNamespace(
                payload={
                    "source": "tts",
                    "strict": True,
                    "units": [
                        {
                            "unit_id": "unit_0001",
                            "text": script,
                            "start": 0.0,
                            "end": 2.0,
                            "confidence": 1.0,
                        }
                    ],
                }
            ),
            ArtifactKind.audio_alignment: SimpleNamespace(
                payload={
                    "audio_artifact_id": "audio",
                    "segments": [],
                    "source": "tts",
                    "tokens": [
                        token.model_dump(mode="json")
                        for token in proportional_tokens(script, start=0.0, end=2.0)
                    ],
                }
            ),
            ArtifactKind.creative_intent: SimpleNamespace(
                payload={"emphasis": [{"phrase": "字幕", "display_mode": "inline"}]}
            ),
        },
    )
    metrics = SimpleNamespace(cell_height=1000, ascender=800)
    resolved = ResolvedFont("Normal", tmp_path, tmp_path / "normal.ttf")
    monkeypatch.setattr(
        caption_composition_planning,
        "_resolve_required_font",
        lambda *_args, **_kwargs: resolved,
    )
    monkeypatch.setattr(caption_composition_planning, "load_font_metrics", lambda _path: metrics)
    monkeypatch.setattr(
        caption_composition_planning,
        "make_text_measurer",
        lambda *_args: (lambda text: len(text) * 10.0, "hmtx"),
    )
    monkeypatch.setattr(
        caption_composition_planning,
        "font_text_safety_report",
        lambda *_args: caption_composition_planning.FontTextSafetyReport(),
    )
    ctx = SimpleNamespace(
        state=state,
        node_run=SimpleNamespace(node_id="CaptionCompositionPlanning"),
        artifact=lambda kind, payload, schema: Artifact(
            id="art_caption_disabled",
            kind=kind,
            payload=payload,
            payload_schema=schema,
        ),
    )

    output = caption_composition_planning.run(ctx)

    assert output.status == NodeStatus.succeeded
    assert output.artifacts[0].payload["emphasis_enabled"] is False
    assert output.artifacts[0].payload["diagnostics"]["hints_total"] == 0


def test_final_mix_rejects_an_invalid_composition_artifact() -> None:
    artifacts = {
        ArtifactKind.video_rendered: SimpleNamespace(),
        ArtifactKind.audio_tts: SimpleNamespace(),
        ArtifactKind.plan_caption_composition: SimpleNamespace(payload={}),
    }
    state = SimpleNamespace(require=artifacts.__getitem__)

    with pytest.raises(NodeExecutionError) as exc:
        subtitle_and_bgm_mix.run(SimpleNamespace(state=state))

    assert exc.value.error.code == ErrorCode.artifact_schema_mismatch
    assert exc.value.error.retryable is False


@pytest.mark.parametrize(
    "workflow_template_id",
    ["digital_human_v2", "digital_human_editing_agent_v2"],
)
def test_caption_composition_to_mix_roundtrip_is_shared_by_both_workflows(
    tmp_path,
    monkeypatch,
    workflow_template_id: str,
) -> None:
    repository = Repository()
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr(digital_human_module, "get_object_store", lambda: object_store)
    script = "普通字幕包含重点强调"

    _register_font(
        repository,
        object_store,
        tmp_path=tmp_path,
        asset_id=DEFAULT_EMPHASIS_FONT_ASSET_ID,
        family="Caption Normal",
        characters=script,
    )
    _register_font(
        repository,
        object_store,
        tmp_path=tmp_path,
        asset_id="font_emphasis",
        family="Caption Emphasis",
        characters=script.replace("点", ""),
    )
    rendered_path = tmp_path / "rendered.mp4"
    rendered_path.write_bytes(b"rendered fixture")
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"voice fixture")
    rendered = _stored_artifact(
        repository,
        object_store,
        path=rendered_path,
        kind=ArtifactKind.video_rendered,
        media_type="video",
    )
    audio = _stored_artifact(
        repository,
        object_store,
        path=audio_path,
        kind=ArtifactKind.audio_tts,
        media_type="audio",
    )
    timeline = repository.create_artifact(
        kind=ArtifactKind.plan_timeline,
        payload_schema="TimelinePlanArtifact.v1",
        payload={"fps": 30, "total_frames": 60, "tracks": [], "validation": {"valid": True}},
        case_id="case_caption_roundtrip",
    )
    narration = repository.create_artifact(
        kind=ArtifactKind.narration_units,
        payload_schema="NarrationUnitsArtifact.v1",
        payload={
            "source": "tts",
            "strict": True,
            "units": [
                {
                    "unit_id": "unit_0001",
                    "text": script,
                    "start": 0.0,
                    "end": 2.0,
                    "confidence": 1.0,
                }
            ]
        },
        case_id="case_caption_roundtrip",
    )
    alignment = repository.create_artifact(
        kind=ArtifactKind.audio_alignment,
        payload_schema="AlignmentArtifact.v1",
        payload={
            "audio_artifact_id": audio.id,
            "segments": [],
            "source": "tts",
            "tokens": [
                token.model_dump(mode="json")
                for token in proportional_tokens(script, start=0.0, end=2.0)
            ],
        },
        case_id="case_caption_roundtrip",
    )
    intent = repository.create_artifact(
        kind=ArtifactKind.creative_intent,
        payload_schema="CreativeIntentArtifact.v1",
        payload={
            "intent": {"hook": "h", "beats": ["a"]},
            "emphasis": [{"phrase": "重点强调", "priority": 90, "display_mode": "inline"}],
        },
        case_id="case_caption_roundtrip",
    )
    material = repository.create_artifact(
        kind=ArtifactKind.plan_material_pack,
        payload_schema="MaterialPackArtifact.v1",
        payload={"candidates": []},
        case_id="case_caption_roundtrip",
    )
    request = DigitalHumanVideoRequest(
        case_id="case_caption_roundtrip",
        script=script,
        voice={"voice_id": "voice_sandbox"},
        subtitle={
            "enabled": True,
            "normal_enabled": True,
            "emphasis_enabled": True,
            "font_id": DEFAULT_EMPHASIS_FONT_ASSET_ID,
            "emphasis_font_id": "font_emphasis",
            "font_size": 42,
            "emphasis_font_size": 46,
            "emphasis_primary_color": "#FFE84A",
            "position": {"x": 0.5, "y": 0.84},
        },
        bgm={"enabled": False},
        output={"width": 1080, "height": 1920, "fps": 30},
    )
    style = repository.create_artifact(
        kind=ArtifactKind.plan_style,
        payload_schema="StylePlanArtifact.v1",
        payload={
            "subtitle": {
                "normal_enabled": True,
                "emphasis_enabled": True,
                "primary_color": "#FFFFFF",
                "outline_color": "#000000",
                "outline": 4,
                "emphasis_primary_color": "#FFE84A",
                "emphasis_outline_color": "#000000",
                "emphasis_outline": 4,
            },
            "bgm": {"enabled": False},
            "font_asset_id": DEFAULT_EMPHASIS_FONT_ASSET_ID,
        },
        case_id="case_caption_roundtrip",
    )
    state = RunState(
        request=request,
        artifacts={
            ArtifactKind.video_rendered: rendered,
            ArtifactKind.audio_tts: audio,
            ArtifactKind.plan_timeline: timeline,
            ArtifactKind.narration_units: narration,
            ArtifactKind.audio_alignment: alignment,
            ArtifactKind.creative_intent: intent,
            ArtifactKind.plan_material_pack: material,
            ArtifactKind.plan_style: style,
        },
    )
    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    run = WorkflowRun(
        id=f"run_{workflow_template_id}",
        job_id="job_caption_roundtrip",
        case_id="case_caption_roundtrip",
        workflow_template_id=workflow_template_id,
        workflow_version="v2",
        status=RunStatus.running,
    )

    caption_output = caption_composition_planning.run(
        NodeContext(
            adapter=adapter,
            run=run,
            node_run=NodeRun(
                id=f"node_caption_{workflow_template_id}",
                run_id=run.id,
                node_id="CaptionCompositionPlanning",
                node_version="v1",
                status=NodeStatus.running,
                input_manifest_hash="sha256:caption",
            ),
            state=state,
        )
    )
    assert caption_output.status == NodeStatus.degraded
    assert caption_output.warnings == [WarningCode.font_glyph_fallback]
    composition = next(
        artifact
        for artifact in caption_output.artifacts
        if artifact.kind == ArtifactKind.plan_caption_composition
    )
    state.artifacts[ArtifactKind.plan_caption_composition] = composition
    assert [
        item["text"]
        for cue in composition.payload["cues"]
        for line in cue["lines"]
        for item in line["runs"]
        if item["role"] == "emphasis"
    ] == ["重点强调"]
    assert [
        item["font_asset_id"]
        for cue in composition.payload["cues"]
        for line in cue["lines"]
        for item in line["runs"]
        if item["role"] == "emphasis"
    ] == [DEFAULT_EMPHASIS_FONT_ASSET_ID]

    if workflow_template_id == "digital_human_editing_agent_v2":
        bgm_output = bgm_agent_planning.run(
            NodeContext(
                adapter=adapter,
                run=run,
                node_run=NodeRun(
                    id="node_bgm_roundtrip",
                    run_id=run.id,
                    node_id="BgmAgentPlanning",
                    node_version="v1",
                    status=NodeStatus.running,
                    input_manifest_hash="sha256:bgm",
                ),
                state=state,
            )
        )
        for artifact in bgm_output.artifacts:
            state.artifacts[artifact.kind] = artifact
        assert ArtifactKind.plan_bgm_diagnostics in state.artifacts

    if workflow_template_id == "digital_human_v2":
        repository.media_assets["sfx_broken"] = MediaAssetRecord(
            id="sfx_broken",
            title="Broken light pop",
            kind="sfx",
            tags=["caption_emphasis", "light_pop"],
            source_artifact_id="art_missing_sfx",
            usable=True,
        )
    else:
        sfx_path = tmp_path / "light-pop.wav"
        sfx_path.write_bytes(b"sfx fixture")
        sfx_source = _stored_artifact(
            repository,
            object_store,
            path=sfx_path,
            kind=ArtifactKind.uploaded_file,
            media_type="audio",
        )
        repository.media_assets["sfx_light"] = MediaAssetRecord(
            id="sfx_light",
            title="Light pop",
            kind="sfx",
            tags=["caption_emphasis", "light_pop"],
            source_artifact_id=sfx_source.id,
            usable=True,
        )

    captured: dict[str, object] = {}
    render_calls = 0

    def fake_render_final_media(**kwargs):
        nonlocal render_calls
        render_calls += 1
        if kwargs["subtitle_path"] is not None:
            captured["ass"] = kwargs["subtitle_path"].read_text(encoding="utf-8")
        if (
            workflow_template_id == "digital_human_editing_agent_v2"
            and kwargs["sfx_events"]
            and render_calls == 1
        ):
            raise FfmpegCommandError("simulated SFX mix failure")
        kwargs["output_path"].write_bytes(b"final fixture")
        return SimpleNamespace(
            metadata={
                "fallback_reason": (
                    "loudness_probe_failed"
                    if workflow_template_id == "digital_human_editing_agent_v2"
                    else None
                )
            }
        )

    monkeypatch.setattr(subtitle_and_bgm_mix, "render_final_media", fake_render_final_media)
    monkeypatch.setattr(
        subtitle_and_bgm_mix,
        "ffmpeg_filter_available",
        lambda _name: workflow_template_id == "digital_human_v2",
    )
    monkeypatch.setattr(
        subtitle_and_bgm_mix,
        "validate_rendered_output",
        lambda *_args, **_kwargs: MediaInfo(
            media_type="video",
            codec="h264",
            format="mp4",
            duration_sec=2.0,
            width=1080,
            height=1920,
            fps=30,
        ),
    )
    monkeypatch.setattr(subtitle_and_bgm_mix, "probe_audio_channels", lambda _path: 2)
    monkeypatch.setattr(
        subtitle_and_bgm_mix,
        "probe_media",
        lambda _path: MediaInfo(media_type="subtitle", codec="ass", format="ass"),
    )

    mixed = subtitle_and_bgm_mix.run(
        NodeContext(
            adapter=adapter,
            run=run,
            node_run=NodeRun(
                id=f"node_mix_{workflow_template_id}",
                run_id=run.id,
                node_id="SubtitleAndBgmMix",
                node_version="v1",
                status=NodeStatus.running,
                input_manifest_hash="sha256:mix",
            ),
            state=state,
        )
    )

    assert {artifact.kind for artifact in mixed.artifacts} == {
        ArtifactKind.video_final,
        ArtifactKind.subtitle_ass,
    }
    assert mixed.status == NodeStatus.degraded
    if workflow_template_id == "digital_human_v2":
        assert "Style: Normal,Caption Normal" in str(captured["ass"])
        assert "Style: Emphasis,Caption Emphasis" in str(captured["ass"])
        assert "\\fnCaption Normal\\b0" in str(captured["ass"])
        assert "重点强调" in str(captured["ass"])
    else:
        assert render_calls == 2
