from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from packages.core.storage.object_store import ObjectStore
from packages.media.video.ffmpeg import probe_media
from . import jianying_draft_json as jy_json
from ._broll_overlays import _placement_or_none


_PORTABLE_PACKAGE_SCHEMA_VERSION = "jianying_draft_portable_v2"
_INVALID_RESOURCE_NAME_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class JianyingVideoSegment:
    track_name: str
    source_path: Path
    timeline_start_frame: int
    timeline_end_frame: int
    source_start_frame: int
    source_end_frame: int
    asset_id: str | None = None
    clip_id: str | None = None
    volume: float = 0.0
    fade_frames: int | None = None
    placement: str | None = None


@dataclass(frozen=True)
class JianyingAudioSegment:
    track_name: str
    source_path: Path
    start_us: int
    duration_us: int
    source_start_us: int = 0
    source_duration_us: int | None = None
    volume: float = 1.0


@dataclass(frozen=True)
class JianyingTextSegment:
    track_name: str
    text: str
    start_us: int
    duration_us: int
    transform_y: float = -0.8


def build_video_segments_from_plans(
    timeline_plan: dict[str, Any] | None,
    portrait_plan: dict[str, Any] | None,
    broll_plan: dict[str, Any] | None,
    resolve_source_path: Callable[[str], str | Path],
) -> list[JianyingVideoSegment]:
    timeline = timeline_plan or {}
    fps = int(timeline.get("fps") or (portrait_plan or {}).get("fps") or 30)
    portrait_by_id = _segments_by_timeline_id(portrait_plan, "portrait")
    broll_by_id = _segments_by_timeline_id(broll_plan, "broll")
    segments: list[JianyingVideoSegment] = []
    for raw in timeline.get("tracks") or []:
        if not isinstance(raw, dict):
            continue
        track_id = str(raw.get("track_id") or "").lower()
        segment_id = str(raw.get("segment_id") or "")
        if track_id == "portrait":
            source = portrait_by_id.get(segment_id)
            track_name = "主视频"
        elif track_id == "broll":
            source = broll_by_id.get(segment_id)
            track_name = "B-roll覆盖"
        else:
            continue
        if not isinstance(source, dict):
            continue
        asset_id = _str_or_none(source.get("asset_id"))
        if not asset_id:
            continue
        timeline_start_frame = _frame_from_payload(raw, "timeline_start_frame", "start_sec", fps)
        timeline_end_frame = _frame_from_payload(raw, "timeline_end_frame", "end_sec", fps)
        source_start_frame = _frame_from_payload(
            raw, "source_start_frame", "source_start_sec", fps, source, "source_start"
        )
        source_end_frame = _frame_from_payload(
            raw, "source_end_frame", "source_end_sec", fps, source, "source_end"
        )
        if timeline_end_frame <= timeline_start_frame or source_end_frame <= source_start_frame:
            continue
        segments.append(
            JianyingVideoSegment(
                track_name=track_name,
                source_path=Path(resolve_source_path(asset_id)),
                timeline_start_frame=timeline_start_frame,
                timeline_end_frame=timeline_end_frame,
                source_start_frame=source_start_frame,
                source_end_frame=source_end_frame,
                asset_id=asset_id,
                clip_id=_str_or_none(source.get("clip_id")),
                fade_frames=_int_or_none(raw.get("fade_frames", source.get("fade_frames"))),
                placement=_placement_or_none(raw.get("placement", source.get("placement"))),
            )
        )
    return segments


def build_audio_segments_from_sources(
    audio_path: Path | None,
    duration_sec: float,
    style_plan: dict[str, Any] | None,
    resolve_source_path: Callable[[str], str | Path],
) -> list[JianyingAudioSegment]:
    duration_us = max(1, _sec_to_us(duration_sec))
    segments: list[JianyingAudioSegment] = []
    if audio_path is not None:
        segments.append(
            JianyingAudioSegment(
                track_name="旁白", source_path=audio_path, start_us=0, duration_us=duration_us
            )
        )
    style = style_plan or {}
    bgm_plan = style.get("bgm") if isinstance(style.get("bgm"), dict) else {}
    bgm_asset_id = _str_or_none(style.get("bgm_asset_id") or bgm_plan.get("asset_id"))
    if bgm_plan and bgm_plan.get("enabled", True) and bgm_asset_id:
        source_start_us = _sec_to_us(bgm_plan.get("source_start") or 0)
        source_end = bgm_plan.get("source_end")
        source_duration_us = None
        if source_end is not None:
            source_duration_us = max(1, _sec_to_us(float(source_end)) - source_start_us)
        elif bgm_plan.get("duration") is not None:
            source_duration_us = max(1, _sec_to_us(bgm_plan.get("duration")))
        segments.append(
            JianyingAudioSegment(
                track_name="BGM",
                source_path=Path(resolve_source_path(bgm_asset_id)),
                start_us=0,
                duration_us=duration_us,
                source_start_us=source_start_us,
                source_duration_us=source_duration_us,
                volume=float(bgm_plan.get("volume") or 0.25),
            )
        )
    return segments


def build_text_segments_from_narration(
    narration_units: list[dict[str, Any]],
    style_plan: dict[str, Any] | None = None,
) -> list[JianyingTextSegment]:
    segments: list[JianyingTextSegment] = []
    for unit in narration_units:
        if not isinstance(unit, dict):
            continue
        text = str(unit.get("text") or unit.get("content") or "").strip()
        if not text:
            continue
        start_us = _sec_to_us(unit.get("start") or unit.get("start_sec") or 0)
        end_us = _sec_to_us(unit.get("end") or unit.get("end_sec") or 0)
        if end_us > start_us:
            segments.append(
                JianyingTextSegment(
                    track_name="字幕", text=text, start_us=start_us, duration_us=end_us - start_us
                )
            )
    style = style_plan or {}
    overlay_events = style.get("overlay_events") if isinstance(style.get("overlay_events"), list) else []
    for event in overlay_events:
        if not isinstance(event, dict):
            continue
        text = str(event.get("text") or "").strip()
        if not text:
            continue
        start_us = _sec_to_us(event.get("start") or 0)
        end_us = _sec_to_us(event.get("end") or 0)
        if end_us > start_us:
            segments.append(
                JianyingTextSegment(
                    track_name="花字",
                    text=text,
                    start_us=start_us,
                    duration_us=end_us - start_us,
                    transform_y=-0.48,
                )
            )
    return segments


@dataclass(frozen=True)
class JianyingDraftInput:
    finished_video_id: str
    title: str
    video_path: Path
    audio_path: Path | None = None
    subtitle_path: Path | None = None
    duration_sec: float = 0
    template_id: str | None = None
    timeline_plan: dict[str, Any] | None = None
    narration_units: list[dict[str, Any]] = field(default_factory=list)
    video_segments: list[JianyingVideoSegment] = field(default_factory=list)
    audio_segments: list[JianyingAudioSegment] = field(default_factory=list)
    text_segments: list[JianyingTextSegment] = field(default_factory=list)


@dataclass(frozen=True)
class JianyingDraftBuild:
    package_uri: str
    sha256: str
    size_bytes: int
    draft_name: str
    draft_id: str
    tracks_summary: dict[str, int]
    manifest: dict[str, Any]


class JianyingDraftBuilder:
    def __init__(self, object_store: ObjectStore) -> None:
        self.object_store = object_store

    def build(self, source: JianyingDraftInput) -> JianyingDraftBuild:
        with tempfile.TemporaryDirectory(prefix="cutagent-jianying-") as directory:
            root = Path(directory) / "draft_root"
            draft_name = _safe_draft_name(source.title, source.finished_video_id)
            draft_id = str(uuid.uuid4()).upper()
            draft_dir = root / draft_name
            created_us = _now_us()
            (draft_dir / "Resources" / "video").mkdir(parents=True, exist_ok=True)
            (draft_dir / "Resources" / "audio").mkdir(parents=True, exist_ok=True)

            used_names: set[str] = set()
            staged_videos: dict[Path, str] = {}
            staged_audios: dict[Path, str] = {}
            video_dir = draft_dir / "Resources" / "video"
            audio_dir = draft_dir / "Resources" / "audio"
            probe_video_path = (
                source.video_segments[0].source_path if source.video_segments else source.video_path
            )
            video_path = _stage_once(
                probe_video_path, video_dir, used_names, staged_videos
            )
            audio_path = (
                _stage_once(source.audio_path, audio_dir, used_names, staged_audios)
                if source.audio_path
                else None
            )
            video_info = probe_media(video_path)
            audio_info = probe_media(audio_path) if audio_path else None
            duration_us = max(
                _sec_to_us(source.duration_sec),
                _sec_to_us(video_info.duration_sec or 0),
                _sec_to_us(audio_info.duration_sec or 0) if audio_info else 0,
                1,
            )
            width = int(video_info.width or 1080)
            height = int(video_info.height or 1920)

            materials = jy_json.empty_materials()
            tracks: list[dict[str, Any]] = []
            speeds: list[dict[str, Any]] = []

            if source.video_segments:
                video_track_segments, video_materials = _explicit_video_tracks(
                    source, draft_dir, video_dir, used_names, staged_videos
                )
                materials["videos"].extend(video_materials)
                for render_index, (track_name, segments) in enumerate(video_track_segments.items()):
                    tracks.append(_track("video", track_name, render_index, segments))
                    for segment in segments:
                        speeds.append(
                            _speed_material(segment["extra_material_refs"][0], segment["speed"])
                        )
                main_segments = [
                    segment
                    for track_name, segments in video_track_segments.items()
                    if not _is_broll_track(track_name)
                    for segment in segments
                ]
                broll_segments = [
                    segment
                    for track_name, segments in video_track_segments.items()
                    if _is_broll_track(track_name)
                    for segment in segments
                ]
            else:
                video_material = _video_material(
                    _portable_resource_path(video_path, draft_dir), duration_us, width, height
                )
                materials["videos"].append(video_material)
                main_segments = _main_video_segments(source, video_material["id"], duration_us)
                tracks.append(_track("video", "video", 0, main_segments))
                for segment in main_segments:
                    speeds.append(
                        _speed_material(segment["extra_material_refs"][0], segment["speed"])
                    )

                broll_segments = _broll_segments(source, video_material["id"])
                if broll_segments:
                    tracks.append(_track("video", "broll", 1, broll_segments))
                    for segment in broll_segments:
                        speeds.append(
                            _speed_material(segment["extra_material_refs"][0], segment["speed"])
                        )

            if source.audio_segments:
                audio_track_segments, audio_materials = _explicit_audio_tracks(
                    source.audio_segments, draft_dir, audio_dir, used_names, staged_audios
                )
                materials["audios"].extend(audio_materials)
                for render_index, (track_name, segments) in enumerate(audio_track_segments.items()):
                    tracks.append(_track("audio", track_name, render_index, segments))
                    for segment in segments:
                        speeds.append(
                            _speed_material(segment["extra_material_refs"][0], segment["speed"])
                        )
            elif audio_path and audio_info:
                audio_duration_us = max(
                    1, _sec_to_us(audio_info.duration_sec or source.duration_sec)
                )
                audio_material = _audio_material(
                    _portable_resource_path(audio_path, draft_dir), audio_duration_us
                )
                materials["audios"].append(audio_material)
                audio_segment = _media_segment(
                    audio_material["id"], 0, audio_duration_us, 0, audio_duration_us, volume=1.0
                )
                tracks.append(_track("audio", "audio", 0, [audio_segment]))
                speeds.append(
                    _speed_material(audio_segment["extra_material_refs"][0], audio_segment["speed"])
                )

            subtitle_segments: list[dict[str, Any]] = []
            huazi_segments: list[dict[str, Any]] = []
            if source.text_segments:
                text_tracks: dict[str, list[dict[str, Any]]] = {}
                for text_segment in source.text_segments:
                    material_id = uuid.uuid4().hex
                    materials["texts"].append(_text_material(material_id, text_segment.text))
                    segment = _text_segment(
                        material_id,
                        text_segment.start_us,
                        max(1, text_segment.duration_us),
                        transform_y=text_segment.transform_y,
                    )
                    text_tracks.setdefault(text_segment.track_name, []).append(segment)
                    if _is_huazi_track(text_segment.track_name):
                        huazi_segments.append(segment)
                    else:
                        subtitle_segments.append(segment)
                for index, (track_name, segments) in enumerate(text_tracks.items()):
                    tracks.append(_track("text", track_name, 15200 + index, segments))
            else:
                subtitle_entries = jy_json.subtitle_entries(
                    source.subtitle_path, source.narration_units
                )
                for entry in subtitle_entries:
                    material_id = uuid.uuid4().hex
                    duration = max(1, int(entry["end_us"] - entry["start_us"]))
                    materials["texts"].append(_text_material(material_id, entry["text"]))
                    subtitle_segments.append(
                        _text_segment(material_id, int(entry["start_us"]), duration)
                    )
                if subtitle_segments:
                    tracks.append(_track("text", "subtitle", 15200, subtitle_segments))

            materials["speeds"] = speeds
            content = jy_json.draft_content(
                draft_id=draft_id,
                draft_name=draft_name,
                width=width,
                height=height,
                duration_us=duration_us,
                created_us=created_us,
                materials=materials,
                tracks=sorted(
                    tracks,
                    key=lambda item: (
                        item["segments"][0].get("render_index", 0) if item["segments"] else 0
                    ),
                ),
            )
            jy_json.dump_json(draft_dir / "draft_content.json", content)
            jy_json.ensure_supporting_files(draft_dir)
            folder_size = jy_json.folder_size_bytes(draft_dir)
            jy_json.dump_json(
                draft_dir / "draft_meta_info.json",
                jy_json.draft_meta(draft_name, draft_id, duration_us, folder_size, created_us),
            )
            jy_json.dump_json(
                root / "root_meta_info.json",
                jy_json.root_meta(draft_name, draft_id, duration_us, folder_size, created_us),
            )

            zip_path = Path(directory) / f"{draft_name}.zip"
            jy_json.zip_root(root, zip_path)
            stored = self.object_store.put_bytes(
                self.object_store.prepare_upload(zip_path.name, "jianying-drafts"),
                zip_path.read_bytes(),
            )
            tracks_summary = {
                "main_video": len(main_segments),
                "voice_audio": _voice_audio_count(source, audio_path),
                "subtitle_segments": len(subtitle_segments),
                "broll_segments": len(broll_segments),
                "overlay_tracks": 0,
                "cover_tracks": 0,
                "huazi_segments": len(huazi_segments),
            }
            bgm_audio = _bgm_audio_count(source.audio_segments)
            if bgm_audio:
                tracks_summary["bgm_audio"] = bgm_audio
            manifest = {
                "finished_video_id": source.finished_video_id,
                "template_id": source.template_id or "default",
                "draft_name": draft_name,
                "draft_id": draft_id,
                "package_schema_version": _PORTABLE_PACKAGE_SCHEMA_VERSION,
                "portable_resources": True,
                "duration_us": duration_us,
                "tracks_summary": tracks_summary,
                "package_uri": stored.ref.uri,
                "assets": {
                    "video": _material_names(materials["videos"], "material_name"),
                    "audio": _material_names(materials["audios"], "name"),
                    "subtitle": Path(source.subtitle_path).name if source.subtitle_path else None,
                },
                "effects": _draft_effects_manifest(source, huazi_segments=len(huazi_segments)),
                "warnings": [],
            }
            return JianyingDraftBuild(
                stored.ref.uri,
                stored.sha256,
                stored.size_bytes,
                draft_name,
                draft_id,
                tracks_summary,
                manifest,
            )


def _safe_draft_name(title: str, task_id: str) -> str:
    base = re.sub(r'[\\/:*?"<>|]', "", (title or "").strip())
    base = re.sub(r"\s+", " ", base).strip() or "剪映草稿"
    if len(base) > 28:
        base = base[:28].rstrip()
    return f"{base}丨{task_id[:8]}"


def _now_us() -> int:
    return int(time.time() * 1_000_000)


def _sec_to_us(value: float | int | None) -> int:
    return int(round(float(value or 0) * 1_000_000))


def _frame_to_us(frame: int, fps: int) -> int:
    return int(float(frame) / float(fps or 30) * 1_000_000)


def _stage_media_file(
    source_path: Path, target_dir: Path, used_names: set[str]
) -> str:
    source = Path(source_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"素材不存在: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _unique_name(_safe_resource_name(source.name), used_names)
    try:
        os.link(source, target)
    except OSError:
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            raise OSError(f"素材复制失败: {source} -> {target}") from exc
    return str(target)


def _stage_once(
    source_path: Path,
    target_dir: Path,
    used_names: set[str],
    staged: dict[Path, str],
) -> str:
    source = Path(source_path).expanduser().resolve()
    existing = staged.get(source)
    if existing:
        return existing
    staged_path = _stage_media_file(source, target_dir, used_names)
    staged[source] = staged_path
    return staged_path


def _portable_resource_path(path: str | Path, draft_dir: Path) -> str:
    return Path(path).resolve().relative_to(draft_dir.resolve()).as_posix()


def _safe_resource_name(original_name: str) -> str:
    name = _INVALID_RESOURCE_NAME_CHARS.sub("_", Path(original_name).name)
    name = name.strip(" .")
    if not name or name in {".", ".."}:
        name = "material"
    path = Path(name)
    stem = path.stem.strip(" .") or "material"
    suffix = path.suffix.strip(" .")
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"{stem}_"
    if suffix:
        suffix = _INVALID_RESOURCE_NAME_CHARS.sub("_", suffix)
        return f"{stem}.{suffix.lstrip('.')}"
    return stem


def _unique_name(original_name: str, used_names: set[str]) -> str:
    candidate = original_name
    stem = Path(original_name).stem
    suffix = Path(original_name).suffix
    index = 1
    while candidate in used_names:
        candidate = f"{stem}_{index}{suffix}"
        index += 1
    used_names.add(candidate)
    return candidate


def _segments_by_timeline_id(plan: dict[str, Any] | None, prefix: str) -> dict[str, dict[str, Any]]:
    payload = plan or {}
    # B-roll plans are keyed by the canonical ``overlays`` (#104); portrait plans
    # (and pre-#104 persisted B-roll plans) still carry the legacy ``segments``.
    items = payload.get("overlays") or payload.get("segments") or []
    result: dict[str, dict[str, Any]] = {}
    for index, segment in enumerate(items):
        if not isinstance(segment, dict):
            continue
        segment_id = (
            _str_or_none(segment.get("segment_id"))
            or _str_or_none(segment.get("overlay_id"))
            or f"{prefix}_{index + 1}"
        )
        result[segment_id] = segment
    return result


def _frame_from_payload(
    payload: dict[str, Any],
    frame_key: str,
    seconds_key: str,
    fps: int,
    fallback: dict[str, Any] | None = None,
    fallback_seconds_key: str | None = None,
) -> int:
    value = payload.get(frame_key)
    if value is not None:
        return int(value)
    value = payload.get(seconds_key)
    if value is not None:
        return int(round(float(value) * fps))
    if fallback is not None and fallback_seconds_key:
        value = fallback.get(fallback_seconds_key)
        if value is not None:
            return int(round(float(value) * fps))
    return 0


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _explicit_video_tracks(
    source: JianyingDraftInput,
    draft_dir: Path,
    video_dir: Path,
    used_names: set[str],
    staged_videos: dict[Path, str],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    fps = int((source.timeline_plan or {}).get("fps") or 30)
    tracks: dict[str, list[dict[str, Any]]] = {}
    materials: list[dict[str, Any]] = []
    material_by_path: dict[Path, str] = {}
    for segment in source.video_segments:
        start = _frame_to_us(segment.timeline_start_frame, fps)
        end = _frame_to_us(segment.timeline_end_frame, fps)
        source_start = _frame_to_us(segment.source_start_frame, fps)
        source_end = _frame_to_us(segment.source_end_frame, fps)
        if end <= start or source_end <= source_start:
            continue
        source_path = Path(segment.source_path).expanduser().resolve()
        material_id = material_by_path.get(source_path)
        if material_id is None:
            staged_path = _stage_once(source_path, video_dir, used_names, staged_videos)
            info = probe_media(staged_path)
            material = _video_material(
                _portable_resource_path(staged_path, draft_dir),
                max(_sec_to_us(info.duration_sec or 0), source_end, 1),
                int(info.width or 1080),
                int(info.height or 1920),
            )
            material_id = material["id"]
            material_by_path[source_path] = material_id
            materials.append(material)
        tracks.setdefault(segment.track_name, []).append(
            _media_segment(
                material_id,
                start,
                end - start,
                source_start,
                source_end - source_start,
                volume=segment.volume,
                effects=_segment_effects(segment),
            )
        )
    return tracks, materials


def _explicit_audio_tracks(
    audio_segments: list[JianyingAudioSegment],
    draft_dir: Path,
    audio_dir: Path,
    used_names: set[str],
    staged_audios: dict[Path, str],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    tracks: dict[str, list[dict[str, Any]]] = {}
    materials: list[dict[str, Any]] = []
    material_by_path: dict[Path, str] = {}
    for segment in audio_segments:
        if segment.duration_us <= 0:
            continue
        source_duration = max(1, segment.source_duration_us or segment.duration_us)
        source_path = Path(segment.source_path).expanduser().resolve()
        material_id = material_by_path.get(source_path)
        if material_id is None:
            staged_path = _stage_once(source_path, audio_dir, used_names, staged_audios)
            info = probe_media(staged_path)
            material = _audio_material(
                _portable_resource_path(staged_path, draft_dir),
                max(
                    _sec_to_us(info.duration_sec or 0), segment.source_start_us + source_duration, 1
                ),
            )
            material_id = material["id"]
            material_by_path[source_path] = material_id
            materials.append(material)
        tracks.setdefault(segment.track_name, []).append(
            _media_segment(
                material_id,
                segment.start_us,
                segment.duration_us,
                segment.source_start_us,
                source_duration,
                volume=segment.volume,
            )
        )
    return tracks, materials


def _main_video_segments(
    source: JianyingDraftInput, material_id: str, duration_us: int
) -> list[dict[str, Any]]:
    plan = source.timeline_plan or {}
    fps = int(plan.get("fps") or 30)
    segments = []
    for raw in plan.get("tracks") or []:
        if str(raw.get("track_id") or "").lower() != "portrait":
            continue
        start = _frame_to_us(int(raw.get("timeline_start_frame") or 0), fps)
        end = _frame_to_us(int(raw.get("timeline_end_frame") or 0), fps)
        source_start = _frame_to_us(int(raw.get("source_start_frame") or 0), fps)
        source_end = _frame_to_us(
            int(raw.get("source_end_frame") or raw.get("timeline_end_frame") or 0), fps
        )
        if end > start:
            segments.append(
                _media_segment(
                    material_id,
                    start,
                    end - start,
                    source_start,
                    max(1, source_end - source_start),
                    volume=0.0,
                )
            )
    return segments or [_media_segment(material_id, 0, duration_us, 0, duration_us, volume=0.0)]


def _broll_segments(source: JianyingDraftInput, material_id: str) -> list[dict[str, Any]]:
    plan = source.timeline_plan or {}
    fps = int(plan.get("fps") or 30)
    segments = []
    for raw in plan.get("tracks") or []:
        if str(raw.get("track_id") or "").lower() != "broll":
            continue
        start = _frame_to_us(int(raw.get("timeline_start_frame") or 0), fps)
        end = _frame_to_us(int(raw.get("timeline_end_frame") or 0), fps)
        source_start = _frame_to_us(int(raw.get("source_start_frame") or 0), fps)
        source_end = _frame_to_us(
            int(raw.get("source_end_frame") or raw.get("timeline_end_frame") or 0), fps
        )
        if end > start:
            segments.append(
                _media_segment(
                    material_id,
                    start,
                    end - start,
                    source_start,
                    max(1, source_end - source_start),
                    volume=0.0,
                    effects=_track_effects(raw),
                )
            )
    return segments


def _media_segment(
    material_id: str,
    start: int,
    duration: int,
    source_start: int,
    source_duration: int,
    *,
    volume: float,
    effects: dict[str, Any] | None = None,
) -> dict[str, Any]:
    speed_id = uuid.uuid4().hex
    payload = _base_segment(material_id, start, duration)
    payload.update(
        {
            "source_timerange": {"start": int(source_start), "duration": int(source_duration)},
            "speed": 1.0,
            "volume": volume,
            "extra_material_refs": [speed_id],
            "is_tone_modify": False,
            "clip": _clip(),
            "uniform_scale": {"on": True, "value": 1.0},
        }
    )
    if effects:
        payload["cutflow_effects"] = effects
    return payload


def _segment_effects(segment: JianyingVideoSegment) -> dict[str, Any]:
    effects: dict[str, Any] = {}
    if segment.fade_frames is not None:
        effects["fade_frames"] = segment.fade_frames
    if segment.placement:
        effects["placement"] = segment.placement
    return effects


def _track_effects(raw: dict[str, Any]) -> dict[str, Any]:
    effects: dict[str, Any] = {}
    fade_frames = _int_or_none(raw.get("fade_frames"))
    placement = _placement_or_none(raw.get("placement"))
    if fade_frames is not None:
        effects["fade_frames"] = fade_frames
    if placement:
        effects["placement"] = placement
    return effects


def _text_segment(
    material_id: str, start: int, duration: int, *, transform_y: float = -0.8
) -> dict[str, Any]:
    payload = _base_segment(material_id, start, duration)
    payload.update(
        {
            "source_timerange": None,
            "speed": 1.0,
            "volume": 1.0,
            "extra_material_refs": [uuid.uuid4().hex],
            "is_tone_modify": False,
            "clip": _clip(transform_y=transform_y),
            "uniform_scale": {"on": True, "value": 1.0},
        }
    )
    return payload


def _voice_audio_count(source: JianyingDraftInput, audio_path: str | None) -> int:
    if source.audio_segments:
        return sum(1 for segment in source.audio_segments if not _is_bgm_track(segment.track_name))
    return 1 if audio_path else 0


def _bgm_audio_count(audio_segments: list[JianyingAudioSegment]) -> int:
    return sum(1 for segment in audio_segments if _is_bgm_track(segment.track_name))


def _draft_effects_manifest(
    source: JianyingDraftInput, *, huazi_segments: int
) -> dict[str, Any]:
    video_effects = []
    for segment in source.video_segments:
        effects = _segment_effects(segment)
        if effects:
            video_effects.append(
                {
                    "track_name": segment.track_name,
                    "asset_id": segment.asset_id,
                    "clip_id": segment.clip_id,
                    **effects,
                }
            )
    return {
        "video_segments": video_effects,
        "huazi_segments": huazi_segments,
        "manual_acceptance_required": bool(video_effects or huazi_segments),
        "effect_id_policy": "cutflow_effects metadata is authoritative when Jianying native effect_id drifts.",
    }


def _is_broll_track(track_name: str) -> bool:
    normalized = track_name.lower()
    return (
        "b-roll" in normalized
        or "broll" in normalized
        or "b_roll" in normalized
        or "覆盖" in track_name
    )


def _is_bgm_track(track_name: str) -> bool:
    normalized = track_name.lower()
    return "bgm" in normalized or "background" in normalized or "配乐" in track_name


def _is_huazi_track(track_name: str) -> bool:
    normalized = track_name.lower()
    return "huazi" in normalized or "花字" in track_name


def _material_names(materials: list[dict[str, Any]], key: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for material in materials:
        raw = str(material.get(key) or material.get("path") or "")
        name = Path(raw).name
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _base_segment(material_id: str, start: int, duration: int) -> dict[str, Any]:
    return {
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": 1.0,
        "reverse": False,
        "track_attribute": 0,
        "track_render_index": 0,
        "visible": True,
        "id": uuid.uuid4().hex,
        "material_id": material_id,
        "target_timerange": {"start": int(start), "duration": int(duration)},
        "common_keyframes": [],
        "keyframe_refs": [],
    }


def _track(track_type: str, name: str, render_index: int, segments: list[dict[str, Any]]) -> dict[str, Any]:
    for segment in segments:
        segment["render_index"] = render_index
    return {"attribute": 0, "flag": 0, "id": uuid.uuid4().hex, "is_default_name": False, "name": name, "segments": segments, "type": track_type}


def _video_material(path: str, duration_us: int, width: int, height: int) -> dict[str, Any]:
    material_id = uuid.uuid4().hex
    return {
        "audio_fade": None,
        "category_id": "",
        "category_name": "local",
        "check_flag": 63487,
        "crop": {"upper_left_x": 0.0, "upper_left_y": 0.0, "upper_right_x": 1.0, "upper_right_y": 0.0, "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0, "lower_right_y": 1.0},
        "crop_ratio": "free",
        "crop_scale": 1.0,
        "duration": int(duration_us),
        "height": int(height),
        "id": material_id,
        "local_material_id": "",
        "material_id": material_id,
        "material_name": Path(path).name,
        "media_path": "",
        "path": path,
        "type": "video",
        "width": int(width),
    }


def _audio_material(path: str, duration_us: int) -> dict[str, Any]:
    material_id = uuid.uuid4().hex
    return {"app_id": 0, "category_id": "", "category_name": "local", "check_flag": 3, "copyright_limit_type": "none", "duration": int(duration_us), "effect_id": "", "formula_id": "", "id": material_id, "local_material_id": material_id, "music_id": material_id, "name": Path(path).name, "path": path, "source_platform": 0, "type": "extract_music", "wave_points": []}


def _speed_material(speed_id: str, speed: float) -> dict[str, Any]:
    return {"curve_speed": None, "id": speed_id, "mode": 0, "speed": speed, "type": "speed"}


def _text_material(material_id: str, text: str) -> dict[str, Any]:
    content = {
        "styles": [{"fill": {"alpha": 1.0, "content": {"render_type": "solid", "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]}}}, "range": [0, len(text)], "size": 8.0, "bold": False, "italic": False, "underline": False, "strokes": [{"content": {"solid": {"alpha": 1.0, "color": [0.0, 0.0, 0.0]}}, "width": 0.072}]}],
        "text": text,
    }
    return {"id": material_id, "content": json.dumps(content, ensure_ascii=False), "typesetting": 0, "alignment": 1, "letter_spacing": 0.0, "line_spacing": 0.02, "line_feed": 1, "line_max_width": 0.86, "force_apply_line_max_width": False, "check_flag": 15, "type": "subtitle", "global_alpha": 1.0}


def _clip(transform_y: float = 0.0) -> dict[str, Any]:
    return {
        "alpha": 1.0,
        "flip": {"horizontal": False, "vertical": False},
        "rotation": 0.0,
        "scale": {"x": 1.0, "y": 1.0},
        "transform": {"x": 0.0, "y": transform_y},
    }
