"""DashScope VideoReTalk provider (lipsync fallback).

Ported (intent, not verbatim) from the original
``backend/app/ai/adapters/dashscope_videoretalk.py``. Submits an async
video-synthesis task (``X-DashScope-Async: enable``) and polls ``/tasks/{id}``
until it succeeds, then downloads the result video and stores it as a
``video_lipsync`` artifact. The DashScope poll loop is shared with the ASR
provider via :func:`packages.ai.providers.dashscope.poll_dashscope_task`.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
from packages.media.assets import local_object_path, store_file
from packages.media.video.ffmpeg import FfmpegCommandError, compress_video_to_budget

# VideoReTalk's documented hard input cap is 300MB; we compress to a slightly lower
# budget so the encoded output lands safely under it. Both are overridable per profile.
VIDEORETALK_MAX_INPUT_MB = 300.0
VIDEORETALK_COMPRESS_BUDGET_MB = 280.0


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
        # DashScope's cloud must DOWNLOAD the inputs, so any internal object-store
        # URI (s3://, local://) is presigned to a public HTTPS URL first. Inputs that
        # are already http(s) (e.g. tests) pass through unchanged. The video input is
        # additionally compressed to fit the provider's hard input-size cap before
        # presigning (no-silent-degrade guard against an over-budget upload).
        raw_video_uri = str(call.input.get("video_url") or call.input.get("portrait_uri") or "")
        raw_audio_uri = str(call.input.get("audio_url") or call.input.get("audio_uri") or "")
        if not raw_video_uri or not raw_audio_uri:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "VideoReTalk requires public video_url and audio_url.",
            )
        video_url = self._video_public_url(context, raw_video_uri, call)
        audio_url = self._public_url(context, raw_audio_uri)
        task_id = self._submit(base_url, api_key, video_url, audio_url, call, context)
        context.mark_polling(task_id)
        return self._collect_result(base_url, api_key, task_id, options, call, context)

    def resume_with_context(
        self,
        call: ProviderCall,
        context: ProviderInvocationContext,
        external_job_id: str,
    ) -> ProviderResult:
        # Recovery entrypoint: the synthesis task was already submitted (external_job_id
        # durable), so skip presign/submit and only poll + download + store. The rebuilt
        # request would carry fresh presigned URLs, but recovery consumes only the task id.
        if call.capability_id != "lipsync.video":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                f"DashScope VideoReTalk cannot run {call.capability_id}.",
            )
        api_key = require_secret(context)
        options = context.profile.default_options
        base_url = str(options.get("base_url") or "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
        return self._collect_result(base_url, api_key, external_job_id, options, call, context)

    def _collect_result(
        self,
        base_url: str,
        api_key: str,
        task_id: str,
        options: dict[str, Any],
        call: ProviderCall,
        context: ProviderInvocationContext,
    ) -> ProviderResult:
        task_payload, attempts = poll_dashscope_task(
            client=self.client,
            base_url=base_url,
            api_key=api_key,
            task_id=task_id,
            options=options,
            timeout_sec=context.profile.timeout_sec,
            timeout_minutes=call.input.get("timeout_minutes"),
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
            filename=_filename_from_url(result_url, "videoretalk-result.mp4"),
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

    @staticmethod
    def _public_url(context: ProviderInvocationContext, uri: str) -> str:
        """Presign an internal object-store URI to a vendor-reachable HTTPS URL.

        Mirrors ``narration_alignment`` handing DashScope ASR a signed URL. The
        2h expiry comfortably outlasts the VideoReTalk poll window. The result MUST
        be a public http(s) URL — a backing store that returns a non-public URI
        (e.g. the local dev store, whose signed URL is still ``local://``) cannot be
        fetched by DashScope, so we fail loudly here instead of submitting a dead
        link and letting the remote call fail opaquely (spec no-silent-degrade)."""
        if not uri.startswith(("s3://", "local://")):
            return uri
        signed = context.object_store.signed_url(uri, expires_in=timedelta(hours=2)).url
        if not signed.startswith(("http://", "https://")):
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "VideoReTalk needs a publicly fetchable input URL, but the object "
                "store produced a non-public signed URL. Configure a durable "
                "(S3/OSS) object store for the real lipsync path.",
            )
        return signed

    def _video_public_url(
        self, context: ProviderInvocationContext, uri: str, call: ProviderCall
    ) -> str:
        """Resolve the portrait/video input to a public URL, compressing first when
        the source exceeds the provider's input-size cap.

        For an internal object-store URI we know the local bytes, so we can measure
        and (if needed) re-encode below the budget BEFORE presigning. http(s) inputs
        (already external) are passed straight through — the cap is enforced by the
        caller that produced them."""
        if not uri.startswith(("s3://", "local://")):
            return uri
        compressed_uri = self._compress_oversized_video(context, uri, call)
        return self._public_url(context, compressed_uri or uri)

    def _compress_oversized_video(
        self, context: ProviderInvocationContext, uri: str, call: ProviderCall
    ) -> str | None:
        """Return a NEW object-store URI for a compressed copy when the input video
        exceeds the size cap; ``None`` when it already fits (use the original)."""
        max_input_mb = float(option(context, "max_input_mb", VIDEORETALK_MAX_INPUT_MB))
        budget_mb = float(option(context, "compress_budget_mb", VIDEORETALK_COMPRESS_BUDGET_MB))
        try:
            local_path = local_object_path(context.object_store, uri)
        except (ValueError, FileNotFoundError):
            # Cannot resolve local bytes (e.g. a remote-only S3 store); leave the
            # input untouched and let the presigned original carry the cap risk.
            return None
        if not local_path.exists():
            return None
        size_mb = local_path.stat().st_size / (1024 * 1024)
        if size_mb <= max_input_mb:
            return None
        with tempfile.TemporaryDirectory(prefix="cutagent-vrt-compress-") as directory:
            output = Path(directory) / "compressed.mp4"
            try:
                result = compress_video_to_budget(
                    local_path,
                    max_size_mb=budget_mb,
                    output_path=output,
                    timeout_sec=int(context.profile.timeout_sec),
                )
            except FfmpegCommandError as exc:
                raise ProviderRuntimeError(
                    ErrorCode.provider_remote_failed,
                    f"VideoReTalk input video is {size_mb:.0f}MB (> {max_input_mb:.0f}MB cap) "
                    f"and compression failed: {exc}",
                ) from exc
            stored = store_file(
                context.object_store,
                result.path,
                purpose="generated-video",
                tier="ephemeral",
            )
        return stored.ref.uri

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


def _filename_from_url(url: str, fallback: str) -> str:
    return Path(urlparse(str(url)).path).name or fallback
