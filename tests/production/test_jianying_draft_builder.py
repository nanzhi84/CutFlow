from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from packages.core.storage.object_store import LocalObjectStore, parse_object_uri
from packages.production.jianying_draft import (
    JianyingAudioSegment,
    JianyingDraftBuilder,
    JianyingDraftInput,
    JianyingTextSegment,
    JianyingVideoSegment,
    _frame_from_payload,
    _broll_segments,
    _explicit_audio_tracks,
    _explicit_video_tracks,
    _main_video_segments,
    _portrait_segments_by_timeline_id,
    _safe_resource_name,
    _segment_effects,
    _stage_media_file,
    _track_effects,
    _unique_name,
    build_audio_segments_from_sources,
    build_text_segments_from_narration,
    build_video_segments_from_plans,
)


def test_jianying_builder_writes_real_draft_zip_with_tracks_and_microseconds(
    tmp_path, media_fixture_factory
):
    video = media_fixture_factory.video(
        duration_sec=2, width=320, height=568, filename="portrait.mp4"
    )
    audio = media_fixture_factory.audio(duration_sec=2, filename="voice.wav")
    subtitle = tmp_path / "narration.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n第一句\n\n2\n00:00:01,000 --> 00:00:02,000\n第二句\n",
        encoding="utf-8",
    )
    narration_units = [
        {"unit_id": "n1", "start": 0.0, "end": 1.0, "text": "第一句"},
        {"unit_id": "n2", "start": 1.0, "end": 2.0, "text": "第二句"},
    ]
    timeline_plan = {
        "fps": 30,
        "total_frames": 60,
        "tracks": [
            {
                "track_id": "portrait",
                "segment_id": "portrait_1",
                "timeline_start_frame": 0,
                "timeline_end_frame": 30,
                "source_start_frame": 0,
                "source_end_frame": 30,
                "asset_path": str(video),
            },
            {
                "track_id": "portrait",
                "segment_id": "portrait_2",
                "timeline_start_frame": 30,
                "timeline_end_frame": 60,
                "source_start_frame": 30,
                "source_end_frame": 60,
                "asset_path": str(video),
            },
        ],
    }

    object_store = LocalObjectStore(tmp_path / "objects")
    result = JianyingDraftBuilder(object_store).build(
        JianyingDraftInput(
            finished_video_id="fv_test",
            title="测试剪映草稿",
            video_path=video,
            audio_path=audio,
            subtitle_path=subtitle,
            duration_sec=2.0,
            template_id="clean-template",
            timeline_plan=timeline_plan,
            narration_units=narration_units,
        )
    )

    assert result.package_uri.startswith("local://")
    assert result.draft_name
    assert result.tracks_summary == {
        "main_video": 2,
        "voice_audio": 1,
        "subtitle_segments": 2,
        "broll_segments": 0,
        "overlay_tracks": 0,
        "cover_tracks": 0,
        "emphasis_segments": 0,
    }

    package_path = object_store._path(parse_object_uri(result.package_uri))
    with zipfile.ZipFile(package_path) as archive:
        names = set(archive.namelist())
        draft_prefix = f"{result.draft_name}/"
        assert "root_meta_info.json" in names
        assert f"{draft_prefix}draft_content.json" in names
        assert f"{draft_prefix}draft_meta_info.json" in names
        assert any(
            name.startswith(f"{draft_prefix}Resources/video/") and name.endswith(".mp4")
            for name in names
        )
        assert any(
            name.startswith(f"{draft_prefix}Resources/audio/") and name.endswith(".wav")
            for name in names
        )

        content = json.loads(archive.read(f"{draft_prefix}draft_content.json").decode("utf-8"))
        meta = json.loads(archive.read(f"{draft_prefix}draft_meta_info.json").decode("utf-8"))
        root_meta = json.loads(archive.read("root_meta_info.json").decode("utf-8"))
        _assert_portable_resource_paths(archive, result.draft_name, content)

    assert content["duration"] == 2_000_000
    assert content["canvas_config"]["width"] == 320
    assert content["canvas_config"]["height"] == 568
    assert meta["tm_duration"] == 2_000_000
    assert meta["draft_timeline_materials_size_"] > 0
    assert "cutagent-jianying-" not in json.dumps(meta)
    assert "cutagent-jianying-" not in json.dumps(root_meta)
    assert meta["draft_fold_path"] == result.draft_name
    assert meta["draft_root_path"] == "."
    assert root_meta["root_path"] == "."
    assert root_meta["all_draft_store"][0]["draft_json_file"] == (
        f"{result.draft_name}/draft_content.json"
    )
    assert result.manifest["portable_resources"] is True

    tracks = {track["name"]: track for track in content["tracks"]}
    assert tracks["video"]["type"] == "video"
    assert tracks["audio"]["type"] == "audio"
    assert tracks["subtitle"]["type"] == "text"
    video_ranges = [segment["target_timerange"] for segment in tracks["video"]["segments"]]
    assert video_ranges == [
        {"start": 0, "duration": 1_000_000},
        {"start": 1_000_000, "duration": 1_000_000},
    ]
    assert all(isinstance(value, int) for item in video_ranges for value in item.values())
    assert tracks["audio"]["segments"][0]["target_timerange"] == {"start": 0, "duration": 2_000_000}
    assert len(tracks["subtitle"]["segments"]) == len(narration_units)
    assert [segment["target_timerange"]["start"] for segment in tracks["subtitle"]["segments"]] == [
        0,
        1_000_000,
    ]

    subtitle_texts = [
        json.loads(material["content"])["text"] for material in content["materials"]["texts"]
    ]
    assert subtitle_texts == ["第一句", "第二句"]


def test_jianying_builder_exports_editable_multitrack_broll_project(
    tmp_path, media_fixture_factory
):
    portrait = media_fixture_factory.video(
        duration_sec=2, width=320, height=568, filename="portrait-source.mp4"
    )
    broll = media_fixture_factory.video(
        duration_sec=2, width=320, height=568, filename="broll-source.mp4"
    )
    voice = media_fixture_factory.audio(duration_sec=2, filename="voice-track.wav")
    bgm = media_fixture_factory.audio(duration_sec=2, frequency=880, filename="bgm-track.wav")
    unsafe_voice = tmp_path / 'run:unsafe"voice?.wav'
    voice.rename(unsafe_voice)

    object_store = LocalObjectStore(tmp_path / "objects")
    result = JianyingDraftBuilder(object_store).build(
        JianyingDraftInput(
            finished_video_id="fv_broll",
            title="B-roll覆盖工程",
            video_path=portrait,
            duration_sec=2.0,
            video_segments=[
                JianyingVideoSegment(
                    track_name="主视频",
                    source_path=portrait,
                    timeline_start_frame=0,
                    timeline_end_frame=60,
                    source_start_frame=0,
                    source_end_frame=60,
                    asset_id="asset_portrait",
                    clip_id="clip_portrait",
                ),
                JianyingVideoSegment(
                    track_name="B-roll覆盖",
                    source_path=broll,
                    timeline_start_frame=15,
                    timeline_end_frame=45,
                    source_start_frame=30,
                    source_end_frame=60,
                    asset_id="asset_broll",
                    clip_id="clip_broll",
                    placement="pip_fixed",
                ),
            ],
            audio_segments=[
                JianyingAudioSegment(
                    track_name="旁白", source_path=unsafe_voice, start_us=0, duration_us=2_000_000
                ),
                JianyingAudioSegment(
                    track_name="BGM",
                    source_path=bgm,
                    start_us=0,
                    duration_us=2_000_000,
                    volume=0.25,
                ),
            ],
            text_segments=[
                JianyingTextSegment(
                    track_name="字幕", text="第一句字幕", start_us=0, duration_us=1_000_000
                ),
                JianyingTextSegment(
                    track_name="字幕-强调",
                    text="重点强调",
                    start_us=500_000,
                    duration_us=800_000,
                    role="emphasis",
                    hint_id="hint_0001",
                    line_index=0,
                    effect_id="pop",
                ),
                JianyingTextSegment(
                    track_name="字幕",
                    text="收尾",
                    start_us=0,
                    duration_us=1_000_000,
                ),
            ],
            timeline_plan={"fps": 30, "total_frames": 60},
        )
    )

    assert result.tracks_summary == {
        "main_video": 1,
        "voice_audio": 1,
        "subtitle_segments": 2,
        "broll_segments": 1,
        "overlay_tracks": 0,
        "cover_tracks": 0,
        "emphasis_segments": 1,
        "bgm_audio": 1,
    }

    package_path = object_store._path(parse_object_uri(result.package_uri))
    with zipfile.ZipFile(package_path) as archive:
        content = json.loads(
            archive.read(f"{result.draft_name}/draft_content.json").decode("utf-8")
        )
        _assert_portable_resource_paths(archive, result.draft_name, content)

    tracks = {track["name"]: track for track in content["tracks"]}
    assert {
        "主视频",
        "B-roll覆盖",
        "旁白",
        "BGM",
        "字幕",
        "字幕-2",
        "字幕-强调",
    }.issubset(tracks)
    assert tracks["主视频"]["type"] == "video"
    assert tracks["B-roll覆盖"]["type"] == "video"
    assert tracks["旁白"]["type"] == "audio"
    assert tracks["BGM"]["type"] == "audio"
    assert tracks["字幕"]["type"] == "text"
    assert tracks["字幕-强调"]["type"] == "text"

    video_materials = {material["id"]: material for material in content["materials"]["videos"]}
    main_segment = tracks["主视频"]["segments"][0]
    broll_segment = tracks["B-roll覆盖"]["segments"][0]
    assert video_materials[main_segment["material_id"]]["material_name"] == "portrait-source.mp4"
    assert video_materials[broll_segment["material_id"]]["material_name"] == "broll-source.mp4"
    assert broll_segment["target_timerange"] == {"start": 500_000, "duration": 1_000_000}
    assert broll_segment["source_timerange"] == {"start": 1_000_000, "duration": 1_000_000}
    assert broll_segment["render_index"] > main_segment["render_index"]
    assert broll_segment["cutflow_effects"] == {"placement": "pip_fixed"}

    audio_materials = {material["id"]: material for material in content["materials"]["audios"]}
    assert (
        audio_materials[tracks["旁白"]["segments"][0]["material_id"]]["name"]
        == "run_unsafe_voice_.wav"
    )
    assert audio_materials[tracks["BGM"]["segments"][0]["material_id"]]["name"] == "bgm-track.wav"
    assert tracks["BGM"]["segments"][0]["volume"] == 0.25

    text_materials = {
        material["id"]: json.loads(material["content"])["text"]
        for material in content["materials"]["texts"]
    }
    assert text_materials[tracks["字幕"]["segments"][0]["material_id"]] == "第一句字幕"
    assert text_materials[tracks["字幕-2"]["segments"][0]["material_id"]] == "收尾"
    assert text_materials[tracks["字幕-强调"]["segments"][0]["material_id"]] == "重点强调"
    for track_name in ("字幕", "字幕-2", "字幕-强调"):
        ranges = sorted(
            (segment["target_timerange"] for segment in tracks[track_name]["segments"]),
            key=lambda item: item["start"],
        )
        assert all(
            current["start"] + current["duration"] <= following["start"]
            for current, following in zip(ranges, ranges[1:], strict=False)
        )
    assert result.manifest["assets"]["video"] == ["portrait-source.mp4", "broll-source.mp4"]
    assert result.manifest["assets"]["audio"] == ["run_unsafe_voice_.wav", "bgm-track.wav"]
    assert result.manifest["portable_resources"] is True
    assert result.manifest["effects"]["video_segments"] == [
        {
            "track_name": "B-roll覆盖",
            "asset_id": "asset_broll",
            "clip_id": "clip_broll",
            "placement": "pip_fixed",
        }
    ]
    assert result.manifest["effects"]["emphasis_segments"] == 1
    assert result.manifest["effects"]["caption_run_effects"] == [
        {
            "track_name": "字幕-强调",
            "text": "重点强调",
            "role": "emphasis",
            "hint_id": "hint_0001",
            "line_index": 0,
            "effect_id": "pop",
        }
    ]
    assert result.manifest["effects"]["manual_acceptance_required"] is True


def test_build_video_segments_from_plans_uses_timeline_frames_and_asset_sources():
    timeline_plan = {
        "fps": 30,
        "tracks": [
            {
                "track_id": "portrait",
                "segment_id": "portrait_1",
                "timeline_start_frame": 0,
                "timeline_end_frame": 60,
                "source_start_frame": 0,
                "source_end_frame": 60,
            },
            {
                "track_id": "broll",
                "segment_id": "broll_1",
                "timeline_start_frame": 15,
                "timeline_end_frame": 45,
                "source_start_frame": 30,
                "source_end_frame": 60,
                "placement": "pip_fixed",
            },
        ],
    }
    portrait_plan = {
        "segments": [
            {"segment_id": "portrait_1", "asset_id": "asset_portrait", "clip_id": "clip_portrait"}
        ]
    }
    broll_plan = {
        "overlays": [{"overlay_id": "broll_1", "asset_id": "asset_broll", "clip_id": "clip_broll"}]
    }
    paths = {
        "asset_portrait": "/sources/portrait.mp4",
        "asset_broll": "/sources/broll.mp4",
    }

    segments = build_video_segments_from_plans(
        timeline_plan,
        portrait_plan,
        broll_plan,
        resolve_source_path=lambda asset_id: paths[asset_id],
    )

    assert segments == [
        JianyingVideoSegment(
            track_name="主视频",
            source_path=Path("/sources/portrait.mp4"),
            timeline_start_frame=0,
            timeline_end_frame=60,
            source_start_frame=0,
            source_end_frame=60,
            asset_id="asset_portrait",
            clip_id="clip_portrait",
        ),
        JianyingVideoSegment(
            track_name="B-roll覆盖",
            source_path=Path("/sources/broll.mp4"),
            timeline_start_frame=15,
            timeline_end_frame=45,
            source_start_frame=30,
            source_end_frame=60,
            asset_id="asset_broll",
            clip_id="clip_broll",
            placement="pip_fixed",
        ),
    ]


def test_build_text_segments_from_caption_composition_exports_run_semantics():
    from packages.core.contracts.artifacts import CaptionCompositionPlanArtifact

    segments = build_text_segments_from_narration(
        [{"unit_id": "n1", "text": "普通字幕", "start": 0.0, "end": 1.0}],
        CaptionCompositionPlanArtifact.model_validate({
            "fps": 30,
            "width": 1080,
            "height": 1920,
            "normal_enabled": True,
            "emphasis_enabled": True,
            "normal_font_asset_id": "font_normal",
            "emphasis_font_asset_id": "font_emphasis",
            "normal_font_size": 64,
            "emphasis_font_size": 72,
            "band": {"anchor_x": 0.5, "baseline_y": 0.84, "line_height_ratio": 1.12},
            "cues": [
                {
                    "cue_id": "cue_1",
                    "text": "普通强调",
                    "start_frame": 0,
                    "end_frame": 30,
                    "spoken_span": {"start_frame": 0, "end_frame": 30},
                    "display_span": {"start_frame": 0, "end_frame": 30},
                    "source_unit_ids": ["n1"],
                    "lines": [
                        {
                            "advance_px": 136,
                            "runs": [
                                {
                                    "run_id": "run_1",
                                    "text": "普通",
                                    "role": "normal",
                                    "char_span": [0, 2],
                                    "enter_frame": 0,
                                    "exit_frame": 30,
                                    "effect_id": "soft_in",
                                    "advance_px": 40,
                                    "baseline_offset_px": 48,
                                },
                                {
                                    "run_id": "run_2",
                                    "text": "强调",
                                    "role": "emphasis",
                                    "hint_id": "hint_0001",
                                    "char_span": [2, 4],
                                    "enter_frame": 9,
                                    "exit_frame": 30,
                                    "effect_id": "pop",
                                    "advance_px": 96,
                                    "baseline_offset_px": 55,
                                },
                            ],
                        }
                    ]
                }
            ],
        }),
    )

    assert [segment.track_name for segment in segments] == ["字幕-普通", "字幕-强调"]
    assert segments[1].text == "强调"
    assert segments[1].start_us == 300_000
    assert segments[1].duration_us == 700_000
    assert segments[0].transform_x == pytest.approx(-0.0888889)
    assert segments[1].transform_x == pytest.approx(0.037037, abs=1e-6)
    assert segments[0].transform_y == pytest.approx(-0.6633, abs=1e-3)
    assert segments[1].transform_y == pytest.approx(-0.6602, abs=1e-3)
    assert [segment.font_size_px for segment in segments] == [64, 72]
    assert segments[1].hint_id == "hint_0001"
    assert segments[1].effect_id == "pop"


def test_build_video_segments_from_plans_reads_legacy_broll_segments():
    # Back-compat: a pre-#104 persisted BrollPlanArtifact only carried the legacy
    # dict ``segments`` (no ``overlays``). The jianying draft builder must still
    # resolve the B-roll source from it.
    timeline_plan = {
        "fps": 30,
        "tracks": [
            {
                "track_id": "broll",
                "segment_id": "broll_1",
                "timeline_start_frame": 15,
                "timeline_end_frame": 45,
                "source_start_frame": 30,
                "source_end_frame": 60,
            },
        ],
    }
    broll_plan = {"segments": [{"asset_id": "asset_broll", "clip_id": "clip_broll"}]}

    segments = build_video_segments_from_plans(
        timeline_plan,
        None,
        broll_plan,
        resolve_source_path=lambda asset_id: f"/sources/{asset_id}.mp4",
    )

    assert segments == [
        JianyingVideoSegment(
            track_name="B-roll覆盖",
            source_path=Path("/sources/asset_broll.mp4"),
            timeline_start_frame=15,
            timeline_end_frame=45,
            source_start_frame=30,
            source_end_frame=60,
            asset_id="asset_broll",
            clip_id="clip_broll",
        ),
    ]


def test_build_video_segments_does_not_revive_stale_legacy_broll_segments():
    timeline_plan = {
        "fps": 30,
        "tracks": [
            {
                "track_id": "broll",
                "segment_id": "broll_1",
                "timeline_start_frame": 0,
                "timeline_end_frame": 30,
                "source_start_frame": 0,
                "source_end_frame": 30,
            }
        ],
    }
    broll_plan = {
        "overlays": [],
        "segments": [{"asset_id": "stale_asset", "clip_id": "stale_clip"}],
    }

    segments = build_video_segments_from_plans(
        timeline_plan,
        None,
        broll_plan,
        resolve_source_path=lambda asset_id: f"/sources/{asset_id}.mp4",
    )

    assert segments == []


def test_jianying_input_normalizers_cover_invalid_and_fallback_shapes(tmp_path):
    video_segments = build_video_segments_from_plans(
        {
            "fps": 30,
            "tracks": [
                None,
                {"track_id": "unknown", "segment_id": "x"},
                {"track_id": "portrait", "segment_id": "missing"},
                {
                    "track_id": "portrait",
                    "segment_id": "no-asset",
                    "timeline_start_frame": 0,
                    "timeline_end_frame": 30,
                    "source_start_frame": 0,
                    "source_end_frame": 30,
                },
                {
                    "track_id": "portrait",
                    "segment_id": "bad-range",
                    "timeline_start_frame": 30,
                    "timeline_end_frame": 30,
                    "source_start_frame": 0,
                    "source_end_frame": 30,
                },
            ],
        },
        {
            "segments": [
                {"segment_id": "no-asset", "asset_id": ""},
                {"segment_id": "bad-range", "asset_id": "asset"},
            ]
        },
        None,
        resolve_source_path=lambda asset_id: tmp_path / f"{asset_id}.mp4",
    )
    assert video_segments == []

    text_segments = build_text_segments_from_narration(
        [
            None,
            {"text": ""},
            {"text": "bad timing", "start": 1, "end": 1},
            {"text": "fallback", "start": 0, "end": 1},
        ],
        None,
    )
    assert [segment.text for segment in text_segments] == ["fallback"]

    with_end = build_audio_segments_from_sources(
        None,
        2.0,
        {
            "bgm": {
                "enabled": True,
                "asset_id": "bgm",
                "source_start": 1,
                "source_end": 3,
            }
        },
        resolve_source_path=lambda asset_id: tmp_path / f"{asset_id}.wav",
    )
    with_duration = build_audio_segments_from_sources(
        None,
        2.0,
        {"bgm": {"enabled": True, "asset_id": "bgm", "duration": 4}},
        resolve_source_path=lambda asset_id: tmp_path / f"{asset_id}.wav",
    )
    assert with_end[0].source_duration_us == 2_000_000
    assert with_duration[0].source_duration_us == 4_000_000

    assert _safe_resource_name("") == "material"
    assert _safe_resource_name("CON") == "CON_"
    assert _safe_resource_name("bad:name.mp4") == "bad_name.mp4"
    used = {"clip.mp4", "clip_1.mp4"}
    assert _unique_name("clip.mp4", used) == "clip_2.mp4"
    assert _portrait_segments_by_timeline_id({"segments": [None, {"asset_id": "a"}]}) == {
        "portrait_2": {"asset_id": "a"}
    }
    assert _frame_from_payload({"frame": 7}, "frame", "seconds", 30) == 7
    assert _frame_from_payload({"seconds": 0.5}, "frame", "seconds", 30) == 15
    assert _frame_from_payload({}, "frame", "seconds", 30, {"start": 0.25}, "start") == 8
    assert _frame_from_payload({}, "frame", "seconds", 30) == 0

    segment = JianyingVideoSegment(
        track_name="主视频",
        source_path=tmp_path / "video.mp4",
        timeline_start_frame=0,
        timeline_end_frame=1,
        source_start_frame=0,
        source_end_frame=1,
        placement="pip_fixed",
    )
    assert _segment_effects(segment) == {"placement": "pip_fixed"}
    assert _track_effects({"placement": "fullscreen"}) == {"placement": "fullscreen"}
    assert _track_effects({}) == {}


def test_jianying_staging_and_explicit_track_guards(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError, match="素材不存在"):
        _stage_media_file(tmp_path / "missing.mp4", tmp_path / "target", set())

    source_path = tmp_path / "source.mp4"
    source_path.write_bytes(b"fixture")
    monkeypatch.setattr(
        "packages.production.jianying_draft.os.link",
        lambda *_args: (_ for _ in ()).throw(OSError()),
    )
    staged = _stage_media_file(source_path, tmp_path / "copied", set())
    assert Path(staged).read_bytes() == b"fixture"

    monkeypatch.setattr(
        "packages.production.jianying_draft.shutil.copy2",
        lambda *_args: (_ for _ in ()).throw(OSError("copy failed")),
    )
    with pytest.raises(OSError, match="素材复制失败"):
        _stage_media_file(source_path, tmp_path / "broken", set())

    draft = JianyingDraftInput(
        finished_video_id="fv_guard",
        title="guard",
        video_path=source_path,
        timeline_plan={
            "fps": 30,
            "tracks": [
                {
                    "track_id": "broll",
                    "timeline_start_frame": 0,
                    "timeline_end_frame": 30,
                    "source_start_frame": 0,
                    "source_end_frame": 30,
                }
            ],
        },
        video_segments=[
            JianyingVideoSegment(
                track_name="主视频",
                source_path=source_path,
                timeline_start_frame=1,
                timeline_end_frame=1,
                source_start_frame=0,
                source_end_frame=1,
            )
        ],
        audio_segments=[
            JianyingAudioSegment(
                track_name="旁白",
                source_path=source_path,
                start_us=0,
                duration_us=0,
            )
        ],
    )
    assert _explicit_video_tracks(draft, tmp_path, tmp_path, set(), {}) == ({}, [])
    assert _explicit_audio_tracks(draft.audio_segments, tmp_path, tmp_path, set(), {}) == (
        {},
        [],
    )
    assert len(_main_video_segments(draft, "mat", 1_000_000)) == 1
    assert len(_broll_segments(draft, "mat")) == 1


def _assert_portable_resource_paths(
    archive: zipfile.ZipFile, draft_name: str, content: dict[str, object]
) -> None:
    names = set(archive.namelist())
    materials = content["materials"]
    assert isinstance(materials, dict)
    for material in materials["videos"]:
        assert isinstance(material, dict)
        _assert_portable_resource_path(names, draft_name, str(material["path"]), "Resources/video/")
    for material in materials["audios"]:
        assert isinstance(material, dict)
        _assert_portable_resource_path(names, draft_name, str(material["path"]), "Resources/audio/")


def _assert_portable_resource_path(
    names: set[str], draft_name: str, path: str, expected_prefix: str
) -> None:
    assert path.startswith(expected_prefix)
    assert not Path(path).is_absolute()
    assert "cutagent-jianying-" not in path
    assert "\\" not in path
    basename = Path(path).name
    assert not set('<>:"\\|?*').intersection(basename)
    assert f"{draft_name}/{path}" in names
