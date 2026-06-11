from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, JsonValue

from packages.core.contracts import (
    ErrorCode,
    Money,
    OpsAlertEvent,
    ProviderError,
    ProviderInvocation,
    ProviderPriceItem,
    ProviderProfile,
    ProviderStatus,
    UsageMeterRecord,
    zero_money,
    utcnow,
)
from packages.core.contracts.state_machines import assert_transition
from packages.core.observability import record_provider_invocation
from packages.core.storage import ObjectStore, get_object_store
from packages.core.storage import Repository
from packages.core.storage.repository import new_id
from packages.core.storage.secret_store import SecretStore
from packages.ai.gateway.provider_context import ProviderInvocationContext


class ProviderCall(BaseModel):
    case_id: str | None = None
    run_id: str | None = None
    node_run_id: str | None = None
    provider_profile_id: str
    capability_id: str
    prompt_version_id: str | None = None
    input: dict[str, JsonValue] = Field(default_factory=dict)
    idempotency_key: str | None = None


class ProviderResult(BaseModel):
    output: dict[str, JsonValue] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    audio_seconds: float = 0
    video_seconds: float = 0
    image_count: int = 0
    provider_credits: Decimal | None = None
    raw_usage: dict[str, JsonValue] = Field(default_factory=dict)
    estimated_cost: Money = Field(default_factory=zero_money)


class ProviderPlugin(Protocol):
    provider_id: str

    def invoke(self, call: ProviderCall) -> ProviderResult:
        ...


class ProviderRuntimeReader(Protocol):
    def get_profile(self, profile_id: str) -> ProviderProfile | None:
        ...

    def list_price_items(self) -> Iterable[ProviderPriceItem]:
        ...

    def secret_is_active(self, secret_ref: str) -> bool:
        ...


class ProviderRuntimeError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SandboxProvider:
    provider_id = "sandbox"

    def invoke(self, call: ProviderCall) -> ProviderResult:
        simulate = str(call.input.get("simulate", ""))
        if simulate == "quota_exceeded":
            raise ProviderRuntimeError(ErrorCode.provider_quota_exceeded, "Sandbox quota exceeded")
        if simulate == "timeout":
            raise ProviderRuntimeError(ErrorCode.provider_timeout, "Sandbox provider timed out")
        if simulate == "remote_failed":
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed, "Sandbox provider failed")
        if call.capability_id == "tts.speech":
            text = str(call.input.get("text", ""))
            duration = max(1.0, len(text) / 6.0)
            return ProviderResult(
                output={"audio_uri": f"sandbox://audio/{uuid4().hex}.wav", "duration_sec": duration},
                input_tokens=len(text),
                audio_seconds=duration,
            )
        if call.capability_id == "llm.chat":
            script = str(call.input.get("script", ""))
            return ProviderResult(
                output={
                    "intent": {
                        "hook": script[:80],
                        "tone": "clear",
                        "audience": "case_target_audience",
                        "beats": [s.strip() for s in script.replace("。", ".").split(".") if s.strip()][:6],
                    }
                },
                input_tokens=len(script),
                output_tokens=96,
            )
        if call.capability_id == "lipsync.video":
            return ProviderResult(
                output={"video_uri": f"sandbox://video/lipsync/{uuid4().hex}.mp4", "report": "pass"},
                video_seconds=float(call.input.get("duration_sec", 0) or 0),
            )
        if call.capability_id == "vlm.annotation":
            return ProviderResult(output={"labels": ["sandbox"], "quality": "usable"})
        if call.capability_id == "image.generate":
            return ProviderResult(output={"image_uri": f"sandbox://cover/{uuid4().hex}.png"})
        return ProviderResult(output={"ok": True, "capability": call.capability_id})


@dataclass
class ProviderGateway:
    repository: Repository
    provider_reader: ProviderRuntimeReader | None = None
    secret_store: SecretStore | None = None
    object_store: ObjectStore | None = None
    http_client: object | None = None
    auto_register_real_plugins: bool = True

    def __post_init__(self) -> None:
        if self.object_store is None:
            self.object_store = get_object_store()
        self.plugins: dict[str, ProviderPlugin] = {"sandbox": SandboxProvider()}
        if self.auto_register_real_plugins:
            from packages.ai.providers import register_real_provider_plugins

            register_real_provider_plugins(self)

    def register(self, plugin: ProviderPlugin) -> None:
        self.plugins[plugin.provider_id] = plugin

    def invoke(self, call: ProviderCall) -> tuple[ProviderInvocation, ProviderResult | None]:
        profile = self._get_profile(call.provider_profile_id)
        started_at = utcnow()
        started = perf_counter()
        invocation = ProviderInvocation(
            id=new_id("pinv"),
            case_id=call.case_id,
            run_id=call.run_id,
            node_run_id=call.node_run_id,
            provider_id=profile.provider_id,
            model_id=profile.model_id,
            provider_profile_id=profile.id,
            capability_id=call.capability_id,
            prompt_version_id=call.prompt_version_id,
            status=ProviderStatus.prepared,
            started_at=started_at,
        )
        self.repository.provider_invocations[invocation.id] = invocation
        validation_error = self._validate_profile(profile, call)
        if validation_error is not None:
            assert_transition("provider", invocation.status, ProviderStatus.failed)
            invocation = invocation.model_copy(
                update={
                    "status": ProviderStatus.failed,
                    "error": validation_error,
                    "duration_ms": int((perf_counter() - started) * 1000),
                    "finished_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )
            self.repository.provider_invocations[invocation.id] = invocation
            record_provider_invocation(invocation)
            return invocation, None
        assert_transition("provider", invocation.status, ProviderStatus.submitted)
        invocation = invocation.model_copy(
            update={"status": ProviderStatus.submitted, "updated_at": utcnow()}
        )
        self.repository.provider_invocations[invocation.id] = invocation
        plugin = self.plugins[profile.provider_id]
        try:
            context = ProviderInvocationContext(
                repository=self.repository,
                profile=profile,
                invocation_id=invocation.id,
                secret_store=self.secret_store,
                object_store=self.object_store,
            )
            contextual_invoke = getattr(plugin, "invoke_with_context", None)
            if callable(contextual_invoke):
                result = contextual_invoke(call, context)
            else:
                result = plugin.invoke(call)
            duration_ms = int((perf_counter() - started) * 1000)
            price_items = self._matching_price_items(
                provider_id=profile.provider_id,
                model_id=profile.model_id,
                capability_id=call.capability_id,
            )
            price_item_id = price_items[0].id if price_items else None
            cost_unpriced = price_item_id is None
            if cost_unpriced:
                self._record_unpriced_alert(invocation)
            estimated_cost = self._estimated_cost_from_usage(result, price_items)
            usage = UsageMeterRecord(
                id=new_id("usage"),
                provider_invocation_id=invocation.id,
                provider_id=invocation.provider_id,
                model_id=invocation.model_id,
                capability_id=invocation.capability_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cached_input_tokens=result.cached_input_tokens,
                audio_seconds=result.audio_seconds,
                video_seconds=result.video_seconds,
                image_count=result.image_count,
                provider_credits=result.provider_credits,
                raw_usage=result.raw_usage,
            )
            current_invocation = self.repository.provider_invocations[invocation.id]
            assert_transition("provider", current_invocation.status, ProviderStatus.succeeded)
            invocation = current_invocation.model_copy(
                update={
                    "status": ProviderStatus.succeeded,
                    "usage": usage,
                    "price_item_id": price_item_id,
                    "billing_status": "unpriced" if cost_unpriced else "estimated",
                    "duration_ms": duration_ms,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "estimated_cost": estimated_cost,
                    "finished_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )
            self.repository.usage_records[usage.id] = usage
            self.repository.provider_invocations[invocation.id] = invocation
            record_provider_invocation(invocation)
            return invocation, result
        except ProviderRuntimeError as exc:
            status = ProviderStatus.failed
            if exc.code == ErrorCode.provider_timeout:
                status = ProviderStatus.timed_out
            current_invocation = self.repository.provider_invocations[invocation.id]
            assert_transition("provider", current_invocation.status, status)
            invocation = current_invocation.model_copy(
                update={
                    "status": status,
                    "duration_ms": int((perf_counter() - started) * 1000),
                    "error": ProviderError(code=exc.code, message=exc.message, retryable=True),
                    "finished_at": utcnow(),
                    "updated_at": utcnow(),
                }
            )
            self.repository.provider_invocations[invocation.id] = invocation
            record_provider_invocation(invocation)
            return invocation, None

    def _get_profile(self, profile_id: str) -> ProviderProfile:
        if self.provider_reader is not None:
            profile = self.provider_reader.get_profile(profile_id)
            if profile is not None:
                return profile
        return self.repository.provider_profiles[profile_id]

    def _validate_profile(self, profile: ProviderProfile, call: ProviderCall) -> ProviderError | None:
        if not profile.enabled:
            return ProviderError(
                code=ErrorCode.provider_auth_failed,
                message="Provider profile is disabled.",
                retryable=False,
            )
        if profile.capability != call.capability_id:
            return ProviderError(
                code=ErrorCode.provider_unsupported_option,
                message=f"Provider profile capability {profile.capability} cannot run {call.capability_id}.",
                retryable=False,
            )
        if profile.provider_id not in self.plugins:
            return ProviderError(
                code=ErrorCode.provider_unsupported_option,
                message=f"Provider {profile.provider_id} is not registered.",
                retryable=False,
            )
        if profile.secret_ref and not self._secret_is_active(profile.secret_ref):
            return ProviderError(
                code=ErrorCode.provider_auth_failed,
                message="Provider secret is missing.",
                retryable=False,
            )
        return None

    def _matching_price_items(self, *, provider_id: str, model_id: str, capability_id: str) -> list[ProviderPriceItem]:
        items = (
            self.provider_reader.list_price_items()
            if self.provider_reader is not None
            else self.repository.price_items.values()
        )
        matches: list[ProviderPriceItem] = []
        for item in items:
            if item.provider_id != provider_id:
                continue
            model_matches = item.model_id in {model_id, "*"}
            capability_matches = item.capability_id in {capability_id, "*"}
            if model_matches and capability_matches:
                matches.append(item)
        return matches

    def _estimated_cost_from_usage(self, result: ProviderResult, items: list[ProviderPriceItem]) -> Money:
        if result.estimated_cost.amount:
            return result.estimated_cost
        amount = Decimal("0")
        for item in items:
            if item.unit == "input_token":
                amount += item.unit_price.amount * Decimal(result.input_tokens)
            elif item.unit == "output_token":
                amount += item.unit_price.amount * Decimal(result.output_tokens)
            elif item.unit == "media_second":
                amount += item.unit_price.amount * Decimal(str(result.audio_seconds + result.video_seconds))
            elif item.unit == "call":
                amount += item.unit_price.amount
        if amount:
            return Money(amount=amount, currency=items[0].unit_price.currency)
        return result.estimated_cost

    def _secret_is_active(self, secret_ref: str) -> bool:
        if self.secret_store is not None:
            if self.secret_store.get(secret_ref) is None:
                return False
            if self.provider_reader is None and not self.repository.secrets:
                return True
        if self.provider_reader is not None:
            return self.provider_reader.secret_is_active(secret_ref)
        for secret in self.repository.secrets.values():
            status = secret.status.value if hasattr(secret.status, "value") else secret.status
            if secret.secret_ref == secret_ref and status == "active":
                return True
        return False

    def _record_unpriced_alert(self, invocation: ProviderInvocation) -> None:
        alert_id = f"alert_unpriced_{invocation.provider_id}_{invocation.model_id}_{invocation.capability_id}"
        self.repository.alerts[alert_id] = OpsAlertEvent(
            id=alert_id,
            code="cost.unpriced",
            message=(
                f"Provider invocation {invocation.id} has no active price for "
                f"{invocation.provider_id}/{invocation.model_id}/{invocation.capability_id}."
            ),
            severity="warning",
        )
