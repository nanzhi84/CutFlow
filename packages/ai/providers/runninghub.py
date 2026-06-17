from __future__ import annotations

import mimetypes
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urlencode

import httpx

from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderResult,
    ProviderRuntimeError,
)
from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.providers.common import extract_data, first_value, request, require_secret, response_json
from packages.core.contracts import ArtifactKind, ErrorCode

RUNNINGHUB_RETRY_ATTEMPTS = 3
RUNNINGHUB_RETRY_BASE_DELAY = 1.0
RUNNINGHUB_RETRY_MAX_DELAY = 4.0

# RunningHub returns HTTP 200 even on application-level failures, carrying the
# real outcome in the envelope ``code``/``msg`` (success is ``code == 0``, or no
# code at all). Capacity limits are transient and should be retried; auth and
# balance failures are terminal and must be surfaced verbatim — not collapsed
# into the misleading "missing task ID". Codes per RunningHub API error docs.
RUNNINGHUB_OK_CODES = {"0", "200"}
# 415 TASK_INSTANCE_MAXED / 421 TASK_QUEUE_MAXED / 500 / 1000 UNKNOWN_ERROR.
RUNNINGHUB_RETRYABLE_CODES = {"415", "421", "500", "1000"}
# 801/802/806 APIKEY_* / 811 CORPAPIKEY_INVALID / 1002 invalid key.
RUNNINGHUB_AUTH_CODES = {"801", "802", "806", "811", "1002"}
# 416 not-enough-wallet / 812 enterprise funds exhausted.
RUNNINGHUB_QUOTA_CODES = {"416", "812"}

_T = TypeVar("_T")


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
        video_file = self._upload(base_url, api_key, portrait_path, "video", context.profile.timeout_sec, options)
        audio_file = self._upload(base_url, api_key, audio_path, "audio", context.profile.timeout_sec, options)
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
            tier="ephemeral",
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
        options: dict[str, Any],
    ) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = self._request_with_retry(
            "POST",
            f"{base_url}/openapi/v2/media/upload/binary",
            options=options,
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
        if not webapp_id:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "RunningHub webapp_id is required.",
            )
        video_node_id, video_field, audio_node_id, audio_field = self._resolve_nodes(
            base_url, api_key, options, timeout_sec
        )
        payload = {
            "webappId": webapp_id,
            "apiKey": api_key,
            "nodeInfoList": [
                {
                    "nodeId": video_node_id,
                    "fieldName": video_field,
                    "fieldValue": video_file,
                },
                {
                    "nodeId": audio_node_id,
                    "fieldName": audio_field,
                    "fieldValue": audio_file,
                },
            ],
        }

        def attempt() -> str:
            response = request(
                self.client,
                "POST",
                f"{base_url}/task/openapi/ai-app/run",
                headers={"Authorization": f"Bearer {api_key}"},
                json_body=payload,
                timeout=float(timeout_sec),
            )
            body = response_json(response)
            data = extract_data(body)
            if isinstance(data, dict):
                task_id = first_value(data, "taskId", "task_id", "id")
                if task_id:
                    return str(task_id)
            if isinstance(data, str) and data:
                return data
            # No task id: surface the real envelope reason instead of the
            # misleading "missing task ID". Capacity limits raise a retryable
            # error so this loop rides out a transient spike before falling back.
            raise _runninghub_submit_error(body)

        return self._with_retry(attempt, options, retryable=_runninghub_submit_retryable)

    def _resolve_nodes(
        self,
        base_url: str,
        api_key: str,
        options: dict[str, Any],
        timeout_sec: int,
    ) -> tuple[str, str, str, str]:
        video_node_id = str(options.get("video_node_id") or "").strip()
        audio_node_id = str(options.get("audio_node_id") or "").strip()
        video_field = str(options.get("video_field_name") or "video").strip()
        audio_field = str(options.get("audio_field_name") or "audio").strip()
        if video_node_id and audio_node_id:
            return video_node_id, video_field, audio_node_id, audio_field

        webapp_id = str(options.get("webapp_id") or "").strip()
        query = urlencode({"apiKey": api_key, "webappId": webapp_id})
        response = self._request_with_retry(
            "GET",
            f"{base_url}/api/webapp/apiCallDemo?{query}",
            options=options,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(timeout_sec),
        )
        data = extract_data(response_json(response))
        candidates = _runninghub_node_candidates(data)
        video_candidate = _pick_runninghub_node(candidates, "video")
        audio_candidate = _pick_runninghub_node(candidates, "audio")
        if not video_candidate or not audio_candidate:
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "RunningHub HeyGem node mapping is missing. Configure or expose video/audio nodeId and fieldName.",
            )
        return (
            str(video_candidate["nodeId"]),
            str(video_candidate.get("fieldName") or video_field),
            str(audio_candidate["nodeId"]),
            str(audio_candidate.get("fieldName") or audio_field),
        )

    def _poll(
        self,
        base_url: str,
        api_key: str,
        task_id: str,
        options: dict[str, Any],
        timeout_sec: int,
    ) -> tuple[dict[str, Any], int]:
        interval = float(options["poll_interval"] if options.get("poll_interval") is not None else 2)
        max_attempts = int(options["poll_max_attempts"] if options.get("poll_max_attempts") is not None else 120)
        for attempt in range(1, max_attempts + 1):
            status_payload = self._post_task(base_url, api_key, "/task/openapi/status", task_id, timeout_sec, options)
            status = self._normalize_status(extract_data(status_payload))
            if status in {"success", "succeeded", "completed", "finish", "finished"}:
                output_payload = self._post_task(
                    base_url, api_key, "/task/openapi/outputs", task_id, timeout_sec, options
                )
                data = extract_data(output_payload)
                return data if isinstance(data, dict) else output_payload, attempt
            if status in {"failed", "fail", "error", "canceled", "cancelled"}:
                raise ProviderRuntimeError(
                    ErrorCode.provider_remote_failed,
                    f"RunningHub task {task_id} failed: {status_payload}.",
                )
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
        options: dict[str, Any],
    ) -> dict[str, Any]:
        response = self._request_with_retry(
            "POST",
            f"{base_url}{path}",
            options=options,
            headers={"Authorization": f"Bearer {api_key}"},
            json_body={"apiKey": api_key, "taskId": task_id},
            timeout=float(timeout_sec),
        )
        return response_json(response)

    def _with_retry(
        self,
        fn: Callable[[], _T],
        options: dict[str, Any],
        *,
        retryable: Callable[[ProviderRuntimeError], bool] | None = None,
    ) -> _T:
        """Run ``fn`` with exponential backoff.

        By default only transport-level errors (``provider_remote_failed`` /
        ``provider_timeout``) are retried; terminal errors (auth, quota,
        unsupported option) propagate immediately. Callers that classify their
        own application-level errors pass ``retryable`` to decide per-exception
        (it still falls back to the transport rule for unmarked errors)."""
        attempts = int(options.get("retry_attempts") or RUNNINGHUB_RETRY_ATTEMPTS)
        base_delay = float(
            options["retry_base_delay"]
            if options.get("retry_base_delay") is not None
            else RUNNINGHUB_RETRY_BASE_DELAY
        )
        max_delay = float(options.get("retry_max_delay") or RUNNINGHUB_RETRY_MAX_DELAY)
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except ProviderRuntimeError as exc:
                if retryable is not None:
                    should_retry = retryable(exc)
                else:
                    should_retry = exc.code in {
                        ErrorCode.provider_remote_failed,
                        ErrorCode.provider_timeout,
                    }
                if attempt >= attempts or not should_retry:
                    raise
                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                if delay > 0:
                    time.sleep(delay)
        raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "RunningHub retry loop exhausted.")

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        options: dict[str, Any],
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        return self._with_retry(
            lambda: request(
                self.client,
                method,
                url,
                headers=headers,
                json_body=json_body,
                data=data,
                files=files,
                timeout=timeout,
            ),
            options,
        )

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
            if value.startswith(("http://", "https://")) and any(
                ext in value.lower() for ext in (".mp4", ".mov", ".webm", ".m4v")
            ):
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


def _runninghub_submit_error(payload: dict[str, Any]) -> ProviderRuntimeError:
    """Translate a RunningHub run-response that carries no task id into a
    meaningful, correctly-classified error.

    RunningHub answers HTTP 200 even when it refuses the task, putting the
    reason in ``code``/``msg``. Only the transient capacity codes are tagged
    ``rh_retryable`` (consumed by :meth:`_runninghub_submit_retryable`) so the
    submit loop rides out a brief spike before the pipeline falls back; auth,
    balance and unrecognised rejections are terminal and surfaced verbatim
    instead of the misleading "missing task ID". Falls back to the legacy
    message only when there is no envelope to report (e.g. a malformed body)."""
    code = payload.get("code")
    code_str = "" if code is None else str(code).strip()
    msg = str(payload.get("msg") or payload.get("message") or "").strip()
    detail = " ".join(part for part in (f"code={code_str}" if code_str else "", msg) if part).strip()

    # No envelope to report, or a success code that simply lacks a task id:
    # the legacy message is the accurate description in both cases.
    if (not code_str and not msg) or code_str in RUNNINGHUB_OK_CODES:
        return _mark(ProviderRuntimeError(
            ErrorCode.provider_remote_failed, "RunningHub submit response missing task ID."
        ), retryable=False)
    if code_str in RUNNINGHUB_RETRYABLE_CODES:
        return _mark(ProviderRuntimeError(
            ErrorCode.provider_remote_failed,
            f"RunningHub 任务提交受限，请稍后重试（{detail}）。",
        ), retryable=True)
    if code_str in RUNNINGHUB_AUTH_CODES:
        return _mark(ProviderRuntimeError(
            ErrorCode.provider_auth_failed,
            f"RunningHub APIKey 鉴权失败（{detail}）。",
        ), retryable=False)
    if code_str in RUNNINGHUB_QUOTA_CODES:
        return _mark(ProviderRuntimeError(
            ErrorCode.provider_quota_exceeded,
            f"RunningHub 账户余额/额度不足（{detail}）。",
        ), retryable=False)
    return _mark(ProviderRuntimeError(
        ErrorCode.provider_remote_failed,
        f"RunningHub 拒绝任务提交（{detail}）。",
    ), retryable=False)


def _mark(error: ProviderRuntimeError, *, retryable: bool) -> ProviderRuntimeError:
    """Tag a classified submit error with whether the submit loop should retry."""
    error.rh_retryable = retryable
    return error


def _runninghub_submit_retryable(error: ProviderRuntimeError) -> bool:
    """Retry decision for the submit loop: honour ONLY the explicit capacity tag
    set by :func:`_runninghub_submit_error` (415/421/500/1000 -> True; auth /
    balance / unrecognised -> False).

    Transport/HTTP errors raised by ``request`` (timeout, reset, network 5xx) are
    deliberately NOT retried here: the /run submit creates — and bills — a task
    and carries no idempotency key, so re-sending it after a lost response would
    duplicate the task. Upload/poll are idempotent and still retry transport
    errors via the default rule in :meth:`_with_retry`; only /run is exempt."""
    return bool(getattr(error, "rh_retryable", False))


def _nested_get(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _runninghub_node_candidates(data: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "nodeId" in item and "fieldName" in item:
                candidates.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return candidates


def _pick_runninghub_node(candidates: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    for candidate in candidates:
        haystack = " ".join(
            str(candidate.get(key, ""))
            for key in ("fieldName", "fieldType", "nodeName", "displayName", "label")
        ).lower()
        if kind in haystack:
            return candidate
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return Decimal(str(value))
