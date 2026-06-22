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
from packages.ai.providers.common import money_cny, option, request, require_secret, response_json
from packages.ai.providers.volc_openapi import VolcSpeechOpenAPI
from packages.core.contracts import ArtifactKind, ErrorCode

_DEFAULT_DATA_BASE_URL = "https://openspeech.bytedance.com"
_CLONE_RESOURCE_ID = "volc.megatts.voiceclone"


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
        addition = result.get("addition") if isinstance(result.get("addition"), dict) else {}
        duration = float(addition.get("duration") or 0) / 1000.0
        if artifact.media_info and artifact.media_info.duration_sec:
            duration = artifact.media_info.duration_sec
        estimated = (Decimal(len(text)) / Decimal(1000)) * self.cost_per_1k_chars
        return ProviderResult(
            output={
                "audio_artifact_id": artifact.id,
                "audio_uri": artifact.uri,
                "duration_sec": duration,
                "voice_id": voice_id,
            },
            input_tokens=len(text),
            audio_seconds=duration,
            raw_usage={"characters": len(text)},
            estimated_cost=money_cny(estimated),
        )

    def _voice_list(self, call: ProviderCall, context: ProviderInvocationContext) -> ProviderResult:
        appid = self._appid(context)
        voices = self._openapi(context).list_voices(appid)
        return ProviderResult(output={"voices": voices})

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
        if code not in (0, 3000, None):
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

    def _reference_audio_path(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> Path:
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
