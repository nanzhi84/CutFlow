"""Balance poller registry + per-profile dispatch.

``build_pollers`` is the single place that lists the installed plugins (order is
only diagnostic). ``query_balance`` resolves the profile's secret from the
``SecretStore``, picks the matching poller, and applies the universal
"no secret -> unconfigured" rule UNLESS the poller is structurally
secret-independent (e.g. MiniMax is always ``unsupported``). Profiles with no
matching poller report ``unsupported`` rather than crash.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from packages.core.contracts import ProviderBalanceItem, ProviderProfile, utcnow
from packages.core.storage.secret_store import SecretStore

from .port import BalancePoller
from .providers.aliyun_bss import AliyunBssPoller
from .providers.deepseek import DeepSeekPoller
from .providers.heygem import HeyGemPoller
from .providers.kimi import KimiPoller
from .providers.minimax import MiniMaxPoller
from .providers.openai_relay import OpenAIRelayPoller
from .providers.volcengine import VolcenginePoller


def build_pollers() -> list[BalancePoller]:
    """Return the installed balance pollers (one plugin per provider family)."""
    return [
        DeepSeekPoller(),
        KimiPoller(),
        OpenAIRelayPoller(),
        HeyGemPoller(),
        MiniMaxPoller(),
        AliyunBssPoller(),
        VolcenginePoller(),
    ]


def _find(pollers: list[BalancePoller], provider_id: str) -> BalancePoller | None:
    return next((p for p in pollers if p.handles(provider_id)), None)


def query_balance(
    profile: ProviderProfile,
    *,
    secret_store: SecretStore,
    client: httpx.Client,
    pollers: list[BalancePoller] | None = None,
    checked_at: datetime | None = None,
) -> ProviderBalanceItem:
    """Dispatch ``profile`` to its poller, resolving the secret first.

    Never raises: an unknown provider -> ``unsupported``; a missing secret (for a
    secret-requiring poller) -> ``unconfigured``; a crashing plugin -> ``error``.
    """
    checked = checked_at or utcnow()
    pollers = pollers if pollers is not None else build_pollers()
    poller = _find(pollers, profile.provider_id)
    if poller is None:
        return ProviderBalanceItem(
            provider_id=profile.provider_id,
            account_group=profile.id,
            checked_at=checked,
            status="unsupported",
            detail="该 provider 未接入余额查询",
        )
    secret = secret_store.get(profile.secret_ref) if profile.secret_ref else None
    if not secret and getattr(poller, "requires_secret", True):
        return ProviderBalanceItem(
            provider_id=profile.provider_id,
            account_group=profile.id,
            checked_at=checked,
            status="unconfigured",
            detail="未配置或无法读取 provider secret",
        )
    try:
        return poller.query(profile, secret=secret, client=client, checked_at=checked)
    except Exception as exc:
        from .base import scrub

        return ProviderBalanceItem(
            provider_id=profile.provider_id,
            account_group=profile.id,
            checked_at=checked,
            status="error",
            detail=scrub(f"poller 未捕获异常: {exc}", secret),
        )
