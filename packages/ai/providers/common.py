from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import ProviderRuntimeError
from packages.core.contracts import ErrorCode, Money


def require_secret(context: ProviderInvocationContext) -> str:
    secret = context.get_secret()
    if not secret:
        raise ProviderRuntimeError(ErrorCode.provider_auth_failed, "Provider secret is missing.")
    return secret


def option(context: ProviderInvocationContext, name: str, default: Any = None) -> Any:
    return context.profile.default_options.get(name, default)


def merged_options(context: ProviderInvocationContext, call_input: dict[str, Any]) -> dict[str, Any]:
    merged = dict(context.profile.default_options)
    merged.update({key: value for key, value in call_input.items() if value is not None})
    return merged


def money_cny(amount: Decimal | int | str) -> Money:
    return Money(amount=Decimal(str(amount)), currency="CNY")


def map_http_status(status_code: int, body: str = "") -> ProviderRuntimeError:
    if status_code in {401, 403}:
        return ProviderRuntimeError(ErrorCode.provider_auth_failed, f"Provider auth failed: HTTP {status_code}.")
    if status_code == 429:
        return ProviderRuntimeError(ErrorCode.provider_quota_exceeded, "Provider quota exceeded.")
    return ProviderRuntimeError(
        ErrorCode.provider_remote_failed,
        f"Provider request failed: HTTP {status_code} {body[:160]}".strip(),
    )


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> httpx.Response:
    try:
        response = client.request(
            method,
            url,
            headers=headers,
            json=json_body,
            data=data,
            files=files,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise ProviderRuntimeError(ErrorCode.provider_timeout, "Provider request timed out.") from exc
    except httpx.HTTPError as exc:
        raise ProviderRuntimeError(ErrorCode.provider_remote_failed, str(exc)) from exc
    if response.status_code >= 400:
        raise map_http_status(response.status_code, response.text)
    return response


def response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "Provider returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "Provider returned non-object JSON.")
    return payload


def extract_data(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("data", "result", "output"):
            if key in payload:
                return payload[key]
    return payload


def first_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None
