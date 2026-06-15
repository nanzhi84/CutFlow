"""Publishing Copy Node: deterministic derivation, LLM path, §2.3 schema hard-fail."""

from __future__ import annotations

import pytest

from packages.core.contracts import ErrorCode
from packages.core.workflow import NodeExecutionError
from packages.publishing.copy_node import (
    PublishCopyContext,
    derive_publish_copy,
    generate_publish_copy,
    validate_publish_copy_output,
)

_SCRIPT = "今天给大家分享一个汽车补漆案例。效果非常惊艳，省钱又省心，强烈推荐。"


def test_deterministic_copy_fills_all_four_fields():
    copy = derive_publish_copy(PublishCopyContext(script=_SCRIPT))
    assert copy.title
    assert copy.publish_content
    assert copy.cover_title
    # cover_subtitle may be empty when no distinct sentence exists; here it is set.
    assert isinstance(copy.cover_subtitle, str)
    assert len(copy.cover_title) <= 18


def test_generate_publish_copy_without_llm_is_deterministic():
    copy, source, invocation = generate_publish_copy(PublishCopyContext(script=_SCRIPT), llm_chat=None)
    assert source == "deterministic"
    assert invocation is None
    assert copy.title


def test_generate_publish_copy_uses_validated_llm_output():
    def _llm(*, context):
        return (
            {
                "title": "汽车补漆神器实测",
                "publish_content": "实测对比，效果惊艳，强烈推荐给同样有需求的朋友。",
                "cover_title": "补漆神器实测",
                "cover_subtitle": "效果惊艳省钱",
            },
            "prinv_test",
        )

    copy, source, invocation = generate_publish_copy(PublishCopyContext(script=_SCRIPT), llm_chat=_llm)
    assert source == "llm"
    assert invocation == "prinv_test"
    assert copy.title == "汽车补漆神器实测"
    assert copy.cover_subtitle == "效果惊艳省钱"


def test_generate_publish_copy_hard_fails_on_invalid_llm_output():
    def _bad_llm(*, context):
        return ({"title": 123}, None)  # title not a string

    with pytest.raises(NodeExecutionError) as exc:
        generate_publish_copy(PublishCopyContext(script=_SCRIPT), llm_chat=_bad_llm)
    assert exc.value.error.code == ErrorCode.prompt_output_invalid


def test_validate_publish_copy_output_requires_non_empty_title():
    with pytest.raises(NodeExecutionError) as exc:
        validate_publish_copy_output(
            {"title": "  ", "publish_content": "x", "cover_title": "y", "cover_subtitle": ""}
        )
    assert exc.value.error.code == ErrorCode.prompt_output_invalid
