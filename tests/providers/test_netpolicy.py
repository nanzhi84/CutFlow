"""Unit tests for the SSRF outbound-host allow-list (packages.ai.netpolicy)."""

from __future__ import annotations

import pytest

from packages.ai import netpolicy


def test_default_provider_hosts_are_allowed():
    for url in (
        "https://api.openai.com/v1",
        "https://dashscope.aliyuncs.com/api/v1",
        "https://api.minimaxi.com/v1",
        "https://www.runninghub.ai",  # subdomain of an allowed host
        "https://neuromashv1.cn/v1",
    ):
        assert netpolicy.is_host_allowed(url), url


def test_offlist_host_is_blocked():
    assert not netpolicy.is_host_allowed("https://evil.example.com/v1")
    assert not netpolicy.is_host_allowed("http://169.254.169.254/latest/meta-data")
    # Lookalike that merely contains an allowed host as a substring must NOT pass.
    assert not netpolicy.is_host_allowed("https://api.openai.com.evil.example.com/v1")


def test_empty_or_invalid_url_is_blocked():
    assert not netpolicy.is_host_allowed("")
    assert not netpolicy.is_host_allowed(None)  # type: ignore[arg-type]


def test_assert_host_allowed_raises_for_offlist():
    with pytest.raises(ValueError) as excinfo:
        netpolicy.assert_host_allowed("https://evil.example.com")
    assert "not allowed" in str(excinfo.value)


def test_env_extension_adds_a_sanctioned_proxy(monkeypatch):
    assert not netpolicy.is_host_allowed("https://proxy.internal.test/v1")
    monkeypatch.setenv("CUTAGENT_ALLOWED_API_HOSTS", "proxy.internal.test")
    assert netpolicy.is_host_allowed("https://proxy.internal.test/v1")


def test_assert_options_hosts_allowed_checks_every_url_key():
    # base_url + the explicit per-endpoint overrides the adapters read.
    netpolicy.assert_options_hosts_allowed(
        {
            "base_url": "https://dashscope.aliyuncs.com/api/v1",
            "embedding_url": "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding",
            "transcription_url": "https://dashscope.aliyuncs.com/services/audio/asr",
            "chat_completions_url": "https://api.openai.com/v1/chat/completions",
        }
    )
    with pytest.raises(ValueError):
        netpolicy.assert_options_hosts_allowed({"embedding_url": "https://evil.example.com/embed"})
    with pytest.raises(ValueError):
        netpolicy.assert_options_hosts_allowed({"transcription_url": "https://evil.example.com"})


def test_assert_options_hosts_allowed_skips_missing_and_nonstring():
    # No URL keys, empty, and non-string values are all no-ops (adapter falls back
    # to its trusted built-in default).
    netpolicy.assert_options_hosts_allowed(None)
    netpolicy.assert_options_hosts_allowed({})
    netpolicy.assert_options_hosts_allowed({"base_url": "", "timeout": 30})
