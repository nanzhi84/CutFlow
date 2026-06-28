"""Evaluate current-period spend against a Budget.

``evaluate_budget`` computes spend for a budget's scope over the current period
(day/week/month) and reports the ratio, whether the ``alert_threshold`` was
crossed, and whether the limit was exceeded. The ops repository turns
crossings/exceedances into ``OpsAlertEvent`` and, per env policy, can block
over-budget provider calls.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from packages.core.contracts import Budget, BudgetEvaluation, Money


@dataclass
class SpendRecord:
    """A flattened provider-invocation for budget attribution."""

    amount: Decimal
    currency: str
    created_at: datetime
    provider_id: str | None = None
    capability_id: str | None = None
    case_id: str | None = None


def period_start(period: str, now: datetime) -> datetime:
    """Start of the current budget period containing ``now`` (UTC).

    ``day`` -> midnight UTC today; ``week`` -> Monday 00:00 UTC; ``month`` -> 1st
    00:00 UTC."""

    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return day_start - timedelta(days=now.weekday())
    if period == "month":
        return day_start.replace(day=1)
    return day_start


def _scope_matches(budget: Budget, record: SpendRecord) -> bool:
    scope_type = budget.scope_type
    scope_id = budget.scope_id
    if scope_type == "global" or scope_id is None:
        return True
    if scope_type == "provider":
        return record.provider_id == scope_id
    if scope_type == "capability":
        return record.capability_id == scope_id
    if scope_type == "case":
        return record.case_id == scope_id
    # team / unknown scopes: no per-record attribution available -> count all.
    return True


def evaluate_budget(
    budget: Budget,
    spend_records: Iterable[SpendRecord],
    *,
    now: datetime | None = None,
) -> BudgetEvaluation:
    now = now or datetime.now(timezone.utc)
    start = period_start(budget.period, now)
    currency = budget.limit.currency
    total = Decimal("0")
    for record in spend_records:
        created = record.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < start:
            continue
        if not _scope_matches(budget, record):
            continue
        total += record.amount or Decimal("0")

    limit_amount = budget.limit.amount
    ratio: float | None = None
    if limit_amount and limit_amount > 0:
        ratio = float(total / limit_amount)
    threshold_crossed = bool(
        budget.enabled and ratio is not None and ratio >= budget.alert_threshold
    )
    exceeded = bool(budget.enabled and ratio is not None and ratio >= 1.0)

    return BudgetEvaluation(
        budget_id=budget.id,
        scope_type=budget.scope_type,
        scope_id=budget.scope_id,
        period=budget.period,
        period_start=start,
        spend=Money(amount=total, currency=currency),
        limit=budget.limit,
        ratio=ratio,
        threshold_crossed=threshold_crossed,
        exceeded=exceeded,
    )
