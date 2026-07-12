"""火山豆包语音 TTS provider (``volcengine.tts``).

Two auth planes behind one ``tts.speech`` capability:

- **data plane** — synthesis ``/api/v1/tts`` (header ``x-api-key``) and clone
  upload ``/api/v1/mega_tts/audio/upload`` (header ``Authorization: Bearer;<key>``
  + ``Resource-Id: volc.megatts.voiceclone``, body carries ``appid``);
- **management plane** — sync cloned voices + issue/list the x-api-key, via
  AK/SK V4 signing in :class:`VolcSpeechOpenAPI`.

The profile secret is the account ``AccessKeyId:SecretAccessKey`` pair; the
data-plane x-api-key is auto-issued from it (path B) and cached per appid.

Auth shapes verified against the live account (synthesis + management); the clone
upload auth was probed (Bearer;<key> reaches the business layer asking for appid).
The exact ``mega_tts/audio/upload`` body fields (source/language/model_type) are
best-effort from the docs and overridable via options — confirm with a real
training run before production (it consumes a paid clone slot).
"""

from __future__ import annotations

import base64
import json
import uuid
from decimal import Decimal
from pathlib import Path

import httpx

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderResult,
    ProviderRuntimeError,
)
from packages.ai.providers.common import (
    map_http_status,
    money_cny,
    option,
    request,
    require_secret,
    response_json,
)
from packages.ai.providers.volc_openapi import VolcSpeechOpenAPI
from packages.core.contracts import (
    ArtifactKind,
    ErrorCode,
    SpeechSegmentTiming,
    SpeechTiming,
    SpeechTokenTiming,
    TtsSpeechOutput,
)

_DEFAULT_DATA_BASE_URL = "https://openspeech.bytedance.com"
_CLONE_RESOURCE_ID = "volc.megatts.voiceclone"
# v3 单向流式 (HTTP chunked, 逐行 JSON) 端点与复刻/预设音色的资源 id。v1 的 data
# 接口对 volcano_icl 复刻音色不回时间戳；v3 支持 ``audio_params.enable_timestamp``。
_V3_SYNTH_PATH = "/api/v3/tts/unidirectional"
# 复刻音色（S_ 前缀）用 ``volc.megatts.default`` 才会回字级时间戳；``seed-icl-2.0``
# 能合成但 ``sentence.words`` 恒空（实测）。预设/官方音色用 ``seed-tts-2.0``。
_V3_CLONE_RESOURCE_ID = "volc.megatts.default"
_V3_TTS_RESOURCE_ID = "seed-tts-2.0"
_V3_STREAM_DONE_CODE = 20000000


class VolcengineTTSProvider:
    provider_id = "volcengine.tts"
    # 火山豆包语音大模型约 6.5→4.9 元/万字符；取保守 0.65 元/千字，上线前按账单校准。
    cost_per_1k_chars = Decimal("0.65")

    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self._api_key_cache: dict[str, str] = {}

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "tts.speech":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                f"Volcengine TTS cannot run {call.capability_id}.",
            )
        operation = str(call.input.get("operation") or "speech")
        if operation == "speech":
            return self._speech(call, context)
        if operation == "clone":
            return self._clone(call, context)
        if operation == "voice_list":
            return self._voice_list(call, context)
        if operation == "train_status":
            return self._train_status(call, context)
        # design intentionally unsupported: Volcengine has no text-design API and
        # the feature is removed product-wide.
        raise ProviderRuntimeError(
            ErrorCode.provider_unsupported_option,
            f"Volcengine TTS operation {operation} is not supported.",
        )

    # --- credentials ---------------------------------------------------------

    def _appid(self, context: ProviderInvocationContext) -> str:
        appid = str(option(context, "appid", "") or "").strip()
        if not appid:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option, "Volcengine appid is required."
            )
        return appid

    def _openapi(self, context: ProviderInvocationContext) -> VolcSpeechOpenAPI:
        secret = require_secret(context)
        access_key_id, _, secret_access_key = secret.partition(":")
        if not access_key_id or not secret_access_key:
            raise ProviderRuntimeError(
                ErrorCode.provider_auth_failed,
                "Volcengine secret must be 'access_key_id:secret_access_key'.",
            )
        return VolcSpeechOpenAPI(self.client, access_key_id, secret_access_key)

    def _x_api_key(self, context: ProviderInvocationContext) -> str:
        appid = self._appid(context)
        cached = self._api_key_cache.get(appid)
        if cached:
            return cached
        name = str(option(context, "api_key_name", "cutagent-tts"))
        key = self._openapi(context).ensure_api_key(appid, name)
        self._api_key_cache[appid] = key
        return key

    # --- operations ----------------------------------------------------------

    def _speech(self, call: ProviderCall, context: ProviderInvocationContext) -> ProviderResult:
        text = str(call.input.get("text") or "")
        voice_id = str(call.input.get("voice_id") or option(context, "voice_id") or "")
        if not text.strip() or not voice_id.strip():
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option, "Text and voice_id are required."
            )
        if str(option(context, "api_version", "v1")) == "v3":
            return self._speech_v3(call, context, text=text, voice_id=voice_id)
        x_api_key = self._x_api_key(context)
        base_url = str(option(context, "data_base_url", _DEFAULT_DATA_BASE_URL)).rstrip("/")
        cluster = str(option(context, "cluster", "volcano_icl"))
        fmt = str(option(context, "format", "mp3"))
        payload = {
            "app": {"cluster": cluster},
            "user": {"uid": str(option(context, "uid", "cutagent"))},
            "audio": {
                "voice_type": voice_id,
                "encoding": fmt,
                "speed_ratio": float(call.input.get("speed") or option(context, "speed", 1.0)),
            },
            "request": {
                "reqid": call.idempotency_key or f"cutagent-{uuid.uuid4().hex}",
                "text": text,
                "operation": "query",
                # ``with_timestamp`` is the provider-supported original-text timing
                # mode.  ``with_frontend``/``frontend_type`` keep compatibility with
                # older clusters that return ``addition.frontend.words``.
                "with_timestamp": 1,
                "with_frontend": 1,
                "frontend_type": "unitTson",
            },
        }
        response = request(
            self.client,
            "POST",
            f"{base_url}/api/v1/tts",
            headers={"x-api-key": x_api_key, "Content-Type": "application/json"},
            json_body=payload,
            timeout=float(context.profile.timeout_sec),
        )
        result = response_json(response)
        code = result.get("code")
        if code != 3000:
            message = str(result.get("message") or "Volcengine TTS failed.")
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, f"Volcengine TTS code={code}: {message}"
            )
        audio_b64 = str(result.get("data") or "")
        if not audio_b64:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, "Volcengine TTS response missing audio."
            )
        try:
            audio_bytes = base64.b64decode(audio_b64)
        except ValueError as exc:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, "Volcengine TTS audio is invalid."
            ) from exc
        artifact = context.store_media_bytes(
            content=audio_bytes,
            filename=f"{call.idempotency_key or 'volcengine-tts'}.{fmt}",
            purpose="generated-audio",
            kind=ArtifactKind.audio_tts,
            call=call,
        )
        addition = _json_object(result.get("addition"))
        duration = float(addition.get("duration") or 0) / 1000.0
        if artifact.media_info and artifact.media_info.duration_sec:
            duration = artifact.media_info.duration_sec
        estimated = (Decimal(len(text)) / Decimal(1000)) * self.cost_per_1k_chars
        output = TtsSpeechOutput(
            audio_artifact_id=artifact.id,
            audio_uri=artifact.uri,
            duration_sec=duration,
            voice_id=voice_id,
            timing=_timing_from_volcengine_addition(addition, duration=duration, text=text),
        ).model_dump(mode="json")
        return ProviderResult(
            output=output,
            input_tokens=len(text),
            audio_seconds=duration,
            raw_usage={"characters": len(text)},
            estimated_cost=money_cny(estimated),
        )

    def _speech_v3(
        self,
        call: ProviderCall,
        context: ProviderInvocationContext,
        *,
        text: str,
        voice_id: str,
    ) -> ProviderResult:
        """v3 单向流式合成：拿回 v1 对复刻音色不返回的字级时间戳。"""
        x_api_key = self._x_api_key(context)
        appid = self._appid(context)
        base_url = str(option(context, "data_base_url_v3", _DEFAULT_DATA_BASE_URL)).rstrip("/")
        fmt = str(option(context, "format", "mp3"))
        sample_rate = int(option(context, "sample_rate", 24000))
        default_resource = (
            _V3_CLONE_RESOURCE_ID if voice_id.startswith("S_") else _V3_TTS_RESOURCE_ID
        )
        resource_id = str(option(context, "resource_id", default_resource))
        speed = float(call.input.get("speed") or option(context, "speed", 1.0))
        audio_params: dict = {
            "format": fmt,
            "sample_rate": sample_rate,
            "enable_timestamp": True,
        }
        # v3 语速是整数 ``speech_rate``（0=常速，[-50,100]，+100≈2x、-50≈0.5x），与 v1
        # 的 ``speed_ratio`` 量纲不同；把倍率线性映射过去，常速时不下发。
        if abs(speed - 1.0) > 1e-6:
            audio_params["speech_rate"] = max(-50, min(100, round((speed - 1.0) * 100)))
        req_params: dict = {
            "text": text,
            "speaker": voice_id,
            "audio_params": audio_params,
            "additions": json.dumps(
                {"disable_markdown_filter": True, "enable_timestamp": True},
                ensure_ascii=False,
            ),
        }
        model = option(context, "v3_model")
        if model:
            req_params["model"] = str(model)
        payload = {"user": {"uid": str(option(context, "uid", "cutagent"))}, "req_params": req_params}
        # New-console single-key auth (``X-Api-Key`` = the issued x-api-key). The
        # legacy ``X-Api-App-Id`` + ``X-Api-Access-Key`` pair is rejected for this
        # account's v3 grants ("requested grant not found in SaaS storage").
        headers = {
            "X-Api-Key": x_api_key,
            "X-Api-App-Id": appid,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": call.idempotency_key or f"cutagent-{uuid.uuid4().hex}",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        audio_bytes, sentences = self._stream_v3(
            f"{base_url}{_V3_SYNTH_PATH}",
            headers=headers,
            payload=payload,
            timeout=float(context.profile.timeout_sec),
        )
        if not audio_bytes:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, "Volcengine TTS v3 response missing audio."
            )
        artifact = context.store_media_bytes(
            content=audio_bytes,
            filename=f"{call.idempotency_key or 'volcengine-tts'}.{fmt}",
            purpose="generated-audio",
            kind=ArtifactKind.audio_tts,
            call=call,
        )
        timing = _timing_from_v3_sentences(sentences, text=text)
        duration = 0.0
        if artifact.media_info and artifact.media_info.duration_sec:
            duration = artifact.media_info.duration_sec
        elif timing and timing.tokens:
            duration = timing.tokens[-1].end
        estimated = (Decimal(len(text)) / Decimal(1000)) * self.cost_per_1k_chars
        output = TtsSpeechOutput(
            audio_artifact_id=artifact.id,
            audio_uri=artifact.uri,
            duration_sec=duration,
            voice_id=voice_id,
            timing=timing,
        ).model_dump(mode="json")
        return ProviderResult(
            output=output,
            input_tokens=len(text),
            audio_seconds=duration,
            raw_usage={"characters": len(text)},
            estimated_cost=money_cny(estimated),
        )

    def _stream_v3(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict,
        timeout: float,
    ) -> tuple[bytes, list]:
        """POST 到 v3 端点并把 chunked JSON 流拼成 (音频字节, sentence 事件列表)。"""
        audio = bytearray()
        sentences: list = []
        try:
            with self.client.stream(
                "POST", url, headers=headers, json=payload, timeout=timeout
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise map_http_status(response.status_code, response.text)
                for line in response.iter_lines():
                    event = _parse_v3_line(line)
                    if event is None:
                        continue
                    code = event.get("code", 0)
                    if code == _V3_STREAM_DONE_CODE:
                        break
                    if code:
                        message = str(event.get("message") or "Volcengine TTS v3 failed.")
                        raise ProviderRuntimeError(
                            ErrorCode.provider_remote_failed,
                            f"Volcengine TTS v3 code={code}: {message}",
                        )
                    chunk = event.get("data")
                    if chunk:
                        try:
                            audio.extend(base64.b64decode(chunk))
                        except (ValueError, TypeError) as exc:
                            raise ProviderRuntimeError(
                                ErrorCode.provider_remote_failed,
                                "Volcengine TTS v3 audio chunk is invalid.",
                            ) from exc
                    sentence = event.get("sentence")
                    if sentence:
                        sentences.append(sentence)
        except httpx.TimeoutException as exc:
            raise ProviderRuntimeError(
                ErrorCode.provider_timeout, "Volcengine TTS v3 request timed out."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, str(exc)) from exc
        return bytes(audio), sentences

    def _voice_list(self, call: ProviderCall, context: ProviderInvocationContext) -> ProviderResult:
        appid = self._appid(context)
        voices = self._openapi(context).list_voices(appid)
        return ProviderResult(output={"voices": voices})

    def _train_status(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        """Poll one platform-initiated clone's status (ready/training/failed)."""
        appid = self._appid(context)
        speaker_id = str(call.input.get("voice_id") or "")
        status = self._openapi(context).get_train_status(appid, speaker_id)
        return ProviderResult(output={"voice_id": speaker_id, "status": status or "training"})

    def _clone(self, call: ProviderCall, context: ProviderInvocationContext) -> ProviderResult:
        appid = self._appid(context)
        api = self._openapi(context)
        speaker_id = str(call.input.get("voice_id") or "").strip()
        if not speaker_id:
            free = api.list_free_slots(appid)
            if not free:
                raise ProviderRuntimeError(
                    ErrorCode.provider_quota_exceeded,
                    "No free Volcengine clone slot available (purchase more quota).",
                )
            speaker_id = free[0]
        audio_path = self._reference_audio_path(call, context)
        audio_bytes = audio_path.read_bytes()
        audio_format = audio_path.suffix.lstrip(".").lower() or "mp3"
        x_api_key = self._x_api_key(context)
        base_url = str(option(context, "data_base_url", _DEFAULT_DATA_BASE_URL)).rstrip("/")
        payload = {
            "appid": appid,
            "speaker_id": speaker_id,
            "audios": [
                {
                    "audio_bytes": base64.b64encode(audio_bytes).decode("ascii"),
                    "audio_format": audio_format,
                }
            ],
            "source": int(option(context, "clone_source", 2)),
            "language": int(option(context, "clone_language", 0)),
            "model_type": int(option(context, "clone_model_type", 1)),
        }
        response = request(
            self.client,
            "POST",
            f"{base_url}/api/v1/mega_tts/audio/upload",
            headers={
                "Authorization": f"Bearer;{x_api_key}",
                "Resource-Id": _CLONE_RESOURCE_ID,
                "Content-Type": "application/json",
            },
            json_body=payload,
            timeout=float(context.profile.timeout_sec),
        )
        result = response_json(response)
        code = result.get("code")
        # Strict success allow-list (mirrors _speech's code != 3000): a missing or
        # non-zero code is a failure, never silently filed as training.
        if code not in (0, 3000):
            message = str(result.get("message") or "Volcengine clone upload failed.")
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, f"Volcengine clone code={code}: {message}"
            )
        display_name = str(call.input.get("display_name") or call.input.get("name") or speaker_id)
        # Clone is async: return status=training. The service layer polls
        # VolcSpeechOpenAPI.get_train_status until Success → ready.
        return ProviderResult(
            output={"voice_id": speaker_id, "display_name": display_name, "status": "training"}
        )

    def _reference_audio_path(self, call: ProviderCall, context: ProviderInvocationContext) -> Path:
        reference_uri = call.input.get("reference_audio_uri")
        if isinstance(reference_uri, str) and reference_uri:
            return context.local_path_for_uri(reference_uri)
        upload_id = call.input.get("reference_upload_session_id")
        if isinstance(upload_id, str) and upload_id:
            upload = context.repository.uploads.get(upload_id)
            if upload is None:
                raise ProviderRuntimeError(
                    ErrorCode.provider_unsupported_option, "Voice reference upload is missing."
                )
            if upload.object_uri:
                return context.local_path_for_uri(upload.object_uri)
            if upload.local_temp_path:
                return context.local_path_for_uri(upload.local_temp_path)
        raise ProviderRuntimeError(
            ErrorCode.provider_unsupported_option, "Reference audio is required."
        )


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_value(value: object) -> object:
    if not isinstance(value, str) or not value.strip():
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _timing_from_volcengine_addition(
    addition: dict, *, duration: float, text: str
) -> SpeechTiming | None:
    """Normalize both legacy ``addition.frontend`` and newer subtitle payloads."""

    frontend = _json_object(addition.get("frontend")) or addition
    raw_words = frontend.get("words")
    if not isinstance(raw_words, list):
        subtitles = _json_value(frontend.get("subtitles"))
        if isinstance(subtitles, dict):
            raw_words = subtitles.get("words") or subtitles.get("subtitles")
        elif isinstance(subtitles, list):
            raw_words = [
                word
                for subtitle in subtitles
                if isinstance(subtitle, dict)
                for word in (
                    subtitle.get("words") if isinstance(subtitle.get("words"), list) else [subtitle]
                )
            ]
    tokens: list[SpeechTokenTiming] = []
    for item in raw_words or []:
        if not isinstance(item, dict):
            continue
        token = str(item.get("word") or item.get("text") or "").strip()
        if not token:
            continue
        try:
            if "start" in item or "end" in item:
                start = _timestamp_seconds(item.get("start"))
                end = _timestamp_seconds(item.get("end"))
            else:
                start = max(0.0, float(item.get("start_time") or 0.0) / 1000.0)
                end = max(0.0, float(item.get("end_time") or 0.0) / 1000.0)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        tokens.append(SpeechTokenTiming(text=token, start=start, end=end))
    if not tokens:
        return None
    return SpeechTiming(
        segments=[
            SpeechSegmentTiming(
                text=text,
                start=tokens[0].start,
                end=tokens[-1].end,
            )
        ],
        tokens=tokens,
        granularity="character" if all(len(item.text) == 1 for item in tokens) else "token",
        text_basis="original",
    )


def _timestamp_seconds(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, number)


def _parse_v3_line(line: object) -> dict | None:
    """Decode one line of the v3 chunked/SSE stream into a JSON object (or None)."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "ignore")
    if not isinstance(line, str):
        return None
    text = line.strip()
    if text.startswith("data:"):  # tolerate the SSE (``.../unidirectional/sse``) framing too
        text = text[len("data:") :].strip()
    if not text or text == "[DONE]":
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _v3_word_bounds(item: dict) -> tuple[float, float]:
    """Read one v3 word's (start, end) in seconds.

    The verified ``volc.megatts.default`` shape is camelCase ``startTime`` /
    ``endTime`` already in seconds; snake_case ``start_time`` / ``end_time`` (the
    legacy v1 millisecond shape) is tolerated as a fallback.
    """
    if "startTime" in item or "endTime" in item:
        return _timestamp_seconds(item.get("startTime")), _timestamp_seconds(item.get("endTime"))
    return (
        _timestamp_seconds(item.get("start_time")) / 1000.0,
        _timestamp_seconds(item.get("end_time")) / 1000.0,
    )


def _timing_from_v3_sentences(sentences: list, *, text: str) -> SpeechTiming | None:
    """Build canonical timing from the v3 stream's ``sentence`` events.

    Each ``sentence`` carries a ``words`` list of ``{word, startTime, endTime}``
    (seconds). Word order is the stream order; a sentence may also arrive as a JSON
    string, so it is parsed defensively.
    """

    tokens: list[SpeechTokenTiming] = []
    for sentence in sentences:
        obj = sentence if isinstance(sentence, dict) else _json_object(sentence)
        raw_words = obj.get("words")
        if not isinstance(raw_words, list):
            continue
        for item in raw_words:
            if not isinstance(item, dict):
                continue
            token = str(item.get("word") or item.get("text") or "")
            if not token.strip():
                continue
            start, end = _v3_word_bounds(item)
            if end <= start:
                continue
            tokens.append(SpeechTokenTiming(text=token.strip(), start=start, end=end))
    if not tokens:
        return None
    return SpeechTiming(
        segments=[
            SpeechSegmentTiming(text=text, start=tokens[0].start, end=tokens[-1].end)
        ],
        tokens=tokens,
        granularity="character" if all(len(item.text) == 1 for item in tokens) else "token",
        text_basis="original",
    )
