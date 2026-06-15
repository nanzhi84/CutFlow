"""PublishPlatformAdapter port + adapters (§2.1 must-retain 小V猫 / M6c).

The publish subsystem talks to platforms through the ``PublishPlatformAdapter``
port. Two implementations ship:

- ``SandboxPublishAdapter`` (``adapter_id="sandbox.publish"``): the existing
  in-process state-machine adapter. It walks the publish_item/publish_batch
  lifecycle and records ``PublishAttempt`` rows WITHOUT touching any external
  platform. This is the default and the only adapter exercised by tests.

- ``XiaoVmaoPublishAdapter`` (``adapter_id="xiaovmao.cdp"``): a port of the origin
  CDP driver (digital-human-Cutagent ``app/services/publishers/xiaovmao_adapter.py``)
  that drives the 小V猫 Electron app over its CDP endpoint. The driver/connector
  scaffolding is present and faithful to the origin, but it is **UNVERIFIED
  against the live 小V猫 app/platforms** and is NEVER reached by tests. It runs
  out-of-process and requires the real desktop app + logged-in platform accounts.

``select_adapter`` chooses the adapter from an explicit override, then the
``CUTAGENT_PUBLISH_ADAPTER`` feature flag, defaulting to the sandbox adapter so
production stays a safe no-op until the connector is wired and verified.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from packages.core.contracts import PlatformAccount

SANDBOX_ADAPTER_ID = "sandbox.publish"
XIAOVMAO_ADAPTER_ID = "xiaovmao.cdp"

# 小V猫 platform-id mapping (origin PLATFORM_KEY_MAP / PLATFORM_NAME_MAP).
XIAOVMAO_PLATFORM_KEY_MAP = {
    "douyin": "Douyin",
    "kuaishou": "KuaiShou",
    "shipinhao": "Channels",
    "xiaohongshu": "XiaoHongShu",
    "bilibili": "Bilibili",
}
XIAOVMAO_PLATFORM_NAME_MAP = {
    "douyin": "抖音",
    "kuaishou": "快手",
    "shipinhao": "视频号",
    "xiaohongshu": "小红书",
    "bilibili": "哔哩哔哩",
}


@dataclass(frozen=True)
class PublishPayload:
    """Platform-agnostic publish payload assembled from a publish item."""

    title: str
    description: str = ""
    platforms: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    location: str | None = None
    account_group: str | None = None
    case_name: str | None = None
    scheduled_at: datetime | None = None
    video_uri: str | None = None
    cover_uri: str | None = None
    manual_review: bool = False
    # Sandbox-only deterministic failure switch (parity with the existing
    # simulate_publish_failure submit knob); never used by real adapters.
    simulate_failure: bool = False


@dataclass(frozen=True)
class PublishOutcome:
    success: bool
    adapter_id: str
    external_task_id: str | None = None
    results: list[dict] = field(default_factory=list)
    error_message: str | None = None
    scheduled: bool = False


class PublishPlatformAdapter(Protocol):
    adapter_id: str

    def probe_accounts(
        self,
        *,
        account_group: str | None = None,
        case_name: str | None = None,
    ) -> tuple[list[PlatformAccount], bool, str | None]:
        """Return ``(accounts, available, unavailable_reason)``."""
        ...

    def publish(self, payload: PublishPayload) -> PublishOutcome:
        ...


@dataclass
class SandboxPublishAdapter:
    """In-process state-machine adapter. Records attempts; never touches a
    platform. Returns deterministic outcomes (honouring ``simulate_failure``)."""

    adapter_id: str = SANDBOX_ADAPTER_ID

    def probe_accounts(
        self,
        *,
        account_group: str | None = None,
        case_name: str | None = None,
    ) -> tuple[list[PlatformAccount], bool, str | None]:
        # A deterministic stub account set so the platform-accounts endpoint and
        # account-group matching are exercisable without the live app.
        accounts = [
            PlatformAccount(
                uid=f"sandbox-{platform}",
                platform=platform,
                nickname=f"沙盒账号-{platform}",
                account_group=account_group,
                is_login=True,
            )
            for platform in ("douyin", "kuaishou", "shipinhao", "xiaohongshu")
        ]
        return accounts, True, None

    def publish(self, payload: PublishPayload) -> PublishOutcome:
        if payload.manual_review:
            return PublishOutcome(
                success=True,
                adapter_id=self.adapter_id,
                results=[{"platform": p, "manual_review_ready": True} for p in payload.platforms],
            )
        if payload.simulate_failure:
            return PublishOutcome(
                success=False,
                adapter_id=self.adapter_id,
                results=[{"platform": p, "success": False} for p in payload.platforms],
                error_message="Sandbox publish adapter simulated a failed publish.",
            )
        scheduled = payload.scheduled_at is not None
        return PublishOutcome(
            success=True,
            adapter_id=self.adapter_id,
            results=[{"platform": p, "success": True, "scheduled": scheduled} for p in payload.platforms],
            scheduled=scheduled,
        )


@dataclass
class XiaoVmaoPublishAdapter:
    """小V猫 CDP adapter (M6c).

    UNVERIFIED: this adapter drives the real 小V猫 Electron desktop app over its
    CDP endpoint and submits to real 抖音/快手/视频号/小红书 accounts. It requires
    the desktop app + logged-in accounts and is intended to run out-of-process
    (e.g. on the publishing host / mac mini), NOT inside the API request path and
    NOT inside tests. ``publish`` raises ``XiaoVmaoUnavailableError`` whenever the
    real driver dependencies are missing so callers degrade to manual review,
    never silently to a fake success.
    """

    adapter_id: str = XIAOVMAO_ADAPTER_ID
    host: str = "127.0.0.1"
    port: int = 9222

    def probe_accounts(
        self,
        *,
        account_group: str | None = None,
        case_name: str | None = None,
    ) -> tuple[list[PlatformAccount], bool, str | None]:
        try:
            from packages.publishing.connectors.xiaovmao_cdp import probe_xiaovmao_accounts
        except Exception as exc:  # pragma: no cover - optional connector deps
            return [], False, f"小V猫 connector unavailable: {exc}"
        return probe_xiaovmao_accounts(
            host=self.host,
            port=self.port,
            account_group=account_group,
            case_name=case_name,
        )

    def publish(self, payload: PublishPayload) -> PublishOutcome:
        # UNVERIFIED real-platform path. The connector raises when the desktop app
        # / accounts are not reachable; we surface that as a non-success outcome
        # rather than fabricating a publish.
        try:
            from packages.publishing.connectors.xiaovmao_cdp import (
                XiaoVmaoUnavailableError,
                publish_via_xiaovmao,
            )
        except Exception as exc:  # pragma: no cover - optional connector deps
            return PublishOutcome(
                success=False,
                adapter_id=self.adapter_id,
                error_message=f"小V猫 connector unavailable: {exc}",
            )
        try:
            return publish_via_xiaovmao(payload, host=self.host, port=self.port)
        except XiaoVmaoUnavailableError as exc:  # pragma: no cover - real-platform path
            return PublishOutcome(
                success=False,
                adapter_id=self.adapter_id,
                error_message=str(exc),
            )


def resolve_adapter_id(explicit: str | None = None) -> str:
    """Resolve the publish adapter id: explicit override > feature flag > sandbox.

    ``CUTAGENT_PUBLISH_ADAPTER`` selects the production adapter (e.g.
    ``xiaovmao.cdp``). Default is the sandbox adapter so production publishing
    stays a safe, explicit no-op until the M6c connector is verified.
    """
    if explicit:
        return explicit
    return os.getenv("CUTAGENT_PUBLISH_ADAPTER") or SANDBOX_ADAPTER_ID


def select_adapter(explicit: str | None = None) -> PublishPlatformAdapter:
    adapter_id = resolve_adapter_id(explicit)
    if adapter_id == XIAOVMAO_ADAPTER_ID:
        return XiaoVmaoPublishAdapter()
    return SandboxPublishAdapter()
