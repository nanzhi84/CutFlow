"""Fallback price catalog for local cost estimation.

These rates mirror the original digital-human-Cutagent fixed catalog
(TTS 0.15 CNY / 1k characters, lip-sync 5.0 CNY / minute). They let the
cost-estimate endpoints return a real number even when no provider price
catalog has been published yet (gateway UNCONFIGURED). When an operator
publishes a catalog the configured price always takes precedence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from packages.core.contracts import Money

# Capability identifiers shared with the gateway/jobs estimate.
TTS_CAPABILITY_ID = "tts.speech"
LIPSYNC_CAPABILITY_ID = "lipsync.video"
IMAGE_CAPABILITY_ID = "image.generate"

# Unit identifiers. TTS is priced per character (matching the gateway's
# input_token unit); lip-sync is priced per media_second -- the only
# catalog-persistable unit for lipsync.video (ProviderPriceItem.unit) and the
# unit the gateway actually bills (provider_gateway media_second), so a
# published lip-sync price can match and the estimate mirrors real billing;
# image is per call.
TTS_UNIT = "input_token"
LIPSYNC_UNIT = "media_second"
IMAGE_UNIT = "call"

# Origin fixed rates.
TTS_COST_PER_1K_CHARS = Decimal("0.15")
LIPSYNC_COST_PER_MINUTE = Decimal("5.0")
IMAGE_COST_PER_IMAGE = Decimal("0.4")  # image:gpt-image-2[-all] = 0.4 CNY / image

# Derived per-unit prices.
TTS_PRICE_PER_CHAR = TTS_COST_PER_1K_CHARS / Decimal(1000)  # 0.00015
LIPSYNC_PRICE_PER_MINUTE = LIPSYNC_COST_PER_MINUTE  # 5.0 (kept for reference)
LIPSYNC_PRICE_PER_SECOND = LIPSYNC_COST_PER_MINUTE / Decimal(60)  # 5.0 CNY/min expressed per media_second
IMAGE_PRICE_PER_CALL = IMAGE_COST_PER_IMAGE  # 0.4

DEFAULT_PROVIDER_ID = "default"
DEFAULT_CURRENCY = "CNY"


@dataclass(frozen=True)
class DefaultPriceItem:
    """A minimal price item independent of the persisted catalog rows."""

    provider_id: str
    capability_id: str
    unit: str
    unit_price: Money


DEFAULT_TTS_PRICE = DefaultPriceItem(
    provider_id=DEFAULT_PROVIDER_ID,
    capability_id=TTS_CAPABILITY_ID,
    unit=TTS_UNIT,
    unit_price=Money(amount=TTS_PRICE_PER_CHAR, currency=DEFAULT_CURRENCY),
)

DEFAULT_LIPSYNC_PRICE = DefaultPriceItem(
    provider_id=DEFAULT_PROVIDER_ID,
    capability_id=LIPSYNC_CAPABILITY_ID,
    unit=LIPSYNC_UNIT,
    unit_price=Money(amount=LIPSYNC_PRICE_PER_SECOND, currency=DEFAULT_CURRENCY),
)

DEFAULT_IMAGE_PRICE = DefaultPriceItem(
    provider_id=DEFAULT_PROVIDER_ID,
    capability_id=IMAGE_CAPABILITY_ID,
    unit=IMAGE_UNIT,
    unit_price=Money(amount=IMAGE_PRICE_PER_CALL, currency=DEFAULT_CURRENCY),
)


def default_price_for(capability_id: str) -> DefaultPriceItem | None:
    """Return the fallback price item for a capability, if any."""
    if capability_id == TTS_CAPABILITY_ID:
        return DEFAULT_TTS_PRICE
    if capability_id == LIPSYNC_CAPABILITY_ID:
        return DEFAULT_LIPSYNC_PRICE
    if capability_id == IMAGE_CAPABILITY_ID:
        return DEFAULT_IMAGE_PRICE
    return None


__all__ = [
    "TTS_CAPABILITY_ID",
    "LIPSYNC_CAPABILITY_ID",
    "IMAGE_CAPABILITY_ID",
    "TTS_UNIT",
    "LIPSYNC_UNIT",
    "IMAGE_UNIT",
    "TTS_COST_PER_1K_CHARS",
    "LIPSYNC_COST_PER_MINUTE",
    "IMAGE_COST_PER_IMAGE",
    "TTS_PRICE_PER_CHAR",
    "LIPSYNC_PRICE_PER_MINUTE",
    "LIPSYNC_PRICE_PER_SECOND",
    "IMAGE_PRICE_PER_CALL",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_CURRENCY",
    "DefaultPriceItem",
    "DEFAULT_TTS_PRICE",
    "DEFAULT_LIPSYNC_PRICE",
    "DEFAULT_IMAGE_PRICE",
    "default_price_for",
]
