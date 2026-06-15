"""Publishing Copy Node (§2.1 must-retain / §28.3 generate-copy).

Generates publish copy for a publish item: ``title`` + ``publish_content`` +
``cover_title`` + ``cover_subtitle`` from the adopted script / case context.

Design (faithful port of the origin ``ScriptService.generate_title_and_description``
+ ``_fallback_publish_copy`` from digital-human-Cutagent ``app/services/script_service.py``):

- When a real ``llm.chat`` provider is armed, the node renders the seeded
  ``PublishingCopy`` prompt through the prompt registry, invokes the gateway, and
  validates the structured output against the ``publish_copy.output`` schema. A
  schema-invalid output AFTER the retry budget is exhausted raises
  ``prompt.output_invalid`` — the §2.3 no-silent-degrade hard-fail.
- When no real LLM is armed (e.g. the in-memory sandbox), the node derives the
  copy deterministically from the script text. This is an honest, non-fabricated
  derivation (the same behavior the origin returned when ``llm_model_id`` was
  unset), NOT a silent degrade of an LLM result.

This module is side-effect free apart from the optional ``llm_chat`` port, which
the service supplies; it never reaches a real provider on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from packages.core.contracts import ErrorCode
from packages.core.workflow import NodeExecutionError

_TITLE_DEFAULT_LIMIT = 30
_COVER_TITLE_LIMIT = 18
_COVER_SUBTITLE_LIMIT = 18

# Output schema id the registry validates against (see registry.validate_output).
PUBLISH_COPY_OUTPUT_SCHEMA_ID = "publish_copy.output"
PUBLISH_COPY_NODE_ID = "PublishingCopy"


@dataclass(frozen=True)
class PublishCopyContext:
    script: str
    case_name: str | None = None
    description: str | None = None
    product_name: str | None = None
    industry: str | None = None
    target_audience: str | None = None
    key_selling_points: str | None = None
    brand_voice: str | None = None
    ip_persona: str | None = None
    title_limit: int | None = None


@dataclass(frozen=True)
class PublishCopy:
    title: str
    publish_content: str
    cover_title: str
    cover_subtitle: str


class LlmChatPort(Protocol):
    """Returns a parsed JSON object for the rendered publishing-copy prompt, plus
    the prompt_invocation_id for provenance. Raises on provider failure so the
    caller can decide retry/fallback policy."""

    def __call__(self, *, context: PublishCopyContext) -> tuple[dict, str | None]:
        ...


# ---------------------------------------------------------------------------
# Deterministic derivation (origin _fallback_publish_copy parity)
# ---------------------------------------------------------------------------


def _limit_text(text: str, limit: int) -> str:
    cleaned = (text or "").strip()
    return cleaned[:limit]


def _split_sentences(*sources: str) -> list[str]:
    sentences: list[str] = []
    for source in sources:
        for piece in re.split(r"[。！？!?；;\n]", source or ""):
            piece = piece.strip()
            if piece:
                sentences.append(piece)
    return sentences


def _fallback_title(script_content: str, title_limit: int | None) -> str:
    cleaned = re.sub(r"\s+", " ", (script_content or "").strip())
    if not cleaned:
        return ""
    first_sentence = re.split(r"[。！？!?；;\n]", cleaned, maxsplit=1)[0].strip()
    candidate = first_sentence or cleaned
    return _limit_text(candidate, title_limit or _TITLE_DEFAULT_LIMIT)


def _fallback_publish_content(script_content: str) -> str:
    return re.sub(r"\s+\n", "\n", (script_content or "").strip())


def _fallback_cover_title(title: str, script_content: str) -> str:
    candidate = re.sub(r"\s+", " ", (title or "").strip())
    if candidate:
        return _limit_text(candidate, _COVER_TITLE_LIMIT)
    return _fallback_title(script_content, _COVER_TITLE_LIMIT)


def _fallback_cover_subtitle(script_content: str, publish_content: str, cover_title: str) -> str:
    title_text = re.sub(r"\s+", " ", (cover_title or "").strip())
    for sentence in _split_sentences(publish_content, script_content):
        candidate = sentence.strip(" ,，。！？!?；;：:、")
        if not candidate or candidate == title_text or len(candidate) < 5:
            continue
        return candidate[:_COVER_SUBTITLE_LIMIT]
    return ""


def derive_publish_copy(context: PublishCopyContext) -> PublishCopy:
    """Deterministic, non-LLM publish copy from the script (origin fallback parity)."""
    script = context.script or ""
    title = _fallback_title(script, context.title_limit)
    publish_content = _fallback_publish_content(script)
    cover_title = _fallback_cover_title(title, script)
    cover_subtitle = _fallback_cover_subtitle(script, publish_content, cover_title)
    return PublishCopy(
        title=title,
        publish_content=publish_content,
        cover_title=cover_title,
        cover_subtitle=cover_subtitle,
    )


# ---------------------------------------------------------------------------
# Schema validation (§2.3 prompt.output_invalid hard-fail)
# ---------------------------------------------------------------------------


def validate_publish_copy_output(output: dict) -> None:
    """Validate the LLM publish-copy output. Raises ``prompt.output_invalid``
    when the structure is wrong — the §2.3 no-silent-degrade hard-fail."""
    if not isinstance(output, dict):
        raise NodeExecutionError(
            ErrorCode.prompt_output_invalid,
            "Publish copy output must be a JSON object.",
        )
    required = ("title", "publish_content", "cover_title", "cover_subtitle")
    for field in required:
        if not isinstance(output.get(field), str):
            raise NodeExecutionError(
                ErrorCode.prompt_output_invalid,
                f"Publish copy output field '{field}' must be a string.",
            )
    if not output["title"].strip():
        raise NodeExecutionError(
            ErrorCode.prompt_output_invalid,
            "Publish copy output 'title' must be non-empty.",
        )


def _coerce_copy_from_output(output: dict, context: PublishCopyContext) -> PublishCopy:
    validate_publish_copy_output(output)
    fallback = derive_publish_copy(context)
    limit = context.title_limit or _TITLE_DEFAULT_LIMIT
    return PublishCopy(
        title=_limit_text(output["title"], limit) or fallback.title,
        publish_content=output["publish_content"].strip() or fallback.publish_content,
        cover_title=_limit_text(output["cover_title"], _COVER_TITLE_LIMIT) or fallback.cover_title,
        cover_subtitle=_limit_text(output["cover_subtitle"], _COVER_SUBTITLE_LIMIT),
    )


def generate_publish_copy(
    context: PublishCopyContext,
    *,
    llm_chat: LlmChatPort | None = None,
) -> tuple[PublishCopy, str, str | None]:
    """Generate publish copy.

    Returns ``(copy, source, prompt_invocation_id)`` where ``source`` is ``"llm"``
    when a real provider produced + validated the output, or ``"deterministic"``
    when the copy was derived from the script (no real LLM armed).

    Raises ``prompt.output_invalid`` (§2.3) when an LLM result is structurally
    invalid — this is NOT swallowed into a silent fallback.
    """
    if llm_chat is None:
        return derive_publish_copy(context), "deterministic", None
    output, invocation_id = llm_chat(context=context)
    copy = _coerce_copy_from_output(output, context)
    return copy, "llm", invocation_id
