from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import (
    CreateProviderProfileRequest,
    ErrorCode,
    GovernedActionRequest,
    Money,
    PatchProviderProfileRequest,
    ProviderBalanceItem,
    ProviderBalanceReport,
    ProviderCapability,
    ProviderHealthCheckResponse,
    ProviderOptionsSchemaRef,
    ProviderPriceCatalog,
    ProviderPriceItem,
    ProviderProfile,
    TestProviderProfileRequest,
    UpsertPriceCatalogRequest,
    utcnow,
)
from packages.core.storage.database import (
    ProviderCapabilityRow,
    ProviderPriceCatalogRow,
    ProviderPriceItemRow,
    ProviderProfileRow,
    SecretRow,
)
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError


def provider_profile_row_to_contract(row: ProviderProfileRow) -> ProviderProfile:
    return ProviderProfile(
        id=row.id,
        provider_id=row.provider_id,
        model_id=row.model_id,
        capability=row.capability,
        display_name=row.display_name,
        environment=row.environment,
        secret_ref=row.secret_ref,
        concurrency_key=row.concurrency_key,
        timeout_sec=row.timeout_sec,
        retry_policy=row.retry_policy or {},
        cost_policy_id=row.cost_policy_id,
        options_schema_ref=ProviderOptionsSchemaRef.model_validate(row.options_schema_ref),
        default_options=row.default_options,
        enabled=row.enabled,
        version=row.version,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def provider_capability_row_to_contract(row: ProviderCapabilityRow) -> ProviderCapability:
    return ProviderCapability(
        id=row.id,
        capability=row.capability_id,
        provider_id=row.provider_id,
        model_id=row.model_id,
        display_name=row.display_name,
        input_schema_id=row.input_schema_id,
        output_schema_id=row.output_schema_id,
        options_schema_id=row.options_schema_id,
        supports_async_job=row.supports_async_job,
        supports_cancel=row.supports_cancel,
        max_payload_bytes=row.max_payload_bytes,
        max_duration_sec=row.max_duration_sec,
        default_timeout_sec=row.default_timeout_sec,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def price_catalog_row_to_contract(row: ProviderPriceCatalogRow) -> ProviderPriceCatalog:
    return ProviderPriceCatalog(
        id=row.id,
        provider_id=row.provider_id,
        status=row.status,
        currency=row.currency,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def price_item_row_to_contract(row: ProviderPriceItemRow) -> ProviderPriceItem:
    return ProviderPriceItem(
        id=row.id,
        catalog_id=row.catalog_id,
        provider_id=row.provider_id,
        model_id=row.model_id,
        capability_id=row.capability_id,
        unit=row.unit,
        unit_price=Money.model_validate(row.unit_price),
        active_from=row.active_from,
        active_to=row.active_to,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyProviderRuntimeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def get_profile(self, profile_id: str) -> ProviderProfile | None:
        with self.session_factory() as session:
            row = session.get(ProviderProfileRow, profile_id)
            return provider_profile_row_to_contract(row) if row is not None else None

    def list_price_items(self) -> list[ProviderPriceItem]:
        with self.session_factory() as session:
            statement = select(ProviderPriceItemRow)
            return [price_item_row_to_contract(row) for row in session.scalars(statement)]

    def secret_is_active(self, secret_ref: str) -> bool:
        with self.session_factory() as session:
            statement = (
                select(SecretRow.id)
                .where(SecretRow.secret_ref == secret_ref)
                .where(SecretRow.status == "active")
                .limit(1)
            )
            return session.scalar(statement) is not None


class SqlAlchemyProviderRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def list_profiles(
        self,
        *,
        provider_id: str | None = None,
        capability: str | None = None,
        environment: str | None = None,
        limit: int = 50,
    ) -> list[ProviderProfile]:
        with self.session_factory() as session:
            statement = select(ProviderProfileRow)
            if provider_id:
                statement = statement.where(ProviderProfileRow.provider_id == provider_id)
            if capability:
                statement = statement.where(ProviderProfileRow.capability == capability)
            if environment:
                statement = statement.where(ProviderProfileRow.environment == environment)
            statement = statement.order_by(ProviderProfileRow.updated_at.desc()).limit(limit)
            return [provider_profile_row_to_contract(row) for row in session.scalars(statement)]

    def create_profile(self, payload: CreateProviderProfileRequest) -> ProviderProfile:
        with self.session_factory() as session:
            row = ProviderProfileRow(
                id=new_id("provider_profile"),
                provider_id=payload.provider_id,
                model_id=payload.model_id,
                capability=payload.capability,
                display_name=payload.display_name,
                environment=payload.environment,
                secret_ref=payload.secret_ref,
                concurrency_key=payload.concurrency_key,
                timeout_sec=payload.timeout_sec,
                retry_policy=payload.retry_policy.model_dump(mode="json"),
                cost_policy_id=payload.cost_policy_id,
                options_schema_ref=payload.options_schema_ref.model_dump(mode="json"),
                default_options=payload.default_options,
                enabled=True,
                version=payload.version,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return provider_profile_row_to_contract(row)

    def patch_profile(self, profile_id: str, payload: PatchProviderProfileRequest) -> ProviderProfile:
        with self.session_factory() as session:
            row = session.get(ProviderProfileRow, profile_id)
            if row is None:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Provider profile not found.")
            for key, value in payload.model_dump(exclude_none=True).items():
                setattr(row, key, value)
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return provider_profile_row_to_contract(row)

    def test_profile(
        self, profile_id: str, payload: TestProviderProfileRequest
    ) -> ProviderHealthCheckResponse:
        with self.session_factory() as session:
            row = session.get(ProviderProfileRow, profile_id)
            return ProviderHealthCheckResponse(
                profile_id=profile_id,
                ok=row is not None and row.enabled,
                latency_ms=1 if row is not None and row.enabled else None,
            )

    def list_capabilities(self) -> list[ProviderCapability]:
        with self.session_factory() as session:
            statement = select(ProviderCapabilityRow).order_by(ProviderCapabilityRow.provider_id.asc())
            return [provider_capability_row_to_contract(row) for row in session.scalars(statement)]

    def balances(
        self,
        *,
        request_id: str,
        provider_id: str | None = None,
        environment: str | None = None,
    ) -> ProviderBalanceReport:
        with self.session_factory() as session:
            statement = select(ProviderProfileRow)
            if provider_id:
                statement = statement.where(ProviderProfileRow.provider_id == provider_id)
            if environment:
                statement = statement.where(ProviderProfileRow.environment == environment)
            rows = list(session.scalars(statement))
        providers = sorted({row.provider_id for row in rows})
        return ProviderBalanceReport(
            items=[
                ProviderBalanceItem(
                    provider_id=item,
                    balance=Money(amount=9999, currency="CNY"),
                    quota_remaining=1_000_000,
                    checked_at=utcnow(),
                    status="ok",
                )
                for item in providers
            ],
            request_id=request_id,
        )

    def list_price_catalogs(
        self,
        *,
        provider_id: str | None = None,
        active_only: bool = False,
        limit: int = 50,
    ) -> list[ProviderPriceCatalog]:
        with self.session_factory() as session:
            statement = select(ProviderPriceCatalogRow)
            if provider_id:
                statement = statement.where(ProviderPriceCatalogRow.provider_id == provider_id)
            if active_only:
                statement = statement.where(ProviderPriceCatalogRow.status == "published")
            statement = statement.order_by(ProviderPriceCatalogRow.updated_at.desc()).limit(limit)
            return [price_catalog_row_to_contract(row) for row in session.scalars(statement)]

    def upsert_price_catalog(self, payload: UpsertPriceCatalogRequest) -> ProviderPriceCatalog:
        catalog = payload.catalog
        with self.session_factory() as session:
            catalog_row = ProviderPriceCatalogRow(
                id=catalog.id,
                provider_id=catalog.provider_id,
                status=catalog.status,
                currency=catalog.currency,
                schema_version=catalog.schema_version,
                created_at=catalog.created_at,
                updated_at=utcnow(),
            )
            merged_catalog = session.merge(catalog_row)
            for item in payload.items:
                item_row = ProviderPriceItemRow(
                    id=item.id,
                    catalog_id=item.catalog_id,
                    provider_id=item.provider_id,
                    model_id=item.model_id,
                    capability_id=item.capability_id,
                    unit=item.unit,
                    unit_price=item.unit_price.model_dump(mode="json"),
                    active_from=item.active_from,
                    active_to=item.active_to,
                    schema_version=item.schema_version,
                    created_at=item.created_at,
                    updated_at=utcnow(),
                )
                session.merge(item_row)
            session.commit()
            session.refresh(merged_catalog)
            return price_catalog_row_to_contract(merged_catalog)

    def patch_price_catalog_status(
        self, catalog_id: str, status: str, payload: GovernedActionRequest
    ) -> ProviderPriceCatalog:
        with self.session_factory() as session:
            row = session.get(ProviderPriceCatalogRow, catalog_id)
            if row is None:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Provider price catalog not found.")
            row.status = status
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return price_catalog_row_to_contract(row)
