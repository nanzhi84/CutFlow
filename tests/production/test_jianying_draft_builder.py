from __future__ import annotations

import json
import zipfile
from pathlib import Path

from packages.core.storage.object_store import LocalObjectStore, parse_object_uri
from packages.production.jianying_draft import (
    JianyingAudioSegment,
    JianyingDraftBuilder,
    JianyingDraftInput,
    JianyingTextSegment,
    JianyingVideoSegment,
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
        "huazi_segments": 0,
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
                    track_name="花字", text="重点花字", start_us=500_000, duration_us=800_000
                ),
            ],
            timeline_plan={"fps": 30, "total_frames": 60},
        )
    )

    assert result.tracks_summary == {
        "main_video": 1,
        "voice_audio": 1,
        "subtitle_segments": 1,
        "broll_segments": 1,
        "overlay_tracks": 0,
        "cover_tracks": 0,
        "huazi_segments": 1,
        "bgm_audio": 1,
    }

    package_path = object_store._path(parse_object_uri(result.package_uri))
    with zipfile.ZipFile(package_path) as archive:
        content = json.loads(
            archive.read(f"{result.draft_name}/draft_content.json").decode("utf-8")
        )
        _assert_portable_resource_paths(archive, result.draft_name, content)

    tracks = {track["name"]: track for track in content["tracks"]}
    assert {"主视频", "B-roll覆盖", "旁白", "BGM", "字幕", "花字"}.issubset(tracks)
    assert tracks["主视频"]["type"] == "video"
    assert tracks["B-roll覆盖"]["type"] == "video"
    assert tracks["旁白"]["type"] == "audio"
    assert tracks["BGM"]["type"] == "audio"
    assert tracks["字幕"]["type"] == "text"
    assert tracks["花字"]["type"] == "text"

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
    assert text_materials[tracks["花字"]["segments"][0]["material_id"]] == "重点花字"
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
    assert result.manifest["effects"]["huazi_segments"] == 1
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
        "overlays": [
            {"overlay_id": "broll_1", "asset_id": "asset_broll", "clip_id": "clip_broll"}
        ]
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


def test_build_text_segments_from_narration_adds_huazi_overlay_events():
    segments = build_text_segments_from_narration(
        [{"unit_id": "n1", "text": "普通字幕", "start": 0.0, "end": 1.0}],
        {
            "overlay_events": [
                {"text": "重点花字", "start": 0.25, "end": 0.75, "style": "pop"}
            ]
        },
    )

    assert [segment.track_name for segment in segments] == ["字幕", "花字"]
    assert segments[1].text == "重点花字"
    assert segments[1].start_us == 250_000
    assert segments[1].duration_us == 500_000
    assert segments[1].transform_y == -0.48


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


def _assert_portable_resource_paths(
    archive: zipfile.ZipFile, draft_name: str, content: dict[str, object]
) -> None:
    names = set(archive.namelist())
    materials = content["materials"]
    assert isinstance(materials, dict)
    for material in materials["videos"]:
        assert isinstance(material, dict)
        _assert_portable_resource_path(
            names, draft_name, str(material["path"]), "Resources/video/"
        )
    for material in materials["audios"]:
        assert isinstance(material, dict)
        _assert_portable_resource_path(
            names, draft_name, str(material["path"]), "Resources/audio/"
        )


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
