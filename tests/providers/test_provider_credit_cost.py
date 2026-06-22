"""Regression: the gateway must turn provider-reported ``provider_credits`` into
estimated cost via a ``provider_credit`` price item.

Before this fix ``_estimated_cost_from_usage`` only consumed token/second/call
units, so RunningHub HeyGem (which reports ``consumeCoins`` as ``provider_credits``
and has no token/second usage) recorded 0 cost for the most expensive node.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from packages.ai.gateway.provider_gateway import ProviderGateway, ProviderResult
from packages.core.contracts import Money, ProviderPriceItem
from packages.core.storage.provider_seed import seed_real_provider_configuration
from packages.core.storage.repository import Repository


def _credit_item(rate: str) -> ProviderPriceItem:
    return ProviderPriceItem(
        id="price_credit",
        catalog_id="c",
        provider_id="runninghub.heygem",
        model_id="heygem-webapp",
        capability_id="lipsync.video",
        unit="provider_credit",
        unit_price=Money(currency="CNY", amount=Decimal(rate)),
    )


def _cost(result: ProviderResult, items: list[ProviderPriceItem]) -> Money:
    # _estimated_cost_from_usage uses only (result, items); a dummy self is fine.
    return ProviderGateway._estimated_cost_from_usage(SimpleNamespace(), result, items)


def test_provider_credit_unit_priced_from_credits():
    result = ProviderResult(provider_credits=Decimal("65952"))
    cost = _cost(result, [_credit_item("0.0000394226")])
    assert cost.currency == "CNY"
    assert cost.amount == Decimal("0.0000394226") * Decimal("65952")
    assert cost.amount > Decimal("0")  # the heygem-恒0 bug is fixed


def test_provider_credit_without_credits_is_zero():
    result = ProviderResult()  # provider_credits is None
    cost = _cost(result, [_credit_item("0.0000394226")])
    assert cost.amount == Decimal("0")


def test_explicit_estimated_cost_wins_over_credits():
    result = ProviderResult(
        provider_credits=Decimal("100"),
        estimated_cost=Money(currency="CNY", amount=Decimal("9.99")),
    )
    cost = _cost(result, [_credit_item("0.5")])
    assert cost.amount == Decimal("9.99")


def test_seed_prices_heygem_with_provider_credit_unit():
    repo = Repository()
    seed_real_provider_configuration(repo)
    items = [item for item in repo.price_items.values() if item.provider_id == "runninghub.heygem"]
    assert items, "expected a runninghub.heygem price item"
    assert {item.unit for item in items} == {"provider_credit"}
    assert items[0].model_id == "heygem-webapp"
    assert items[0].capability_id == "lipsync.video"
    assert items[0].unit_price.amount > Decimal("0")
