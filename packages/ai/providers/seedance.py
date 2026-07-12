"""Volcengine Ark Seedance video generation provider (capability ``video.generate``).

国内火山引擎方舟（Volcengine Ark）的 Seedance 文生/图生视频。异步任务链路：
``POST {base_url}/contents/generations/tasks`` 提交拿 task id ->
``GET  {base_url}/contents/generations/tasks/{id}`` 轮询直到 ``succeeded`` ->
下载 ``content.video_url`` 的成片 bytes -> 落对象存储成 ``video.rendered`` artifact。

参考图（图生视频）：把内部对象存储 URI presign 成火山可下载的公网 HTTPS（沿用
``videoretalk._public_url`` 同款 2h 签名 + 非公网 fail-loud），拼进 ``content`` 数组的
``image_url`` 条目。火山成片 URL 仅 24h 有效，因此 provider 内立刻下载转存，绝不把
``video_url`` 当成片落库。

火山方舟 API 文档：https://www.volcengine.com/docs/82379/1520757
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderResult,
    ProviderRuntimeError,
)
from packages.ai.providers._volc_sigv4 import signed_headers as volc_signed_headers
from packages.ai.providers.common import (
    first_value,
    map_http_status,
    option,
    poll_budget,
    request,
    require_secret,
    response_json,
)
from packages.core.contracts import ArtifactKind, ErrorCode

ARK_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
ARK_DEFAULT_REGION = "cn-beijing"
ARK_SERVICE = "ark"
ARK_OPENAPI_HOST = "ark.cn-beijing.volcengineapi.com"
ARK_OPENAPI_VERSION = "2024-01-01"
ARK_TEMPORARY_API_KEY_TTL_SECONDS = 604800
# Terminal task states that are not ``succeeded`` (poll loop stops + raises).
_FAILED_STATES = {"failed", "expired", "cancelled", "canceled"}
_OPENAPI_AUTH_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "AuthenticationError",
        "Forbidden",
        "InvalidAccessKey",
        "InvalidCredential",
        "InvalidSecurityToken",
        "MissingAuthenticationToken",
        "NoPermission",
        "SignatureDoesNotMatch",
    }
)


class ArkSeedanceProvider:
    provider_id = "volcengine.seedance"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self._api_key_cache: dict[str, tuple[str, float]] = {}

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "video.generate":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                f"Ark Seedance cannot run {call.capability_id}.",
            )
        secret = require_secret(context)
        base_url = str(option(context, "base_url", ARK_DEFAULT_BASE_URL)).rstrip("/")
        timeout = float(context.profile.timeout_sec)
        model_id, data_secret, data_access_key_auth = self._resolve_data_auth(
            context, secret, timeout
        )

        prompt = str(call.input.get("prompt") or "").strip()
        if not prompt:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option, "Seedance generation requires a prompt."
            )

        duration = int(call.input.get("duration_sec") or option(context, "duration", 15))
        ratio = str(call.input.get("ratio") or option(context, "ratio", "9:16"))
        resolution = str(call.input.get("resolution") or option(context, "resolution", "720p"))
        # Native audio generation. The Seedance ad path uses this for voiceover;
        # BGM/captions are controlled by the prompt because Ark exposes no
        # separate subtitle/BGM switches for this task API.
        generate_audio = bool(
            call.input.get("generate_audio")
            if call.input.get("generate_audio") is not None
            else option(context, "generate_audio", True)
        )
        # Seedance 2.0 takes ratio/resolution/duration as top-level JSON fields;
        # 1.x takes them as ``--rt/--rs/--dur`` suffixes inside the text prompt.
        param_style = str(option(context, "param_style", "json_fields"))

        body = self._build_body(
            model_id=model_id,
            prompt=prompt,
            references=self._reference_content(context, call),
            ratio=ratio,
            resolution=resolution,
            duration=duration,
            generate_audio=generate_audio,
            param_style=param_style,
        )

        task_id = self._submit(
            base_url,
            data_secret,
            body,
            timeout,
            access_key_auth=data_access_key_auth,
            context=context,
        )
        context.mark_polling(task_id)
        return self._collect_result(
            base_url,
            data_secret,
            task_id,
            context,
            call,
            timeout,
            access_key_auth=data_access_key_auth,
            duration=duration,
        )

    def resume_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext, external_job_id: str
    ) -> ProviderResult:
        # Recovery entrypoint: the generation task was already submitted (external_job_id
        # durable), so re-derive the poll credentials but skip submit — only poll +
        # download + store the existing task.
        if call.capability_id != "video.generate":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                f"Ark Seedance cannot run {call.capability_id}.",
            )
        secret = require_secret(context)
        base_url = str(option(context, "base_url", ARK_DEFAULT_BASE_URL)).rstrip("/")
        timeout = float(context.profile.timeout_sec)
        _, data_secret, data_access_key_auth = self._resolve_data_auth(context, secret, timeout)
        duration = int(call.input.get("duration_sec") or option(context, "duration", 15))
        return self._collect_result(
            base_url,
            data_secret,
            external_job_id,
            context,
            call,
            timeout,
            access_key_auth=data_access_key_auth,
            duration=duration,
        )

    # ------------------------------------------------------------------ helpers

    def _resolve_data_auth(
        self, context: ProviderInvocationContext, secret: str, timeout: float
    ) -> tuple[str, str, bool]:
        """Resolve the data-plane model id, poll secret and signed-auth flag.

        Derives the credentials the submit/poll/download calls use from the profile
        secret; runs no vendor generation call (GetApiKey is an idempotent key mint),
        so resume can re-run it without re-submitting a task."""
        access_key_auth = self._use_access_key_auth(secret, context)
        direct_signed_auth = self._use_direct_signed_auth(context)
        model_id = context.profile.model_id
        data_secret = secret
        data_access_key_auth = False
        if access_key_auth:
            resource_type, resource_id, project_name = self._api_key_resource(context)
            if resource_type == "endpoint":
                model_id = resource_id
            if direct_signed_auth:
                data_access_key_auth = True
            else:
                data_secret = self._temporary_api_key(
                    context,
                    secret,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    project_name=project_name,
                    timeout=timeout,
                )
        return model_id, data_secret, data_access_key_auth

    def _collect_result(
        self,
        base_url: str,
        secret: str,
        task_id: str,
        context: ProviderInvocationContext,
        call: ProviderCall,
        timeout: float,
        *,
        access_key_auth: bool,
        duration: int,
    ) -> ProviderResult:
        payload, attempts = self._poll(
            base_url,
            secret,
            task_id,
            context,
            call,
            timeout,
            access_key_auth=access_key_auth,
        )
        video_url = self._result_video_url(payload)
        if not video_url:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed,
                "Seedance task succeeded but returned no video_url.",
            )
        input_tokens, output_tokens = self._usage_tokens(payload)
        # The vendor's video_url is short-lived (24h); download to durable storage now.
        video_path = self._download_video(video_url, timeout)
        try:
            artifact = context.store_media_file(
                local_path=video_path,
                filename="seedance.mp4",
                purpose="generated-video",
                kind=ArtifactKind.video_rendered,
                call=call,
                tier="durable",
            )
        finally:
            video_path.unlink(missing_ok=True)
        return ProviderResult(
            output={
                "video_artifact_id": artifact.id,
                "video_uri": artifact.uri,
                "external_job_id": task_id,
                "poll_attempts": attempts,
                "report": "pass",
            },
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            video_seconds=float(duration),
            raw_usage={"poll_attempts": attempts, "provider_response": payload},
        )

    def _reference_content(
        self, context: ProviderInvocationContext, call: ProviderCall
    ) -> list[dict[str, Any]]:
        """Build the Seedance content entries for each reference asset.

        ``call.input['references']`` is ``[{"uri": "s3://...", "kind": "image"|"video"}, ...]``
        produced by the SeedanceGenerateVideo node. Each internal uri is presigned to
        a vendor-reachable public HTTPS URL (non-public stores fail loudly). Images
        become ``image_url`` entries (role reference_image); videos become
        ``video_url`` entries (role reference_video).
        # TODO 核对火山官方 doc:video_url 条目的精确字段名/role 取值(域内)。"""
        references = call.input.get("references") or []
        entries: list[dict[str, Any]] = []
        for ref in references:
            if not isinstance(ref, dict):
                continue
            uri = str(ref.get("uri") or "").strip()
            if not uri:
                continue
            url = self._public_url(context, uri)
            if str(ref.get("kind") or "image") == "video":
                entries.append(
                    {"type": "video_url", "video_url": {"url": url}, "role": "reference_video"}
                )
            else:
                entries.append(
                    {"type": "image_url", "image_url": {"url": url}, "role": "reference_image"}
                )
        return entries

    @staticmethod
    def _build_body(
        *,
        model_id: str,
        prompt: str,
        references: list[dict[str, Any]],
        ratio: str,
        resolution: str,
        duration: int,
        generate_audio: bool,
        param_style: str,
    ) -> dict[str, Any]:
        text = prompt
        if param_style == "prompt_suffix":  # Seedance 1.x: params ride the text prompt
            text = f"{prompt} --rt {ratio} --rs {resolution} --dur {duration}"
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        content.extend(references)
        body: dict[str, Any] = {
            "model": model_id,
            "content": content,
            "generate_audio": generate_audio,
        }
        if param_style != "prompt_suffix":  # Seedance 2.0: top-level JSON fields
            body.update(
                {"ratio": ratio, "resolution": resolution, "duration": duration, "watermark": False}
            )
        return body

    def _submit(
        self,
        base_url: str,
        secret: str,
        body: dict[str, Any],
        timeout: float,
        *,
        access_key_auth: bool,
        context: ProviderInvocationContext,
    ) -> str:
        url = f"{base_url}/contents/generations/tasks"
        if access_key_auth:
            response = self._signed_request(
                context, "POST", url, secret, timeout=timeout, json_body=body
            )
        else:
            response = request(
                self.client,
                "POST",
                url,
                headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
                json_body=body,
                timeout=timeout,
            )
        task_id = str(response_json(response).get("id") or "")
        if not task_id:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, "Seedance submit response missing task id."
            )
        return task_id

    def _poll(
        self,
        base_url: str,
        secret: str,
        task_id: str,
        context: ProviderInvocationContext,
        call: ProviderCall,
        timeout: float,
        *,
        access_key_auth: bool,
    ) -> tuple[dict[str, Any], int]:
        interval, max_attempts = poll_budget(
            context.profile.default_options,
            default_interval=8,
            default_max_attempts=180,
            timeout_minutes=call.input.get("timeout_minutes"),
        )
        payload: dict[str, Any] = {}
        for attempt in range(1, max_attempts + 1):
            url = f"{base_url}/contents/generations/tasks/{task_id}"
            if access_key_auth:
                response = self._signed_request(context, "GET", url, secret, timeout=timeout)
            else:
                response = request(
                    self.client,
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {secret}"},
                    timeout=timeout,
                )
            payload = response_json(response)
            status = str(payload.get("status") or "").lower()
            if status == "succeeded":
                return payload, attempt
            if status in _FAILED_STATES:
                raise ProviderRuntimeError(
                    ErrorCode.provider_remote_failed,
                    f"Seedance task {status}: {payload.get('error') or payload}",
                )
            time.sleep(interval)
        raise ProviderRuntimeError(
            ErrorCode.provider_timeout,
            f"Seedance task {task_id} did not finish within poll budget.",
        )

    @staticmethod
    def _auth_type(context: ProviderInvocationContext) -> str:
        # Recognised values: "auto" (default; AK/SK auto-detected from the secret),
        # "api_key"/"bearer" (force a literal Bearer key), "access_key" (force AK/SK
        # + temporary-key derivation), "signed" (force AK/SK + per-request signing).
        return str(option(context, "auth_type", "auto") or "auto").strip().lower()

    @staticmethod
    def _use_access_key_auth(secret: str, context: ProviderInvocationContext) -> bool:
        auth_type = ArkSeedanceProvider._auth_type(context)
        if auth_type in {"api_key", "bearer"}:
            return False
        if auth_type in {"access_key", "signed"}:
            return True
        access_key_id, _, secret_access_key = secret.partition(":")
        return bool(access_key_id and secret_access_key)

    @staticmethod
    def _use_direct_signed_auth(context: ProviderInvocationContext) -> bool:
        return ArkSeedanceProvider._auth_type(context) == "signed"

    @staticmethod
    def _api_key_resource(context: ProviderInvocationContext) -> tuple[str, str, str | None]:
        configured = str(
            option(context, "endpoint_id", "")
            or option(context, "ark_endpoint_id", "")
            or ""
        ).strip()
        if configured:
            return "endpoint", configured, None
        model_id = str(context.profile.model_id or "").strip()
        if model_id.startswith("ep-"):
            return "endpoint", model_id, None
        if not model_id:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "Volcengine Ark AK/SK auth requires profile.model_id.",
            )
        project_name = str(option(context, "project_name", "default") or "default").strip()
        return "presetendpoint", model_id, project_name

    def _temporary_api_key(
        self,
        context: ProviderInvocationContext,
        secret: str,
        *,
        resource_type: str,
        resource_id: str,
        project_name: str | None,
        timeout: float,
    ) -> str:
        ttl = int(
            option(
                context,
                "temporary_api_key_ttl_seconds",
                ARK_TEMPORARY_API_KEY_TTL_SECONDS,
            )
            or ARK_TEMPORARY_API_KEY_TTL_SECONDS
        )
        # Do not key the cache by the secret itself; a short hash is enough to
        # avoid cross-account collisions without retaining plaintext credentials.
        secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]
        cache_key = f"{secret_hash}:{resource_type}:{resource_id}:{project_name or ''}:{ttl}"
        cached = self._api_key_cache.get(cache_key)
        now = time.time()
        if cached and cached[1] > now + 60:
            return cached[0]
        body: dict[str, Any] = {
            "DurationSeconds": ttl,
            "ResourceType": resource_type,
            "ResourceIds": [resource_id],
        }
        if project_name is not None:
            body["ProjectName"] = project_name
        result = self._ark_openapi_call(
            context,
            secret,
            action="GetApiKey",
            body=body,
            timeout=timeout,
        )
        api_key = str(result.get("ApiKey") or "").strip()
        if not api_key:
            raise ProviderRuntimeError(
                ErrorCode.provider_auth_failed,
                "Volcengine Ark GetApiKey response missing ApiKey.",
            )
        expires_at = now + ttl
        try:
            expired_time = float(result.get("ExpiredTime") or 0)
        except (TypeError, ValueError):
            expired_time = 0
        if expired_time > now:
            expires_at = expired_time
        self._api_key_cache[cache_key] = (api_key, expires_at)
        return api_key

    def _ark_openapi_call(
        self,
        context: ProviderInvocationContext,
        secret: str,
        *,
        action: str,
        body: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        access_key_id, _, secret_access_key = secret.partition(":")
        if not access_key_id or not secret_access_key:
            raise ProviderRuntimeError(
                ErrorCode.provider_auth_failed,
                "Volcengine Ark OpenAPI auth requires 'access_key_id:secret_access_key'.",
            )
        url = f"https://{ARK_OPENAPI_HOST}/?Action={action}&Version={ARK_OPENAPI_VERSION}"
        raw_body = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = volc_signed_headers(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            method="POST",
            url=url,
            body=raw_body,
            region=str(option(context, "ark_region", ARK_DEFAULT_REGION) or ARK_DEFAULT_REGION),
            service=ARK_SERVICE,
        )
        headers["Content-Type"] = "application/json"
        try:
            response = self.client.post(url, headers=headers, content=raw_body, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderRuntimeError(
                ErrorCode.provider_timeout, "Provider request timed out."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, str(exc)) from exc
        if response.status_code in (401, 403):
            raise ProviderRuntimeError(
                ErrorCode.provider_auth_failed,
                f"Volcengine Ark OpenAPI {action} auth failed (HTTP {response.status_code}).",
            )
        payload = response_json(response)
        error = (
            (payload.get("ResponseMetadata") or {}).get("Error")
            if isinstance(payload, dict)
            else None
        )
        if error:
            code = str(error.get("Code") or "unknown")
            if code in _OPENAPI_AUTH_ERROR_CODES:
                raise ProviderRuntimeError(
                    ErrorCode.provider_auth_failed,
                    f"Volcengine Ark OpenAPI {action} auth failed (Code={code}).",
                )
            if code.startswith("InvalidParameter.ResourceIds"):
                raise ProviderRuntimeError(
                    ErrorCode.provider_unsupported_option,
                    f"Volcengine Ark resource id is invalid for {action} (Code={code}).",
                )
            if code.startswith("InvalidParameter.ProjectName"):
                raise ProviderRuntimeError(
                    ErrorCode.provider_unsupported_option,
                    f"Volcengine Ark project_name is invalid for {action} (Code={code}).",
                )
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed,
                f"Volcengine Ark OpenAPI {action} failed (Code={code}).",
            )
        result = payload.get("Result") if isinstance(payload, dict) else None
        return result if isinstance(result, dict) else {}

    def _signed_request(
        self,
        context: ProviderInvocationContext,
        method: str,
        url: str,
        secret: str,
        *,
        timeout: float,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        access_key_id, _, secret_access_key = secret.partition(":")
        if not access_key_id or not secret_access_key:
            raise ProviderRuntimeError(
                ErrorCode.provider_auth_failed,
                "Volcengine Ark signed auth requires 'access_key_id:secret_access_key'.",
            )
        raw_body = (
            json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if json_body is not None
            else b""
        )
        headers = volc_signed_headers(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            method=method,
            url=url,
            body=raw_body,
            region=str(option(context, "ark_region", ARK_DEFAULT_REGION) or ARK_DEFAULT_REGION),
            service=ARK_SERVICE,
        )
        if raw_body:
            headers["Content-Type"] = "application/json"
        try:
            response = self.client.request(
                method, url, headers=headers, content=raw_body, timeout=timeout
            )
        except httpx.TimeoutException as exc:
            raise ProviderRuntimeError(
                ErrorCode.provider_timeout, "Provider request timed out."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, str(exc)) from exc
        if response.status_code >= 400:
            raise map_http_status(response.status_code, response.text)
        return response

    @staticmethod
    def _result_video_url(payload: dict[str, Any]) -> str | None:
        content = payload.get("content")
        if isinstance(content, dict):
            value = first_value(content, "video_url", "videoUrl", "url")
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        # Some task shapes nest the result under ``data``.
        data = payload.get("data")
        if isinstance(data, dict):
            inner = data.get("content") if isinstance(data.get("content"), dict) else data
            value = first_value(inner, "video_url", "videoUrl", "url")
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        return None

    def _download_video(self, url: str, timeout: float) -> Path:
        temp = tempfile.NamedTemporaryFile(prefix="seedance-", suffix=".mp4", delete=False)
        temp_path = Path(temp.name)
        temp.close()
        try:
            with self.client.stream("GET", url, timeout=timeout) as response:
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", errors="replace")
                    raise map_http_status(response.status_code, body)
                with temp_path.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        if chunk:
                            handle.write(chunk)
        except httpx.TimeoutException as exc:
            temp_path.unlink(missing_ok=True)
            raise ProviderRuntimeError(ErrorCode.provider_timeout, "Provider request timed out.") from exc
        except httpx.HTTPError as exc:
            temp_path.unlink(missing_ok=True)
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, str(exc)) from exc
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return temp_path

    @staticmethod
    def _usage_tokens(payload: dict[str, Any]) -> tuple[int, int]:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            data = payload.get("data")
            usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return 0, 0

        output_tokens = _non_negative_int(
            first_value(
                usage,
                "completion_tokens",
                "completionTokens",
                "output_tokens",
                "outputTokens",
            )
        )
        total_tokens = _non_negative_int(first_value(usage, "total_tokens", "totalTokens"))
        input_tokens = _non_negative_int(
            first_value(usage, "prompt_tokens", "promptTokens", "input_tokens", "inputTokens")
        )
        if output_tokens == 0 and total_tokens:
            output_tokens = total_tokens
        if input_tokens == 0 and total_tokens >= output_tokens:
            input_tokens = total_tokens - output_tokens
        return input_tokens, output_tokens

    @staticmethod
    def _public_url(context: ProviderInvocationContext, uri: str) -> str:
        """Presign an internal object-store URI to a vendor-reachable HTTPS URL.

        Same contract as ``videoretalk._public_url``: the 2h expiry outlasts the
        Seedance poll window, and a non-public signed URL (e.g. the local dev store,
        whose signed URL is still ``local://``) fails loudly here instead of handing
        Ark a dead link (spec no-silent-degrade). Inputs already http(s) pass through."""
        if not uri.startswith(("s3://", "local://")):
            return uri
        signed = context.object_store.signed_url(uri, expires_in=timedelta(hours=2)).url
        if not signed.startswith(("http://", "https://")):
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "Seedance needs a publicly fetchable reference-image URL, but the object "
                "store produced a non-public signed URL. Configure a durable (S3/OSS) "
                "object store for the real Seedance reference-image path.",
            )
        return signed


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)
