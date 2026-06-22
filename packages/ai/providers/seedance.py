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

import time
from datetime import timedelta
from typing import Any

import httpx

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderResult,
    ProviderRuntimeError,
)
from packages.ai.providers.common import (
    first_value,
    option,
    poll_budget,
    request,
    require_secret,
    response_json,
)
from packages.core.contracts import ArtifactKind, ErrorCode

ARK_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
# Terminal task states that are not ``succeeded`` (poll loop stops + raises).
_FAILED_STATES = {"failed", "expired", "cancelled", "canceled"}


class ArkSeedanceProvider:
    provider_id = "volcengine.seedance"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "video.generate":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                f"Ark Seedance cannot run {call.capability_id}.",
            )
        api_key = require_secret(context)
        base_url = str(option(context, "base_url", ARK_DEFAULT_BASE_URL)).rstrip("/")
        model_id = context.profile.model_id
        timeout = float(context.profile.timeout_sec)

        prompt = str(call.input.get("prompt") or "").strip()
        if not prompt:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option, "Seedance generation requires a prompt."
            )

        duration = int(call.input.get("duration_sec") or option(context, "duration", 15))
        ratio = str(call.input.get("ratio") or option(context, "ratio", "9:16"))
        resolution = str(call.input.get("resolution") or option(context, "resolution", "720p"))
        # Seedance 2.0 takes ratio/resolution/duration as top-level JSON fields;
        # 1.x takes them as ``--rt/--rs/--dur`` suffixes inside the text prompt.
        param_style = str(option(context, "param_style", "json_fields"))

        body = self._build_body(
            model_id=model_id,
            prompt=prompt,
            references=self._reference_image_urls(context, call),
            ratio=ratio,
            resolution=resolution,
            duration=duration,
            param_style=param_style,
        )

        task_id = self._submit(base_url, api_key, body, timeout)
        context.mark_polling(task_id)
        payload, attempts = self._poll(base_url, api_key, task_id, context, call, timeout)

        video_url = self._result_video_url(payload)
        if not video_url:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, "Seedance task succeeded but returned no video_url."
            )
        # The vendor's video_url is short-lived (24h); download to durable storage now.
        video_bytes = request(self.client, "GET", video_url, timeout=timeout).content
        artifact = context.store_media_bytes(
            content=video_bytes,
            filename="seedance.mp4",
            purpose="generated-video",
            kind=ArtifactKind.video_rendered,
            call=call,
            tier="durable",
        )
        return ProviderResult(
            output={
                "video_artifact_id": artifact.id,
                "video_uri": artifact.uri,
                "external_job_id": task_id,
                "poll_attempts": attempts,
                "report": "pass",
            },
            video_seconds=float(duration),
            raw_usage={"poll_attempts": attempts, "provider_response": payload},
        )

    # ------------------------------------------------------------------ helpers

    def _reference_image_urls(
        self, context: ProviderInvocationContext, call: ProviderCall
    ) -> list[dict[str, str]]:
        """Presign each reference asset URI to a vendor-reachable public HTTPS URL.

        ``call.input['references']`` is ``[{"uri": "s3://...", "role": "reference_image"}, ...]``
        produced by the SeedanceGenerateVideo node. Non-public stores fail loudly."""
        references = call.input.get("references") or []
        resolved: list[dict[str, str]] = []
        for ref in references:
            if not isinstance(ref, dict):
                continue
            uri = str(ref.get("uri") or "").strip()
            if not uri:
                continue
            resolved.append(
                {
                    "url": self._public_url(context, uri),
                    "role": str(ref.get("role") or "reference_image"),
                }
            )
        return resolved

    @staticmethod
    def _build_body(
        *,
        model_id: str,
        prompt: str,
        references: list[dict[str, str]],
        ratio: str,
        resolution: str,
        duration: int,
        param_style: str,
    ) -> dict[str, Any]:
        text = prompt
        if param_style == "prompt_suffix":  # Seedance 1.x: params ride the text prompt
            text = f"{prompt} --rt {ratio} --rs {resolution} --dur {duration}"
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for ref in references:
            content.append(
                {"type": "image_url", "image_url": {"url": ref["url"]}, "role": ref["role"]}
            )
        body: dict[str, Any] = {"model": model_id, "content": content}
        if param_style != "prompt_suffix":  # Seedance 2.0: top-level JSON fields
            body.update(
                {"ratio": ratio, "resolution": resolution, "duration": duration, "watermark": False}
            )
        return body

    def _submit(self, base_url: str, api_key: str, body: dict[str, Any], timeout: float) -> str:
        response = request(
            self.client,
            "POST",
            f"{base_url}/contents/generations/tasks",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
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
        api_key: str,
        task_id: str,
        context: ProviderInvocationContext,
        call: ProviderCall,
        timeout: float,
    ) -> tuple[dict[str, Any], int]:
        interval, max_attempts = poll_budget(
            context.profile.default_options,
            default_interval=8,
            default_max_attempts=180,
            timeout_minutes=call.input.get("timeout_minutes"),
        )
        payload: dict[str, Any] = {}
        for attempt in range(1, max_attempts + 1):
            response = request(
                self.client,
                "GET",
                f"{base_url}/contents/generations/tasks/{task_id}",
                headers={"Authorization": f"Bearer {api_key}"},
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
            ErrorCode.provider_timeout, f"Seedance task {task_id} did not finish within poll budget."
        )

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
