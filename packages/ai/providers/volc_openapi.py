"""Volcengine ``speech_saas_prod`` management-plane OpenAPI (AK/SK V4-signed).

Drives the multi-vendor voice management the data-plane ``x-api-key`` cannot:

- ``list_voices(appid)`` — pull the account's cloned voices via
  ``ListMegaTTSTrainStatus`` (the Volcengine "sync" equivalent;豆包语音 has no
  data-plane list API, only this AK/SK management endpoint).
- ``ensure_api_key(appid, name)`` — auto-issue / fetch the plaintext
  ``x-api-key`` the data-plane synthesis endpoint needs (path B), via
  ``ListAPIKeys`` with a ``CreateAPIKey`` fallback.

V4 HMAC signing uses the shared Volcengine signer, extended here to POST + JSON body.
The Action→Version split is fixed by the official SDK: train-status is
``2025-05-21``, key management is ``2025-05-20``. All requests hit the shared
gateway host ``open.volcengineapi.com`` with the ``speech_saas_prod`` signing scope.
"""

from __future__ import annotations

import json

import httpx

from packages.ai.gateway.provider_gateway import ProviderRuntimeError
from packages.ai.providers._volc_sigv4 import signed_headers as volc_signed_headers
from packages.core.contracts import ErrorCode

_HOST = "open.volcengineapi.com"
_SERVICE = "speech_saas_prod"
_REGION = "cn-north-1"
_VERSION_TRAIN = "2025-05-21"
_VERSION_KEY = "2025-05-20"

# Volcengine V4 ResponseMetadata.Error codes meaning a key/permission problem
# (map to provider_auth_failed) rather than a transient remote fault.
_AUTH_ERROR_CODES = frozenset(
    {
        "AuthenticationError",
        "InvalidAccessKey",
        "InvalidCredential",
        "InvalidSecurityToken",
        "SignatureDoesNotMatch",
        "AccessDenied",
        "Forbidden",
        "NoPermission",
        "MissingAuthenticationToken",
    }
)

_READY_STATES = frozenset({"Success", "Active"})
_FAILED_STATES = frozenset({"Failed", "Failure", "Fail", "Error"})


def _signed_headers(
    access_key_id: str, secret_access_key: str, action: str, version: str, body: bytes
) -> tuple[dict[str, str], str]:
    """Build Volcengine V4 signed headers for a POST management-plane call.

    Delegates the canonical-request / string-to-sign / 4-step signing-key chain to
    the shared :func:`packages.ai.providers._volc_sigv4.signed_headers`, then adds
    the (unsigned) Content-Type and returns the query the caller puts on the URL.
    """
    query = f"Action={action}&Version={version}"
    headers = volc_signed_headers(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        method="POST",
        url=f"https://{_HOST}/?{query}",
        body=body,
        region=_REGION,
        service=_SERVICE,
    )
    headers["Content-Type"] = "application/json"
    return headers, query


def _map_state(state: str | None) -> str:
    """Map a Volcengine clone train State to our VoiceProfile.status enum."""
    if state in _READY_STATES:
        return "ready"
    if state in _FAILED_STATES:
        return "failed"
    return "training"


def _icl2_preview_url(item: dict) -> str | None:
    """Return the type-5 preview without exposing an unusable internal voice id.

    The async ``/api/v3/tts/submit`` API selects ICL 2.0 with
    ``X-Api-Resource-Id=seed-icl-2.0`` and accepts the purchased ``S_`` SpeakerID.
    The ``IclSpeakerId`` nested under model type 5 is an internal model-specific ID;
    passing it as ``req_params.speaker`` fails the live API's resource check.
    """

    details = item.get("ModelTypeDetails")
    if not isinstance(details, list):
        return None
    for detail in details:
        if not isinstance(detail, dict):
            continue
        try:
            model_type = int(detail.get("ModelType"))
        except (TypeError, ValueError):
            continue
        if model_type != 5:
            continue
        preview_url = str(detail.get("DemoAudio") or detail.get("demo_audio") or "").strip()
        return preview_url or None
    return None


class VolcSpeechOpenAPI:
    """AK/SK-signed client for the ``speech_saas_prod`` management plane."""

    def __init__(self, client: httpx.Client, access_key_id: str, secret_access_key: str) -> None:
        self.client = client
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key

    def _call(self, action: str, version: str, body: dict, *, timeout: float = 30.0) -> dict:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers, query = _signed_headers(
            self.access_key_id, self.secret_access_key, action, version, raw
        )
        try:
            response = self.client.post(
                f"https://{_HOST}/?{query}", headers=headers, content=raw, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, f"火山 OpenAPI {action} 请求失败: {exc}"
            ) from exc
        # A non-JSON 401/403 (e.g. gateway error page) must map to auth_failed, not
        # remote_failed — mirrors the balance poller. Signature/permission errors that
        # come back as HTTP 200 + ResponseMetadata.Error are handled below.
        if response.status_code in (401, 403):
            raise ProviderRuntimeError(
                ErrorCode.provider_auth_failed,
                f"火山 OpenAPI {action} 鉴权失败 (HTTP {response.status_code})",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, f"火山 OpenAPI {action} 响应非 JSON"
            ) from exc
        error = (data.get("ResponseMetadata") or {}).get("Error") if isinstance(data, dict) else None
        if error:
            code = str(error.get("Code") or "unknown")
            # NEVER echo error["Message"]: Volcengine signature errors embed the
            # canonical request / Credential=<AccessKeyId>. Code is a fixed enum.
            if code in _AUTH_ERROR_CODES:
                raise ProviderRuntimeError(
                    ErrorCode.provider_auth_failed, f"火山 OpenAPI {action} 鉴权失败 (Code={code})"
                )
            raise ProviderRuntimeError(
                ErrorCode.provider_remote_failed, f"火山 OpenAPI {action} 失败 (Code={code})"
            )
        result = data.get("Result") if isinstance(data, dict) else None
        return result if isinstance(result, dict) else {}

    def _train_statuses(self, appid: str) -> list[dict]:
        result = self._call("ListMegaTTSTrainStatus", _VERSION_TRAIN, {"AppID": str(appid)})
        return [s for s in (result.get("Statuses") or []) if isinstance(s, dict)]

    def list_voices(self, appid: str) -> list[dict]:
        """Pull the account's successfully cloned voices (normalized shape).

        Only ``State=Success`` voices are returned. Empty/unallocated slots
        (``State=Unknown``, no Alias — the user's purchased-but-unused clone
        quota) and in-flight trainings are skipped, so bulk sync never surfaces
        empty slots as fake voices. The platform clone flow tracks its own
        in-flight voices per-voice via :meth:`get_train_status`.
        """
        voices: list[dict] = []
        for item in self._train_statuses(appid):
            if _map_state(item.get("State")) != "ready":
                continue
            speaker_id = str(item.get("SpeakerID") or "").strip()
            if not speaker_id:
                continue
            model_preview = _icl2_preview_url(item)
            voice = {
                "voice_id": speaker_id,
                "display_name": str(item.get("Alias") or speaker_id),
                "status": "ready",
                "preview_url": model_preview or item.get("DemoAudio") or None,
            }
            voices.append(voice)
        return voices

    def get_train_status(self, appid: str, speaker_id: str) -> str | None:
        """Return one speaker's clone status (ready/training/failed), or None if absent.

        Polls a platform-initiated clone: ``Unknown`` (slot allocated, training
        not finished) maps to ``training`` so the UI keeps polling until Success.
        """
        for item in self._train_statuses(appid):
            if str(item.get("SpeakerID") or "") == speaker_id:
                return _map_state(item.get("State"))
        return None

    def list_free_slots(self, appid: str) -> list[str]:
        """Return SpeakerIDs of empty clone slots a platform clone can claim.

        Empty slots are the purchased-but-unused quota: ``State=Unknown`` with no
        ``Alias``. Successful/failed/named voices are excluded. NOTE: a freshly
        claimed-but-still-training slot also has no Alias and a non-ready state, so
        this assumes serialized clones per appid — two concurrent clones on one
        appid could pick the same slot. The platform clone flow is single-shot today.
        """
        slots: list[str] = []
        for item in self._train_statuses(appid):
            if item.get("Alias"):
                continue
            if _map_state(item.get("State")) in ("ready", "failed"):
                continue
            speaker_id = str(item.get("SpeakerID") or "").strip()
            if speaker_id:
                slots.append(speaker_id)
        return slots

    def ensure_api_key(self, appid: str, name: str) -> str:
        """Return a usable data-plane x-api-key, creating one if none exists (path B)."""
        existing = self._list_active_keys(appid)
        if existing:
            return existing[0]
        self._call("CreateAPIKey", _VERSION_KEY, {"AppID": str(appid), "Name": name})
        created = self._list_active_keys(appid, name=name)
        if not created:
            raise ProviderRuntimeError(
                ErrorCode.provider_auth_failed, "火山 CreateAPIKey 后未能取回 API Key"
            )
        return created[0]

    def _list_active_keys(self, appid: str, *, name: str | None = None) -> list[str]:
        result = self._call("ListAPIKeys", _VERSION_KEY, {"AppID": str(appid)})
        keys: list[str] = []
        for item in result.get("APIKeys") or []:
            if not isinstance(item, dict) or item.get("Disable"):
                continue
            api_key = str(item.get("APIKey") or "").strip()
            if not api_key:
                continue
            if name is not None and item.get("Name") != name:
                continue
            keys.append(api_key)
        return keys
