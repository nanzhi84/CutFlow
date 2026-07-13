from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request

from apps.api.dependencies import require_role
from apps.api.services import providers as service
from packages.core import contracts as c

router = APIRouter()

@router.get("/api/providers/profiles", response_model=c.PageResponse[c.ProviderProfile])
def provider_profiles(
    request: Request,
    limit: int = 50,
    provider_id: str | None = None,
    capability: str | None = None,
    environment: str | None = None,
) -> c.PageResponse[c.ProviderProfile]:
    return service.provider_profiles(request, limit, provider_id, capability, environment)


@router.post("/api/providers/profiles", response_model=c.ProviderProfile, status_code=201)
def create_provider_profile(payload: c.CreateProviderProfileRequest, request: Request) -> c.ProviderProfile:
    require_role(request, c.UserRole.admin)
    return service.create_provider_profile(payload, request)


@router.patch("/api/providers/profiles/{profile_id}", response_model=c.ProviderProfile)
def patch_provider_profile(
    profile_id: str, payload: c.PatchProviderProfileRequest, request: Request
) -> c.ProviderProfile:
    require_role(request, c.UserRole.admin)
    return service.patch_provider_profile(profile_id, payload, request)


@router.post("/api/providers/profiles/{profile_id}/test", response_model=c.ProviderHealthCheckResponse)
def test_provider_profile(
    profile_id: str, payload: c.TestProviderProfileRequest, request: Request
) -> c.ProviderHealthCheckResponse:
    require_role(request, c.UserRole.admin)
    return service.test_provider_profile(profile_id, payload, request)


@router.get("/api/providers/capabilities", response_model=list[c.ProviderCapability])
def provider_capabilities(request: Request) -> list[c.ProviderCapability]:

    return service.provider_capabilities(request)


@router.get("/api/providers/price-catalogs", response_model=c.PageResponse[c.ProviderPriceCatalog])
def price_catalogs(
    request: Request,
    limit: int = 50,
    provider_id: str | None = None,
    active_only: bool = False,
) -> c.PageResponse[c.ProviderPriceCatalog]:
    return service.price_catalogs(request, limit, provider_id, active_only)


@router.get("/api/providers/price-catalogs/{catalog_id}/items", response_model=c.PageResponse[c.ProviderPriceItem])
def price_catalog_items(request: Request, catalog_id: str, limit: int = 200) -> c.PageResponse[c.ProviderPriceItem]:
    return service.price_catalog_items(request, catalog_id, limit)


@router.post("/api/providers/price-catalogs", response_model=c.ProviderPriceCatalog, status_code=201)
def upsert_price_catalog(payload: c.UpsertPriceCatalogRequest, request: Request) -> c.ProviderPriceCatalog:
    require_role(request, c.UserRole.admin)
    return service.upsert_price_catalog(payload, request)


@router.post("/api/providers/price-catalogs/{catalog_id}/approve", response_model=c.ProviderPriceCatalog)
def approve_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    require_role(request, c.UserRole.admin)
    return service.approve_price_catalog(catalog_id, payload, request)


@router.post("/api/providers/price-catalogs/{catalog_id}/publish", response_model=c.ProviderPriceCatalog)
def publish_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    require_role(request, c.UserRole.admin)
    return service.publish_price_catalog(catalog_id, payload, request)


@router.post("/api/providers/price-catalogs/{catalog_id}/deprecate", response_model=c.ProviderPriceCatalog)
def deprecate_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    require_role(request, c.UserRole.admin)
    return service.deprecate_price_catalog(catalog_id, payload, request)


@router.get("/api/providers/usage", response_model=c.ProviderUsageReport)
def provider_usage(
    request: Request,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    provider_id: str | None = None,
    case_id: str | None = None,
) -> c.ProviderUsageReport:
    return service.provider_usage(request, window_start, window_end, provider_id, case_id)


@router.get("/api/providers/balances", response_model=c.ProviderBalanceReport)
def provider_balances(
    request: Request,
    provider_id: str | None = None,
    environment: str | None = None,
) -> c.ProviderBalanceReport:
    require_role(request, c.UserRole.admin)
    return service.provider_balances(request, provider_id, environment)


@router.post("/api/providers/balances/refresh", response_model=c.ProviderBalanceReport)
def refresh_provider_balances(
    request: Request,
) -> c.ProviderBalanceReport:
    require_role(request, c.UserRole.operator)
    return service.refresh_all_balances(request)


@router.post("/api/providers/reconcile-billing", response_model=c.ReconcileBillingResponse, status_code=202)
def reconcile_billing(payload: c.ReconcileBillingRequest, request: Request) -> c.ReconcileBillingResponse:
    require_role(request, c.UserRole.admin)
    return service.reconcile_billing(payload, request)
