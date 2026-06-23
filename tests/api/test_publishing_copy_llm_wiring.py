"""Publish-center copy node wiring: uses a real ``llm.chat`` provider when armed,
falls back to the deterministic derivation otherwise."""

from __future__ import annotations

from types import SimpleNamespace

from apps.api.services.publishing_nodes import run_copy_node
from packages.ai.gateway import ProviderResult
from packages.ai.gateway.provider_gateway import ProviderGateway
from packages.ai.prompts.registry import PromptRegistry
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore


def _item():
    return SimpleNamespace(
        title="汽车补漆案例分享，效果惊艳省钱省心",
        description="",
        publish_package_id=None,
    )


def test_run_copy_node_deterministic_without_gateway(tmp_path):
    repo = Repository()
    copy, source, invocation_id = run_copy_node(repo, None, _item())
    assert source == "deterministic"
    assert invocation_id is None
    assert copy.title


def test_run_copy_node_uses_llm_when_armed(tmp_path):
    repo = Repository()
    secret_store = LocalSecretStore(tmp_path / "secrets")
    gateway = ProviderGateway(repo, secret_store=secret_store, auto_register_real_plugins=False)
    secret_store.put("dashscope-key", secret_ref="dashscope_prod.secret")

    class _FakeLlmProvider:
        provider_id = "dashscope.llm"

        def invoke(self, call):
            return ProviderResult(
                output={
                    "title": "补漆神器实测真香",
                    "publish_content": "实测对比，效果惊艳，强烈推荐。",
                    "cover_title": "补漆神器实测",
                    "cover_subtitle": "效果惊艳省钱",
                }
            )

    gateway.register(_FakeLlmProvider())

    copy, source, invocation_id = run_copy_node(
        repo, None, _item(), gateway=gateway, prompt_registry=PromptRegistry(repo)
    )

    assert source == "llm"
    assert copy.title == "补漆神器实测真香"
    assert invocation_id
