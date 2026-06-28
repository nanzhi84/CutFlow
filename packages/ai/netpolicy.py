"""Outbound endpoint allow-listing for user-supplied provider base URLs (SSRF guard).

The gateway delivers a provider profile's bearer secret to whatever ``base_url``
the profile carries in ``default_options``. That value is settable at *runtime*
through the authenticated provider-profile create/patch API
(``POST/PATCH /api/providers/profiles``). Without a gate, an operator (or an
attacker who reaches that admin surface) could point ``base_url`` at an arbitrary
host and have the stored provider key delivered there — an SSRF / key-exfiltration
vector.

This module gates only those *user-supplied* URL overrides against an allow-list.
The default list is the set of provider hosts already shipped in the registry /
provider seed, so typing the real provider URL is never falsely blocked — only
off-list hosts are. The list is extendable per-deployment via
``CUTAGENT_ALLOWED_API_HOSTS`` (comma-separated hostnames) so a sanctioned proxy
can be added without a code change.

Enforced in two places (defense in depth):
- at provider-profile create/patch time (``apps/api/services/providers.py``), so a
  bad host is rejected before it is ever persisted; and
- in the provider gateway before the adapter posts the secret to ``base_url``
  (``ProviderGateway._validate_profile``), so an already-stored / seeded-then-
  tampered profile cannot leak the key on the hot path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from packages.core.config.settings import build_providers_settings

#: Sanctioned provider hosts a user may point a ``base_url`` override at by
#: default — the hosts already shipped in the registry config / provider seed, so
#: typing the real provider URL is never falsely blocked; only off-list hosts are.
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "api.openai.com",
        "openai.com",
        "neuromashv1.cn",
        "api.deepseek.com",
        "api.moonshot.cn",
        "dashscope.aliyuncs.com",
        "api.minimaxi.com",
        "openspeech.bytedance.com",
        "open.volcengineapi.com",
        "runninghub.ai",
        # Volcengine Ark (Seedance video.generate). Apex on purpose: Ark is
        # multi-region (ark.cn-beijing.volces.com etc.), so allow *.volces.com.
        "volces.com",
    }
)

#: ``default_options`` keys whose value is an outbound URL the secret may be
#: delivered to. Kept in sync with the provider adapters
#: (``packages/ai/providers/*.py``): every adapter reads ``base_url`` and a few
#: read explicit per-endpoint URL overrides too.
URL_OPTION_KEYS: tuple[str, ...] = (
    "base_url",
    "data_base_url",
    "transcription_url",
    "chat_completions_url",
)


def allowed_hosts() -> set[str]:
    """The effective allow-list: defaults + per-deployment extensions from env.

    Read at call time (not import time) so a ``monkeypatch.setenv`` / runtime
    config change is observed, matching the rest of the infra-config conventions.
    """
    hosts = set(DEFAULT_ALLOWED_HOSTS)
    for item in build_providers_settings().allowed_api_hosts.split(","):
        host = item.strip().lower()
        if host:
            hosts.add(host)
    return hosts


def _host_of(url: str) -> str:
    parsed = urlsplit(url if "://" in url else f"//{url}", scheme="https")
    return (parsed.hostname or "").lower()


def is_host_allowed(url: str) -> bool:
    host = _host_of(str(url or ""))
    if not host:
        return False
    for allowed in allowed_hosts():
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def assert_host_allowed(url: str) -> None:
    """Raise ``ValueError`` if ``url``'s host is not allow-listed."""
    if not is_host_allowed(url):
        host = _host_of(str(url or "")) or "(none)"
        raise ValueError(
            f"Outbound base_url host not allowed: {host}. "
            f"Add it to CUTAGENT_ALLOWED_API_HOSTS to permit it."
        )


def assert_options_hosts_allowed(default_options: Mapping[str, Any] | None) -> None:
    """Validate every user-supplied URL override in ``default_options``.

    Checks each key in :data:`URL_OPTION_KEYS`; a value that is missing, empty, or
    not a string is skipped (the adapter will fall back to the trusted built-in
    default). Raises ``ValueError`` for the first off-list host found.
    """
    if not default_options:
        return
    for key in URL_OPTION_KEYS:
        value = default_options.get(key)
        if isinstance(value, str) and value.strip():
            assert_host_allowed(value)
