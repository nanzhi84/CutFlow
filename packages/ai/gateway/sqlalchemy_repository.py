from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from packages.core.contracts import (
    CreateProviderProfileRequest,
    ErrorCode,
    GovernedActionRequest,
    Money,
    PatchProviderProfileRequest,
    ProviderBalanceItem,
    ProviderBalanceReport,
    ProviderBalanceSnapshot,
    ProviderCapability,
    ProviderError,
    ProviderHealthCheckResponse,
    ProviderInvocation,
    ProviderOptionsSchemaRef,
    ProviderPriceCatalog,
    ProviderPriceItem,
    ProviderProfile,
    ProviderStatus,
    TestProviderProfileRequest,
    UpsertPriceCatalogRequest,
    utcnow,
)
from packages.core.provider_balance_accounts import coalesce_balance_items
from packages.core.storage.database import (
    ProviderCapabilityRow,
    ProviderBalanceSnapshotRow,
    ProviderInvocationRow,
    ProviderPriceCatalogRow,
    ProviderPriceItemRow,
    ProviderProfileRow,
    SecretRow,
)
from packages.core.storage.base_repository import BaseRepository
from packages.core.storage.repository import new_id
from packages.core.storage.row_mapper import map_row
from packages.core.workflow import NodeExecutionError


DEFAULT_HEALTH_CHECK_LATENCY_MS = 100


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


def balance_snapshot_row_to_contract(row: ProviderBalanceSnapshotRow) -> ProviderBalanceSnapshot:
    balance = None
    if row.balance_amount is not None and row.currency:
        balance = Money(amount=row.balance_amount, currency=row.currency)
    return ProviderBalanceSnapshot(
        id=row.id,
        provider_id=row.provider_id,
        account_group=row.account_group,
        balance=balance,
        quota_remaining=row.quota_remaining,
        unit=row.unit,
        status=row.status,
        detail=row.detail,
        checked_at=row.checked_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def balance_snapshot_to_item(snapshot: ProviderBalanceSnapshot) -> ProviderBalanceItem:
    return ProviderBalanceItem(
        provider_id=snapshot.provider_id,
        account_group=snapshot.account_group,
        balance=snapshot.balance,
        quota_remaining=snapshot.quota_remaining,
        unit=snapshot.unit,
        checked_at=snapshot.checked_at,
        status=snapshot.status,
        detail=snapshot.detail,
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


def provider_invocation_row_to_contract(row: ProviderInvocationRow) -> ProviderInvocation:
    return map_row(
        row,
        ProviderInvocation,
        status=ProviderStatus(row.status),
        estimated_cost=Money.model_validate(row.estimated_cost) if row.estimated_cost else None,
        actual_cost=Money.model_validate(row.actual_cost) if row.actual_cost else None,
        error=ProviderError.model_validate(row.error) if row.error else None,
    )


_NON_TERMINAL_PROVIDER_STATUSES = (
    ProviderStatus.prepared.value,
    ProviderStatus.submitted.value,
    ProviderStatus.polling.value,
)


class SqlAlchemyProviderInvocationStore(BaseRepository):
    """Durable, idempotency-key-keyed persistence for provider invocations.

    Only Run-scoped provider calls (keys minted by
    ``build_provider_call_idempotency_key``) flow through here. Every method runs in
    its own short transaction and never holds a row lock across the vendor call, so
    an infrastructure retry within the same Workflow Run recovers the prior call
    identity from durable state instead of re-submitting to the vendor.
    """

    def load_by_key(self, idempotency_key: str) -> ProviderInvocation | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ProviderInvocationRow).where(
                    ProviderInvocationRow.idempotency_key == idempotency_key
                )
            )
            return provider_invocation_row_to_contract(row) if row is not None else None

    def get_or_create(self, invocation: ProviderInvocation) -> ProviderInvocation:
        """Insert the ``prepared`` invocation, or return the row a concurrent creator won.

        ``ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING``
        makes a duplicate insert a no-op (the plain conflict target cannot match a
        partial index); the surviving row — ours or a concurrent creator's — is then
        read back by key.

        The durable row is inserted with ``node_run_id=NULL``: the NodeRun is not
        persisted until the node's completion snapshot, so writing its id here would
        violate the FK. The returned contract carries the caller's ``node_run_id`` so
        the in-memory invocation keeps it for the snapshot back-fill and for the
        failure-path linkage; the snapshot writes the (now persisted) NodeRun id.
        """
        insert_stmt = (
            pg_insert(ProviderInvocationRow)
            .values(
                id=invocation.id,
                idempotency_key=invocation.idempotency_key,
                case_id=invocation.case_id,
                run_id=invocation.run_id,
                node_run_id=None,
                provider_id=invocation.provider_id,
                model_id=invocation.model_id,
                provider_profile_id=invocation.provider_profile_id,
                capability_id=invocation.capability_id,
                prompt_version_id=invocation.prompt_version_id,
                status=invocation.status.value,
                billing_status=invocation.billing_status,
                started_at=invocation.started_at,
            )
            .on_conflict_do_nothing(
                index_elements=[ProviderInvocationRow.idempotency_key],
                index_where=ProviderInvocationRow.idempotency_key.isnot(None),
            )
        )
        with self.session_factory() as session:
            session.execute(insert_stmt)
            session.commit()
            row = session.scalar(
                select(ProviderInvocationRow).where(
                    ProviderInvocationRow.idempotency_key == invocation.idempotency_key
                )
            )
            return provider_invocation_row_to_contract(row).model_copy(
                update={"node_run_id": invocation.node_run_id}
            )

    def claim_submit(self, invocation_id: str) -> bool:
        """Conditionally advance ``prepared -> submitted``; ``True`` when this caller won."""
        with self.session_factory() as session:
            result = session.execute(
                update(ProviderInvocationRow)
                .where(ProviderInvocationRow.id == invocation_id)
                .where(ProviderInvocationRow.status == ProviderStatus.prepared.value)
                .values(status=ProviderStatus.submitted.value, updated_at=utcnow())
            )
            session.commit()
            return result.rowcount == 1

    def mark_polling(self, invocation_id: str, external_job_id: str) -> None:
        """Publish ``external_job_id`` and advance ``submitted -> polling`` immediately.

        Conditional on the row still being ``submitted`` so a late writer from a
        superseded attempt is a silent no-op rather than a regression.
        """
        with self.session_factory() as session:
            session.execute(
                update(ProviderInvocationRow)
                .where(ProviderInvocationRow.id == invocation_id)
                .where(ProviderInvocationRow.status == ProviderStatus.submitted.value)
                .values(
                    status=ProviderStatus.polling.value,
                    external_job_id=external_job_id,
                    updated_at=utcnow(),
                )
            )
            session.commit()

    def mark_terminal(
        self,
        invocation_id: str,
        status: ProviderStatus,
        error: ProviderError | None,
        *,
        expected_status: ProviderStatus | None = None,
    ) -> bool:
        """Forward-only write of a terminal status; a row already terminal is untouched.

        ``expected_status`` narrows the conditional update to one source status. A
        failure raised BEFORE this executor crossed the vendor boundary (profile
        validation, budget, circuit breaker) must pass ``prepared``: without it, a
        concurrent executor's in-flight ``submitted``/``polling`` row — a real vendor
        task — would be overwritten as failed and orphaned.
        """
        allowed = (
            (expected_status.value,)
            if expected_status is not None
            else _NON_TERMINAL_PROVIDER_STATUSES
        )
        with self.session_factory() as session:
            result = session.execute(
                update(ProviderInvocationRow)
                .where(ProviderInvocationRow.id == invocation_id)
                .where(ProviderInvocationRow.status.in_(allowed))
                .values(
                    status=status.value,
                    error=error.model_dump(mode="json") if error is not None else None,
                    finished_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            session.commit()
            return result.rowcount == 1


class SqlAlchemyProviderRuntimeRepository(BaseRepository):

    def get_profile(self, profile_id: str) -> ProviderProfile | None:
        with self.session_factory() as session:
            row = session.get(ProviderProfileRow, profile_id)
            return provider_profile_row_to_contract(row) if row is not None else None

    def list_profiles(
        self,
        *,
        provider_id: str | None = None,
        capability: str | None = None,
        environment: str | None = None,
        limit: int = 200,
    ) -> list[ProviderProfile]:
        with self.session_factory() as session:
            statement = select(ProviderProfileRow)
            if provider_id:
                statement = statement.where(ProviderProfileRow.provider_id == provider_id)
            if capability:
                statement = statement.where(ProviderProfileRow.capability == capability)
            if environment:
                statement = statement.where(ProviderProfileRow.environment == environment)
            statement = statement.order_by(ProviderProfileRow.id.asc()).limit(limit)
            return [provider_profile_row_to_contract(row) for row in session.scalars(statement)]

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


class SqlAlchemyProviderRepository(BaseRepository):

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
            ok = row is not None and row.enabled
            latency_ms = None
            if ok:
                latency_ms = self._recent_profile_p95_latency_ms(session, profile_id)
                if latency_ms is None:
                    latency_ms = DEFAULT_HEALTH_CHECK_LATENCY_MS
            return ProviderHealthCheckResponse(
                profile_id=profile_id,
                ok=ok,
                latency_ms=latency_ms,
            )

    def _recent_profile_p95_latency_ms(
        self,
        session: Session,
        profile_id: str,
        *,
        window_hours: int = 24,
    ) -> int | None:
        window_start = utcnow() - timedelta(hours=window_hours)
        ranked = (
            select(
                ProviderInvocationRow.duration_ms.label("duration_ms"),
                func.row_number().over(order_by=ProviderInvocationRow.duration_ms.asc()).label("duration_rank"),
                func.count(ProviderInvocationRow.id).over().label("duration_count"),
            )
            .where(ProviderInvocationRow.provider_profile_id == profile_id)
            .where(ProviderInvocationRow.started_at >= window_start)
            .subquery()
        )
        statement = select(func.min(ranked.c.duration_ms)).where(
            ranked.c.duration_rank * 100 >= ranked.c.duration_count * 95
        )
        value = session.scalar(statement)
        return int(value) if value is not None else None

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
        snapshots = self.latest_balance_snapshots(provider_id=provider_id, environment=environment)
        items = coalesce_balance_items(balance_snapshot_to_item(item) for item in snapshots)
        return ProviderBalanceReport(
            items=items,
            request_id=request_id,
            status="ok" if items else "pending",
        )

    def latest_balance_snapshots(
        self,
        *,
        provider_id: str | None = None,
        environment: str | None = None,
    ) -> list[ProviderBalanceSnapshot]:
        with self.session_factory() as session:
            allowed_groups: set[str] | None = None
            if environment:
                profile_statement = select(ProviderProfileRow.id)
                if provider_id:
                    profile_statement = profile_statement.where(ProviderProfileRow.provider_id == provider_id)
                profile_statement = profile_statement.where(ProviderProfileRow.environment == environment)
                allowed_groups = set(session.scalars(profile_statement))
                if not allowed_groups:
                    return []
            statement = select(ProviderBalanceSnapshotRow)
            if provider_id:
                statement = statement.where(ProviderBalanceSnapshotRow.provider_id == provider_id)
            if allowed_groups is not None:
                statement = statement.where(ProviderBalanceSnapshotRow.account_group.in_(allowed_groups))
            statement = statement.order_by(
                ProviderBalanceSnapshotRow.provider_id.asc(),
                ProviderBalanceSnapshotRow.account_group.asc(),
            )
            return [balance_snapshot_row_to_contract(row) for row in session.scalars(statement)]

    def upsert_balance_snapshot(self, snapshot: ProviderBalanceSnapshot) -> ProviderBalanceSnapshot:
        with self.session_factory() as session:
            statement = select(ProviderBalanceSnapshotRow).where(
                ProviderBalanceSnapshotRow.provider_id == snapshot.provider_id
            )
            if snapshot.account_group is None:
                statement = statement.where(ProviderBalanceSnapshotRow.account_group.is_(None))
            else:
                statement = statement.where(ProviderBalanceSnapshotRow.account_group == snapshot.account_group)
            row = session.scalar(statement.limit(1))
            amount = snapshot.balance.amount if snapshot.balance is not None else None
            currency = snapshot.balance.currency if snapshot.balance is not None else None
            if row is None:
                row = ProviderBalanceSnapshotRow(
                    id=snapshot.id,
                    provider_id=snapshot.provider_id,
                    account_group=snapshot.account_group,
                    balance_amount=amount,
                    currency=currency,
                    quota_remaining=snapshot.quota_remaining,
                    unit=snapshot.unit,
                    status=snapshot.status,
                    detail=snapshot.detail,
                    checked_at=snapshot.checked_at,
                )
                session.add(row)
            else:
                row.balance_amount = amount
                row.currency = currency
                row.quota_remaining = snapshot.quota_remaining
                row.unit = snapshot.unit
                row.status = snapshot.status
                row.detail = snapshot.detail
                row.checked_at = snapshot.checked_at
                row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return balance_snapshot_row_to_contract(row)

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

    def list_price_items(self, *, catalog_id: str, limit: int = 200) -> list[ProviderPriceItem]:
        with self.session_factory() as session:
            statement = (
                select(ProviderPriceItemRow)
                .where(ProviderPriceItemRow.catalog_id == catalog_id)
                .order_by(ProviderPriceItemRow.created_at.asc())
                .limit(limit)
            )
            return [price_item_row_to_contract(row) for row in session.scalars(statement)]

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
