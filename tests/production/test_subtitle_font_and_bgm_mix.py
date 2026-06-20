"""Subtitle-font landing + BGM adaptive-mix coverage (PR7 cluster).

Three gaps under test:

1. a selected subtitle ``font_id`` is resolved to its uploaded file, staged into a
   libass ``fontsdir`` and its *family name* stamped into the ASS style (not silently
   burned as Arial);
2. ``auto_mix`` is actually consumed: LUFS-targeted volume + sidechain ducking + fades
   (not a dead flag yielding plain fixed-volume mixing);
3. both land in the real ffmpeg burn (a smoke pass when ffmpeg is available).

Pure-logic branches run with zero IO (synthetic font + monkeypatched loudness probe);
the real-ffmpeg test is skipped when ffmpeg is absent.
"""

from __future__ import annotations

import shutil
import struct
import wave

import pytest

from packages.core.contracts import (
    ArtifactKind,
    DigitalHumanVideoRequest,
    MediaAssetRecord,
    MediaInfo,
    NodeRun,
    NodeStatus,
    RunStatus,
    WorkflowRun,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.production.pipeline import _ffmpeg
from packages.production.pipeline._ffmpeg import (
    AUTO_MIX_MAX_BGM_VOLUME,
    resolve_adaptive_bgm_volume,
)
from packages.production.pipeline._fonts import resolve_subtitle_font
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline._subtitles import write_ass_subtitles
from packages.production.pipeline.digital_human import LocalRuntimeAdapter
from packages.media.assets import store_file
from packages.media.video.ffmpeg import FfmpegRunner, ffmpeg_bin, probe_media, probe_stream_types


# --- synthetic font (minimal sfnt with a name table) ----------------------------
def _build_min_font(family: str) -> bytes:
    """Build the smallest valid-enough sfnt carrying one family-name record."""
    fam = family.encode("utf-16-be")
    count = 1
    string_offset = 6 + count * 12
    record = struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(fam), 0)  # platform=3 (Windows)
    name_table = struct.pack(">HHH", 0, count, string_offset) + record + fam
    sfnt_header = struct.pack(">4sHHHH", b"\x00\x01\x00\x00", 1, 0, 0, 0)
    name_offset = 12 + 16
    entry = struct.pack(">4sIII", b"name", 0, name_offset, len(name_table))
    return sfnt_header + entry + name_table


# --- gap 3: font resolution -----------------------------------------------------
def test_resolve_subtitle_font_reads_family_and_stages_file(tmp_path):
    font_file = tmp_path / "brand.ttf"
    font_file.write_bytes(_build_min_font("My Brand Sans"))
    runtime = tmp_path / "runtime_fonts"

    resolved = resolve_subtitle_font(
        font_path=font_file, runtime_dir=runtime, fallback_name="ignored"
    )

    assert resolved is not None
    assert resolved.family_name == "My Brand Sans"
    assert (runtime / "brand.ttf").exists()  # staged for libass fontsdir
    assert resolved.fonts_dir == runtime


def test_resolve_subtitle_font_uses_fallback_when_unparseable(tmp_path):
    # A .ttf whose bytes are not a parseable sfnt -> family name falls back to title.
    font_file = tmp_path / "weird.ttf"
    font_file.write_bytes(b"not a real font")
    runtime = tmp_path / "rt"

    resolved = resolve_subtitle_font(
        font_path=font_file, runtime_dir=runtime, fallback_name="案例字体"
    )

    assert resolved is not None
    assert resolved.family_name == "案例字体"


def test_resolve_subtitle_font_none_for_missing_or_non_font(tmp_path):
    assert (
        resolve_subtitle_font(font_path=tmp_path / "nope.ttf", runtime_dir=tmp_path / "r") is None
    )
    not_font = tmp_path / "image.png"
    not_font.write_bytes(b"\x89PNG")
    assert resolve_subtitle_font(font_path=not_font, runtime_dir=tmp_path / "r2") is None


def test_write_ass_subtitles_uses_resolved_font_name(tmp_path):
    out = tmp_path / "sub.ass"
    write_ass_subtitles(
        out,
        narration={"units": [{"text": "hi", "start": 0.0, "end": 1.0}]},
        style={"subtitle": {"font_size": 48}},
        width=1080,
        height=1080,
        font_name="My Brand Sans",
    )
    text = out.read_text(encoding="utf-8")
    assert "Style: Default,My Brand Sans,48," in text
    assert "Arial" not in text


def test_write_ass_subtitles_scales_ui_size_for_portrait_output(tmp_path):
    out = tmp_path / "sub.ass"
    write_ass_subtitles(
        out,
        narration={"units": [{"text": "hi", "start": 0.0, "end": 1.0}]},
        style={"subtitle": {"font_size": 38}},
        width=1080,
        height=1920,
    )
    assert "Style: Default,Arial,68," in out.read_text(encoding="utf-8")


def test_write_ass_subtitles_wraps_long_portrait_text_inside_safe_width(tmp_path):
    out = tmp_path / "sub.ass"
    write_ass_subtitles(
        out,
        narration={
            "units": [
                {
                    "text": "就在邻水海风小镇旭通超市进店看看顺手买真的不贵",
                    "start": 0.0,
                    "end": 2.0,
                }
            ]
        },
        style={"subtitle": {"font_size": 38}},
        width=1080,
        height=1920,
    )

    dialogue = next(
        line
        for line in out.read_text(encoding="utf-8").splitlines()
        if line.startswith("Dialogue:")
    )
    assert r"\N" in dialogue


def test_write_ass_subtitles_defaults_to_arial_without_font(tmp_path):
    out = tmp_path / "sub.ass"
    write_ass_subtitles(
        out,
        narration={"units": [{"text": "hi", "start": 0.0, "end": 1.0}]},
        style={},
        width=1080,
        height=1920,
    )
    assert "Style: Default,Arial," in out.read_text(encoding="utf-8")


# --- gap 2: adaptive mix volume -------------------------------------------------
def test_adaptive_volume_passthrough_when_auto_mix_off(tmp_path):
    result = resolve_adaptive_bgm_volume(
        voice_path=tmp_path / "v.wav",
        bgm_path=tmp_path / "b.wav",
        requested_bgm_volume=0.25,
        auto_mix=False,
    )
    assert result.bgm_volume == 0.25
    assert result.metadata["auto_mix"] is False
    assert result.metadata["effective_bgm_volume"] == 0.25


def test_adaptive_volume_targets_voice_lufs(monkeypatch, tmp_path):
    # voice at -14 LUFS, bgm at -10 LUFS, margin 12 -> target = -26 LUFS.
    lufs = {str(tmp_path / "v.wav"): -14.0, str(tmp_path / "b.wav"): -10.0}
    monkeypatch.setattr(_ffmpeg, "measure_loudness_lufs", lambda p: lufs.get(str(p)))
    result = resolve_adaptive_bgm_volume(
        voice_path=tmp_path / "v.wav",
        bgm_path=tmp_path / "b.wav",
        requested_bgm_volume=0.3,  # neutral slider -> trust the LUFS target as-is
        auto_mix=True,
    )
    # gain to drop -10 LUFS to -26 LUFS = 10**((-26 - -10)/20) ~= 0.1585; slider neutral.
    assert result.metadata["target_bgm_lufs"] == -26.0
    assert 0.10 < result.bgm_volume < 0.25
    assert result.bgm_volume <= AUTO_MIX_MAX_BGM_VOLUME


def test_adaptive_volume_falls_back_when_probe_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(_ffmpeg, "measure_loudness_lufs", lambda _p: None)
    result = resolve_adaptive_bgm_volume(
        voice_path=tmp_path / "v.wav",
        bgm_path=tmp_path / "b.wav",
        requested_bgm_volume=0.2,
        auto_mix=True,
    )
    assert result.bgm_volume == 0.2
    assert result.metadata["fallback_reason"] == "loudness_probe_failed"


def test_mix_filter_graph_has_ducking_and_fades_only_when_auto():
    auto = _ffmpeg._build_bgm_audio_filters(
        bgm_volume=0.2, duration=5.0, auto_mix=True, fade_in=1.0, fade_out=1.5
    )
    assert "sidechaincompress" in auto
    assert "afade=t=in" in auto and "afade=t=out" in auto
    assert "asplit=2[voice][voicesc]" in auto

    plain = _ffmpeg._build_bgm_audio_filters(
        bgm_volume=0.2, duration=5.0, auto_mix=False, fade_in=0.0, fade_out=0.0
    )
    assert "sidechaincompress" not in plain
    assert "afade" not in plain
    assert "anull[bgm]" in plain


def test_mix_filter_graph_loops_only_selected_bgm_segment_window():
    graph = _ffmpeg._build_bgm_audio_filters(
        bgm_volume=0.2,
        duration=5.0,
        auto_mix=False,
        fade_in=0.0,
        fade_out=0.0,
        bgm_source_start=62.5,
        bgm_source_end=64.5,
    )

    assert "atrim=62.500:64.500" in graph
    assert "aloop=loop=-1:size=96000" in graph
    assert "atrim=0:5.000" in graph
    assert "asetpts=PTS-STARTPTS" in graph


def test_subtitle_bgm_mix_passes_selected_bgm_segment_window(monkeypatch, tmp_path):
    repository = Repository()
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store", lambda: object_store
    )

    def stored_artifact(kind: ArtifactKind, filename: str, *, media_type: str):
        path = tmp_path / filename
        path.write_bytes(b"test")
        stored = store_file(object_store, path, purpose=kind.value)
        return repository.create_artifact(
            kind=kind,
            payload_schema="uri-only",
            payload=None,
            case_id="case_demo",
            uri=stored.ref.uri,
            sha256=stored.sha256,
            media_info=MediaInfo(
                media_type=media_type,
                codec="h264" if media_type == "video" else "mp3",
                format="mp4" if media_type == "video" else "mp3",
                duration_sec=2.0,
            ),
        )

    rendered = stored_artifact(ArtifactKind.video_rendered, "rendered.mp4", media_type="video")
    audio = stored_artifact(ArtifactKind.audio_tts, "voice.wav", media_type="audio")
    bgm_source = stored_artifact(ArtifactKind.uploaded_file, "bgm.mp3", media_type="audio")
    repository.media_assets["asset_bgm_demo"] = MediaAssetRecord(
        id="asset_bgm_demo",
        case_id="case_demo",
        title="BGM demo",
        kind="bgm",
        source_artifact_id=bgm_source.id,
        usable=True,
    )

    timeline = repository.create_artifact(
        kind=ArtifactKind.plan_timeline,
        payload_schema="TimelinePlanArtifact.v1",
        payload={"fps": 30, "total_frames": 60, "tracks": [], "validation": {"valid": True}},
        case_id="case_demo",
    )
    style = repository.create_artifact(
        kind=ArtifactKind.plan_style,
        payload_schema="StylePlanArtifact.v1",
        payload={
            "subtitle": {"enabled": False},
            "bgm_asset_id": "asset_bgm_demo",
            "bgm": {
                "enabled": True,
                "asset_id": "asset_bgm_demo",
                "segment_id": "bgm_segment_2",
                "source_start": 62.5,
                "source_end": 122.5,
                "volume": 0.21,
                "auto_mix": False,
            },
        },
        case_id="case_demo",
    )
    narration = repository.create_artifact(
        kind=ArtifactKind.narration_units,
        payload_schema="NarrationUnitsArtifact.v1",
        payload={"source": "estimated", "units": [], "strict": False},
        case_id="case_demo",
    )

    adapter = object.__new__(LocalRuntimeAdapter)
    adapter.repository = repository
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
            subtitle={"enabled": False},
            bgm={"enabled": True},
        ),
        artifacts={
            ArtifactKind.video_rendered: rendered,
            ArtifactKind.audio_tts: audio,
            ArtifactKind.plan_timeline: timeline,
            ArtifactKind.plan_style: style,
            ArtifactKind.narration_units: narration,
        },
    )
    ctx = NodeContext(
        adapter=adapter,
        run=WorkflowRun(
            id="run_mix",
            job_id="job_mix",
            case_id="case_demo",
            workflow_template_id="digital_human_v2",
            workflow_version="v1",
            status=RunStatus.running,
        ),
        node_run=NodeRun(
            id="nr_mix",
            run_id="run_mix",
            node_id="SubtitleAndBgmMix",
            node_version="v1",
            status=NodeStatus.running,
            input_manifest_hash="sha256:test",
        ),
        state=state,
    )

    captured = {}

    def fake_render_final_media(**kwargs):
        captured.update(kwargs)
        kwargs["output_path"].write_bytes(b"fake video")
        return None

    monkeypatch.setattr(
        "packages.production.pipeline.nodes.subtitle_and_bgm_mix.render_final_media",
        fake_render_final_media,
    )
    monkeypatch.setattr(
        "packages.production.pipeline.nodes.subtitle_and_bgm_mix.validate_rendered_output",
        lambda *_args, **_kwargs: MediaInfo(
            media_type="video", codec="h264", format="mp4", duration_sec=2.0
        ),
    )

    from packages.production.pipeline import nodes

    nodes.subtitle_and_bgm_mix.run(ctx)

    assert captured["bgm_source_start"] == 62.5
    assert captured["bgm_source_end"] == 122.5


# --- gap 2+3: real ffmpeg end-to-end burn --------------------------------------
@pytest.mark.skipif(shutil.which(ffmpeg_bin()) is None, reason="ffmpeg not available")
def test_render_final_media_auto_mix_and_fontsdir_real_ffmpeg(tmp_path):
    """A real burn with a selected font (fontsdir) + auto-mixed BGM produces a valid AV file."""
    fps = 30
    duration = 2.0
    total_frames = int(round(duration * fps))
    video = tmp_path / "video.mp4"
    voice = tmp_path / "voice.wav"
    bgm = tmp_path / "bgm.wav"
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=480x854:rate={fps}",
            "-t",
            f"{duration:.3f}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(video),
        ]
    )
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:sample_rate=48000:duration={duration:.3f}",
            "-ac",
            "2",
            str(voice),
        ]
    )
    FfmpegRunner().run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration:.3f}",
            "-ac",
            "2",
            str(bgm),
        ]
    )
    # Stage a (synthetic) font into a fontsdir and burn a subtitle that references it.
    fonts_dir = tmp_path / "fonts"
    font_file = tmp_path / "brand.ttf"
    font_file.write_bytes(_build_min_font("My Brand Sans"))
    resolved = resolve_subtitle_font(font_path=font_file, runtime_dir=fonts_dir, fallback_name="x")
    assert resolved is not None
    sub = tmp_path / "sub.ass"
    write_ass_subtitles(
        sub,
        narration={"units": [{"text": "测试字幕", "start": 0.0, "end": duration}]},
        style={"subtitle": {"font_size": 48}},
        width=480,
        height=854,
        font_name=resolved.family_name,
    )

    out = tmp_path / "final.mp4"
    mix = _ffmpeg.render_final_media(
        rendered_path=video,
        audio_path=voice,
        output_path=out,
        subtitle_path=sub,
        bgm_path=bgm,
        bgm_volume=0.3,
        duration=duration,
        fps=fps,
        fonts_dir=resolved.fonts_dir,
        auto_mix=True,
    )
    assert out.exists()
    info = probe_media(out)
    assert info.media_type == "video"
    assert {"video", "audio"} <= probe_stream_types(out)
    # A mix decision was returned (auto_mix consumed, not a dead flag).
    assert mix is not None
    assert mix.metadata["auto_mix"] is True
    # frame count matches the timeline (same invariant the node enforces).
    from packages.media.video.ffmpeg import probe_video_frame_count

    assert probe_video_frame_count(out) == total_frames


@pytest.mark.skipif(shutil.which(ffmpeg_bin()) is None, reason="ffmpeg not available")
def test_render_final_media_real_ffmpeg_trims_bgm_from_selected_source_start(tmp_path):
    fps = 30
    duration = 1.0
    video = tmp_path / "video.mp4"
    voice = tmp_path / "voice.wav"
    bgm = tmp_path / "bgm.wav"
    out = tmp_path / "final.mp4"
    decoded = tmp_path / "decoded.wav"
    runner = FfmpegRunner()

    runner.run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x320:rate={fps}",
            "-t",
            f"{duration:.3f}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(video),
        ]
    )
    runner.run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=48000",
            "-t",
            f"{duration:.3f}",
            "-ac",
            "1",
            str(voice),
        ]
    )
    runner.run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=48000:duration=1.000",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=990:sample_rate=48000:duration=1.000",
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1[a]",
            "-map",
            "[a]",
            "-ac",
            "1",
            str(bgm),
        ]
    )

    _ffmpeg.render_final_media(
        rendered_path=video,
        audio_path=voice,
        output_path=out,
        subtitle_path=None,
        bgm_path=bgm,
        bgm_volume=1.0,
        duration=duration,
        fps=fps,
        auto_mix=False,
        bgm_source_start=1.0,
        fade_in=0.0,
        fade_out=0.0,
    )
    runner.run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(out),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-t",
            "0.800",
            str(decoded),
        ]
    )

    assert _zero_crossing_frequency(decoded, skip_seconds=0.1) > 750.0


@pytest.mark.skipif(shutil.which(ffmpeg_bin()) is None, reason="ffmpeg not available")
def test_render_final_media_real_ffmpeg_loops_selected_bgm_clip_not_whole_track(tmp_path):
    fps = 30
    duration = 0.8
    video = tmp_path / "video.mp4"
    voice = tmp_path / "voice.wav"
    bgm = tmp_path / "bgm.wav"
    out = tmp_path / "final.mp4"
    decoded = tmp_path / "decoded.wav"
    runner = FfmpegRunner()

    runner.run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x320:rate={fps}",
            "-t",
            f"{duration:.3f}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(video),
        ]
    )
    runner.run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=48000",
            "-t",
            f"{duration:.3f}",
            "-ac",
            "1",
            str(voice),
        ]
    )
    runner.run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=48000:duration=0.400",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=990:sample_rate=48000:duration=0.800",
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1[a]",
            "-map",
            "[a]",
            "-ac",
            "1",
            str(bgm),
        ]
    )

    _ffmpeg.render_final_media(
        rendered_path=video,
        audio_path=voice,
        output_path=out,
        subtitle_path=None,
        bgm_path=bgm,
        bgm_volume=1.0,
        duration=duration,
        fps=fps,
        auto_mix=False,
        bgm_source_start=0.0,
        bgm_source_end=0.4,
        fade_in=0.0,
        fade_out=0.0,
    )
    runner.run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(out),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-t",
            "0.750",
            str(decoded),
        ]
    )

    assert _zero_crossing_frequency(decoded, skip_seconds=0.5) < 500.0


def _zero_crossing_frequency(path, *, skip_seconds: float = 0.0) -> float:
    with wave.open(str(path), "rb") as wav:
        sample_rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    assert channels == 1
    assert width == 2
    samples = [
        int.from_bytes(raw[i : i + 2], "little", signed=True)
        for i in range(0, len(raw), 2)
    ]
    start = min(len(samples), int(sample_rate * skip_seconds))
    samples = [value for value in samples[start:] if abs(value) > 16]
    crossings = sum(
        1
        for prev, cur in zip(samples, samples[1:])
        if (prev < 0 <= cur) or (prev > 0 >= cur)
    )
    if len(samples) < 2:
        return 0.0
    return crossings * sample_rate / (2.0 * len(samples))
