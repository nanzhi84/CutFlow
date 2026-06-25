"""Single source of truth for material-class routing in the V4 annotation pipeline.

Replaces the per-module ``is_portrait`` predicates (pipeline / vlm / report /
_assemble) that had drifted apart (some keyed on the ``talk`` substring, one on a
``talking``/``talking_head`` set). A ``material_type`` string (the asset ``kind``)
maps to exactly one of three classes:

- ``portrait`` — dedicated talking-head footage.
- ``broll``    — dedicated cutaway / scenery footage.
- ``video``    — the unified bucket: mixed footage whose clips are classified
                 per-clip (lip-sync portrait vs cover b-roll) by the annotator,
                 so the operator never pre-classifies A-roll vs B-roll at upload.

The ``video`` class runs the FULL sensor suite (VAD speech islands + multi-face,
like portrait) and a superset VLM prompt, so every clip carries both portrait
usability (``recommended_for_lip_sync`` + authoritative ``face_count_max``) and
b-roll semantics. Downstream selection then picks clips by per-clip usability
rather than by asset kind. Frame sampling / boundary refinement intentionally
treat ``video`` like b-roll (capped sampling is ample for face detection; split —
not drop — preserves both A-roll and B-roll segments of one mixed clip).
"""

from __future__ import annotations

from typing import Literal

MaterialClass = Literal["portrait", "broll", "video"]

# Unified video bucket markers (asset kind ``video`` + a few human synonyms).
_VIDEO_TOKENS = ("video", "视频", "mixed", "混合")
# Dedicated talking-head markers.
_PORTRAIT_TOKENS = ("portrait", "口播", "talk")


def material_class(material_type: str) -> MaterialClass:
    """Classify a material_type string into the canonical 3-way material class."""
    mt = str(material_type or "").strip().lower()
    if any(tok in mt for tok in _VIDEO_TOKENS):
        return "video"
    if any(tok in mt for tok in _PORTRAIT_TOKENS):
        return "portrait"
    return "broll"


def is_video(material_type: str) -> bool:
    """True for the unified video bucket (per-clip classification)."""
    return material_class(material_type) == "video"


def runs_speech_and_face(material_type: str) -> bool:
    """Whether to run the VAD speech-island + multi-face sensors.

    Both the portrait class and the unified video bucket need them (lip-sync
    boundary snapping + single-face gating); dedicated b-roll does not.
    """
    return material_class(material_type) in {"portrait", "video"}
