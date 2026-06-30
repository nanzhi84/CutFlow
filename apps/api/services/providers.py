from __future__ import annotations

from datetime import datetime

import httpx
from fastapi import Request

from apps.api.common import (
    ops_repository,
    provider_repository,
    request_id,
    secret_store,
)
from packages.ai.netpolicy import assert_options_hosts_allowed
from packages.core import contracts as c
from packages.core.provider_balance_accounts import coalesce_balance_items
from packages.core.workflow import NodeExecutionError
from packages.ops.balance import BalancePollerService, refresh_balances


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


def provider_profiles(
    request: Request,
    limit: int = 50,
    provider_id: str | None = None,
    capability: str | None = None,
    environment: str | None = None,
) -> c.PageResponse[c.ProviderProfile]:
    values = provider_repository(request).list_profiles(
        provider_id=provider_id,
        capability=capability,
        environment=environment,
        limit=limit,
    )
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_provider_profile(payload: c.CreateProviderProfileRequest, request: Request) -> c.ProviderProfile:
    _validate_outbound_hosts(payload.default_options)
    return provider_repository(request).create_profile(payload)


def patch_provider_profile(
    profile_id: str, payload: c.PatchProviderProfileRequest, request: Request
) -> c.ProviderProfile:
    # default_options is optional on patch; only validate when it is being set.
    _validate_outbound_hosts(payload.default_options)
    return provider_repository(request).patch_profile(profile_id, payload)


def test_provider_profile(
    profile_id: str, payload: c.TestProviderProfileRequest, request: Request
) -> c.ProviderHealthCheckResponse:
    return provider_repository(request).test_profile(profile_id, payload)


def provider_capabilities(request: Request) -> list[c.ProviderCapability]:

    return provider_repository(request).list_capabilities()


def price_catalogs(
    request: Request,
    limit: int = 50,
    provider_id: str | None = None,
    active_only: bool = False,
) -> c.PageResponse[c.ProviderPriceCatalog]:
    values = provider_repository(request).list_price_catalogs(
        provider_id=provider_id,
        active_only=active_only,
        limit=limit,
    )
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def price_catalog_items(request: Request, catalog_id: str, limit: int = 200) -> c.PageResponse[c.ProviderPriceItem]:
    values = provider_repository(request).list_price_items(catalog_id=catalog_id, limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def upsert_price_catalog(payload: c.UpsertPriceCatalogRequest, request: Request) -> c.ProviderPriceCatalog:
    return provider_repository(request).upsert_price_catalog(payload)


def approve_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    return provider_repository(request).patch_price_catalog_status(catalog_id, "approved", payload)


def publish_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    return provider_repository(request).patch_price_catalog_status(catalog_id, "published", payload)


def deprecate_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    return provider_repository(request).patch_price_catalog_status(catalog_id, "deprecated", payload)


def provider_usage(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    provider_id: str | None = None,
    case_id: str | None = None,
) -> c.ProviderUsageReport:
    return ops_repository(request).provider_usage(
        window_start=window_start,
        window_end=window_end,
        provider_id=provider_id,
        case_id=case_id,
    )


def provider_balances(
    request: Request,
    provider_id: str | None = None,
    environment: str | None = None,
) -> c.ProviderBalanceReport:
    snapshots = provider_repository(request).latest_balance_snapshots(
        provider_id=provider_id,
        environment=environment,
    )
    snapshots.sort(key=lambda item: (item.provider_id, item.account_group or ""))
    items = coalesce_balance_items(_balance_item_from_snapshot(item) for item in snapshots)
    return c.ProviderBalanceReport(
        items=items,
        request_id=request_id(),
        status="ok" if items else "pending",
    )


def _list_provider_profiles(request: Request) -> list[c.ProviderProfile]:
    repo = provider_repository(request)
    return repo.list_profiles(limit=200)


def _persist_balance_snapshot(request: Request, item: c.ProviderBalanceItem) -> None:
    repo = provider_repository(request)
    snapshot = _snapshot_from_item(item)
    repo.upsert_balance_snapshot(snapshot)


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
        return repo.list_profiles(limit=200)

    def on_results(items: list[c.ProviderBalanceItem]) -> None:
        repo = getattr(app.state, "sqlalchemy_provider_repository", None)
        for item in items:
            snapshot = _snapshot_from_item(item)
            repo.upsert_balance_snapshot(snapshot)

    return BalancePollerService(
        profiles_provider=profiles_provider,
        secret_store=app.state.secret_store,
        on_results=on_results,
        settings=app.state.settings.balance,
    )


def reconcile_billing(payload: c.ReconcileBillingRequest, request: Request) -> c.ReconcileBillingResponse:
    return ops_repository(request).reconcile_billing(payload, request_id())
