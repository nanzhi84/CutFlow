from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from packages.ai.gateway.provider_gateway import ProviderCall, ProviderGateway
from packages.core.contracts import (
    Budget,
    ErrorCode,
    Money,
    ReconcileBillingRequest,
    ProviderStatus,
    UpsertBudgetRequest,
    utcnow,
)
from packages.core.storage.database import (
    AuditEventRow,
    BudgetRow,
    OpsAlertEventRow,
    ProviderBalanceSnapshotRow,
    ProviderBillingReconciliationRow,
    ProviderInvocationRow,
    ProviderPriceCatalogRow,
    ProviderPriceItemRow,
    UsageMeterRecordRow,
)
from packages.core.storage.repository import Repository
from packages.ops.budget_guard import BudgetEnforcementGuard
from packages.ops.sqlalchemy_repository import SqlAlchemyOpsRepository


sqlite3.register_adapter(dict, json.dumps)
sqlite3.register_adapter(list, json.dumps)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _sqlite_ops_repository():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for table in (
        BudgetRow.__table__,
        ProviderPriceCatalogRow.__table__,
        ProviderPriceItemRow.__table__,
        ProviderInvocationRow.__table__,
        UsageMeterRecordRow.__table__,
        ProviderBalanceSnapshotRow.__table__,
        ProviderBillingReconciliationRow.__table__,
        AuditEventRow.__table__,
        OpsAlertEventRow.__table__,
    ):
        table.create(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return SqlAlchemyOpsRepository(session_factory), session_factory


def _money(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="CNY")


def _money_json(amount: str) -> dict:
    return _money(amount).model_dump(mode="json")


def _insert_provider_invocation(
    session_factory,
    *,
    invocation_id: str,
    estimated: str,
    provider_id: str = "sandbox",
    model_id: str = "tts.local",
    capability_id: str = "tts.speech",
    case_id: str = "case_budget",
    price_item_id: str | None = None,
    created_offset_hours: int = 0,
) -> None:
    created_at = utcnow() + timedelta(hours=created_offset_hours)
    with session_factory() as session:
        session.add(
            ProviderInvocationRow(
                id=invocation_id,
                case_id=case_id,
                provider_id=provider_id,
                model_id=model_id,
                provider_profile_id=f"{provider_id}.{capability_id}",
                capability_id=capability_id,
                status="succeeded",
                price_item_id=price_item_id,
                billing_status="estimated",
                estimated_cost=_money_json(estimated),
                created_at=created_at,
                updated_at=created_at,
                started_at=created_at,
                finished_at=created_at,
            )
        )
        session.commit()


def _invoke_with_budget_guard(ops_repository: SqlAlchemyOpsRepository):
    runtime_repository = Repository()
    gateway = ProviderGateway(
        runtime_repository,
        budget_guard=BudgetEnforcementGuard(ops_repository),
        auto_register_real_plugins=False,
    )
    return gateway.invoke(
        ProviderCall(
            provider_profile_id="sandbox.tts.default",
            capability_id="tts.speech",
            case_id="case_budget",
            input={"text": "hello"},
        )
    )


def test_enforced_budget_blocks_provider_gateway_and_reports_degradation():
    ops_repository, session_factory = _sqlite_ops_repository()
    ops_repository.upsert_budget(
        UpsertBudgetRequest(
            budget=Budget(
                id="budget_provider_sandbox",
                scope_type="provider",
                scope_id="sandbox",
                limit=_money("1.00"),
                enforce=True,
            )
        )
    )
    _insert_provider_invocation(
        session_factory,
        invocation_id="pinv_existing_over_budget",
        estimated="1.20",
    )

    invocation, result = _invoke_with_budget_guard(ops_repository)

    assert result is None
    assert invocation.status == ProviderStatus.failed
    assert invocation.error
    assert invocation.error.code == ErrorCode.provider_quota_exceeded
    assert "over budget" in invocation.error.message
    with session_factory() as session:
        alert = session.get(OpsAlertEventRow, "alert_budget_budget_provider_sandbox")
        assert alert is not None
        assert alert.code == "budget.exceeded"
        assert alert.severity == "critical"


def test_budget_guard_skips_expensive_evaluation_when_no_enforcing_budget():
    """The guard runs on EVERY provider call on the SQL backend. It can only ever
    block on an enabled+enforce budget, so when none exists it must early-exit
    after the cheap budget list and must NOT run evaluate_budgets() — which does a
    full spend scan + alert-sync write transaction — on the provider hot path.
    """

    class _SpyRepo:
        def __init__(self, budgets):
            self._budgets = budgets
            self.list_calls = 0
            self.evaluate_calls = 0

        def list_budgets(self, *, limit=50):
            self.list_calls += 1
            return list(self._budgets)

        def evaluate_budgets(self, *, now=None):
            self.evaluate_calls += 1
            return []

    # An enabled but warning-only (enforce=False) budget — the default, common case.
    repo = _SpyRepo(
        [
            Budget(
                id="budget_warning_only",
                scope_type="provider",
                scope_id="sandbox",
                limit=_money("1.00"),
                enforce=False,
            )
        ]
    )
    guard = BudgetEnforcementGuard(repo)

    result = guard.evaluate(call=object(), invocation=object())

    assert result is None
    assert repo.evaluate_calls == 0  # expensive scan + alert-sync write skipped
    assert repo.list_calls >= 1  # cheap enforce-prefilter still consulted budgets


def test_budget_guard_allows_warning_only_or_under_limit_budgets():
    warning_repository, warning_session_factory = _sqlite_ops_repository()
    warning_repository.upsert_budget(
        UpsertBudgetRequest(
            budget=Budget(
                id="budget_warning_only",
                scope_type="provider",
                scope_id="sandbox",
                limit=_money("1.00"),
                enforce=False,
            )
        )
    )
    _insert_provider_invocation(
        warning_session_factory,
        invocation_id="pinv_warning_only_over_budget",
        estimated="2.00",
    )

    warning_invocation, warning_result = _invoke_with_budget_guard(warning_repository)
    assert warning_result is not None
    assert warning_invocation.status == ProviderStatus.succeeded

    under_repository, under_session_factory = _sqlite_ops_repository()
    under_repository.upsert_budget(
        UpsertBudgetRequest(
            budget=Budget(
                id="budget_under_limit",
                scope_type="provider",
                scope_id="sandbox",
                limit=_money("1.00"),
                enforce=True,
            )
        )
    )
    _insert_provider_invocation(
        under_session_factory,
        invocation_id="pinv_under_budget",
        estimated="0.50",
    )

    under_invocation, under_result = _invoke_with_budget_guard(under_repository)
    assert under_result is not None
    assert under_invocation.status == ProviderStatus.succeeded


def test_reconcile_billing_aggregates_estimated_recorded_and_variance():
    repository, session_factory = _sqlite_ops_repository()
    now = utcnow()
    with session_factory() as session:
        session.add(
            ProviderPriceCatalogRow(
                id="catalog_sandbox",
                provider_id="sandbox",
                status="published",
                currency="CNY",
            )
        )
        session.add_all(
            [
                ProviderPriceItemRow(
                    id="price_tts_input",
                    catalog_id="catalog_sandbox",
                    provider_id="sandbox",
                    model_id="tts.local",
                    capability_id="tts.speech",
                    unit="input_token",
                    unit_price=_money_json("0.02"),
                    active_from=now - timedelta(days=1),
                ),
                ProviderPriceItemRow(
                    id="price_llm_call",
                    catalog_id="catalog_sandbox",
                    provider_id="sandbox",
                    model_id="llm.local",
                    capability_id="llm.chat",
                    unit="call",
                    unit_price=_money_json("0.30"),
                    active_from=now - timedelta(days=1),
                ),
            ]
        )
        session.commit()
    _insert_provider_invocation(
        session_factory,
        invocation_id="pinv_tts",
        estimated="1.00",
        price_item_id="price_tts_input",
    )
    _insert_provider_invocation(
        session_factory,
        invocation_id="pinv_llm",
        estimated="0.50",
        model_id="llm.local",
        capability_id="llm.chat",
        price_item_id="price_llm_call",
    )
    with session_factory() as session:
        session.add_all(
            [
                UsageMeterRecordRow(
                    id="usage_tts",
                    provider_invocation_id="pinv_tts",
                    provider_id="sandbox",
                    model_id="tts.local",
                    capability_id="tts.speech",
                    input_tokens=40,
                    raw_usage={},
                ),
                UsageMeterRecordRow(
                    id="usage_llm",
                    provider_invocation_id="pinv_llm",
                    provider_id="sandbox",
                    model_id="llm.local",
                    capability_id="llm.chat",
                    raw_usage={},
                ),
            ]
        )
        session.commit()

    response = repository.reconcile_billing(
        ReconcileBillingRequest(
            provider_id="sandbox",
            window_start=now - timedelta(hours=1),
            window_end=now + timedelta(hours=1),
            dry_run=False,
        ),
        request_id="req_reconcile",
    )

    assert response.status == "completed"
    assert response.estimated_cost.amount == Decimal("1.50")
    assert response.recorded_usage_cost.amount == Decimal("1.10")
    assert response.variance.amount == Decimal("-0.40")
    line_items = {(item.provider_id, item.capability_id): item for item in response.line_items}
    assert line_items[("sandbox", "tts.speech")].estimated_cost.amount == Decimal("1.00")
    assert line_items[("sandbox", "tts.speech")].recorded_usage_cost.amount == Decimal("0.80")
    assert line_items[("sandbox", "tts.speech")].variance.amount == Decimal("-0.20")
    assert line_items[("sandbox", "llm.chat")].estimated_cost.amount == Decimal("0.50")
    assert line_items[("sandbox", "llm.chat")].recorded_usage_cost.amount == Decimal("0.30")
    assert line_items[("sandbox", "llm.chat")].variance.amount == Decimal("-0.20")
    with session_factory() as session:
        row = session.get(ProviderBillingReconciliationRow, response.reconciliation_run_id)
        assert row is not None
        assert row.status == "completed"
        audit = session.scalar(
            select(AuditEventRow).where(
                AuditEventRow.action == "billing.reconcile_completed",
                AuditEventRow.resource_id == response.reconciliation_run_id,
            )
        )
        assert audit is not None


def test_reconcile_billing_dry_run_does_not_persist_result_or_audit():
    repository, session_factory = _sqlite_ops_repository()
    now = utcnow()

    response = repository.reconcile_billing(
        ReconcileBillingRequest(
            provider_id="sandbox",
            window_start=now - timedelta(hours=1),
            window_end=now + timedelta(hours=1),
            dry_run=True,
        ),
        request_id="req_reconcile_dry",
    )

    assert response.status == "completed"
    with session_factory() as session:
        assert session.get(ProviderBillingReconciliationRow, response.reconciliation_run_id) is None
        assert session.scalar(
            select(AuditEventRow).where(
                AuditEventRow.action == "billing.reconcile_completed",
                AuditEventRow.resource_id == response.reconciliation_run_id,
            )
        ) is None
