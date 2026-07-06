"""V4 window prompt construction + VLM response parsing (with failure classification).

Ported from the b-roll analyzer's ``annotation_v4_prompt`` (the VLM/"brain" half
that the pure-CV sensor suite in this package does NOT cover):

- :func:`build_window_prompt`  - one unified prompt (portrait/b-roll share it,
  guiding which semantic fields to fill by material_type). Injects the whole-clip
  ASR text (cross-window text channel) and the window's sensor signals, and
  explicitly tells the VLM that black/freeze/blur/shake are owned by the sensor
  layer (it must NOT judge those).
- :func:`parse_window_response` - parse raw VLM text -> ``list[ClipV4]``, raising
  :class:`~packages.media.annotation.errors.SchemaError` /
  :class:`~packages.media.annotation.errors.SemanticError` per the V4 taxonomy.

Discipline: the parser only classifies "format vs semantics" and raises the
matching error; it does NO degraded back-fill (V4 has no needs_review / default
fallbacks). ``RuntimeVLMError`` is the caller's concern (network/5xx), never raised here.
"""

from __future__ import annotations

import json
import re
from typing import Any, get_args

from pydantic import ValidationError

from packages.core.contracts import ClipSemanticsV4, ClipV4, UsageRole

from .errors import SchemaError, SemanticError
from ._material import material_class

# Semantic-check thresholds (parser defaults).
_MIN_CONFIDENCE = 0.3
_WINDOW_BOUND_TOL = 0.5  # tolerance (sec) for a segment exceeding the window bounds
_COVERAGE_GAP_TOL = 0.75  # tolerance (sec) for a gap in window coverage

# Legal role tokens (kept faithful to origin; cover is the b-roll voiceover role).
_LEGAL_ROLES = {role.value for role in UsageRole}


# build_window_prompt


def build_window_prompt(
    *,
    material_type: str,
    window_start: float,
    window_end: float,
    sensor_signals: dict[str, Any],
    full_asr_text: str,
    retry_hint: str = "",
) -> str:
    """Build the VLM annotation prompt for a single window.

    portrait / b-roll / video share this function; only "which semantic fields to
    fill" differs. For the unified ``video`` bucket the VLM is asked to judge EACH
    segment independently as talking-head vs cutaway and fill the matching side, so
    one mixed clip yields both lip-sync-usable and cover-usable segments. The prompt
    and emitted JSON contract stay faithful to the origin.
    """
    cls = material_class(material_type)

    if cls == "portrait":
        semantics_guide = (
            "口播专属语义字段(填到 segment.semantics):\n"
            "  subject_type / scene_type / gaze_to_camera(是否直视镜头) / "
            "mouth_visible(嘴部是否可见) / mouth_moving(是否在说话) / "
            "gesture_type / body_orientation / emotion_state / "
            "speaker_intent(口播意图) / speech_action_alignment(话术与动作一致性) / "
            "retake_cue(重拍/笑场提示)"
        )
        retrieval_guide = (
            "retrieval 填写指南:\n"
            "  retrieval.summary 必须写详细的人物外观与环境描述,包括衣服颜色/款式/材质、"
            "发型、配饰(眼镜/首饰/帽子)、背景环境与陈设,写细不限长;\n"
            "  retrieval.keywords 并入外观/实体词,如\"白色衬衫\"\"金丝眼镜\";"
            "retrieval_sentence 保持一句话检索摘要,不堆细节。"
        )
        role_hint = "role 取值: hook(开场钩子)/main(正片口播)/backup(备用)/avoid(不建议)"
    elif cls == "video":
        semantics_guide = (
            "这是【混合视频】:同一条素材里可能既有口播(人对着镜头说话)也有空镜/B-roll(转场画面/产品/场景)。\n"
            "请【逐段独立判断】每个 segment 属于哪一类,并只填该类语义字段、其余留默认:\n"
            "  · 口播段(人正对镜头讲话): subject_type / scene_type / gaze_to_camera / "
            "mouth_visible / mouth_moving / gesture_type / body_orientation / emotion_state / "
            "speaker_intent / speech_action_alignment / retake_cue;\n"
            "  · 空镜/B-roll段(无人讲话/转场/产品/环境): subject_type / scene_type / action / "
            "narrative_role(process_proof/detail_showcase/result_showcase/environment_establish) / "
            "contains_face / process_stage"
        )
        retrieval_guide = (
            "retrieval 填写指南:\n"
            "  口播段按人物外观指南填写:summary 详细写衣服颜色/款式/材质、发型、配饰、"
            "背景环境与陈设,keywords 并入外观/实体词,retrieval_sentence 保持一句话;\n"
            "  空镜/B-roll段按实体指南填写:summary 详细写产品、品牌露出(logo/包装文字)、"
            "场景物件、工序/动作对象,以及画面中人物外观(若有),实体词并入 keywords,"
            "retrieval_sentence 保持一句话。"
        )
        role_hint = (
            "role 取值: 口播段用 hook(开场钩子)/main(正片口播)/backup(备用);"
            "空镜段用 cover(盖旁白的空镜)/backup;都不可用时用 avoid。\n"
            "usage 要与判断一致:口播段 recommended_for_lip_sync=true(画面是单人正脸正对镜头说话时);"
            "空镜段 recommended_for_lip_sync=false 且通常 voiceover_only=true、role=cover。"
        )
    else:
        semantics_guide = (
            "B-roll 专属语义字段(填到 segment.semantics):\n"
            "  subject_type / scene_type / action(主要动作) / "
            "narrative_role(叙事角色:process_proof/detail_showcase/result_showcase/environment_establish) / "
            "contains_face(是否含人脸) / process_stage(工序阶段)"
        )
        retrieval_guide = (
            "retrieval 填写指南:\n"
            "  retrieval.summary 重点详细描述实体:产品、品牌露出(logo/包装文字)、场景物件、"
            "工序/动作对象、画面中人物外观(若有),写细不限长;\n"
            "  retrieval.keywords 并入产品、品牌、物件、工序动作、人物外观等实体词;"
            "retrieval_sentence 保持一句话检索摘要。"
        )
        role_hint = "role 取值: cover(盖旁白的空镜)/backup(备用)/avoid(不建议)"

    sensor_json = json.dumps(sensor_signals or {}, ensure_ascii=False)

    prompt = f"""你是数字人素材的 V4 结构化标注器。只分析这一个时间窗,不要分析窗口以外的内容。

分析窗口: [{window_start}s, {window_end}s]

整片台本(ASR 转写,用于理解上下文/留悬念等跨段语义,不要求转写校对):
{full_asr_text or "(无)"}

该窗传感器信号(切点/人声段/已知质量事件位置,供你对齐边界,不要重复判定):
{sensor_json}

重要免责:black / freeze / blur / shake 等画面质量问题已由传感器层负责检测,
你**不要**判断这些;只输出语义判断(笑场/转身/离镜/能否当 hook/话术-动作一致性等)。

{semantics_guide}
{retrieval_guide}
{role_hint}

只返回一个 JSON 对象(不要 Markdown 代码块包装、不要解释),结构:
{{
  "segments": [
    {{
      "start": <窗口内秒>, "end": <窗口内秒>,
      "semantics": {{ ... 见上 ... }},
      "visual": {{ "shot_scale": "...", "camera_motion": "...", "composition": "..." }},
      "usage": {{ "recommended_for_lip_sync": <bool>, "recommended_for_voiceover": <bool>,
                  "voiceover_only": <bool>, "role": "<上面的 role 之一>" }},
      "retrieval": {{ "summary": "...(写详细描述)", "keywords": ["..."], "retrieval_sentence": "..." }},
      "confidence": <0~1>
    }}
  ]
}}

要求:
1. segments 必须按时间顺序、连续覆盖整个窗口 [{window_start}, {window_end}],不要留大空隙、不要越界。
2. 每段 start/end 必须落在窗口内且 end > start。
3. role 只能取上面列出的合法值;usage 各布尔要与 role 自洽(如 main 必须可对口型)。
4. confidence 反映你对该段判断的把握(不确定就给低分)。"""

    if retry_hint:
        prompt = f"{prompt}\n\n【重试提示】{retry_hint}"
    return prompt


# parse_window_response


def _extract_json_object(raw: str) -> str:
    """Pull the JSON object text out of the raw response (strip a Markdown fence)."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    return text


# String-typed ClipSemanticsV4 fields (derived from the contract, not hardcoded, so it
# can't drift). The VLM occasionally returns a bool/number for one of these (e.g.
# ``retake_cue: false`` meaning "no retake"), which is a benign type quirk — coercing
# it to a string keeps one segment's quirk from failing the whole window's schema
# validation (and the whole asset's annotation). Bool/None -> "" (the "absent" default),
# numbers -> their string form.
_SEMANTIC_STR_FIELDS = frozenset(
    name for name, field in ClipSemanticsV4.model_fields.items() if field.annotation is str
)
_SEMANTIC_BOOL_FIELDS = frozenset(
    name for name, field in ClipSemanticsV4.model_fields.items() if bool in get_args(field.annotation)
)
_BOOL_TRUE_TOKENS = frozenset({"true", "yes", "y", "1", "direct", "是", "有"})
_BOOL_FALSE_TOKENS = frozenset({"false", "no", "n", "0", "否", "无"})
_BOOL_UNKNOWN_TOKENS = frozenset({"", "partial", "unknown", "uncertain", "maybe", "n/a", "na"})


def _coerce_semantics_strings(payload: dict) -> None:
    semantics = payload.get("semantics")
    if not isinstance(semantics, dict):
        return
    for name in _SEMANTIC_STR_FIELDS:
        if name not in semantics:
            continue
        value = semantics[name]
        if isinstance(value, str):
            continue
        semantics[name] = "" if value is None or isinstance(value, bool) else str(value)


def _coerce_semantics_bools(payload: dict) -> None:
    semantics = payload.get("semantics")
    if not isinstance(semantics, dict):
        return
    for name in _SEMANTIC_BOOL_FIELDS:
        if name not in semantics:
            continue
        value = semantics[name]
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in _BOOL_TRUE_TOKENS:
                semantics[name] = True
            elif normalized in _BOOL_FALSE_TOKENS:
                semantics[name] = False
            elif normalized in _BOOL_UNKNOWN_TOKENS:
                semantics[name] = None
        elif isinstance(value, (int, float)):
            semantics[name] = bool(value)


def parse_window_response(
    raw: str,
    *,
    material_type: str,
    window_start: float,
    window_end: float,
    duration: float,
    min_confidence: float = _MIN_CONFIDENCE,
) -> list[ClipV4]:
    """Parse a VLM response -> ``list[ClipV4]``.

    Failure classification (faithful to origin):

    - :class:`SchemaError`: JSON not extractable/parseable, no ``segments``, segments
      not a list, or a segment fails ClipV4 construction (reversed time / missing
      usage / illegal role).
    - :class:`SemanticError`: well-formed but unreasonable - empty segments (no
      coverage), a segment out of window, confidence too low, role/lip_sync
      contradiction, or a large coverage gap.
    """
    text = _extract_json_object(raw)

    # --- (1) format layer: JSON parseable + structurally correct ---
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SchemaError(f"VLM response is not parseable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaError("VLM response top level is not a JSON object")
    segments = _segments_from_payload(data)
    if segments is None:
        raise SchemaError("VLM response is missing the segments field")

    # Empty segments = "no window coverage at all" = semantic, not format.
    if not segments:
        raise SemanticError("segments is empty, no window coverage")

    # --- (2) build each ClipV4 (pydantic failure -> SchemaError) ---
    clips: list[ClipV4] = []
    confidences: list[float | None] = []
    for idx, seg in enumerate(segments):
        if not isinstance(seg, dict):
            raise SchemaError(f"segment {idx} is not an object")
        raw_conf = seg.get("confidence")
        try:
            parsed_conf = float(raw_conf) if raw_conf is not None else None
        except (TypeError, ValueError):
            parsed_conf = None
        payload = {k: v for k, v in seg.items() if k != "confidence"}
        if parsed_conf is not None:
            payload["confidence"] = max(0.0, min(1.0, parsed_conf))
        start = payload.get("start")
        end = payload.get("end")
        try:
            f_start = float(start)
            f_end = float(end)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"segment {idx} start/end is not numeric") from exc
        payload["segment_id"] = f"w{window_start:.3f}_{window_end:.3f}_seg{idx}"
        payload["start"] = f_start
        payload["end"] = f_end
        payload["duration"] = round(f_end - f_start, 3)
        _coerce_semantics_strings(payload)
        _coerce_semantics_bools(payload)
        try:
            clip = ClipV4(**payload)
        except (ValidationError, ValueError, TypeError) as exc:
            raise SchemaError(f"segment {idx} does not match ClipV4 schema: {exc}") from exc
        clips.append(clip)
        confidences.append(parsed_conf)

    # --- (3) semantic layer checks ---
    for clip, conf in zip(clips, confidences):
        if clip.start < window_start - _WINDOW_BOUND_TOL or clip.end > window_end + _WINDOW_BOUND_TOL:
            raise SemanticError(
                f"segment [{clip.start},{clip.end}] is out of window [{window_start},{window_end}]"
            )
        if conf is not None and conf < min_confidence:
            raise SemanticError(f"segment confidence={conf} below threshold {min_confidence}")
        if clip.usage.role == UsageRole.main and not clip.usage.recommended_for_lip_sync:
            raise SemanticError("role=main but recommended_for_lip_sync=False, self-contradiction")

    # --- (4) window coverage integrity (large gap -> semantic) ---
    _assert_window_coverage(clips, window_start, window_end)

    return clips


def _segments_from_payload(data: dict, *, depth: int = 0) -> list | None:
    if depth > 2:
        return None
    if "segments" in data:
        segments = data["segments"]
        if not isinstance(segments, list):
            raise SchemaError("segments is not a list")
        return segments
    if "clips" in data:
        clips = data["clips"]
        if not isinstance(clips, list):
            raise SchemaError("clips is not a list")
        return clips
    for key in ("canonical", "output", "result", "annotation", "data"):
        nested = data.get(key)
        if isinstance(nested, dict):
            segments = _segments_from_payload(nested, depth=depth + 1)
            if segments is not None:
                return segments
    return None


def _assert_window_coverage(clips: list[ClipV4], window_start: float, window_end: float) -> None:
    """Assert the clips' union contiguously covers the window; a big gap raises SemanticError."""
    ordered = sorted(clips, key=lambda c: c.start)
    if ordered[0].start - window_start > _COVERAGE_GAP_TOL:
        raise SemanticError(f"window start uncovered: first start={ordered[0].start} > {window_start}")
    max_end = max(c.end for c in ordered)
    if window_end - max_end > _COVERAGE_GAP_TOL:
        raise SemanticError(f"window end uncovered: last end={max_end} < {window_end}")
    cursor = ordered[0].end
    for clip in ordered[1:]:
        if clip.start - cursor > _COVERAGE_GAP_TOL:
            raise SemanticError(f"large gap inside window: [{cursor},{clip.start}]")
        cursor = max(cursor, clip.end)
