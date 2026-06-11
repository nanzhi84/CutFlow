from __future__ import annotations

import mimetypes
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
from packages.ai.providers.common import extract_data, first_value, request, require_secret, response_json
from packages.core.contracts import ArtifactKind, ErrorCode


class RunningHubHeyGemProvider:
    provider_id = "runninghub.heygem"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "lipsync.video":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                f"RunningHub HeyGem cannot run {call.capability_id}.",
            )
        api_key = require_secret(context)
        options = context.profile.default_options
        base_url = str(options.get("base_url") or "https://www.runninghub.ai").rstrip("/")
        portrait_path = context.local_path_for_uri(str(call.input.get("portrait_uri") or ""))
        audio_path = context.local_path_for_uri(str(call.input.get("audio_uri") or ""))
        video_file = self._upload(base_url, api_key, portrait_path, "video", context.profile.timeout_sec)
        audio_file = self._upload(base_url, api_key, audio_path, "audio", context.profile.timeout_sec)
        task_id = self._submit(base_url, api_key, video_file, audio_file, options, context.profile.timeout_sec)
        context.mark_polling(task_id)
        output_payload, attempts = self._poll(base_url, api_key, task_id, options, context.profile.timeout_sec)
        result_url = self._find_first_video_url(output_payload)
        if not result_url:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "RunningHub output missing video URL.")
        video_bytes = request(
            self.client,
            "GET",
            result_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(context.profile.timeout_sec),
        ).content
        artifact = context.store_media_bytes(
            content=video_bytes,
            filename=Path(str(result_url)).name or "heygem-result.mp4",
            purpose="generated-video",
            kind=ArtifactKind.video_lipsync,
            call=call,
        )
        credits = _decimal_or_none(_nested_get(output_payload, "consumeCoins", "consume_coins", "cost"))
        return ProviderResult(
            output={
                "video_artifact_id": artifact.id,
                "video_uri": artifact.uri,
                "external_job_id": task_id,
                "poll_attempts": attempts,
                "report": "pass",
            },
            video_seconds=float(call.input.get("duration_sec") or 0),
            provider_credits=credits,
            raw_usage={"poll_attempts": attempts, "provider_response": output_payload},
        )

    def _upload(
        self,
        base_url: str,
        api_key: str,
        path: Path,
        file_type: str,
        timeout_sec: int,
    ) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = request(
            self.client,
            "POST",
            f"{base_url}/openapi/v2/media/upload/binary",
            headers={"Authorization": f"Bearer {api_key}"},
            data={"apiKey": api_key, "fileType": file_type},
            files={"file": (path.name, path.read_bytes(), mime_type)},
            timeout=float(timeout_sec),
        )
        payload = response_json(response)
        data = extract_data(payload)
        if isinstance(data, dict):
            file_name = first_value(data, "fileName", "file_name", "name")
            if file_name:
                return str(file_name)
        if isinstance(data, str) and data:
            return data
        raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "RunningHub upload response missing file name.")

    def _submit(
        self,
        base_url: str,
        api_key: str,
        video_file: str,
        audio_file: str,
        options: dict[str, Any],
        timeout_sec: int,
    ) -> str:
        webapp_id = str(options.get("webapp_id") or "").strip()
        video_node_id = str(options.get("video_node_id") or "").strip()
        audio_node_id = str(options.get("audio_node_id") or "").strip()
        if not webapp_id or not video_node_id or not audio_node_id:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "RunningHub webapp_id, video_node_id, and audio_node_id are required.",
            )
        payload = {
            "webappId": webapp_id,
            "apiKey": api_key,
            "nodeInfoList": [
                {
                    "nodeId": video_node_id,
                    "fieldName": str(options.get("video_field_name") or "video"),
                    "fieldValue": video_file,
                },
                {
                    "nodeId": audio_node_id,
                    "fieldName": str(options.get("audio_field_name") or "audio"),
                    "fieldValue": audio_file,
                },
            ],
        }
        response = request(
            self.client,
            "POST",
            f"{base_url}/task/openapi/ai-app/run",
            headers={"Authorization": f"Bearer {api_key}"},
            json_body=payload,
            timeout=float(timeout_sec),
        )
        data = extract_data(response_json(response))
        if isinstance(data, dict):
            task_id = first_value(data, "taskId", "task_id", "id")
            if task_id:
                return str(task_id)
        if isinstance(data, str) and data:
            return data
        raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "RunningHub submit response missing task ID.")

    def _poll(
        self,
        base_url: str,
        api_key: str,
        task_id: str,
        options: dict[str, Any],
        timeout_sec: int,
    ) -> tuple[dict[str, Any], int]:
        interval = float(options.get("poll_interval") or 2)
        max_attempts = int(options.get("poll_max_attempts") or 120)
        for attempt in range(1, max_attempts + 1):
            status_payload = self._post_task(base_url, api_key, "/task/openapi/status", task_id, timeout_sec)
            status = self._normalize_status(extract_data(status_payload))
            if status in {"success", "succeeded", "completed", "finish", "finished"}:
                output_payload = self._post_task(
                    base_url, api_key, "/task/openapi/outputs", task_id, timeout_sec
                )
                data = extract_data(output_payload)
                return data if isinstance(data, dict) else output_payload, attempt
            if status in {"failed", "fail", "error", "canceled", "cancelled"}:
                raise ProviderRuntimeError(ErrorCode.provider_remote_failed, f"RunningHub task failed: {status}.")
            if interval > 0:
                time.sleep(interval)
        raise ProviderRuntimeError(ErrorCode.provider_timeout, "RunningHub task timed out.")

    def _post_task(
        self,
        base_url: str,
        api_key: str,
        path: str,
        task_id: str,
        timeout_sec: int,
    ) -> dict[str, Any]:
        response = request(
            self.client,
            "POST",
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {api_key}"},
            json_body={"apiKey": api_key, "taskId": task_id},
            timeout=float(timeout_sec),
        )
        return response_json(response)

    @staticmethod
    def _normalize_status(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("status", "taskStatus", "task_status", "state"):
                value = payload.get(key)
                if value:
                    return str(value).strip().lower()
        if isinstance(payload, str):
            return payload.strip().lower()
        return ""

    @staticmethod
    def _find_first_video_url(value: Any) -> str | None:
        if isinstance(value, str):
            if value.startswith(("http://", "https://")):
                return value
            return None
        if isinstance(value, list):
            for item in value:
                found = RunningHubHeyGemProvider._find_first_video_url(item)
                if found:
                    return found
        if isinstance(value, dict):
            for key in ("fileUrl", "file_url", "url", "videoUrl", "video_url", "resultUrl", "result_url"):
                found = RunningHubHeyGemProvider._find_first_video_url(value.get(key))
                if found:
                    return found
            for nested in value.values():
                found = RunningHubHeyGemProvider._find_first_video_url(nested)
                if found:
                    return found
        return None


def _nested_get(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))
