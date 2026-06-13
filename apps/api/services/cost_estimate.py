"""Standalone TTS / lip-sync cost estimation (pure local catalog math).

These endpoints intentionally work with the provider gateway UNCONFIGURED:
when no published price catalog matches a capability we fall back to the
default catalog (origin fixed rates). A configured catalog always wins.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import Request

from apps.api.common import provider_repository, repository, request_id
from packages.ai.gateway import (
    LIPSYNC_CAPABILITY_ID,
    LIPSYNC_UNIT,
    TTS_CAPABILITY_ID,
    TTS_UNIT,
    default_price_for,
)
from packages.core import contracts as c

# Origin's heuristic: ~5 characters per second of speech.
CHARS_PER_SECOND = Decimal("5")


def estimate_tts_cost(payload: c.TtsCostEstimateRequest, request: Request) -> c.TtsCostEstimateResponse:
    text_length = len(payload.text)
    unit_price, source = _resolve_unit_price(
        request,
        capability_id=TTS_CAPABILITY_ID,
        unit=TTS_UNIT,
        provider_profile_id=payload.provider_profile_id,
    )
    quantity = Decimal(text_length)
    estimate = _line(
        label="TTS 字符",
        capability_id=TTS_CAPABILITY_ID,
        unit=TTS_UNIT,
        quantity=quantity,
        unit_price=unit_price,
    )
    estimated_duration = float(quantity / CHARS_PER_SECOND)
    return c.TtsCostEstimateResponse(
        text_length=text_length,
        estimated_chars=text_length,
        estimated_duration_sec=estimated_duration,
        estimate=estimate,
        pricing_source=source,
        request_id=request_id(),
    )


def estimate_lipsync_cost(
    payload: c.LipsyncCostEstimateRequest, request: Request
) -> c.LipsyncCostEstimateResponse:
    duration_sec = Decimal(str(payload.video_duration_sec))
    minutes = duration_sec / Decimal("60")
    unit_price, source = _resolve_unit_price(
        request,
        capability_id=LIPSYNC_CAPABILITY_ID,
        unit=LIPSYNC_UNIT,
        provider_profile_id=payload.provider_profile_id,
    )
    estimate = _line(
        label="视频时长（秒）",
        capability_id=LIPSYNC_CAPABILITY_ID,
        unit=LIPSYNC_UNIT,
        quantity=duration_sec,
        unit_price=unit_price,
    )
    return c.LipsyncCostEstimateResponse(
        video_duration_sec=payload.video_duration_sec,
        video_duration_min=float(minutes),
        estimate=estimate,
        pricing_source=source,
        request_id=request_id(),
    )


def _resolve_unit_price(
    request: Request,
    *,
    capability_id: str,
    unit: str,
    provider_profile_id: str | None,
) -> tuple[c.Money, str]:
    """Prefer a published catalog price; otherwise use the default rate."""
    preferred_provider = _provider_id_from_profile(request, provider_profile_id)
    catalog_item = _catalog_unit_price(
        request,
        capability_id=capability_id,
        unit=unit,
        preferred_provider_id=preferred_provider,
    )
    if catalog_item is not None:
        return catalog_item, "catalog"
    default = default_price_for(capability_id)
    if default is None or default.unit != unit:
        # Should not happen for the two supported capabilities, but guard anyway.
        return c.zero_money(), "default"
    return default.unit_price, "default"


def _catalog_unit_price(
    request: Request,
    *,
    capability_id: str,
    unit: str,
    preferred_provider_id: str,
) -> c.Money | None:
    candidates = [
        item
        for item in _active_price_items(request)
        if item.unit == unit and item.capability_id in {capability_id, "*"}
    ]
    if not candidates:
        return None
    price_item = next(
        (item for item in candidates if item.provider_id == preferred_provider_id),
        candidates[0],
    )
    return price_item.unit_price


def _active_price_items(request: Request) -> list[c.ProviderPriceItem]:
    provider_repo = provider_repository(request)
    if provider_repo is not None:
        catalogs = provider_repo.list_price_catalogs(active_only=True, limit=200)
        values: list[c.ProviderPriceItem] = []
        for catalog in catalogs:
            values.extend(provider_repo.list_price_items(catalog_id=catalog.id, limit=500))
        return values
    runtime = repository(request)
    published_catalog_ids = {
        catalog.id for catalog in runtime.price_catalogs.values() if catalog.status == "published"
    }
    return [item for item in runtime.price_items.values() if item.catalog_id in published_catalog_ids]


def _provider_id_from_profile(request: Request, profile_id: str | None) -> str:
    """Resolve the catalog provider_id for a requested profile.

    Prefer the STORED ProviderProfile.provider_id (the same value the gateway bills
    against), so this is correct regardless of the profile-id naming convention:
    real profiles like ``minimax.tts.prod`` resolve to ``minimax.tts`` while sandbox
    seeds like ``sandbox.tts.default`` (whose provider_id is just ``sandbox``) resolve
    to ``sandbox``. Fall back to stripping the trailing env segment only when the
    profile is not found in the repository.
    """
    if not profile_id:
        return "default"
    profile = _lookup_profile(request, profile_id)
    if profile is not None and profile.provider_id:
        return profile.provider_id
    return profile_id.rsplit(".", 1)[0] or "default"


def _lookup_profile(request: Request, profile_id: str):
    # Resolve via the gateway's provider_reader (the runtime repo that exposes
    # get_profile in the DB-backed config -- the same source the gateway bills
    # against), falling back to the in-memory repository. Mirrors
    # digital_human._provider_profile_by_id. NOTE: do NOT use provider_repository()
    # here -- that returns SqlAlchemyProviderRepository, which has no get_profile.
    gateway = getattr(request.app.state, "provider_gateway", None)
    reader = getattr(gateway, "provider_reader", None) if gateway is not None else None
    if reader is not None:
        profile = reader.get_profile(profile_id)
        if profile is not None:
            return profile
    return repository(request).provider_profiles.get(profile_id)


def _line(
    *,
    label: str,
    capability_id: str,
    unit: str,
    quantity: Decimal,
    unit_price: c.Money,
) -> c.CostEstimateLine:
    amount = unit_price.amount * quantity
    return c.CostEstimateLine(
        label=label,
        capability_id=capability_id,
        quantity=quantity,
        unit=unit,
        unit_price=unit_price,
        estimated_cost=c.Money(amount=amount, currency=unit_price.currency),
    )
