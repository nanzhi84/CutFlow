"""Wiring layer that backs the Publishing Copy Node with a real ``llm.chat``
provider (gateway + seeded ``PublishingCopy`` prompt). Returns ``None`` when no
real LLM is armed so the copy node falls back to its deterministic derivation.
"""

from __future__ import annotations

import json

import pytest

from packages.ai.gateway import ProviderResult
from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderGateway,
    ProviderRuntimeError,
)
from packages.core.contracts import ErrorCode
from packages.core.provider_idempotency import (
    build_provider_call_idempotency_key,
    is_provider_call_idempotency_key,
)
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.core.storage.secret_store import LocalSecretStore
from packages.core.workflow import NodeExecutionError
from packages.publishing.copy_llm import _extract_publish_copy_payload, build_copy_llm_chat
from packages.publishing.copy_node import PublishCopyContext

_SCRIPT = "轮毂刮花了，4S 店报价三千？别急着换，局部修复几百块就能搞定。"

_COPY = {
    "title": "轮毂刮花别急着换",
    "publish_content": "局部修复几百块搞定。",
    "cover_title": "轮毂修复省两千",
    "cover_subtitle": "几百块搞定",
}


def _gateway(tmp_path):
    repository = Repository()
    secret_store = LocalSecretStore(tmp_path / "secrets")
    object_store = LocalObjectStore(tmp_path / "objects")
    gateway = ProviderGateway(
        repository,
        secret_store=secret_store,
        object_store=object_store,
        auto_register_real_plugins=False,
    )
    return repository, gateway, secret_store


def _arm_llm_profile(repository, secret_store) -> None:
    # Arm the seeded ``dashscope.llm.prod`` profile by activating its secret; the
    # caller also registers a plugin for provider_id ``dashscope.llm``.
    secret_store.put("dashscope-key", secret_ref="dashscope_prod.secret")


class _FakeLlmProvider:
    provider_id = "dashscope.llm"

    def __init__(self, output):
        self._output = output
        self.prompts: list[str] = []
        self.calls: list[ProviderCall] = []

    def invoke(self, call: ProviderCall) -> ProviderResult:
        self.prompts.append(str(call.input.get("prompt") or ""))
        self.calls.append(call)
        return ProviderResult(output=self._output)


def test_build_copy_llm_chat_returns_none_without_real_profile(tmp_path):
    # A freshly seeded repo has a dashscope.llm.prod profile but no plugin/secret,
    # and only the sandbox llm.chat profile is otherwise present -> not "real".
    _repository, gateway, _secret_store = _gateway(tmp_path)

    port = build_copy_llm_chat(gateway=gateway, repository=_repository)

    assert port is None


def test_build_copy_llm_chat_invokes_gateway_and_returns_output(tmp_path):
    repository, gateway, secret_store = _gateway(tmp_path)
    _arm_llm_profile(repository, secret_store)
    copy_payload = {
        "title": "轮毂刮花别急着换",
        "publish_content": "局部修复几百块搞定，省下两千多。",
        "cover_title": "轮毂修复省两千",
        "cover_subtitle": "几百块搞定",
    }
    provider = _FakeLlmProvider(
        {
            "content": json.dumps(copy_payload, ensure_ascii=False),
            "intent": copy_payload,
        }
    )
    gateway.register(provider)

    port = build_copy_llm_chat(
        gateway=gateway,
        repository=repository,
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_copy",
    )
    assert port is not None

    output, invocation_id = port(
        context=PublishCopyContext(script=_SCRIPT, case_name="龙哥轮毂", description="轮毂修复")
    )

    assert output["title"] == "轮毂刮花别急着换"
    assert output["cover_title"] == "轮毂修复省两千"
    assert invocation_id
    # The seeded PublishingCopy prompt was rendered with the script and reached the provider.
    assert provider.prompts and _SCRIPT in provider.prompts[0]
    prompt_invocation = next(iter(repository.prompt_invocations.values()))
    assert prompt_invocation.run_id == "run_1"
    assert prompt_invocation.node_run_id == "nr_copy"
    assert prompt_invocation.provider_invocation_id == invocation_id


def test_copy_llm_port_raises_on_provider_error(tmp_path):
    repository, gateway, secret_store = _gateway(tmp_path)
    _arm_llm_profile(repository, secret_store)

    class _FailingProvider:
        provider_id = "dashscope.llm"

        def invoke(self, call: ProviderCall) -> ProviderResult:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "upstream 500")

    gateway.register(_FailingProvider())

    port = build_copy_llm_chat(gateway=gateway, repository=repository, case_id="case_demo")
    assert port is not None

    try:
        port(context=PublishCopyContext(script=_SCRIPT))
    except NodeExecutionError as exc:
        assert exc.error.code in {
            ErrorCode.provider_remote_failed,
            ErrorCode.provider_unsupported_option,
        }
    else:  # pragma: no cover
        raise AssertionError("expected NodeExecutionError on provider failure")


def test_extract_payload_prefers_copy_shaped_content_over_unrelated_intent():
    # intent is a dict but NOT copy-shaped; the valid copy lives in content -> the
    # content copy must win rather than the unrelated intent envelope.
    output = {"intent": {"unrelated": "x"}, "content": json.dumps(_COPY, ensure_ascii=False)}
    assert _extract_publish_copy_payload(output) == _COPY


def test_extract_payload_parses_fenced_content_json():
    fenced = "```json\n" + json.dumps(_COPY, ensure_ascii=False) + "\n```"
    assert _extract_publish_copy_payload({"content": fenced}) == _COPY


def test_extract_payload_parses_unfenced_content_json():
    raw = json.dumps(_COPY, ensure_ascii=False)
    assert _extract_publish_copy_payload({"content": raw}) == _COPY


def test_extract_payload_rejects_non_dict_output():
    with pytest.raises(NodeExecutionError) as exc:
        _extract_publish_copy_payload(["not", "a", "dict"])
    assert exc.value.error.code == ErrorCode.prompt_output_invalid


def test_publish_copy_key_is_the_workflow_key_and_is_absent_outside_a_run(tmp_path):
    # The publish-copy call used to mint an ad-hoc key, which never routes through durable
    # persistence: a node re-run bought the copy a second time. A Workflow node now injects
    # its Run-scoped key; the publish-center endpoints run outside a Run and inject none.
    repository, gateway, secret_store = _gateway(tmp_path)
    _arm_llm_profile(repository, secret_store)
    provider = _FakeLlmProvider({"content": json.dumps({"title": "t"}, ensure_ascii=False)})
    gateway.register(provider)
    node_key = build_provider_call_idempotency_key(
        run_id="run_1",
        canonical_node_id="ExportFinishedVideo",
        logical_call_slot="publish_copy",
        provider_profile_id="dashscope.llm.prod",
        input_manifest_hash="sha256:test",
    )

    from_node = build_copy_llm_chat(
        gateway=gateway,
        repository=repository,
        case_id="case_demo",
        run_id="run_1",
        idempotency_key_for_profile=lambda profile_id: build_provider_call_idempotency_key(
            run_id="run_1",
            canonical_node_id="ExportFinishedVideo",
            logical_call_slot="publish_copy",
            provider_profile_id=profile_id,
            input_manifest_hash="sha256:test",
        ),
    )
    from_node(context=PublishCopyContext(script=_SCRIPT))
    from_endpoint = build_copy_llm_chat(
        gateway=gateway, repository=repository, case_id="case_demo"
    )
    from_endpoint(context=PublishCopyContext(script=_SCRIPT))

    keys = [call.idempotency_key for call in provider.calls]
    assert keys == [node_key, None]
    assert is_provider_call_idempotency_key(keys[0])
