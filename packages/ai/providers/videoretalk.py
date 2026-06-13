"""DashScope VideoReTalk provider (lipsync fallback).

Ported (intent, not verbatim) from the original
``backend/app/ai/adapters/dashscope_videoretalk.py``. Submits an async
video-synthesis task (``X-DashScope-Async: enable``) and polls ``/tasks/{id}``
until it succeeds, then downloads the result video and stores it as a
``video_lipsync`` artifact. The DashScope poll loop is shared with the ASR
provider via :func:`packages.ai.providers.dashscope.poll_dashscope_task`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderResult,
    ProviderRuntimeError,
)
from packages.ai.providers.common import option, request, require_secret, response_json
from packages.ai.providers.dashscope import poll_dashscope_task, task_id_from_payload
from packages.core.contracts import ArtifactKind, ErrorCode


class DashScopeVideoReTalkProvider:
    provider_id = "dashscope.videoretalk"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "lipsync.video":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                f"DashScope VideoReTalk cannot run {call.capability_id}.",
            )
        api_key = require_secret(context)
        options = context.profile.default_options
        base_url = str(options.get("base_url") or "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
        video_url = str(call.input.get("video_url") or call.input.get("portrait_uri") or "")
        audio_url = str(call.input.get("audio_url") or call.input.get("audio_uri") or "")
        if not video_url or not audio_url:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "VideoReTalk requires public video_url and audio_url.",
            )
        task_id = self._submit(base_url, api_key, video_url, audio_url, call, context)
        context.mark_polling(task_id)
        task_payload, attempts = poll_dashscope_task(
            client=self.client,
            base_url=base_url,
            api_key=api_key,
            task_id=task_id,
            options=options,
            timeout_sec=context.profile.timeout_sec,
        )
        result_url = self._result_video_url(task_payload)
        if not result_url:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, "VideoReTalk task output missing video_url."
            )
        video_bytes = request(
            self.client,
            "GET",
            result_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(context.profile.timeout_sec),
        ).content
        artifact = context.store_media_bytes(
            content=video_bytes,
            filename=Path(str(result_url)).name or "videoretalk-result.mp4",
            purpose="generated-video",
            kind=ArtifactKind.video_lipsync,
            call=call,
            tier="ephemeral",
        )
        return ProviderResult(
            output={
                "video_artifact_id": artifact.id,
                "video_uri": artifact.uri,
                "external_job_id": task_id,
                "poll_attempts": attempts,
                "report": "pass",
            },
            video_seconds=float(call.input.get("duration_sec") or 0),
            raw_usage={"poll_attempts": attempts, "provider_response": task_payload},
        )

    def _submit(
        self,
        base_url: str,
        api_key: str,
        video_url: str,
        audio_url: str,
        call: ProviderCall,
        context: ProviderInvocationContext,
    ) -> str:
        url = f"{base_url}/services/aigc/image2video/video-synthesis/"
        ref_image_url = str(call.input.get("ref_image_url") or "")
        payload = {
            "model": context.profile.model_id,
            "input": {
                "video_url": video_url,
                "audio_url": audio_url,
                "ref_image_url": ref_image_url,
            },
            "parameters": {
                "video_extension": bool(
                    call.input.get("video_extension", option(context, "video_extension", False))
                ),
                "query_face_threshold": max(
                    120,
                    min(
                        200,
                        int(call.input.get("query_face_threshold") or option(context, "query_face_threshold", 170)),
                    ),
                ),
            },
        }
        response = request(
            self.client,
            "POST",
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            },
            json_body=payload,
            timeout=float(context.profile.timeout_sec),
        )
        task_id = task_id_from_payload(response_json(response))
        if not task_id:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, "VideoReTalk submit response missing task ID."
            )
        return task_id

    @staticmethod
    def _result_video_url(payload: dict[str, Any]) -> str | None:
        output = payload.get("output") if isinstance(payload.get("output"), dict) else payload
        if isinstance(output, dict):
            value = output.get("video_url") or output.get("videoUrl") or output.get("result_url")
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
        return None
