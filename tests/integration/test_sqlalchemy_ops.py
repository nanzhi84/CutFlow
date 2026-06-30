from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select


from apps.api.main import app
from packages.core.storage.bootstrap import get_sqlalchemy_session_factory
from packages.core.storage.database import (
    ApprovalRequestRow,
    AuditEventRow,
    BudgetRow,
    CostRollupRow,
    OpsAlertEventRow,
    ProductionQualityCheckRow,
)


def sqlalchemy_session_factory():
    session_factory = get_sqlalchemy_session_factory()
    if session_factory is None:
        pytest.skip("Set CUTAGENT_STORAGE_BACKEND=sqlalchemy to run database integration tests.")
    return session_factory


def test_sqlalchemy_ops_budget_alert_cost_and_dashboard_flow_is_persisted():
    session_factory = sqlalchemy_session_factory()
    budget_id = f"budget_{uuid4().hex[:8]}"

    with TestClient(app) as client:
        viewer_login = client.post(
            "/api/auth/login",
            json={"email": "viewer@local.cutagent", "password": "local-viewer"},
        )
        assert viewer_login.status_code == 200, viewer_login.text
        forbidden = client.post(
            "/api/ops/budgets",
            json={
                "budget": {
                    "id": f"{budget_id}_forbidden",
                    "scope_type": "provider",
                    "scope_id": "sandbox",
                    "limit": {"currency": "CNY", "amount": 100},
                    "alert_threshold": 0.8,
                    "enabled": True,
                }
            },
        )
        assert forbidden.status_code == 403

        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        upserted = client.post(
            "/api/ops/budgets",
            json={
                "budget": {
                    "id": budget_id,
                    "scope_type": "provider",
                    "scope_id": "sandbox",
                    "limit": {"currency": "CNY", "amount": 100},
                    "alert_threshold": 0.8,
                    "enabled": True,
                }
            },
        )
        assert upserted.status_code == 201, upserted.text
        assert upserted.json()["limit"]["amount"] == "100"
        assert upserted.json()["limit"]["amount_micro"] == 100_000_000

        patched = client.patch(
            f"/api/ops/budgets/{budget_id}",
            json={"limit": {"currency": "CNY", "amount": 250}, "enabled": False},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["limit"]["amount"] == "250"
        assert patched.json()["enabled"] is False

        listed_budgets = client.get("/api/ops/budgets")
        assert listed_budgets.status_code == 200, listed_budgets.text
        assert any(item["id"] == budget_id for item in listed_budgets.json()["items"])

        acked = client.post("/api/ops/alerts/alert_unpriced/ack", json={"note": "seen"})
        assert acked.status_code == 200, acked.text
        assert acked.json()["status"] == "acknowledged"

        resolved = client.post(
            "/api/ops/alerts/alert_unpriced/resolve",
            json={"resolution": "covered by integration test"},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["status"] == "resolved"

        cost = client.get("/api/ops/cost-rollups")
        assert cost.status_code == 200, cost.text
        assert any(item["id"] == "cost_current_all" for item in cost.json()["items"])

        dashboard = client.get("/api/ops/dashboard")
        assert dashboard.status_code == 200, dashboard.text
        dashboard_body = dashboard.json()
        assert any(item["id"] == "alert_unpriced" for item in dashboard_body["alerts"])
        assert any(item["id"] == "cost_current_all" for item in dashboard_body["cost_rollups"])

        reconciled = client.post(
            "/api/providers/reconcile-billing",
            json={
                "provider_id": "sandbox",
                "window_start": "2026-06-01T00:00:00Z",
                "window_end": "2026-06-11T00:00:00Z",
                "dry_run": False,
            },
        )
        assert reconciled.status_code == 202, reconciled.text
        # reconcile_billing now computes a real reconciliation synchronously
        # (estimated vs recorded usage cost) instead of the old queued placeholder.
        assert reconciled.json()["status"] == "completed"
        reconciliation_run_id = reconciled.json()["reconciliation_run_id"]

    with session_factory() as session:
        budget_row = session.get(BudgetRow, budget_id)
        alert_row = session.get(OpsAlertEventRow, "alert_unpriced")
        cost_row = session.get(CostRollupRow, "cost_current_all")
        assert budget_row is not None
        assert budget_row.limit == {"currency": "CNY", "amount": "250", "amount_micro": 250_000_000}
        assert budget_row.enabled is False
        assert alert_row is not None
        assert alert_row.status == "resolved"
        assert cost_row is not None
        assert cost_row.invocations >= 0
        audit_rows = list(
            session.scalars(
                select(AuditEventRow).where(AuditEventRow.action == "billing.reconcile_completed")
            )
        )
        assert any(row.details.get("reconciliation_run_id") == reconciliation_run_id for row in audit_rows)


def test_sqlalchemy_quality_approval_and_audit_flow_is_persisted():
    session_factory = sqlalchemy_session_factory()
    approval_id = f"approval_{uuid4().hex[:8]}"

    with TestClient(app) as client:
        admin_login = client.post(
            "/api/auth/login",
            json={"email": "admin@local.cutagent", "password": "local-admin"},
        )
        assert admin_login.status_code == 200, admin_login.text

        run_check = client.post(
            "/api/runs/run_integration/quality-checks",
            json={"check_type": "auto", "result": "passed", "reason_code": "integration"},
        )
        assert run_check.status_code == 201, run_check.text
        check = run_check.json()
        assert check["target_type"] == "run"
        assert check["result"] == "passed"

        approved = client.post(
            f"/api/approval-requests/{approval_id}/approve",
            json={"reason": "looks good"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "approved"
        assert approved.json()["resource_id"] == approval_id

        rejected = client.post(
            f"/api/approval-requests/{approval_id}/reject",
            json={"reason": "changed mind"},
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "rejected"

        audit = client.get("/api/audit/events")
        assert audit.status_code == 200, audit.text
        actions = {item["action"] for item in audit.json()["items"]}
        assert "quality_check.created" in actions
        assert "approval.rejected" in actions

    with session_factory() as session:
        check_row = session.get(ProductionQualityCheckRow, check["id"])
        approval_row = session.get(ApprovalRequestRow, approval_id)
        audit_rows = session.query(AuditEventRow).all()
        assert check_row is not None
        assert check_row.target_id == "run_integration"
        assert approval_row is not None
        assert approval_row.status == "rejected"
        assert any(row.action == "quality_check.created" for row in audit_rows)
        assert any(row.action == "approval.rejected" for row in audit_rows)
