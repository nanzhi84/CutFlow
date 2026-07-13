"""Portrait-clip lip-sync usability predicate.

A portrait candidate is a clip-level talking-head window. This module owns the
single question "can this clip serve as a lip-sync source", which the material
pack and the editing planners both ask. Pure.
"""

from __future__ import annotations

from packages.planning.material.subject_terms import PERSON_SUBJECT_TERMS

# A lip-sync source window shorter than this is too small to anchor a narration
# chunk and would only fragment the portrait track, so it is never offered.
_MIN_LIPSYNC_CLIP_SEC = 0.6


def clip_is_lip_sync_usable(clip) -> bool:
    """Whether one ``ClipV4`` can serve as a lip-sync source window."""
    usage = clip.usage
    if usage.role.value == "avoid":
        return False
    if usage.voiceover_only:
        return False
    fcm = clip.semantics.face_count_max
    if fcm is not None and fcm > 1:
        return False
    if (float(clip.end) - float(clip.start)) < _MIN_LIPSYNC_CLIP_SEC:
        return False
    if usage.recommended_for_lip_sync:
        return True
    return _looks_like_static_lipsync_source(clip)


def _looks_like_static_lipsync_source(clip) -> bool:
    sem = clip.semantics
    subject = (sem.subject_type or "").lower()
    if not any(term in subject for term in PERSON_SUBJECT_TERMS):
        return False
    if sem.contains_face is False and sem.face_count_max == 0:
        return False
    orientation = (sem.body_orientation or "").lower()
    return (
        sem.mouth_visible is True
        or sem.gaze_to_camera is True
        or "frontal" in orientation
        or "camera" in orientation
    )


