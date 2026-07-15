"""火山豆包语音 TTS provider (``volcengine.tts``).

Two auth planes behind one ``tts.speech`` capability:

- **data plane** — asynchronous full-file synthesis ``/api/v3/tts/submit`` →
  ``/api/v3/tts/query`` (``X-Api-App-Id`` + application ``Access Token``),
  legacy synthesis ``/api/v1/tts`` (header ``x-api-key``), and
  clone upload ``/api/v1/mega_tts/audio/upload`` (header
  ``Authorization: Bearer;<key>`` + ``Resource-Id: volc.megatts.voiceclone``,
  body carries ``appid``);
- **management plane** — sync cloned voices + issue/list the x-api-key, via
  AK/SK V4 signing in :class:`VolcSpeechOpenAPI`.

The production profile secret is a JSON object containing ``access_key_id``,
``secret_access_key`` and the speech application's ``access_token``.  The old
``AccessKeyId:SecretAccessKey`` form remains readable for management and legacy
v1 calls, but intentionally cannot authorize async v3 because an OpenAPI API key
is not the application Access Token documented by that endpoint.

Auth shapes verified against the live account (synthesis + management); the clone
upload auth was probed (Bearer;<key> reaches the business layer asking for appid).
The exact ``mega_tts/audio/upload`` body fields (source/language/model_type) are
best-effort from the docs and overridable via options — confirm with a real
training run before production (it consumes a paid clone slot).
"""

from __future__ import annotations

import base64
import json
import time
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
    money_cny,
    option,
    poll_budget,
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
_V3_ASYNC_SUBMIT_PATH = "/api/v3/tts/submit"
_V3_ASYNC_QUERY_PATH = "/api/v3/tts/query"
_V3_ASYNC_RESOURCE_ID = "seed-icl-2.0"
_V3_ASYNC_MAX_TEXT_CHARS = 100_000
_V3_ASYNC_SAMPLE_RATES = {8000, 16000, 22050, 24000, 32000, 44100, 48000}
_V3_SUCCESS_CODE = 20_000_000
_V3_TASK_RUNNING = 1
_V3_TASK_SUCCEEDED = 2
_V3_TASK_FAILED = 3


class _AcceptedTaskTerminalError(ProviderRuntimeError):
    """The vendor explicitly marked an accepted async task as failed."""


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

    def resume_with_context(
        self,
        call: ProviderCall,
        context: ProviderInvocationContext,
        external_job_id: str,
    ) -> ProviderResult:
        """Resume an accepted async ICL 2.0 task without submitting it again."""

        try:
            if call.capability_id != "tts.speech":
                raise ProviderRuntimeError(
                    ErrorCode.provider_unsupported_option,
                    f"Volcengine TTS cannot run {call.capability_id}.",
                )
            operation = str(call.input.get("operation") or "speech")
            if operation != "speech" or str(option(context, "api_version", "v1")) != "v3":
                raise ProviderRuntimeError(
                    ErrorCode.provider_unsupported_option,
                    "Only Volcengine async v3 speech tasks can be resumed.",
                )
            text = str(call.input.get("text") or "")
            voice_id = str(call.input.get("voice_id") or option(context, "voice_id") or "")
            self._validate_v3_input(context, text=text, voice_id=voice_id)
            return self._collect_v3_result(
                call,
                context,
                text=text,
                voice_id=voice_id,
                task_id=external_job_id,
            )
        except _AcceptedTaskTerminalError:
            raise
        except ProviderRuntimeError as exc:
            if exc.preserve_polling:
                raise
            raise _accepted_task_error(
                exc,
                task_id=external_job_id,
                operation="resume",
            ) from exc

    # --- credentials ---------------------------------------------------------

    def _appid(self, context: ProviderInvocationContext) -> str:
        appid = str(option(context, "appid", "") or "").strip()
        if not appid:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option, "Volcengine appid is required."
            )
        return appid

    def _openapi(self, context: ProviderInvocationContext) -> VolcSpeechOpenAPI:
        credentials = self._credentials(context)
        access_key_id = credentials.get("access_key_id", "")
        secret_access_key = credentials.get("secret_access_key", "")
        if not access_key_id or not secret_access_key:
            raise ProviderRuntimeError(
                ErrorCode.provider_auth_failed,
                "Volcengine secret must include access_key_id and secret_access_key.",
            )
        return VolcSpeechOpenAPI(self.client, access_key_id, secret_access_key)

    def _credentials(self, context: ProviderInvocationContext) -> dict[str, str]:
        secret = require_secret(context).strip()
        if secret.startswith("{"):
            try:
                value = json.loads(secret)
            except json.JSONDecodeError as exc:
                raise ProviderRuntimeError(
                    ErrorCode.provider_auth_failed,
                    "Volcengine secret JSON is invalid.",
                ) from exc
            if not isinstance(value, dict):
                raise ProviderRuntimeError(
                    ErrorCode.provider_auth_failed,
                    "Volcengine secret JSON must be an object.",
                )
            return {
                "access_key_id": str(
                    value.get("access_key_id") or value.get("accessKeyId") or ""
                ).strip(),
                "secret_access_key": str(
                    value.get("secret_access_key")
                    or value.get("secretAccessKey")
                    or ""
                ).strip(),
                "access_token": str(
                    value.get("access_token") or value.get("accessToken") or ""
                ).strip(),
            }
        access_key_id, separator, secret_access_key = secret.partition(":")
        if separator:
            return {
                "access_key_id": access_key_id.strip(),
                "secret_access_key": secret_access_key.strip(),
                "access_token": "",
            }
        return {}

    def _access_token(self, context: ProviderInvocationContext) -> str:
        if not bool(option(context, "async_icl2_ready", False)):
            raise ProviderRuntimeError(
                ErrorCode.provider_auth_failed,
                "Volcengine async ICL 2.0 is not armed. Rotate the provider secret to "
                "include the speech application Access Token, then set "
                "default_options.async_icl2_ready=true before enabling v3.",
            )
        token = self._credentials(context).get("access_token", "")
        if not token:
            raise ProviderRuntimeError(
                ErrorCode.provider_auth_failed,
                "Volcengine async ICL 2.0 requires the speech application Access Token "
                "in the provider secret; an OpenAPI API key cannot be used here.",
            )
        return token

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
            timing=_timing_from_volcengine_addition(addition, text=text),
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
        """Submit one full-text ICL 2.0 task and download its single MP3 result."""

        fmt, sample_rate = self._validate_v3_input(context, text=text, voice_id=voice_id)
        access_token = self._access_token(context)
        base_url = self._v3_base_url(context)
        resource_id = str(option(context, "resource_id", _V3_ASYNC_RESOURCE_ID))
        request_id = _stable_request_id(call.idempotency_key)
        audio_params: dict = {
            "format": fmt,
            "sample_rate": sample_rate,
            "enable_timestamp": True,
        }
        speed = float(call.input.get("speed") or option(context, "speed", 1.0))
        if abs(speed - 1.0) > 1e-6:
            audio_params["speech_rate"] = max(-50, min(100, round((speed - 1.0) * 100)))
        payload = {
            "user": {"uid": str(option(context, "uid", "cutagent"))},
            "unique_id": request_id,
            "req_params": {
                "text": text,
                "speaker": voice_id,
                "audio_params": audio_params,
            },
        }
        result = response_json(
            request(
                self.client,
                "POST",
                f"{base_url}{_V3_ASYNC_SUBMIT_PATH}",
                headers=self._v3_headers(
                    appid=self._appid(context),
                    access_token=access_token,
                    resource_id=resource_id,
                    request_id=request_id,
                ),
                json_body=payload,
                timeout=float(context.profile.timeout_sec),
            )
        )
        data = _v3_response_data(result, operation="submit")
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed,
                "Volcengine TTS v3 submit response missing task_id.",
            )
        context.mark_polling(task_id)
        try:
            return self._collect_v3_result(
                call,
                context,
                text=text,
                voice_id=voice_id,
                task_id=task_id,
                credentials=(access_token, base_url, resource_id),
            )
        except _AcceptedTaskTerminalError:
            raise
        except ProviderRuntimeError as exc:
            if exc.preserve_polling:
                raise
            raise _accepted_task_error(exc, task_id=task_id, operation="collect") from exc

    def _validate_v3_input(
        self,
        context: ProviderInvocationContext,
        *,
        text: str,
        voice_id: str,
    ) -> tuple[str, int]:
        if not text.strip() or not voice_id.strip():
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "Text and voice_id are required.",
            )
        if voice_id.startswith(("ICL_uranus_", "saturn_")):
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "Volcengine async ICL 2.0 requires the external S_ SpeakerID; "
                "ModelTypeDetails.IclSpeakerId is internal model metadata.",
            )
        if len(text) > _V3_ASYNC_MAX_TEXT_CHARS:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "Volcengine async TTS accepts at most 100000 characters; "
                "the narration will not be split into multiple audio tasks.",
            )
        fmt = str(option(context, "format", "mp3")).strip().lower()
        if fmt != "mp3":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "Volcengine async ICL 2.0 is configured to store one complete MP3; "
                "format must be mp3.",
            )
        sample_rate = int(option(context, "sample_rate", 24000))
        if sample_rate not in _V3_ASYNC_SAMPLE_RATES:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                f"Volcengine async TTS sample_rate={sample_rate} is not supported.",
            )
        resource_id = str(option(context, "resource_id", _V3_ASYNC_RESOURCE_ID))
        if resource_id != _V3_ASYNC_RESOURCE_ID:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "Cloned voices on this path require resource_id=seed-icl-2.0.",
            )
        return fmt, sample_rate

    def _v3_base_url(self, context: ProviderInvocationContext) -> str:
        return str(option(context, "data_base_url_v3", _DEFAULT_DATA_BASE_URL)).rstrip("/")

    @staticmethod
    def _v3_headers(
        *,
        appid: str,
        access_token: str,
        resource_id: str,
        request_id: str,
    ) -> dict[str, str]:
        # Despite the header name, the official async long-text API requires the
        # speech application's Access Token here. ``ListAPIKeys`` returns a different
        # credential: substituting it reaches the endpoint but fails grant lookup.
        return {
            "X-Api-App-Id": appid,
            "X-Api-Access-Key": access_token,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Request-Id": request_id,
            "Content-Type": "application/json",
        }

    def _collect_v3_result(
        self,
        call: ProviderCall,
        context: ProviderInvocationContext,
        *,
        text: str,
        voice_id: str,
        task_id: str,
        credentials: tuple[str, str, str] | None = None,
    ) -> ProviderResult:
        if credentials is None:
            credentials = (
                self._access_token(context),
                self._v3_base_url(context),
                str(option(context, "resource_id", _V3_ASYNC_RESOURCE_ID)),
            )
        access_token, base_url, resource_id = credentials
        interval, max_attempts = poll_budget(
            context.profile.default_options,
            default_interval=1.0,
            default_max_attempts=600,
            timeout_minutes=call.input.get("timeout_minutes"),
        )
        terminal_data: dict | None = None
        attempts = 0
        for attempts in range(1, max_attempts + 1):
            try:
                query_result = response_json(
                    request(
                        self.client,
                        "POST",
                        f"{base_url}{_V3_ASYNC_QUERY_PATH}",
                        headers=self._v3_headers(
                            appid=self._appid(context),
                            access_token=access_token,
                            resource_id=resource_id,
                            request_id=str(uuid.uuid4()),
                        ),
                        json_body={"task_id": task_id},
                        timeout=float(context.profile.timeout_sec),
                    )
                )
                data = _v3_response_data(query_result, operation="query")
            except ProviderRuntimeError as exc:
                raise _accepted_task_error(exc, task_id=task_id, operation="query") from exc
            task_status = _int_or_none(data.get("task_status"))
            if task_status == _V3_TASK_SUCCEEDED:
                terminal_data = data
                break
            if task_status == _V3_TASK_FAILED:
                message = str(data.get("message") or "task failed")
                raise _AcceptedTaskTerminalError(
                    ErrorCode.provider_remote_failed,
                    f"Volcengine TTS v3 task {task_id} failed: {message}",
                )
            if task_status != _V3_TASK_RUNNING:
                raise ProviderRuntimeError(
                    ErrorCode.provider_remote_failed,
                    f"Volcengine TTS v3 query returned unknown task_status={task_status}.",
                    preserve_polling=True,
                )
            if attempts < max_attempts and interval > 0:
                time.sleep(interval)
        if terminal_data is None:
            raise ProviderRuntimeError(
                ErrorCode.provider_timeout,
                f"Volcengine TTS v3 task {task_id} did not finish after {attempts} polls.",
                preserve_polling=True,
            )
        audio_url = str(terminal_data.get("audio_url") or "").strip()
        if not audio_url:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed,
                "Volcengine TTS v3 success response missing audio_url.",
                preserve_polling=True,
            )
        try:
            audio_bytes = request(
                self.client,
                "GET",
                audio_url,
                timeout=float(context.profile.timeout_sec),
            ).content
        except ProviderRuntimeError as exc:
            raise _accepted_task_error(exc, task_id=task_id, operation="audio download") from exc
        if not audio_bytes:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed,
                "Volcengine TTS v3 audio download is empty.",
                preserve_polling=True,
            )
        artifact = context.store_media_bytes(
            content=audio_bytes,
            filename=f"{call.idempotency_key or task_id}.mp3",
            purpose="generated-audio",
            kind=ArtifactKind.audio_tts,
            call=call,
        )
        timing = _timing_from_v3_sentences(terminal_data.get("sentences"), text=text)
        duration = 0.0
        if artifact.media_info and artifact.media_info.duration_sec:
            duration = artifact.media_info.duration_sec
        elif timing:
            duration = max((item.end for item in timing.tokens or timing.segments), default=0.0)
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
            raw_usage={
                "characters": len(text),
                "model": resource_id,
                "task_id": task_id,
                "poll_attempts": attempts,
                "source_format": "mp3",
                "stored_format": "mp3",
            },
            estimated_cost=money_cny(estimated),
        )

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


def _timing_from_volcengine_addition(addition: dict, *, text: str) -> SpeechTiming | None:
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


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stable_request_id(idempotency_key: str | None) -> str:
    """Return a vendor-safe UUID while keeping retries stable for one logical call."""

    if idempotency_key:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))
    return str(uuid.uuid4())


def _accepted_task_error(
    exc: ProviderRuntimeError,
    *,
    task_id: str,
    operation: str,
) -> ProviderRuntimeError:
    """Keep an already-paid async task recoverable after query/download failure."""

    return ProviderRuntimeError(
        exc.code,
        f"Volcengine TTS v3 task {task_id} {operation} failed: {exc.message}",
        preserve_polling=True,
    )


def _v3_response_data(result: dict, *, operation: str) -> dict:
    code = _int_or_none(result.get("code"))
    if code != _V3_SUCCESS_CODE:
        message = str(result.get("message") or "request failed")
        raise ProviderRuntimeError(
            ErrorCode.provider_remote_failed,
            f"Volcengine TTS v3 {operation} code={result.get('code')}: {message}",
        )
    data = result.get("data")
    if isinstance(data, str):
        data = _json_object(data)
    if not isinstance(data, dict):
        raise ProviderRuntimeError(
            ErrorCode.provider_remote_failed,
            f"Volcengine TTS v3 {operation} response missing data.",
        )
    return data


def _v3_timing_bounds(item: dict) -> tuple[float, float]:
    """Read v3 async timestamp bounds in seconds.

    Current ICL envelopes use camelCase seconds. Snake-case milliseconds remain
    tolerated for older responses and stored fixtures.
    """

    if "startTime" in item or "endTime" in item:
        return (
            _timestamp_seconds(item.get("startTime")),
            _timestamp_seconds(item.get("endTime")),
        )
    return (
        _timestamp_seconds(item.get("start_time")) / 1000.0,
        _timestamp_seconds(item.get("end_time")) / 1000.0,
    )


def _timing_from_v3_sentences(sentences: object, *, text: str) -> SpeechTiming | None:
    """Normalize async query sentence/word timestamps into canonical timing."""

    raw_sentences = _json_value(sentences)
    if not isinstance(raw_sentences, list):
        return None

    segments: list[SpeechSegmentTiming] = []
    tokens: list[SpeechTokenTiming] = []
    for sentence in raw_sentences:
        obj = sentence if isinstance(sentence, dict) else _json_object(sentence)
        sentence_text = str(obj.get("text") or "").strip()
        start, end = _v3_timing_bounds(obj)
        sentence_tokens: list[SpeechTokenTiming] = []
        raw_words = obj.get("words")
        if isinstance(raw_words, list):
            for item in raw_words:
                if not isinstance(item, dict):
                    continue
                token = str(item.get("word") or item.get("text") or "")
                if not token.strip():
                    continue
                token_start, token_end = _v3_timing_bounds(item)
                if token_end <= token_start:
                    continue
                timing = SpeechTokenTiming(
                    text=token.strip(), start=token_start, end=token_end
                )
                tokens.append(timing)
                sentence_tokens.append(timing)
        if sentence_text and end > start:
            segments.append(SpeechSegmentTiming(text=sentence_text, start=start, end=end))
        elif sentence_text and sentence_tokens:
            segments.append(
                SpeechSegmentTiming(
                    text=sentence_text,
                    start=sentence_tokens[0].start,
                    end=sentence_tokens[-1].end,
                )
            )
    if not segments and tokens:
        segments.append(SpeechSegmentTiming(text=text, start=tokens[0].start, end=tokens[-1].end))
    if not segments and not tokens:
        return None
    granularity = "segment"
    if tokens:
        granularity = "character" if all(len(item.text) == 1 for item in tokens) else "token"
    return SpeechTiming(
        segments=segments,
        tokens=tokens,
        granularity=granularity,
        text_basis="original",
    )
