"""Annotation PatchService + canonical/projection ownership (Spec §12.1 / §12.2 / §12.3).

The canonical AnnotationV4 is the single source of truth; every projection (UI /
search / material-planning / reporting) is REBUILT from it. The UI only submits a
patch; this module merges the patch into the canonical, producing a NEW canonical
version, then rebuilds the projection from that canonical. b-roll / portrait
selection consume the canonical via ``annotation_v4_for_asset``, so manual edits
to segments / quality_events become visible to material planning (they no longer
land in projection-only).

Strict validation is the quality-gate safety net (Spec §2.3): edited segments are
validated as :class:`ClipV4` and edited quality events as :class:`QualityEventV4`
(illegal time / field / enum / out-of-bounds -> ``artifact.schema_mismatch`` / HTTP
400), never silently coerced or dropped.

Two op families:

- structural (``/canonical/segments``, ``/projection/segments``,
  ``/canonical/clips``, ``/canonical/quality_events``, ``/projection/quality_events``):
  validated + merged into the canonical AnnotationV4 -> new version.
- lightweight (``/labels``, ``/usable``, ``/title``, ``/projection/<k>`` notes):
  preserved exactly as before; they never violate the canonical schema.

Both the in-memory and the SQLAlchemy paths call :func:`apply_patch`, so ownership
is identical on every backend.
"""

from __future__ import annotations

from typing import Any

from packages.core import contracts as c
from packages.core.workflow import NodeExecutionError

# Patch paths whose value is a list of edited clips/segments -> merged into canonical.clips.
_SEGMENT_PATHS = {"/canonical/segments", "/projection/segments", "/canonical/clips"}
# Patch paths whose value is a list of edited quality events -> merged into canonical.quality_events.
_QUALITY_EVENT_PATHS = {"/canonical/quality_events", "/projection/quality_events"}
# Edited BGM windows -> merged into canonical.bgm_usage_windows.
_BGM_WINDOW_PATHS = {"/canonical/bgm_usage_windows", "/projection/bgm_usage_windows"}


def _schema_mismatch(message: str) -> NodeExecutionError:
    return NodeExecutionError(c.ErrorCode.artifact_schema_mismatch, message)


def _is_v4_canonical(canonical: dict[str, Any]) -> bool:
    """A canonical dict is a real AnnotationV4 only when it carries a V4 meta layer."""
    meta = canonical.get("meta") if isinstance(canonical, dict) else None
    return isinstance(meta, dict) and "asset_id" in meta


def _canonical_v4(canonical: dict[str, Any], asset: c.MediaAssetRecord) -> c.AnnotationV4:
    """Parse the stored canonical into AnnotationV4, or mint an empty completed shell.

    A stub canonical (labels-only, pre-V4) is upgraded to an empty AnnotationV4 keyed
    to the asset so segment / quality-event edits have a strict structure to merge into.
    """
    if _is_v4_canonical(canonical):
        try:
            return c.AnnotationV4.model_validate(canonical)
        except Exception as exc:  # malformed stored canonical -> surface as 400
            raise _schema_mismatch(f"存储的标注 canonical 不符合 AnnotationV4 schema: {exc}") from exc
    return c.AnnotationV4(
        meta=c.AnnotationMetaV4(
            asset_id=asset.id,
            case_id=asset.case_id,
            material_type=asset.kind,
            duration=0.0,
            annotation_status=c.AnnotationStatus.completed,
        )
    )


def _validate_clips(raw_segments: Any, duration: float) -> list[c.ClipV4]:
    if not isinstance(raw_segments, list):
        raise _schema_mismatch("segments 必须是数组。")
    clips: list[c.ClipV4] = []
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            raise _schema_mismatch(f"segment[{index}] 必须是对象。")
        item = dict(raw)
        item.setdefault("segment_id", f"seg_edit_{index}")
        # A clip needs a usage role; default to 'cover' when the editor omits it
        # rather than rejecting (role is still validated against the enum below).
        usage = dict(item.get("usage") or {})
        usage.setdefault("role", "cover")
        item["usage"] = usage
        if "duration" not in item:
            try:
                item["duration"] = round(float(item.get("end", 0)) - float(item.get("start", 0)), 3)
            except (TypeError, ValueError):
                item["duration"] = 0.0
        try:
            clip = c.ClipV4.model_validate(item)
        except Exception as exc:
            raise _schema_mismatch(f"segment[{index}] 不符合 ClipV4 schema: {exc}") from exc
        if duration and duration > 0 and (clip.start < 0 or clip.end > duration + 1e-6):
            raise NodeExecutionError(
                c.ErrorCode.render_invalid_timeline,
                f"segment[{index}] 时间 [{clip.start}, {clip.end}] 越界 [0, {duration}]。",
            )
        clips.append(clip)
    return clips


def _validate_quality_events(raw_events: Any, duration: float) -> list[c.QualityEventV4]:
    if not isinstance(raw_events, list):
        raise _schema_mismatch("quality_events 必须是数组。")
    events: list[c.QualityEventV4] = []
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            raise _schema_mismatch(f"quality_event[{index}] 必须是对象。")
        item = dict(raw)
        item.setdefault("event_id", f"qe_edit_{index}")
        item.setdefault("source", "manual")
        try:
            event = c.QualityEventV4.model_validate(item)
        except Exception as exc:
            raise _schema_mismatch(
                f"quality_event[{index}] 不符合 QualityEventV4 schema: {exc}"
            ) from exc
        if duration and duration > 0 and (event.start < 0 or event.end > duration + 1e-6):
            raise NodeExecutionError(
                c.ErrorCode.render_invalid_timeline,
                f"quality_event[{index}] 时间 [{event.start}, {event.end}] 越界 [0, {duration}]。",
            )
        events.append(event)
    return events


def _validate_bgm_windows(raw_windows: Any, duration: float) -> list[c.BgmUsageWindowV4]:
    if not isinstance(raw_windows, list):
        raise _schema_mismatch("bgm_usage_windows 必须是数组。")
    windows: list[c.BgmUsageWindowV4] = []
    for index, raw in enumerate(raw_windows):
        if not isinstance(raw, dict):
            raise _schema_mismatch(f"bgm_usage_window[{index}] 必须是对象。")
        item = dict(raw)
        item.setdefault("segment_id", f"bgm_edit_{index}")
        if "duration" not in item:
            try:
                item["duration"] = round(float(item.get("end", 0)) - float(item.get("start", 0)), 3)
            except (TypeError, ValueError):
                item["duration"] = 0.0
        try:
            window = c.BgmUsageWindowV4.model_validate(item)
        except Exception as exc:
            raise _schema_mismatch(
                f"bgm_usage_window[{index}] 不符合 BgmUsageWindowV4 schema: {exc}"
            ) from exc
        if duration and duration > 0 and (window.start < 0 or window.end > duration + 1e-6):
            raise NodeExecutionError(
                c.ErrorCode.render_invalid_timeline,
                f"bgm_usage_window[{index}] 时间 [{window.start}, {window.end}] 越界 [0, {duration}]。",
            )
        windows.append(window)
    return windows


def build_projection(annotation: c.AnnotationV4, asset: c.MediaAssetRecord, **extra: Any) -> dict[str, Any]:
    """Rebuild the UI projection from the canonical AnnotationV4 (canonical-owns-projection).

    Carries the editor-facing views (segments / quality_events / usable) plus any
    persistence extras (e.g. ``annotation_artifact_id``, ``vlm_configured``).
    """
    is_failed = annotation.meta.annotation_status == c.AnnotationStatus.failed
    projection: dict[str, Any] = {
        "title": asset.title,
        "usable": (not is_failed) and bool(annotation.usage_windows),
        "annotation_status": annotation.meta.annotation_status.value,
        "segments": [clip.model_dump(mode="json") for clip in annotation.clips],
        "quality_events": [ev.model_dump(mode="json") for ev in annotation.quality_events],
        "quality_report": annotation.quality_report,
        "usage_windows": [w.model_dump(mode="json") for w in annotation.usage_windows],
        "bgm_usage_windows": [w.model_dump(mode="json") for w in annotation.bgm_usage_windows],
    }
    projection.update(extra)
    return projection


def apply_patch(
    *,
    canonical: dict[str, Any],
    projection: dict[str, Any],
    asset: c.MediaAssetRecord,
    operations: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge editor operations -> a NEW canonical AnnotationV4 + rebuilt projection.

    Structural edits (segments / quality_events) are validated and written into the
    canonical, then the projection is rebuilt from the canonical (canonical owns the
    projection). Lightweight edits (labels / usable / title / projection notes) are
    applied to the carried projection / canonical without disturbing the V4 layers.

    Returns ``(new_canonical, new_projection)``. Raises ``artifact.schema_mismatch``
    / ``render.invalid_timeline`` (HTTP 400) on any invalid structural edit.
    """
    annotation = _canonical_v4(canonical, asset)
    duration = annotation.meta.duration

    structural_clips: list[c.ClipV4] | None = None
    structural_events: list[c.QualityEventV4] | None = None
    structural_bgm: list[c.BgmUsageWindowV4] | None = None
    light_projection: dict[str, Any] = dict(projection or {})
    light_labels: Any = canonical.get("labels") if isinstance(canonical, dict) else None
    labels_touched = False

    for operation in operations or []:
        op_name = operation.get("op", "replace")
        path = operation.get("path")
        if op_name not in {"add", "replace"} or not isinstance(path, str) or "value" not in operation:
            continue
        value = operation["value"]
        if path in _SEGMENT_PATHS:
            structural_clips = _validate_clips(value, duration)
        elif path in _QUALITY_EVENT_PATHS:
            structural_events = _validate_quality_events(value, duration)
        elif path in _BGM_WINDOW_PATHS:
            structural_bgm = _validate_bgm_windows(value, duration)
        elif path == "/labels":
            light_labels = value
            labels_touched = True
        elif path == "/usable":
            light_projection["usable"] = value
        elif path == "/title":
            light_projection["title"] = value
        elif path.startswith("/projection/"):
            _set_nested(light_projection, [p for p in path.removeprefix("/projection/").split("/") if p], value)
        elif path.startswith("/canonical/"):
            # Non-structural canonical notes (free-form) are tolerated as-is.
            light_projection.setdefault("canonical_notes", {})
            _set_nested(
                light_projection["canonical_notes"],
                [p for p in path.removeprefix("/canonical/").split("/") if p],
                value,
            )

    if structural_clips is not None or structural_events is not None or structural_bgm is not None:
        annotation = annotation.model_copy(
            update={
                "clips": structural_clips if structural_clips is not None else annotation.clips,
                "bgm_usage_windows": structural_bgm
                if structural_bgm is not None
                else annotation.bgm_usage_windows,
                "quality_events": structural_events
                if structural_events is not None
                else annotation.quality_events,
            }
        )
        # Re-validate the whole annotation (time-bounds safety net) -> new version.
        try:
            annotation = c.AnnotationV4.model_validate(annotation.model_dump(mode="json"))
        except Exception as exc:
            raise _schema_mismatch(f"合并后的标注不符合 AnnotationV4 schema: {exc}") from exc

    # AnnotationV4 is strict (extra fields forbidden): labels are an editor-facing tag
    # list, not a V4 canonical layer, so they live in the projection (canonical owns
    # only the seven V4 layers). model_dump(mode="json") therefore never carries extras.
    new_canonical = annotation.model_dump(mode="json")

    if labels_touched:
        light_projection["labels"] = light_labels
    elif "labels" not in light_projection and isinstance(light_labels, (list, dict)):
        light_projection["labels"] = light_labels

    _BUILT = {
        "title",
        "usable",
        "segments",
        "quality_events",
        "quality_report",
        "usage_windows",
        "bgm_usage_windows",
        "annotation_status",
    }
    new_projection = build_projection(
        annotation,
        asset,
        **{k: v for k, v in light_projection.items() if k not in _BUILT},
    )
    # Lightweight title/usable edits override the rebuilt defaults.
    if "title" in light_projection:
        new_projection["title"] = light_projection["title"]
    if "usable" in light_projection:
        new_projection["usable"] = light_projection["usable"]
    return new_canonical, new_projection


def _set_nested(target: dict, parts: list[str], value: Any) -> None:
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    if parts:
        current[parts[-1]] = value
