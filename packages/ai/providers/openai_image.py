"""OpenAI-compatible image generation provider (capability ``image.generate``).

Faithful port of the origin ``OpenAIImageAdapter`` (digital-human-Cutagent
``app/ai/adapters/openai_image.py``): a single ``POST {base_url}/images/generations``
call against an OpenAI-compatible image endpoint (OpenAI ``gpt-image`` or the
neuromash mirror ``gpt-image-2-all``), with the neuromash param-narrowing the
origin applies, and the same ``data[0].b64_json`` -> ``data[0].url`` response
fallback. The decoded PNG bytes are stored as a ``cover.image`` artifact.

This is the PAID path. It is only ever reached through the gateway when an
enabled real ``image.generate`` ProviderProfile + active secret exist (see
``ProviderGateway._validate_profile`` and the cover node's gating). Without that
configuration the cover node never constructs a ProviderCall for it, so no
network call and no spend happen.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
from decimal import Decimal
from typing import Any

import httpx

from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_gateway import (
    ProviderCall,
    ProviderResult,
    ProviderRuntimeError,
)
from packages.ai.providers.common import money_cny, option, request, require_secret, response_json
from packages.core.contracts import ArtifactKind, ErrorCode

NEUROMASH_MIRROR_MODEL = "gpt-image-2-all"


class OpenAIImageProvider:
    provider_id = "openai.image"
    # Origin fixed catalog: image:gpt-image-2[-all] = 0.4 CNY / image.
    cost_per_image = Decimal("0.4")

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(
        self, call: ProviderCall, context: ProviderInvocationContext
    ) -> ProviderResult:
        if call.capability_id != "image.generate":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                f"OpenAI image provider cannot run {call.capability_id}.",
            )
        api_key = require_secret(context)
        prompt = str(call.input.get("prompt") or "").strip()
        if not prompt:
            raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "Image prompt is required.")
        base_url = str(option(context, "base_url", "https://api.openai.com/v1")).rstrip("/")
        model_id = context.profile.model_id
        size = str(call.input.get("size") or option(context, "size", "1024x1536"))
        count = int(call.input.get("n") or option(context, "n", 1) or 1)
        template_b64 = str(call.input.get("template_image_b64") or "").strip()
        result: dict[str, Any] | None = None
        if template_b64:
            # Reference-image (style/layout) path: edit-from-template so the uploaded
            # cover template actually conditions the result (mirrors the origin
            # ``/images/edits`` call). Falls back to plain generation if the endpoint
            # or model does not support edits.
            result = self._edit_with_template(
                base_url,
                api_key,
                prompt,
                template_b64=template_b64,
                template_filename=str(call.input.get("template_filename") or "cover-template.png"),
                size=size,
                count=count,
                model_id=model_id,
                context=context,
            )
        if result is None:
            payload = self._generation_payload(model_id, prompt, size=size, count=count, context=context)
            response = request(
                self.client,
                "POST",
                f"{base_url}/images/generations",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json_body=payload,
                timeout=float(context.profile.timeout_sec),
            )
            result = response_json(response)
        image_bytes, output_format = self._decode_image(result, api_key, context)
        artifact = context.store_media_bytes(
            content=image_bytes,
            filename=f"{call.idempotency_key or 'ai-cover'}.{output_format}",
            purpose="covers",
            kind=ArtifactKind.cover_image,
            call=call,
        )
        return ProviderResult(
            output={
                "cover_artifact_id": artifact.id,
                "cover_uri": artifact.uri,
                "model": model_id,
                "size": size,
            },
            image_count=count,
            provider_credits=self.cost_per_image * Decimal(count),
            estimated_cost=money_cny(self.cost_per_image * Decimal(count)),
            raw_usage=result.get("usage") if isinstance(result.get("usage"), dict) else {},
        )

    def _generation_payload(
        self,
        model_id: str,
        prompt: str,
        *,
        size: str,
        count: int,
        context: ProviderInvocationContext,
    ) -> dict[str, Any]:
        # Neuromash mirror only honours size/n; OpenAI accepts quality/output_format.
        if model_id == NEUROMASH_MIRROR_MODEL or str(option(context, "provider_kind", "")) == "neuromash":
            return {"model": model_id, "prompt": prompt, "size": size, "n": count}
        payload: dict[str, Any] = {"model": model_id, "prompt": prompt, "size": size, "n": count}
        quality = option(context, "quality")
        if quality:
            payload["quality"] = str(quality)
        output_format = option(context, "output_format")
        if output_format:
            payload["output_format"] = str(output_format)
        return payload

    def _edit_with_template(
        self,
        base_url: str,
        api_key: str,
        prompt: str,
        *,
        template_b64: str,
        template_filename: str,
        size: str,
        count: int,
        model_id: str,
        context: ProviderInvocationContext,
    ) -> dict[str, Any] | None:
        """POST the prompt + reference image to ``/images/edits`` so the uploaded
        cover template conditions the result. Returns the decoded JSON, or ``None``
        to signal the caller should fall back to plain text-to-image generation
        (when the endpoint/model rejects edits, e.g. HTTP 400/404/422)."""
        try:
            template_bytes = base64.b64decode(template_b64)
        except (binascii.Error, ValueError):
            return None
        if not template_bytes:
            return None
        mime = mimetypes.guess_type(template_filename)[0] or "image/png"
        data = {"model": model_id, "prompt": prompt, "size": size, "n": str(count)}
        # Try the canonical ``image`` field first, then ``image[]`` (some mirrors
        # only accept the array form) — mirrors the origin's two-field attempt.
        for image_field in ("image", "image[]"):
            try:
                response = request(
                    self.client,
                    "POST",
                    f"{base_url}/images/edits",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data=data,
                    files={image_field: (template_filename, template_bytes, mime)},
                    timeout=float(context.profile.timeout_sec),
                )
            except ProviderRuntimeError as exc:
                # auth/quota are hard failures (re-raise); request-shape/endpoint
                # rejections fall back to generation so the cover still ships.
                if exc.code in {ErrorCode.provider_auth_failed, ErrorCode.provider_quota_exceeded}:
                    raise
                continue
            return response_json(response)
        return None

    def _decode_image(
        self, result: dict[str, Any], api_key: str, context: ProviderInvocationContext
    ) -> tuple[bytes, str]:
        data = result.get("data")
        first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        b64_image = str(first.get("b64_json") or "").strip()
        if b64_image:
            return self._b64_to_bytes(b64_image), "png"
        url = str(first.get("url") or "").strip()
        if url:
            return self._url_to_bytes(url, api_key, context), "png"
        raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "Image provider returned no image data.")

    @staticmethod
    def _b64_to_bytes(b64_image: str) -> bytes:
        if b64_image.startswith("data:") and ";base64," in b64_image:
            b64_image = b64_image.split(";base64,", 1)[1].strip()
        try:
            return base64.b64decode(b64_image)
        except (binascii.Error, ValueError) as exc:
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "Image provider returned invalid base64.") from exc

    def _url_to_bytes(self, url: str, api_key: str, context: ProviderInvocationContext) -> bytes:
        if url.startswith("data:") and ";base64," in url:
            return self._b64_to_bytes(url)
        return request(
            self.client,
            "GET",
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(context.profile.timeout_sec),
        ).content
