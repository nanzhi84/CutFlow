from __future__ import annotations

import json
import math
import time
from typing import Any

import httpx

from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderResult,
    ProviderRuntimeError,
    parse_multimodal_embedding_dimension,
)
from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.providers.common import (
    first_value,
    poll_budget,
    request,
    require_secret,
    response_json,
    response_json_value,
)
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
        base_url = str(context.profile.default_options.get("base_url") or "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
        language_hints = call.input.get("language_hints") or context.profile.default_options.get("language_hints") or ["zh"]
        if isinstance(language_hints, str):
            language_hints = [language_hints]
        payload = {
            "model": context.profile.model_id,
            "input": {"file_urls": [audio_uri]},
            "parameters": {
                "language_hints": language_hints,
                "timestamp_alignment_enabled": bool(call.input.get("timestamp_alignment_enabled", True)),
            },
        }
        submit_response = request(
            self.client,
            "POST",
            str(context.profile.default_options.get("transcription_url") or f"{base_url}/services/audio/asr/transcription"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            },
            json_body=payload,
            timeout=float(context.profile.timeout_sec),
        )
        submit_payload = response_json(submit_response)
        task_id = task_id_from_payload(submit_payload)
        if not task_id:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "DashScope ASR submit response missing task ID.")
        context.mark_polling(task_id)
        task_payload, attempts = poll_dashscope_task(
            client=self.client,
            base_url=base_url,
            api_key=api_key,
            task_id=task_id,
            options=context.profile.default_options,
            timeout_sec=context.profile.timeout_sec,
        )
        transcription_url = _find_transcription_url(task_payload)
        if not transcription_url:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "DashScope ASR task output missing transcription_url.")
        transcription_response = request(
            self.client,
            "GET",
            transcription_url,
            timeout=float(context.profile.timeout_sec),
        )
        transcription_payload = response_json_value(transcription_response)
        text, segments = _parse_transcription_payload(transcription_payload)
        duration = float(segments[-1]["end"] if segments else _duration_from_task_payload(task_payload))
        return ProviderResult(
            output={"text": text, "segments": segments, "source": "asr"},
            audio_seconds=duration,
            raw_usage={
                "poll_attempts": attempts,
                "provider_response": task_payload,
                "transcription": transcription_payload,
            },
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
            output={"canonical": canonical, "content": content, "annotation_status": "annotated"},
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


class DashScopeMultimodalEmbeddingProvider:
    provider_id = "dashscope.multimodal_embedding"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "multimodal.embedding":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "DashScope embedding requires multimodal.embedding.",
        )
        api_key = require_secret(context)
        contents = _embedding_contents(call.input)
        if not contents:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "multimodal.embedding requires text, image_url, video_url, or contents.",
            )
        dimension = parse_multimodal_embedding_dimension(
            call.input.get("dimension"),
            context.profile.default_options.get("dimension"),
        )
        parameters: dict[str, Any] = {"dimension": dimension}
        instruct = call.input.get("instruct") or context.profile.default_options.get("instruct")
        if instruct:
            parameters["instruct"] = str(instruct)
        for key in ("fps", "res_level", "max_video_frames"):
            value = call.input.get(key) or context.profile.default_options.get(key)
            if value is not None and value != "":
                parameters[key] = value
        enable_fusion = call.input.get("enable_fusion")
        if enable_fusion is None:
            enable_fusion = context.profile.default_options.get("enable_fusion")
        if enable_fusion is not None:
            parameters["enable_fusion"] = bool(enable_fusion)
        if len(contents) > 1 and not bool(parameters.get("enable_fusion")):
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "multiple multimodal.embedding contents require enable_fusion=true.",
            )
        payload = {
            "model": context.profile.model_id,
            "input": {"contents": contents},
            "parameters": parameters,
        }
        response = request(
            self.client,
            "POST",
            _embedding_url(context.profile.default_options),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_body=payload,
            timeout=float(context.profile.timeout_sec),
        )
        result = response_json(response)
        embedding = _embedding_from_response(result)
        if not embedding:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed,
                "DashScope embedding response missing embedding vector.",
            )
        if len(embedding) != dimension:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed,
                (
                    "DashScope embedding response dimension mismatch: "
                    f"expected {dimension}, got {len(embedding)}."
                ),
            )
        return ProviderResult(
            output={
                "embedding": embedding,
                "embedding_id": str(first_value(result, "id", "request_id") or ""),
                "model": context.profile.model_id,
                "dimension": dimension,
                "normalization": str(
                    call.input.get("normalization")
                    or context.profile.default_options.get("normalization")
                    or "l2"
                ),
                "index_version": str(
                    call.input.get("index_version")
                    or context.profile.default_options.get("index_version")
                    or "clip-video-qwen3-v2"
                ),
            },
            input_tokens=(
                _usage(result, "total_tokens")
                or _usage(result, "input_tokens")
                or _embedding_input_length(contents)
            ),
            raw_usage={"provider_response": result},
        )


def _embedding_contents(call_input: dict[str, Any]) -> list[dict[str, Any] | str]:
    raw_contents = call_input.get("contents")
    if isinstance(raw_contents, list):
        return [
            content
            for item in raw_contents
            if (content := _normalize_embedding_content(item)) is not None
        ]

    contents: list[dict[str, Any] | str] = []
    text = _string_content(call_input.get("text") or call_input.get("retrieval_intent"))
    video_url = _string_content(call_input.get("video_url") or call_input.get("video_uri"))
    image_url = _string_content(call_input.get("image_url") or call_input.get("image_uri"))
    if video_url:
        contents.append({"video": video_url})
    if image_url:
        contents.append({"image": image_url})
    if text:
        contents.append({"text": text})
    return contents


def _normalize_embedding_content(value: Any) -> dict[str, Any] | str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if not isinstance(value, dict):
        return None
    normalized: dict[str, Any] = {}
    for key in ("text", "image", "video", "multi_images"):
        item = value.get(key)
        if key == "multi_images" and isinstance(item, list):
            images = [str(url).strip() for url in item if str(url).strip()]
            if images:
                normalized[key] = images
            continue
        text = _string_content(item)
        if text:
            normalized[key] = text
    return normalized or None


def _string_content(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _embedding_input_length(contents: list[dict[str, Any] | str]) -> int:
    total = 0
    for content in contents:
        if isinstance(content, str):
            total += len(content)
            continue
        total += sum(len(str(value)) for value in content.values())
    return total


class DashScopeOmniProvider:
    provider_id = "dashscope.omni"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "audio.understanding":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "DashScope Omni requires audio.understanding.",
            )
        messages = call.input.get("messages")
        if not isinstance(messages, list):
            prompt = str(call.input.get("prompt") or "")
            audio_uri = str(call.input.get("audio_uri") or "")
            if not audio_uri:
                raise ProviderRuntimeError(
                    ErrorCode.provider_unsupported_option,
                    "audio_uri is required.",
                )
            audio_format = str(call.input.get("audio_format") or "mp3").lstrip(".").lower()
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": audio_uri, "format": audio_format},
                        },
                    ],
                }
            ]
        content, usage = _chat_completion_stream(self.client, call, context, messages)
        intent = _parse_json_object(content)
        return ProviderResult(
            output={"content": content, "intent": intent or {"text": content}},
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            audio_seconds=float(call.input.get("audio_seconds") or 0.0),
            raw_usage={"provider_response": {"streamed": True, "usage": usage}},
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
    payload = {"model": context.profile.model_id, "messages": messages}
    payload.update(_chat_parameters(call, context))
    response = request(
        client,
        "POST",
        _chat_url(context.profile.default_options),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json_body=payload,
        timeout=float(context.profile.timeout_sec),
    )
    return response_json(response)


def _chat_completion_stream(
    client: httpx.Client,
    call: ProviderCall,
    context: ProviderInvocationContext,
    messages: list,
) -> tuple[str, dict[str, Any]]:
    api_key = require_secret(context)
    payload = {
        "model": context.profile.model_id,
        "messages": messages,
        "modalities": ["text"],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    parts: list[str] = []
    usage: dict[str, Any] = {}
    with client.stream(
        "POST",
        _chat_url(context.profile.default_options),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=float(context.profile.timeout_sec),
    ) as response:
        if response.status_code >= 400:
            response.read()
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed,
                f"DashScope Omni HTTP {response.status_code}.",
            )
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data in {"", "[DONE]"}:
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            raw_usage = chunk.get("usage")
            if isinstance(raw_usage, dict):
                usage = raw_usage
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str):
                    parts.append(piece)
    return "".join(parts), usage


def _chat_url(options: dict[str, Any]) -> str:
    explicit_url = options.get("chat_completions_url")
    if explicit_url:
        return str(explicit_url)
    base_url = str(options.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def _embedding_url(options: dict[str, Any]) -> str:
    explicit_url = options.get("embedding_url")
    if explicit_url:
        return str(explicit_url)
    endpoint = "/services/embeddings/multimodal-embedding/multimodal-embedding"
    default_url = f"https://dashscope.aliyuncs.com/api/v1{endpoint}"
    base_url = str(options.get("base_url") or "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
    if "compatible-mode" in base_url:
        return default_url
    if base_url.endswith(endpoint):
        return base_url
    if base_url.endswith("/services/embeddings/multimodal-embedding"):
        return f"{base_url}/multimodal-embedding"
    return f"{base_url}{endpoint}"


def _embedding_from_response(result: dict[str, Any]) -> list[float]:
    output = result.get("output")
    if isinstance(output, dict):
        embeddings = output.get("embeddings")
        if isinstance(embeddings, list):
            for item in embeddings:
                if isinstance(item, dict):
                    parsed = _coerce_embedding(item.get("embedding"))
                    if parsed:
                        return parsed
                parsed = _coerce_embedding(item)
                if parsed:
                    return parsed
        value = output.get("embedding")
        parsed = _coerce_embedding(value)
        if parsed:
            return parsed
    data = result.get("data")
    if isinstance(data, list) and data:
        item = data[0]
        if isinstance(item, dict):
            parsed = _coerce_embedding(item.get("embedding"))
            if parsed:
                return parsed
    return _coerce_embedding(result.get("embedding"))


def _coerce_embedding(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    vector: list[float] = []
    for item in value:
        try:
            component = float(item)
        except (TypeError, ValueError):
            return []
        if not math.isfinite(component):
            return []
        vector.append(component)
    return vector


def _chat_parameters(call: ProviderCall, context: ProviderInvocationContext) -> dict[str, Any]:
    defaults = _chat_defaults(call.capability_id)
    parameters: dict[str, Any] = {}
    for key in ("temperature", "top_p", "response_format", "enable_thinking", "thinking_budget"):
        value = call.input.get(key, context.profile.default_options.get(key, defaults.get(key)))
        if value is not None:
            parameters[key] = value
    return parameters


def _chat_defaults(capability_id: str) -> dict[str, Any]:
    if capability_id == "llm.chat":
        return {"temperature": 0.7}
    if capability_id == "vlm.annotation":
        return {"temperature": 0.2}
    return {}


def _message_content(result: dict[str, Any]) -> str:
    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            return str(message.get("content") or "")
    return str(result.get("content") or result.get("text") or "")


def _parse_json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        fence = lines[0].strip()
        if fence.startswith("```") and fence[3:].strip().lower() in {"", "json"}:
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines).strip()
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


def task_id_from_payload(payload: dict[str, Any]) -> str:
    output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
    value = first_value(output, "task_id", "taskId", "id")
    return str(value) if value else ""


def poll_dashscope_task(
    *,
    client: httpx.Client,
    base_url: str,
    api_key: str,
    task_id: str,
    options: dict[str, Any],
    timeout_sec: int,
    timeout_minutes: Any = None,
) -> tuple[dict[str, Any], int]:
    interval, max_attempts = poll_budget(
        options,
        default_interval=2,
        default_max_attempts=120,
        timeout_minutes=timeout_minutes,
    )
    for attempt in range(1, max_attempts + 1):
        response = request(
            client,
            "GET",
            f"{base_url}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(timeout_sec),
        )
        payload = response_json(response)
        status = _task_status(payload)
        if status in {"succeeded", "success", "completed", "finish", "finished"}:
            return payload, attempt
        if status in {"failed", "fail", "error", "canceled", "cancelled"}:
            output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
            detail = str(output.get("message") or "").strip() if isinstance(output, dict) else ""
            suffix = f": {detail}" if detail else "."
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, f"DashScope task {task_id} failed with status {status}{suffix}"
            )
        if interval > 0:
            time.sleep(interval)
    raise ProviderRuntimeError(ErrorCode.provider_timeout, f"DashScope task {task_id} timed out.")


def _task_status(payload: dict[str, Any]) -> str:
    output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
    value = first_value(output, "task_status", "taskStatus", "status", "state")
    return str(value or "").strip().lower()


def _find_transcription_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value.startswith(("http://", "https://")) else None
    if isinstance(value, list):
        for item in value:
            found = _find_transcription_url(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("transcription_url", "transcriptionUrl"):
            found = _find_transcription_url(value.get(key))
            if found:
                return found
        for nested in value.values():
            found = _find_transcription_url(nested)
            if found:
                return found
    return None


def _parse_transcription_payload(payload: Any) -> tuple[str, list[dict[str, Any]]]:
    segments: list[dict[str, Any]] = []
    fallback_texts: list[str] = []
    for transcript in _transcripts_from_payload(payload):
        local_segments = _segments_from_output(
            {
                "sentence": transcript.get("sentences", transcript.get("sentence", [])),
                "text": transcript.get("content") or transcript.get("text") or "",
            }
        )
        segments.extend(local_segments)
        fallback = str(transcript.get("content") or transcript.get("text") or "").strip()
        if fallback:
            fallback_texts.append(fallback)
    if not segments and isinstance(payload, dict):
        segments = _segments_from_output(payload)
        fallback = str(payload.get("content") or payload.get("text") or "").strip()
        if fallback:
            fallback_texts.append(fallback)
    text = "".join(segment["text"] for segment in segments).strip()
    if not text:
        text = "".join(fallback_texts).strip()
    if text and not segments:
        segments = [{"start": 0.0, "end": 0.0, "text": text}]
    return text, segments


def _transcripts_from_payload(value: Any) -> list[dict[str, Any]]:
    transcripts: list[dict[str, Any]] = []
    if isinstance(value, list):
        for item in value:
            transcripts.extend(_transcripts_from_payload(item))
        return transcripts
    if not isinstance(value, dict):
        return transcripts
    nested = value.get("transcripts")
    if isinstance(nested, list):
        transcripts.extend(item for item in nested if isinstance(item, dict))
    elif isinstance(nested, dict):
        transcripts.append(nested)
    elif "sentences" in value or "sentence" in value:
        transcripts.append(value)
    return transcripts


def _duration_from_task_payload(payload: dict[str, Any]) -> float:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    value = usage.get("duration") if isinstance(usage, dict) else None
    if value in (None, ""):
        return 0.0
    duration = float(value)
    return duration / 1000.0 if duration > 600 else duration


def _segments_from_output(output: dict[str, Any]) -> list[dict[str, Any]]:
    sentence_output = output.get("sentence", output.get("sentences", []))
    if isinstance(sentence_output, dict):
        sentence_output = [sentence_output]
    sentences = sentence_output if isinstance(sentence_output, list) else []
    segments: list[dict[str, Any]] = []
    for sentence in sentences:
        if not isinstance(sentence, dict):
            continue
        words = sentence.get("words")
        if isinstance(words, list) and words:
            segments.extend(_segments_from_words(words))
            continue
        text = str(sentence.get("text") or "").strip()
        if not text:
            continue
        start = float(sentence.get("begin_time", sentence.get("start_time", sentence.get("start", 0))) or 0) / 1000.0
        end = float(sentence.get("end_time", sentence.get("end", start * 1000)) or start * 1000) / 1000.0
        if end <= start:
            end = start + 0.3
        segments.append({"start": start, "end": max(end, start), "text": text})
    if not segments:
        text = str(output.get("content") or output.get("text") or "").strip()
        if text:
            segments.append({"start": 0.0, "end": 0.0, "text": text})
    return segments


def _segments_from_words(words: list[Any]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    current_parts: list[str] = []
    current_start_ms: int | None = None
    current_end_ms: int | None = None
    for word in words:
        if not isinstance(word, dict):
            continue
        token = str(word.get("text") or "").strip()
        punctuation = str(word.get("punctuation") or "").strip()
        if not token and not punctuation:
            continue
        begin_ms = int(word.get("begin_time", word.get("start_time", 0)) or 0)
        end_ms = int(word.get("end_time", word.get("end", begin_ms)) or begin_ms)
        if end_ms <= begin_ms:
            end_ms = begin_ms + 300
        if current_start_ms is None:
            current_start_ms = begin_ms
            current_end_ms = end_ms
        else:
            current_end_ms = max(current_end_ms or end_ms, end_ms)
        current_parts.append(f"{token}{punctuation}")
        current_duration = ((current_end_ms or 0) - (current_start_ms or 0)) / 1000.0
        if punctuation in {"。", "！", "？", "；", ".", "!", "?", ";"} or current_duration >= 6.0:
            _append_word_segment(segments, current_parts, current_start_ms, current_end_ms)
            current_parts = []
            current_start_ms = None
            current_end_ms = None
    _append_word_segment(segments, current_parts, current_start_ms, current_end_ms)
    return segments


def _append_word_segment(
    segments: list[dict[str, Any]],
    parts: list[str],
    start_ms: int | None,
    end_ms: int | None,
) -> None:
    text = "".join(parts).strip()
    if not text:
        return
    start = float(start_ms or 0) / 1000.0
    end = float(end_ms or (start_ms or 0)) / 1000.0
    segments.append({"start": start, "end": max(end, start), "text": text})
