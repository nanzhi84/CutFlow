from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import case, cast, func, or_, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.sqltypes import Numeric

from packages.core.contracts import (
    ErrorCode,
    Money,
    ProviderUsageMetricsItem,
    ProviderUsageMetricsReport,
    utcnow,
)
from packages.core.storage.database import ProviderInvocationRow


@dataclass(frozen=True)
class ProviderProfileHealthMetrics:
    provider_profile_id: str
    provider_id: str
    capability_id: str
    model_id: str | None
    calls: int
    success_count: int
    failure_count: int
    timeout_or_throttle_count: int
    error_rate: float
    timeout_or_throttle_rate: float
    p95_latency_ms: int | None
    circuit_open: bool
    window_hours: int


def sqlalchemy_provider_usage_metrics(
    session_factory: sessionmaker[Session],
    *,
    window_hours: int,
    request_id: str,
) -> ProviderUsageMetricsReport:
    generated_at = utcnow()
    window_start = generated_at - timedelta(hours=window_hours)
    amount = cast(ProviderInvocationRow.estimated_cost["amount"].astext, Numeric(20, 6))
    currency = ProviderInvocationRow.estimated_cost["currency"].astext
    success = case((ProviderInvocationRow.status == "succeeded", 1), else_=0)
    with session_factory() as session:
        statement = (
            select(
                ProviderInvocationRow.provider_id,
                ProviderInvocationRow.capability_id,
                ProviderInvocationRow.model_id,
                func.count(ProviderInvocationRow.id),
                func.sum(success),
                func.coalesce(func.sum(amount), 0),
                func.coalesce(func.max(currency), "CNY"),
            )
            .where(ProviderInvocationRow.started_at >= window_start)
            .group_by(
                ProviderInvocationRow.provider_id,
                ProviderInvocationRow.capability_id,
                ProviderInvocationRow.model_id,
            )
            .order_by(func.count(ProviderInvocationRow.id).desc())
        )
        rows = list(session.execute(statement))
    items = []
    for provider_id, capability_id, model_id, calls, success_count, total, currency_code in rows:
        call_count = int(calls or 0)
        successes = int(success_count or 0)
        items.append(
            ProviderUsageMetricsItem(
                provider_id=provider_id,
                capability_id=capability_id,
                model_id=model_id,
                calls=call_count,
                success_count=successes,
                success_rate=(successes / call_count) if call_count else 0,
                estimated_cost=Money(amount=total, currency=currency_code or "CNY"),
                window_hours=window_hours,
            )
        )
    return ProviderUsageMetricsReport(
        items=items,
        window_hours=window_hours,
        generated_at=generated_at,
        request_id=request_id,
    )


def sqlalchemy_provider_profile_health_metrics(
    session_factory: sessionmaker[Session],
    *,
    window_hours: int,
    error_rate_threshold: float,
    provider_profile_id: str | None = None,
) -> list[ProviderProfileHealthMetrics]:
    generated_at = utcnow()
    window_start = generated_at - timedelta(hours=max(1, window_hours))
    error_code = ProviderInvocationRow.error["code"].astext
    base_statement = (
        select(
            ProviderInvocationRow.id.label("id"),
            ProviderInvocationRow.provider_profile_id.label("provider_profile_id"),
            ProviderInvocationRow.provider_id.label("provider_id"),
            ProviderInvocationRow.capability_id.label("capability_id"),
            ProviderInvocationRow.model_id.label("model_id"),
            ProviderInvocationRow.status.label("status"),
            ProviderInvocationRow.duration_ms.label("duration_ms"),
            error_code.label("error_code"),
        )
        .where(ProviderInvocationRow.started_at >= window_start)
        .where(ProviderInvocationRow.provider_profile_id.is_not(None))
        # The circuit breaker's own fail-fast blocks are recorded as failed
        # invocations but never reached the provider. Exclude them entirely so a
        # provider's health reflects only real calls — otherwise the breaker's
        # blocks keep error_rate pinned high and the circuit can never recover.
        # NULL error_code (successful calls) must survive the filter, hence the
        # explicit is-null branch (``error_code != x`` drops NULLs in SQL).
        .where(
            or_(
                error_code.is_(None),
                error_code != ErrorCode.provider_circuit_open.value,
            )
        )
    )
    if provider_profile_id is not None:
        base_statement = base_statement.where(ProviderInvocationRow.provider_profile_id == provider_profile_id)
    base = base_statement.subquery()
    ranked = (
        select(
            base,
            func.row_number()
            .over(
                partition_by=base.c.provider_profile_id,
                order_by=base.c.duration_ms.asc(),
            )
            .label("duration_rank"),
            func.count(base.c.id).over(partition_by=base.c.provider_profile_id).label("duration_count"),
        )
        .subquery()
    )
    success = case((ranked.c.status == "succeeded", 1), else_=0)
    failure = case((ranked.c.status.in_(("failed", "timed_out")), 1), else_=0)
    timeout_or_throttle = case(
        (
            (ranked.c.status == "timed_out")
            | (ranked.c.error_code.in_(("provider.timeout", "provider.quota_exceeded"))),
            1,
        ),
        else_=0,
    )
    p95_latency = func.min(
        case(
            (
                ranked.c.duration_rank * 100 >= ranked.c.duration_count * 95,
                ranked.c.duration_ms,
            )
        )
    )
    with session_factory() as session:
        statement = (
            select(
                ranked.c.provider_profile_id,
                func.max(ranked.c.provider_id),
                func.max(ranked.c.capability_id),
                func.max(ranked.c.model_id),
                func.count(ranked.c.id),
                func.sum(success),
                func.sum(failure),
                func.sum(timeout_or_throttle),
                p95_latency,
            )
            .group_by(ranked.c.provider_profile_id)
            .order_by(func.count(ranked.c.id).desc(), ranked.c.provider_profile_id.asc())
        )
        rows = list(session.execute(statement))

    threshold = min(max(float(error_rate_threshold), 0.0), 1.0)
    items: list[ProviderProfileHealthMetrics] = []
    for (
        profile_id,
        provider_id,
        capability_id,
        model_id,
        calls,
        success_count,
        failure_count,
        timeout_or_throttle_count,
        p95_latency_ms,
    ) in rows:
        call_count = int(calls or 0)
        failures = int(failure_count or 0)
        timeouts = int(timeout_or_throttle_count or 0)
        error_rate = (failures / call_count) if call_count else 0.0
        timeout_rate = (timeouts / call_count) if call_count else 0.0
        items.append(
            ProviderProfileHealthMetrics(
                provider_profile_id=profile_id,
                provider_id=provider_id,
                capability_id=capability_id,
                model_id=model_id,
                calls=call_count,
                success_count=int(success_count or 0),
                failure_count=failures,
                timeout_or_throttle_count=timeouts,
                error_rate=error_rate,
                timeout_or_throttle_rate=timeout_rate,
                p95_latency_ms=int(p95_latency_ms) if p95_latency_ms is not None else None,
                circuit_open=call_count > 0 and error_rate > threshold,
                window_hours=max(1, window_hours),
            )
        )
    return items
