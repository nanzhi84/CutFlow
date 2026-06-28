from __future__ import annotations

import json
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any


def empty_materials() -> dict[str, list[Any]]:
    return {
        "ai_translates": [],
        "audio_balances": [],
        "audio_effects": [],
        "audio_fades": [],
        "audio_track_indexes": [],
        "audios": [],
        "beats": [],
        "canvases": [],
        "chromas": [],
        "color_curves": [],
        "digital_humans": [],
        "drafts": [],
        "effects": [],
        "flowers": [],
        "green_screens": [],
        "handwrites": [],
        "hsl": [],
        "images": [],
        "log_color_wheels": [],
        "loudnesses": [],
        "manual_deformations": [],
        "masks": [],
        "material_animations": [],
        "material_colors": [],
        "multi_language_refs": [],
        "placeholders": [],
        "plugin_effects": [],
        "primary_color_wheels": [],
        "realtime_denoises": [],
        "shapes": [],
        "smart_crops": [],
        "smart_relights": [],
        "sound_channel_mappings": [],
        "speeds": [],
        "stickers": [],
        "tail_leaders": [],
        "text_templates": [],
        "texts": [],
        "time_marks": [],
        "transitions": [],
        "video_effects": [],
        "video_trackings": [],
        "videos": [],
        "vocal_beautifys": [],
        "vocal_separations": [],
    }


def draft_content(
    *,
    draft_id: str,
    draft_name: str,
    width: int,
    height: int,
    duration_us: int,
    created_us: int,
    materials: dict[str, Any],
    tracks: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "canvas_config": {"height": height, "ratio": "original", "width": width},
        "color_space": 0,
        "config": {
            "adjust_max_index": 1,
            "attachment_info": [],
            "combination_max_index": 1,
            "export_range": None,
            "extract_audio_last_index": 1,
            "lyrics_recognition_id": "",
            "lyrics_sync": True,
            "lyrics_taskinfo": [],
            "maintrack_adsorb": False,
            "material_save_mode": 0,
            "multi_language_current": "none",
            "multi_language_list": [],
            "multi_language_main": "none",
            "multi_language_mode": "none",
            "original_sound_last_index": 1,
            "record_audio_last_index": 1,
            "sticker_max_index": 1,
            "subtitle_keywords_config": None,
            "subtitle_recognition_id": "",
            "subtitle_sync": True,
            "subtitle_taskinfo": [],
            "system_font_list": [],
            "video_mute": False,
            "zoom_info_params": None,
        },
        "cover": None,
        "create_time": created_us,
        "duration": duration_us,
        "extra_info": None,
        "fps": 30.0,
        "free_render_index_mode_on": False,
        "group_container": None,
        "id": draft_id,
        "keyframe_graph_list": [],
        "keyframes": {
            "adjusts": [],
            "audios": [],
            "effects": [],
            "filters": [],
            "handwrites": [],
            "stickers": [],
            "texts": [],
            "videos": [],
        },
        "last_modified_platform": {"app_id": 3704, "app_source": "lv", "app_version": "5.9.0", "os": "windows"},
        "platform": {"app_id": 3704, "app_source": "lv", "app_version": "5.9.0", "os": "windows"},
        "materials": materials,
        "mutable_config": None,
        "name": draft_name,
        "new_version": "110.0.0",
        "relationships": [],
        "render_index_track_mode_on": False,
        "retouch_cover": None,
        "source": "default",
        "static_cover_image_path": "",
        "time_marks": None,
        "tracks": tracks,
        "update_time": created_us,
        "version": 360000,
    }


def subtitle_entries(subtitle_path: Path | None, narration_units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if subtitle_path and subtitle_path.exists():
        if subtitle_path.suffix.lower() == ".srt":
            return _parse_srt_entries(subtitle_path)
        if subtitle_path.suffix.lower() == ".ass":
            entries = _parse_ass_entries(subtitle_path)
            if entries:
                return entries
    entries = []
    for unit in narration_units:
        text = str(unit.get("text") or unit.get("content") or "").strip()
        if not text:
            continue
        start = _sec_to_us(float(unit.get("start") or unit.get("start_sec") or 0))
        end = _sec_to_us(float(unit.get("end") or unit.get("end_sec") or 0))
        if end > start:
            entries.append({"start_us": start, "end_us": end, "text": text})
    return entries


def ensure_supporting_files(draft_dir: Path) -> None:
    for directory in ["Resources", "common_attachment", "subdraft", "adjust_mask", "matting", "qr_upload", "smart_crop", ".backup"]:
        (draft_dir / directory).mkdir(parents=True, exist_ok=True)
    supporting = {
        "attachment_pc_common.json": _default_attachment_pc_common(),
        "attachment_editing.json": _default_attachment_editing(),
        "draft_agency_config.json": {"is_auto_agency_enabled": False, "is_auto_agency_popup": False, "is_single_agency_mode": False, "marterials": None, "use_converter": False, "video_resolution": 720},
        "draft_biz_config.json": {},
        "draft_virtual_store.json": {"draft_materials": [], "draft_virtual_store": [{"type": 0, "value": [{"creation_time": 0, "display_name": "", "filter_type": 0, "id": "", "import_time": 0, "import_time_us": 0, "sort_sub_type": 0, "sort_type": 0}]}, {"type": 1, "value": [{"child_id": str(uuid.uuid4()), "parent_id": ""}]}, {"type": 2, "value": []}]},
        "key_value.json": {},
    }
    for filename, payload in supporting.items():
        dump_json(draft_dir / filename, payload)
    for filename, payload in _common_attachment_files().items():
        dump_json(draft_dir / "common_attachment" / filename, payload)
    content_text = (draft_dir / "draft_content.json").read_text(encoding="utf-8")
    for alias in ("template.tmp", "template-2.tmp", "draft_info.json.bak"):
        (draft_dir / alias).write_text(content_text, encoding="utf-8")


def draft_meta(draft_name: str, draft_id: str, duration_us: int, size: int, created_us: int) -> dict[str, Any]:
    return {
        "draft_fold_path": draft_name,
        "draft_id": draft_id,
        "draft_name": draft_name,
        "draft_root_path": ".",
        "draft_cover": "",
        "draft_new_version": "",
        "draft_timeline_materials_size_": size,
        "tm_draft_create": created_us,
        "tm_draft_modified": created_us,
        "tm_duration": duration_us,
    }


def root_meta(draft_name: str, draft_id: str, duration_us: int, size: int, created_us: int) -> dict[str, Any]:
    return {
        "all_draft_store": [
            {
                "draft_cloud_last_action_download": False,
                "draft_cloud_purchase_info": "",
                "draft_cloud_template_id": "",
                "draft_cloud_tutorial_info": "",
                "draft_cloud_videocut_purchase_info": "",
                "draft_cover": "",
                "draft_fold_path": draft_name,
                "draft_id": draft_id,
                "draft_is_ai_shorts": False,
                "draft_is_invisible": False,
                "draft_json_file": f"{draft_name}/draft_content.json",
                "draft_name": draft_name,
                "draft_new_version": "",
                "draft_root_path": ".",
                "draft_timeline_materials_size": size,
                "draft_type": "",
                "tm_draft_cloud_completed": "",
                "tm_draft_cloud_modified": 0,
                "tm_draft_create": created_us,
                "tm_draft_modified": created_us,
                "tm_draft_removed": 0,
                "tm_duration": duration_us,
            }
        ],
        "draft_ids": 1,
        "root_path": ".",
    }


def folder_size_bytes(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def zip_root(root: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root).as_posix())


def _parse_srt_entries(srt_path: Path) -> list[dict[str, Any]]:
    content = srt_path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", content.strip())
    entries: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.strip("\ufeff").strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        time_line = lines[1] if "-->" in lines[1] else lines[0]
        text_lines = lines[2:] if "-->" in lines[1] else lines[1:]
        if "-->" not in time_line:
            continue
        start_text, end_text = [part.strip() for part in time_line.split("-->", 1)]
        text = "\n".join(text_lines).strip()
        if text:
            entries.append({"start_us": _timestamp_to_us(start_text), "end_us": _timestamp_to_us(end_text), "text": text})
    return entries


def _parse_ass_entries(ass_path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in ass_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) < 10:
            continue
        text = re.sub(r"\{[^}]*\}", "", fields[9]).replace("\\N", "\n").strip()
        if text:
            entries.append({"start_us": _timestamp_to_us(fields[1]), "end_us": _timestamp_to_us(fields[2]), "text": text})
    return entries


def _timestamp_to_us(timestamp: str) -> int:
    timestamp = timestamp.strip().replace(",", ".")
    hours, minutes, seconds = timestamp.split(":")
    return int(round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1_000_000))


def _sec_to_us(value: float | int | None) -> int:
    return int(round(float(value or 0) * 1_000_000))


def _default_attachment_pc_common() -> dict[str, Any]:
    report = {"caption_id_list": [], "commercial_material": "", "material_source": "", "method": "", "page_from": "", "style": "", "task_id": "", "text_style": "", "tos_id": "", "video_category": ""}
    return {"ai_packaging_infos": [], "ai_packaging_report_info": dict(report), "broll": {"ai_packaging_infos": [], "ai_packaging_report_info": dict(report)}, "commercial_music_category_ids": [], "pc_feature_flag": 0, "recognize_tasks": [], "reference_lines_config": {"horizontal_lines": [], "is_lock": False, "is_visible": False, "player_ruler_type": 0, "player_ruler_visible": False, "vertical_lines": []}, "safe_area_type": 0, "template_item_infos": [], "unlock_template_ids": []}


def _default_attachment_editing() -> dict[str, Any]:
    return {"editing_draft": {"ai_remove_filter_words": {"enter_source": "", "right_id": ""}, "ai_shorts_info": {"report_params": "", "type": 0}, "digital_human_template_to_video_info": {"has_upload_material": False, "template_type": 0}, "draft_used_recommend_function": "", "edit_type": 0, "eye_correct_enabled_multi_face_time": 0, "has_adjusted_render_layer": False, "is_open_expand_player": False, "is_use_adjust": False, "is_use_edit_multi_camera": False, "is_use_lock_object": False, "is_use_loudness_unify": False, "is_use_retouch_face": False, "is_use_smart_adjust_color": False, "is_use_smart_motion": False, "is_use_text_to_audio": False, "material_edit_session": {"material_edit_info": [], "session_id": "", "session_time": 0}, "profile_entrance_type": "", "publish_enter_from": "", "publish_type": "", "single_function_type": 0, "text_convert_case_types": [], "version": "1.0.0", "video_recording_create_draft": ""}}


def _common_attachment_files() -> dict[str, dict[str, Any]]:
    return {"aigc_aigc_generate.json": {"aigc_aigc_generate": {"aigc_generate_segment_list": [], "version": "1.0.0"}}, "attachment_script_video.json": {"script_video": {"attachment_valid": False, "language": "", "overdub_recover": [], "overdub_sentence_ids": [], "parts": [], "sync_subtitle": False, "translate_segments": [], "translate_type": "", "version": "1.0.0"}}, "attachment_plugin_draft.json": {"plugin_draft": {"plugin_segments": [], "version": "1.0.0"}}}
