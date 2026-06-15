"""PublishPlatformAdapter port: sandbox behavior, feature-flag selection, xiaovmao skeleton."""

from __future__ import annotations

from packages.publishing.platform_adapter import (
    SANDBOX_ADAPTER_ID,
    XIAOVMAO_ADAPTER_ID,
    PublishPayload,
    SandboxPublishAdapter,
    XiaoVmaoPublishAdapter,
    resolve_adapter_id,
    select_adapter,
)


def test_resolve_adapter_id_defaults_to_sandbox(monkeypatch):
    monkeypatch.delenv("CUTAGENT_PUBLISH_ADAPTER", raising=False)
    assert resolve_adapter_id() == SANDBOX_ADAPTER_ID


def test_resolve_adapter_id_honors_explicit_and_flag(monkeypatch):
    assert resolve_adapter_id("xiaovmao.cdp") == "xiaovmao.cdp"
    monkeypatch.setenv("CUTAGENT_PUBLISH_ADAPTER", "xiaovmao.cdp")
    assert resolve_adapter_id() == "xiaovmao.cdp"


def test_select_adapter_returns_xiaovmao_when_flagged(monkeypatch):
    monkeypatch.setenv("CUTAGENT_PUBLISH_ADAPTER", "xiaovmao.cdp")
    adapter = select_adapter()
    assert isinstance(adapter, XiaoVmaoPublishAdapter)
    assert adapter.adapter_id == XIAOVMAO_ADAPTER_ID


def test_sandbox_adapter_publishes_successfully():
    adapter = SandboxPublishAdapter()
    outcome = adapter.publish(PublishPayload(title="t", platforms=("douyin",)))
    assert outcome.success is True
    assert outcome.adapter_id == SANDBOX_ADAPTER_ID
    assert outcome.scheduled is False


def test_sandbox_adapter_simulates_failure():
    adapter = SandboxPublishAdapter()
    outcome = adapter.publish(PublishPayload(title="t", platforms=("douyin",), simulate_failure=True))
    assert outcome.success is False
    assert outcome.error_message


def test_sandbox_adapter_reports_scheduled():
    from datetime import datetime, timedelta

    adapter = SandboxPublishAdapter()
    outcome = adapter.publish(
        PublishPayload(title="t", platforms=("douyin",), scheduled_at=datetime.now() + timedelta(hours=2))
    )
    assert outcome.success is True
    assert outcome.scheduled is True


def test_sandbox_adapter_probe_accounts_returns_stub_set():
    adapter = SandboxPublishAdapter()
    accounts, available, reason = adapter.probe_accounts(case_name="case")
    assert available is True
    assert reason is None
    assert {a.platform for a in accounts} == {"douyin", "kuaishou", "shipinhao", "xiaohongshu"}


def test_xiaovmao_adapter_publish_degrades_when_app_unreachable():
    # UNVERIFIED real-platform adapter: with no live 小V猫 app reachable, publish
    # must return a non-success outcome (never fabricate a publish).
    adapter = XiaoVmaoPublishAdapter(host="127.0.0.1", port=59999)
    outcome = adapter.publish(PublishPayload(title="t", platforms=("douyin",)))
    assert outcome.success is False
    assert outcome.adapter_id == XIAOVMAO_ADAPTER_ID
    assert outcome.error_message
