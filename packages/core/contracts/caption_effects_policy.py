"""Shared caption effect/role policy loaded from the contract JSON source."""

from __future__ import annotations

import json
from pathlib import Path

_POLICY = json.loads(
    Path(__file__).with_name("caption_effects.json").read_text(encoding="utf-8")
)

CAPTION_EFFECT_ROLES: dict[str, frozenset[str]] = {
    str(effect_id): frozenset(str(role) for role in value["roles"])
    for effect_id, value in _POLICY.items()
}
CAPTION_EFFECT_IDS = frozenset(CAPTION_EFFECT_ROLES)


def caption_effect_roles(effect_id: str) -> frozenset[str]:
    return CAPTION_EFFECT_ROLES.get(effect_id, frozenset())
