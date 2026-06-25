from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import httpx
from fastapi import Request

from apps.api.common import (
    ops_repository,
    page,
    provider_repository,
    repository,
    request_id,
    secret_store,
)
from packages.ai.netpolicy import assert_options_hosts_allowed
from packages.core import contracts as c
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.ops.balance import BalancePollerService, refresh_balances


DEFAULT_HEALTH_CHECK_LATENCY_MS = 100


def _validate_outbound_hosts(default_options: dict | None) -> None:
    """Reject user-supplied base_url overrides whose host is not allow-listed.

    The stored provider secret is delivered to ``default_options.base_url`` on the
    next provider call, so an off-list host is an SSRF / key-exfiltration vector.
    Enforced here (before persist) AND in the gateway (before the secret is sent)
    for defense in depth — see ``packages.ai.netpolicy``.
    """
    try:
        assert_options_hosts_allowed(default_options)
    except ValueError as exc:
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, str(exc)) from exc


def _balance_item_from_snapshot(snapshot: c.ProviderBalanceSnapshot) -> c.ProviderBalanceItem:
    return c.ProviderBalanceItem(
        provider_id=snapshot.provider_id,
        account_group=snapshot.account_group,
        balance=snapshot.balance,
        quota_remaining=snapshot.quota_remaining,
        unit=snapshot.unit,
        checked_at=snapshot.checked_at,
        status=snapshot.status,
        detail=snapshot.detail,
    )


def _snapshot_from_item(item: c.ProviderBalanceItem) -> c.ProviderBalanceSnapshot:
    return c.ProviderBalanceSnapshot(
        id=f"pbs_{item.provider_id.replace('.', '_')}_{(item.account_group or 'default').replace('.', '_')}",
        provider_id=item.provider_id,
        account_group=item.account_group,
        balance=item.balance,
        quota_remaining=item.quota_remaining,
        unit=item.unit,
        status=item.status,
        detail=item.detail,
        checked_at=item.checked_at,
    )


def _recent_profile_p95_latency_ms(
    invocations: list[c.ProviderInvocation],
    *,
    profile_id: str,
    window_hours: int = 24,
) -> int | None:
    window_start = c.utcnow() - timedelta(hours=window_hours)
    durations = sorted(
        invocation.duration_ms
        for invocation in invocations
        if invocation.provider_profile_id == profile_id and invocation.started_at >= window_start
    )
    if not durations:
        return None
    index = max(0, ((len(durations) * 95 + 99) // 100) - 1)
    return int(durations[index])


def provider_profiles(
    request: Request,
    limit: int = 50,
    provider_id: str | None = None,
    capability: str | None = None,
    environment: str | None = None,
) -> c.PageResponse[c.ProviderProfile]:
    if provider_repository(request) is not None:
        values = provider_repository(request).list_profiles(
            provider_id=provider_id,
            capability=capability,
            environment=environment,
            limit=limit,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    values = list(repository(request).provider_profiles.values())
    if provider_id:
        values = [profile for profile in values if profile.provider_id == provider_id]
    if capability:
        values = [profile for profile in values if profile.capability == capability]
    if environment:
        values = [profile for profile in values if profile.environment == environment]
    return page(values, limit)


def create_provider_profile(payload: c.CreateProviderProfileRequest, request: Request) -> c.ProviderProfile:
    _validate_outbound_hosts(payload.default_options)
    if provider_repository(request) is not None:
        return provider_repository(request).create_profile(payload)
    profile = c.ProviderProfile(id=new_id("provider_profile"), **payload.model_dump())
    repository(request).provider_profiles[profile.id] = profile
    return profile


def patch_provider_profile(
    profile_id: str, payload: c.PatchProviderProfileRequest, request: Request
) -> c.ProviderProfile:
    # default_options is optional on patch; only validate when it is being set.
    _validate_outbound_hosts(payload.default_options)
    if provider_repository(request) is not None:
        return provider_repository(request).patch_profile(profile_id, payload)
    return repository(request).patch(repository(request).provider_profiles, profile_id, payload.model_dump(exclude_none=True))


def test_provider_profile(
    profile_id: str, payload: c.TestProviderProfileRequest, request: Request
) -> c.ProviderHealthCheckResponse:
    if provider_repository(request) is not None:
        return provider_repository(request).test_profile(profile_id, payload)
    profile = repository(request).provider_profiles.get(profile_id)
    ok = profile is not None and profile.enabled
    latency_ms = None
    if ok:
        latency_ms = _recent_profile_p95_latency_ms(
            list(repository(request).provider_invocations.values()),
            profile_id=profile_id,
        )
        if latency_ms is None:
            latency_ms = DEFAULT_HEALTH_CHECK_LATENCY_MS
    return c.ProviderHealthCheckResponse(profile_id=profile_id, ok=ok, latency_ms=latency_ms)


def provider_capabilities(request: Request) -> list[c.ProviderCapability]:

    if provider_repository(request) is not None:
        return provider_repository(request).list_capabilities()
    return list(repository(request).provider_capabilities.values())


def price_catalogs(
    request: Request,
    limit: int = 50,
    provider_id: str | None = None,
    active_only: bool = False,
) -> c.PageResponse[c.ProviderPriceCatalog]:
    if provider_repository(request) is not None:
        values = provider_repository(request).list_price_catalogs(
            provider_id=provider_id,
            active_only=active_only,
            limit=limit,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    values = list(repository(request).price_catalogs.values())
    if provider_id:
        values = [catalog for catalog in values if catalog.provider_id == provider_id]
    if active_only:
        values = [catalog for catalog in values if catalog.status == "published"]
    return page(values, limit)


def price_catalog_items(request: Request, catalog_id: str, limit: int = 200) -> c.PageResponse[c.ProviderPriceItem]:
    if provider_repository(request) is not None:
        values = provider_repository(request).list_price_items(catalog_id=catalog_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    values = [item for item in repository(request).price_items.values() if item.catalog_id == catalog_id]
    return page(values, limit)


def upsert_price_catalog(payload: c.UpsertPriceCatalogRequest, request: Request) -> c.ProviderPriceCatalog:
    if provider_repository(request) is not None:
        return provider_repository(request).upsert_price_catalog(payload)
    repository(request).price_catalogs[payload.catalog.id] = payload.catalog
    for item in payload.items:
        repository(request).price_items[item.id] = item
    return payload.catalog


def approve_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    if provider_repository(request) is not None:
        return provider_repository(request).patch_price_catalog_status(catalog_id, "approved", payload)
    return repository(request).patch(repository(request).price_catalogs, catalog_id, {"status": "approved"})


def publish_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    if provider_repository(request) is not None:
        return provider_repository(request).patch_price_catalog_status(catalog_id, "published", payload)
    return repository(request).patch(repository(request).price_catalogs, catalog_id, {"status": "published"})


def deprecate_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    if provider_repository(request) is not None:
        return provider_repository(request).patch_price_catalog_status(catalog_id, "deprecated", payload)
    return repository(request).patch(repository(request).price_catalogs, catalog_id, {"status": "deprecated"})


def provider_usage(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    provider_id: str | None = None,
    case_id: str | None = None,
) -> c.ProviderUsageReport:
    if ops_repository(request) is not None:
        return ops_repository(request).provider_usage(
            window_start=window_start,
            window_end=window_end,
            provider_id=provider_id,
            case_id=case_id,
        )
    invocations = list(repository(request).provider_invocations.values())
    if provider_id:
        invocations = [item for item in invocations if item.provider_id == provider_id]
    if case_id:
        invocations = [item for item in invocations if item.case_id == case_id]
    amount = sum((item.estimated_cost.amount for item in invocations if item.estimated_cost), Decimal("0"))
    return c.ProviderUsageReport(
        invocations=len(invocations),
        estimated_cost=c.Money(amount=amount, currency="CNY"),
        unpriced_invocation_count=len([item for item in invocations if item.billing_status == "unpriced"]),
    )


def provider_balances(
    request: Request,
    provider_id: str | None = None,
    environment: str | None = None,
) -> c.ProviderBalanceReport:
    if provider_repository(request) is not None:
        return provider_repository(request).balances(
            request_id=request_id(),
            provider_id=provider_id,
            environment=environment,
        )
    repo = repository(request)
    snapshots = list(repo.provider_balance_snapshots.values())
    if provider_id:
        snapshots = [item for item in snapshots if item.provider_id == provider_id]
    if environment:
        profile_ids = {
            profile.id
            for profile in repo.provider_profiles.values()
            if profile.environment == environment and (provider_id is None or profile.provider_id == provider_id)
        }
        snapshots = [item for item in snapshots if item.account_group in profile_ids]
    snapshots.sort(key=lambda item: (item.provider_id, item.account_group or ""))
    return c.ProviderBalanceReport(
        items=[_balance_item_from_snapshot(item) for item in snapshots],
        request_id=request_id(),
        status="ok" if snapshots else "pending",
    )


def _list_provider_profiles(request: Request) -> list[c.ProviderProfile]:
    repo = provider_repository(request)
    if repo is not None:
        return repo.list_profiles(limit=200)
    return list(repository(request).provider_profiles.values())


def _persist_balance_snapshot(request: Request, item: c.ProviderBalanceItem) -> None:
    repo = provider_repository(request)
    snapshot = _snapshot_from_item(item)
    if repo is not None:
        repo.upsert_balance_snapshot(snapshot)
    else:
        repository(request).provider_balance_snapshots[snapshot.id] = snapshot


def refresh_all_balances(request: Request, http_client: httpx.Client | None = None) -> c.ProviderBalanceReport:
    profiles = _list_provider_profiles(request)
    timeout = request.app.state.settings.balance.request_timeout_seconds
    close_client = http_client is None
    client = http_client or httpx.Client(trust_env=False, timeout=timeout)
    try:
        items = refresh_balances(
            profiles,
            secret_store=secret_store(request),
            client=client,
        )
    finally:
        if close_client:
            client.close()
    for item in items:
        _persist_balance_snapshot(request, item)
    return provider_balances(request)


def build_balance_poller_service(app) -> BalancePollerService:
    """Build the OPTIONAL periodic balance poller from ``app.state``.

    Gated by ``settings.balance.poller_enabled`` (default OFF). Each tick polls
    every configured provider profile and persists the resulting snapshots so the
    auth-gated GET /api/providers/balances serves fresh values."""

    def profiles_provider() -> list[c.ProviderProfile]:
        repo = getattr(app.state, "sqlalchemy_provider_repository", None)
        if repo is not None:
            return repo.list_profiles(limit=200)
        return list(app.state.repository.provider_profiles.values())

    def on_results(items: list[c.ProviderBalanceItem]) -> None:
        repo = getattr(app.state, "sqlalchemy_provider_repository", None)
        for item in items:
            snapshot = _snapshot_from_item(item)
            if repo is not None:
                repo.upsert_balance_snapshot(snapshot)
            else:
                app.state.repository.provider_balance_snapshots[snapshot.id] = snapshot

    return BalancePollerService(
        profiles_provider=profiles_provider,
        secret_store=app.state.secret_store,
        on_results=on_results,
        settings=app.state.settings.balance,
    )


def reconcile_billing(payload: c.ReconcileBillingRequest, request: Request) -> c.ReconcileBillingResponse:
    if ops_repository(request) is not None:
        return ops_repository(request).reconcile_billing(payload, request_id())
    return c.ReconcileBillingResponse(
        reconciliation_run_id=new_id("recon"),
        status="completed",
        estimated_cost=c.zero_money(),
        recorded_usage_cost=c.zero_money(),
        variance=c.zero_money(),
        request_id=request_id(),
    )
