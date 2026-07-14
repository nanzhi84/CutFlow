from decimal import Decimal

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core.contracts import (
    Money,
    ProviderBalanceSnapshot,
    ProviderStatus,
    utcnow,
)
from packages.core.storage.database import ProviderInvocationRow


def login_admin(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"email": "admin@local.cutagent", "password": "local-admin"})
    assert response.status_code == 200, response.text


class EmptySecretStore:
    def get(self, secret_ref: str) -> None:
        return None

    def put(self, plaintext: str, *, secret_ref: str | None = None) -> str:
        return secret_ref or "test.secret"

    def disable(self, secret_ref: str) -> None:
        return None


def test_provider_balances_reads_snapshots_instead_of_mock_values():
    app = create_app()

    with TestClient(app) as client:
        snapshot = ProviderBalanceSnapshot(
            id="pbs_deepseek",
            provider_id="deepseek",
            account_group="deepseek.prod",
            balance=Money(amount=Decimal("42.5"), currency="CNY"),
            quota_remaining=10,
            unit="credits",
            status="ok",
            checked_at=utcnow(),
        )
        app.state.sqlalchemy_provider_repository.upsert_balance_snapshot(snapshot)
        login_admin(client)
        response = client.get("/api/providers/balances", params={"provider_id": "deepseek"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["items"][0]["provider_id"] == "deepseek"
    # SQL Numeric(20,6) renders 42.5 as "42.500000"; assert the value, not the form.
    assert Decimal(body["items"][0]["balance"]["amount"]) == Decimal("42.5")
    assert body["items"][0]["quota_remaining"] == 10


def test_provider_balances_coalesces_shared_cloud_account_snapshots():
    app = create_app()

    with TestClient(app) as client:
        checked_at = utcnow()
        snapshots = [
            ProviderBalanceSnapshot(
                id="pbs_aliyun",
                provider_id="aliyun.billing",
                account_group="aliyun.billing.prod",
                balance=Money(amount=Decimal("113.24"), currency="CNY"),
                status="ok",
                detail="账户级总余额（OSS / DashScope 等共享）",
                checked_at=checked_at,
            ),
            ProviderBalanceSnapshot(
                id="pbs_dashscope_llm",
                provider_id="dashscope.llm",
                account_group="dashscope.llm.prod",
                status="unsupported",
                checked_at=checked_at,
            ),
            ProviderBalanceSnapshot(
                id="pbs_volcengine_billing",
                provider_id="volcengine.billing",
                account_group="volcengine.billing.prod",
                balance=Money(amount=Decimal("39.24"), currency="CNY"),
                status="ok",
                checked_at=checked_at,
            ),
            ProviderBalanceSnapshot(
                id="pbs_volcengine_tts",
                provider_id="volcengine.tts",
                account_group="volcengine.tts.prod",
                balance=Money(amount=Decimal("39.24"), currency="CNY"),
                status="ok",
                checked_at=checked_at,
            ),
            ProviderBalanceSnapshot(
                id="pbs_sandbox",
                provider_id="sandbox",
                account_group="sandbox.prod",
                status="unsupported",
                checked_at=checked_at,
            ),
            ProviderBalanceSnapshot(
                id="pbs_sandbox_generated",
                provider_id="sandbox-f7c6ad2d",
                account_group="sandbox-f7c6ad2d.prod",
                status="unsupported",
                checked_at=checked_at,
            ),
        ]
        for snapshot in snapshots:
            app.state.sqlalchemy_provider_repository.upsert_balance_snapshot(snapshot)
        login_admin(client)
        response = client.get("/api/providers/balances")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [(item["provider_id"], item["account_group"]) for item in items] == [
        ("aliyun.billing", "aliyun.shared"),
        ("volcengine.billing", "volcengine.shared"),
    ]
    # SQL Numeric(20,6) renders these as "113.240000"/"39.240000"; assert the value.
    assert Decimal(items[0]["balance"]["amount"]) == Decimal("113.24")
    assert Decimal(items[1]["balance"]["amount"]) == Decimal("39.24")


def test_provider_balances_without_snapshots_returns_pending_empty_report():
    with TestClient(create_app()) as client:
        login_admin(client)
        response = client.get("/api/providers/balances", params={"provider_id": "deepseek"})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    assert response.json()["items"] == []


def test_refresh_provider_balances_writes_snapshots_without_real_network():
    app = create_app()

    with TestClient(app) as client:
        app.state.secret_store = EmptySecretStore()
        login_admin(client)
        response = client.post("/api/providers/balances/refresh")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["items"]
    statuses = {item["status"] for item in body["items"]}
    assert statuses <= {"unconfigured", "unsupported"}
    assert all(item.get("balance") is None for item in body["items"] if item["status"] != "ok")


def test_refresh_provider_balances_rejects_unknown_body() -> None:
    with TestClient(create_app()) as client:
        login_admin(client)
        response = client.post(
            "/api/providers/balances/refresh",
            json={"__unexpected": True},
        )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation.invalid_options"


def test_provider_usage_metrics_groups_invocations_by_provider_capability_and_model():
    app = create_app()

    with TestClient(app) as client:
        now = utcnow()
        # Persist the invocations in Postgres so the SQL ops repo's usage-metrics
        # GROUP BY reads them (the in-memory runtime repo is no longer a storage
        # backend). estimated_cost is the JSONB-serialized Money the SQL aggregate
        # casts + sums.
        with app.state.sqlalchemy_session_factory() as session:
            session.add_all(
                [
                    ProviderInvocationRow(
                        id="pinv_1",
                        provider_id="deepseek",
                        model_id="deepseek-chat",
                        provider_profile_id="deepseek.prod",
                        capability_id="llm.chat",
                        status=ProviderStatus.succeeded.value,
                        estimated_cost=Money(amount=Decimal("0.10"), currency="CNY").model_dump(mode="json"),
                        started_at=now,
                    ),
                    ProviderInvocationRow(
                        id="pinv_2",
                        provider_id="deepseek",
                        model_id="deepseek-chat",
                        provider_profile_id="deepseek.prod",
                        capability_id="llm.chat",
                        status=ProviderStatus.failed.value,
                        estimated_cost=Money(amount=Decimal("0.05"), currency="CNY").model_dump(mode="json"),
                        started_at=now,
                    ),
                    ProviderInvocationRow(
                        id="pinv_3",
                        provider_id="kimi",
                        model_id="moonshot-v1",
                        provider_profile_id="kimi.prod",
                        capability_id="llm.chat",
                        status=ProviderStatus.succeeded.value,
                        estimated_cost=Money(amount=Decimal("0.20"), currency="CNY").model_dump(mode="json"),
                        started_at=now,
                    ),
                ]
            )
            session.commit()
        login_admin(client)
        response = client.get("/api/ops/provider-usage-metrics", params={"window_hours": 24})

    assert response.status_code == 200, response.text
    body = response.json()
    deepseek = next(item for item in body["items"] if item["provider_id"] == "deepseek")
    assert deepseek["capability_id"] == "llm.chat"
    assert deepseek["model_id"] == "deepseek-chat"
    assert deepseek["calls"] == 2
    assert deepseek["success_count"] == 1
    assert deepseek["success_rate"] == 0.5
    # SQL Numeric(20,6) aggregate renders 0.15 as "0.150000"; assert the value, not
    # the in-memory string form, so the business meaning (0.15 CNY) is unchanged.
    assert Decimal(deepseek["estimated_cost"]["amount"]) == Decimal("0.15")
    assert body["window_hours"] == 24
