"""Publish scheduled-at validation + tags normalization.

Side-effect-free pure-logic helpers (``normalize_scheduled_at`` /
``normalize_publish_tags``) for Asia/Shanghai scheduling (§23.7) and tag cleanup.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from packages.core.contracts import ErrorCode
from packages.core.workflow import NodeExecutionError

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


# scheduled-at (Asia/Shanghai) validation


def normalize_scheduled_at(
    mode: str,
    value: datetime | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Normalize the publish ``scheduled_at`` for ``mode``.

    - ``immediate`` -> always ``None``.
    - ``scheduled`` -> require a value, interpret naive datetimes as Asia/Shanghai,
      convert tz-aware ones to Asia/Shanghai, and reject non-future times.

    Raises ``validation.invalid_options`` (the API-facing validation hard-fail).
    """
    if mode != "scheduled":
        return None
    if value is None:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "定时发布必须提供 scheduled_at。",
        )
    scheduled_at = value
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=SHANGHAI_TZ)
    else:
        scheduled_at = scheduled_at.astimezone(SHANGHAI_TZ)
    reference = (now.astimezone(SHANGHAI_TZ) if now else datetime.now(tz=SHANGHAI_TZ))
    if scheduled_at <= reference:
        raise NodeExecutionError(
            ErrorCode.validation_invalid_options,
            "定时时间必须晚于当前北京时间。",
        )
    return scheduled_at


# tags normalization (origin _normalize_publish_tags)


def normalize_publish_tags(tags: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags or []:
        for item in re.split(r"[\s,\n，、;；]+", str(raw_tag or "").strip()):
            cleaned = item.strip().lstrip("#").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized
