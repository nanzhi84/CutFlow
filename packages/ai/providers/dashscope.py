from __future__ import annotations

import json
from typing import Any

import httpx

from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderResult,
    ProviderRuntimeError,
)
from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.providers.common import request, require_secret, response_json
from packages.core.contracts import ErrorCode


class DashScopeASRProvider:
    provider_id = "dashscope.asr"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "asr.transcribe":
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "DashScope ASR requires asr.transcribe.")
        api_key = require_secret(context)
        audio_uri = str(call.input.get("audio_uri") or "")
        if not audio_uri:
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "audio_uri is required.")
        # Contract-level HTTP shim for tests/live harnesses. The original stack uses
        # DashScope Recognition.call; this endpoint name preserves that semantic in
        # a mockable request instead of importing the SDK into the clean-slate core.
        payload = {
            "model": context.profile.model_id,
            "file": audio_uri,
            "language_hints": call.input.get("language_hints") or ["zh"],
            "timestamp_alignment_enabled": bool(call.input.get("timestamp_alignment_enabled", True)),
        }
        response = request(
            self.client,
            "POST",
            str(context.profile.default_options.get("recognition_url") or "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/recognition"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_body=payload,
            timeout=float(context.profile.timeout_sec),
        )
        result = response_json(response)
        output = result.get("output") if isinstance(result.get("output"), dict) else result
        segments = _segments_from_output(output)
        text = str(output.get("text") or "".join(segment["text"] for segment in segments))
        duration = float(segments[-1]["end"] if segments else output.get("duration") or 0)
        return ProviderResult(
            output={"text": text, "segments": segments, "source": "asr"},
            audio_seconds=duration,
            raw_usage={"provider_response": result},
        )


class DashScopeVLMProvider:
    provider_id = "dashscope.vlm"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "vlm.annotation":
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "DashScope VLM requires vlm.annotation.")
        if not isinstance(call.input.get("messages"), list):
            prompt = str(call.input.get("prompt") or "")
            asset_uri = str(call.input.get("asset_uri") or "")
            if asset_uri:
                media_type = "image_url" if str(call.input.get("asset_kind") or "").lower() == "image" else "video_url"
                call = call.model_copy(
                    update={
                        "input": {
                            **call.input,
                            "messages": [
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": prompt},
                                        {"type": media_type, media_type: {"url": asset_uri}},
                                    ],
                                }
                            ],
                        }
                    }
                )
        result = _chat_completion(self.client, call, context)
        content = _message_content(result)
        canonical = _parse_json_object(content)
        return ProviderResult(
            output={"canonical": canonical, "annotation_status": "annotated"},
            input_tokens=_usage(result, "prompt_tokens"),
            output_tokens=_usage(result, "completion_tokens"),
            image_count=1,
            raw_usage={"provider_response": result},
        )


class DashScopeLLMProvider:
    provider_id = "dashscope.llm"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "llm.chat":
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "DashScope LLM requires llm.chat.")
        result = _chat_completion(self.client, call, context)
        content = _message_content(result)
        return ProviderResult(
            output={"content": content, "intent": _parse_json_object(content) or {"text": content}},
            input_tokens=_usage(result, "prompt_tokens"),
            output_tokens=_usage(result, "completion_tokens"),
            raw_usage={"provider_response": result},
        )


def _chat_completion(
    client: httpx.Client,
    call: ProviderCall,
    context: ProviderInvocationContext,
) -> dict[str, Any]:
    api_key = require_secret(context)
    messages = call.input.get("messages")
    if not isinstance(messages, list):
        prompt = str(call.input.get("prompt") or call.input.get("script") or "")
        messages = [{"role": "user", "content": prompt}]
    payload = {
        "model": context.profile.model_id,
        "messages": messages,
        "temperature": call.input.get("temperature", context.profile.default_options.get("temperature", 0.2)),
    }
    response = request(
        client,
        "POST",
        str(
            context.profile.default_options.get("base_url")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        ),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json_body=payload,
        timeout=float(context.profile.timeout_sec),
    )
    return response_json(response)


def _message_content(result: dict[str, Any]) -> str:
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            return str(message.get("content") or "")
    return str(result.get("content") or result.get("text") or "")


def _parse_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _usage(result: dict[str, Any], key: str) -> int:
    usage = result.get("usage")
    if isinstance(usage, dict):
        return int(usage.get(key) or 0)
    return 0


def _segments_from_output(output: dict[str, Any]) -> list[dict[str, Any]]:
    sentences = output.get("sentence") if isinstance(output.get("sentence"), list) else []
    segments: list[dict[str, Any]] = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        text = str(sentence.get("text") or "").strip()
        if not text:
            continue
        start = float(sentence.get("begin_time") or sentence.get("start_time") or 0) / 1000.0
        end = float(sentence.get("end_time") or sentence.get("end") or 0) / 1000.0
        segments.append({"start": start, "end": max(end, start), "text": text})
    return segments
