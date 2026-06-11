from __future__ import annotations

import hashlib
import mimetypes
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderResult,
    ProviderRuntimeError,
)
from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.providers.common import money_cny, option, request, require_secret, response_json
from packages.core.contracts import ArtifactKind, ErrorCode


class MiniMaxTTSProvider:
    provider_id = "minimax.tts"
    cost_per_1k_chars = Decimal("0.15")

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "tts.speech":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                f"MiniMax TTS cannot run {call.capability_id}.",
            )
        operation = str(call.input.get("operation") or "speech")
        if operation == "speech":
            return self._speech(call, context)
        if operation == "clone":
            return self._clone(call, context)
        if operation == "design":
            return self._design(call, context)
        raise ProviderRuntimeError(
            ErrorCode.provider_unsupported_option,
            f"MiniMax TTS operation {operation} is not supported by this call.",
        )

    def _speech(self, call: ProviderCall, context: ProviderInvocationContext) -> ProviderResult:
        api_key = require_secret(context)
        group_id = str(call.input.get("group_id") or option(context, "group_id") or "").strip()
        if not group_id:
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "MiniMax group_id is required.")
        text = str(call.input.get("text") or "")
        voice_id = str(call.input.get("voice_id") or option(context, "voice_id") or "")
        if not text.strip() or not voice_id.strip():
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "Text and voice_id are required.")
        base_url = str(option(context, "base_url", "https://api.minimaxi.com/v1")).rstrip("/")
        url = f"{base_url}/t2a_v2?GroupId={group_id}"
        payload = {
            "model": context.profile.model_id,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": float(call.input.get("speed") or option(context, "speed", 1.0)),
                "vol": float(call.input.get("volume") or option(context, "volume", 1.0)),
                "pitch": int(call.input.get("pitch") or option(context, "pitch", 0)),
            },
            "audio_setting": {
                "sample_rate": int(option(context, "sample_rate", 32000)),
                "bitrate": int(option(context, "bitrate", 128000)),
                "format": str(option(context, "format", "mp3")),
            },
        }
        emotion = str(call.input.get("emotion") or option(context, "emotion", "neutral"))
        if emotion and emotion != "neutral":
            payload["voice_setting"]["emotion"] = emotion
        response = request(
            self.client,
            "POST",
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_body=payload,
            timeout=float(context.profile.timeout_sec),
        )
        result = response_json(response)
        self._raise_for_base_resp(result)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        audio_hex = str(data.get("audio") or "")
        if not audio_hex:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "MiniMax TTS response missing audio.")
        try:
            audio_bytes = bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "MiniMax TTS audio is invalid.") from exc
        fmt = str(payload["audio_setting"]["format"] or "mp3")
        artifact = context.store_media_bytes(
            content=audio_bytes,
            filename=f"{call.idempotency_key or 'minimax-tts'}.{fmt}",
            purpose="generated-audio",
            kind=ArtifactKind.audio_tts,
            call=call,
        )
        duration = float(data.get("duration") or 0) / 1000.0
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
            raw_usage={"characters": len(text), "provider_response": _usage_safe(result)},
            estimated_cost=money_cny(estimated),
        )

    def _clone(self, call: ProviderCall, context: ProviderInvocationContext) -> ProviderResult:
        api_key = require_secret(context)
        group_id = self._group_id(call, context)
        audio_path = self._reference_audio_path(call, context)
        file_id = self._upload_audio_file(context, api_key, group_id, audio_path)
        display_name = str(call.input.get("display_name") or call.input.get("name") or "provider voice")
        voice_id = str(call.input.get("voice_id") or _generate_voice_id(display_name))
        base_url = str(option(context, "base_url", "https://api.minimaxi.com/v1")).rstrip("/")
        payload: dict[str, Any] = {
            "voice_id": voice_id,
            "file_id": int(file_id) if str(file_id).isdigit() else file_id,
            "model": str(option(context, "clone_model", context.profile.model_id)),
        }
        demo_text = call.input.get("demo_text")
        if demo_text:
            payload["text"] = str(demo_text)
        response = request(
            self.client,
            "POST",
            f"{base_url}/voice_clone?GroupId={group_id}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_body=payload,
            timeout=float(context.profile.timeout_sec),
        )
        result = response_json(response)
        self._raise_for_base_resp(result)
        output = {
            "voice_id": str(result.get("voice_id") or voice_id),
            "status": "success",
            "provider_response": _usage_safe(result),
        }
        output.update(self._preview_voice(call, context, output["voice_id"]))
        return ProviderResult(output=output, raw_usage={"provider_response": _usage_safe(result)})

    def _design(self, call: ProviderCall, context: ProviderInvocationContext) -> ProviderResult:
        api_key = require_secret(context)
        group_id = self._group_id(call, context)
        display_name = str(call.input.get("display_name") or call.input.get("preferred_name") or "designed voice")
        voice_id = str(call.input.get("voice_id") or _generate_voice_id(display_name, prefix="design"))
        prompt = str(call.input.get("prompt") or call.input.get("voice_prompt") or "")
        if not prompt.strip():
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "Voice design prompt is required.")
        preview_text = str(call.input.get("preview_text") or option(context, "preview_text", "这是试听文本。"))
        base_url = str(option(context, "base_url", "https://api.minimaxi.com/v1")).rstrip("/")
        endpoint = str(option(context, "design_endpoint", "/voice_design")).lstrip("/")
        payload = {
            "model": str(option(context, "design_model", context.profile.model_id)),
            "voice_id": voice_id,
            "voice_prompt": prompt,
            "preview_text": preview_text,
            "preferred_name": display_name,
            "language": str(call.input.get("language") or option(context, "language", "zh")),
        }
        response = request(
            self.client,
            "POST",
            f"{base_url}/{endpoint}?GroupId={group_id}",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_body=payload,
            timeout=float(context.profile.timeout_sec),
        )
        result = response_json(response)
        self._raise_for_base_resp(result)
        self._raise_for_code(result)
        data = _first_mapping(result, "data", "output", "result")
        designed_voice_id = str(data.get("voice_id") or result.get("voice_id") or voice_id)
        output = {
            "voice_id": designed_voice_id,
            "status": "success",
            "provider_response": _usage_safe(result),
        }
        output.update(self._store_inline_preview(call, context, data))
        if "preview_audio_artifact_id" not in output:
            output.update(self._preview_voice(call, context, designed_voice_id))
        return ProviderResult(output=output, raw_usage={"provider_response": _usage_safe(result)})

    def _group_id(self, call: ProviderCall, context: ProviderInvocationContext) -> str:
        group_id = str(call.input.get("group_id") or option(context, "group_id") or "").strip()
        if not group_id:
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "MiniMax group_id is required.")
        return group_id

    def _reference_audio_path(self, call: ProviderCall, context: ProviderInvocationContext) -> Path:
        reference_uri = call.input.get("reference_audio_uri")
        if isinstance(reference_uri, str) and reference_uri:
            return context.local_path_for_uri(reference_uri)
        upload_id = call.input.get("reference_upload_session_id")
        if isinstance(upload_id, str) and upload_id:
            upload = context.repository.uploads.get(upload_id)
            if upload is None:
                raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "Voice reference upload is missing.")
            if upload.object_uri:
                return context.local_path_for_uri(upload.object_uri)
            if upload.local_temp_path:
                return context.local_path_for_uri(upload.local_temp_path)
        raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "Reference audio is required.")

    def _upload_audio_file(
        self,
        context: ProviderInvocationContext,
        api_key: str,
        group_id: str,
        audio_path: Path,
    ) -> str:
        if not audio_path.exists():
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "Reference audio file is missing.")
        max_bytes = int(option(context, "clone_max_bytes", 20 * 1024 * 1024))
        if audio_path.stat().st_size > max_bytes:
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "Reference audio file is too large.")
        base_url = str(option(context, "base_url", "https://api.minimaxi.com/v1")).rstrip("/")
        content_type = mimetypes.guess_type(audio_path.name)[0] or "audio/mpeg"
        response = request(
            self.client,
            "POST",
            f"{base_url}/files/upload?GroupId={group_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            data={"purpose": "voice_clone"},
            files={"file": (audio_path.name, audio_path.read_bytes(), content_type)},
            timeout=float(context.profile.timeout_sec),
        )
        result = response_json(response)
        self._raise_for_base_resp(result)
        file_data = result.get("file") if isinstance(result.get("file"), dict) else {}
        file_id = file_data.get("file_id") or result.get("file_id")
        if not file_id:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "MiniMax upload response missing file_id.")
        return str(file_id)

    def _preview_voice(
        self,
        call: ProviderCall,
        context: ProviderInvocationContext,
        voice_id: str,
    ) -> dict[str, Any]:
        preview_text = str(call.input.get("preview_text") or option(context, "preview_text", "这是试听文本。"))
        if not preview_text.strip():
            return {}
        preview_call = call.model_copy(
            update={"input": {**call.input, "operation": "speech", "text": preview_text, "voice_id": voice_id}}
        )
        preview_result = self._speech(preview_call, context)
        artifact_id = preview_result.output.get("audio_artifact_id")
        if not isinstance(artifact_id, str):
            return {}
        return {
            "preview_audio_artifact_id": artifact_id,
            "preview_audio_uri": preview_result.output.get("audio_uri"),
            "preview_duration_sec": preview_result.output.get("duration_sec"),
        }

    def _store_inline_preview(
        self,
        call: ProviderCall,
        context: ProviderInvocationContext,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        audio_hex = data.get("preview_audio") or data.get("audio")
        if not isinstance(audio_hex, str) or not audio_hex:
            return {}
        try:
            audio_bytes = bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "MiniMax preview audio is invalid.") from exc
        artifact = context.store_media_bytes(
            content=audio_bytes,
            filename=f"{call.idempotency_key or 'minimax-preview'}.mp3",
            purpose="voice-preview",
            kind=ArtifactKind.audio_tts,
            call=call,
        )
        return {"preview_audio_artifact_id": artifact.id, "preview_audio_uri": artifact.uri}

    @staticmethod
    def _raise_for_base_resp(result: dict[str, Any]) -> None:
        base_resp = result.get("base_resp")
        if not isinstance(base_resp, dict):
            return
        code = base_resp.get("status_code")
        if code in {0, "0", None}:
            return
        message = str(base_resp.get("status_msg") or "MiniMax provider failed.")
        if code in {1004, "1004"}:
            error = ErrorCode.provider_auth_failed
        elif code in {1002, 1008, "1002", "1008"}:
            error = ErrorCode.provider_quota_exceeded
        else:
            error = ErrorCode.provider_remote_failed
        raise ProviderRuntimeError(error, message)

    @staticmethod
    def _raise_for_code(result: dict[str, Any]) -> None:
        code = result.get("code")
        if code in {None, 0, 200, "0", "200"}:
            return
        message = str(result.get("message") or result.get("msg") or "MiniMax provider failed.")
        raise ProviderRuntimeError(ErrorCode.provider_remote_failed, message)


def _usage_safe(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return {key: value for key, value in data.items() if key != "audio"}


def _first_mapping(result: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = result.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _generate_voice_id(name: str, *, prefix: str = "voice") -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9]", "", name)
    if not safe_name:
        safe_name = hashlib.md5(name.encode("utf-8")).hexdigest()[:16]
    if not safe_name[0].isalpha():
        safe_name = f"v{safe_name}"
    return f"{prefix}_{safe_name[:10]}_{int(time.time())}"[:256]
