from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import math
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
from packages.core.config.settings import build_providers_settings
from packages.core.contracts.state_machines import assert_transition
from packages.core.observability import record_provider_invocation
from packages.core.storage import ObjectStore, get_object_store
from packages.core.storage import Repository
from packages.core.storage.repository import new_id
from packages.core.storage.secret_store import SecretStore
from packages.ai.gateway.provider_context import ProviderInvocationContext
from packages.ai.gateway.provider_limiter import provider_slot


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

    def list_profiles(
        self,
        *,
        provider_id: str | None = None,
        capability: str | None = None,
        environment: str | None = None,
        limit: int = 200,
    ) -> Iterable[ProviderProfile]:
        ...

    def list_price_items(self) -> Iterable[ProviderPriceItem]:
        ...

    def secret_is_active(self, secret_ref: str) -> bool:
        ...


class BudgetGuard(Protocol):
    def evaluate(
        self,
        *,
        call: ProviderCall,
        invocation: ProviderInvocation,
    ) -> ProviderError | None:
        ...


class CircuitBreakerGuard(Protocol):
    def evaluate(
        self,
        *,
        call: ProviderCall,
        invocation: ProviderInvocation,
    ) -> ProviderError | None:
        ...


class ProviderRuntimeError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


SUPPORTED_MULTIMODAL_EMBEDDING_DIMENSIONS = {1024}


def parse_multimodal_embedding_dimension(*values: object, default: int = 1024) -> int:
    dimension_value: object = default
    for value in values:
        if value is None or value == "":
            continue
        dimension_value = value
        break
    if isinstance(dimension_value, bool):
        dimension: int | None = None
    elif isinstance(dimension_value, int):
        dimension = dimension_value
    elif isinstance(dimension_value, str):
        stripped = dimension_value.strip()
        dimension = int(stripped) if stripped.isdecimal() else None
    else:
        dimension = None
    if dimension is None:
        raise ProviderRuntimeError(
            ErrorCode.provider_unsupported_option,
            "multimodal.embedding dimension must be a supported integer value.",
        )
    if dimension not in SUPPORTED_MULTIMODAL_EMBEDDING_DIMENSIONS:
        supported = ", ".join(str(value) for value in sorted(SUPPORTED_MULTIMODAL_EMBEDDING_DIMENSIONS))
        raise ProviderRuntimeError(
            ErrorCode.provider_unsupported_option,
            f"multimodal.embedding dimension must be one of: {supported}.",
        )
    return dimension


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
        if call.capability_id == "video.generate":
            # Seedance text/image-to-video: no real download/store happens in the
            # sandbox, so there is no video_artifact_id — the node bridges this fake
            # uri into a uri-only artifact (see seedance_generate_video).
            return ProviderResult(
                output={
                    "video_uri": f"sandbox://video/seedance/{uuid4().hex}.mp4",
                    "video_artifact_id": None,
                    "external_job_id": f"sandbox-{uuid4().hex[:8]}",
                    "report": "pass",
                },
                video_seconds=float(call.input.get("duration_sec", 15) or 15),
            )
        if call.capability_id == "multimodal.embedding":
            text = str(call.input.get("text") or call.input.get("retrieval_intent") or "")
            dimension = parse_multimodal_embedding_dimension(call.input.get("dimension"))
            embedding = _deterministic_embedding(
                f"{call.provider_profile_id}:{call.capability_id}:{text}", dimension=dimension
            )
            return ProviderResult(
                output={
                    "embedding": embedding,
                    "embedding_id": f"sandbox-emb-{uuid4().hex[:12]}",
                    "model": str(call.input.get("model") or "qwen3-vl-embedding"),
                    "dimension": dimension,
                    "normalization": str(call.input.get("normalization") or "l2"),
                    "index_version": str(call.input.get("index_version") or "clip-video-qwen3-v3"),
                },
                input_tokens=len(text),
            )
        return ProviderResult(output={"ok": True, "capability": call.capability_id})


def _deterministic_embedding(seed: str, *, dimension: int) -> list[float]:
    values: list[float] = []
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).digest()
        for index in range(0, len(digest), 2):
            if len(values) >= dimension:
                break
            raw = int.from_bytes(digest[index : index + 2], "big")
            values.append((raw / 65535.0) * 2.0 - 1.0)
        counter += 1
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 0:
        return [1.0, *([0.0] * (dimension - 1))]
    return [value / norm for value in values]


@dataclass
class ProviderGateway:
    repository: Repository
    provider_reader: ProviderRuntimeReader | None = None
    secret_store: SecretStore | None = None
    object_store: ObjectStore | None = None
    http_client: object | None = None
    budget_guard: BudgetGuard | None = None
    circuit_breaker: CircuitBreakerGuard | None = None
    auto_register_real_plugins: bool = True

    def __post_init__(self) -> None:
        if self.object_store is None:
            self.object_store = get_object_store()
        self.plugins: dict[str, ProviderPlugin] = {"sandbox": SandboxProvider()}
        # Durable audit sink for live secret reveals (spec §11.3 / §32.9). When the
        # provider_reader is DB-backed it exposes a session_factory, so reveals from
        # worker processes persist to the audit table; otherwise reveals fall back to
        # the in-memory repository audit log (handled inside the context).
        self._secret_read_audit_sink = self._build_secret_read_audit_sink()
        if self.auto_register_real_plugins:
            from packages.ai.providers import register_real_provider_plugins

            register_real_provider_plugins(self)

    def _build_secret_read_audit_sink(self):
        session_factory = getattr(self.provider_reader, "session_factory", None)
        if session_factory is None:
            return None

        def _sink(*, actor, action, resource_type, resource_id, details):
            # Persist the read audit in its own short transaction. NEVER records the
            # secret value — only access metadata.
            from packages.core.storage.database import AuditEventRow

            with session_factory() as session:
                session.add(
                    AuditEventRow(
                        id=new_id("audit"),
                        actor=actor,
                        action=action,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        details=details,
                    )
                )
                session.commit()

        return _sink

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
        if self.budget_guard is not None:
            budget_error = self.budget_guard.evaluate(call=call, invocation=invocation)
            if budget_error is not None:
                assert_transition("provider", invocation.status, ProviderStatus.failed)
                invocation = invocation.model_copy(
                    update={
                        "status": ProviderStatus.failed,
                        "error": budget_error,
                        "duration_ms": int((perf_counter() - started) * 1000),
                        "finished_at": utcnow(),
                        "updated_at": utcnow(),
                    }
                )
                self.repository.provider_invocations[invocation.id] = invocation
                record_provider_invocation(invocation)
                return invocation, None
        if self.circuit_breaker is not None:
            circuit_error = self.circuit_breaker.evaluate(call=call, invocation=invocation)
            if circuit_error is not None:
                assert_transition("provider", invocation.status, ProviderStatus.failed)
                invocation = invocation.model_copy(
                    update={
                        "status": ProviderStatus.failed,
                        "error": circuit_error,
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
                audit_sink=self._secret_read_audit_sink,
            )
            contextual_invoke = getattr(plugin, "invoke_with_context", None)
            # Bound concurrent in-flight provider calls per ProviderProfile
            # concurrency_key (fallback provider_id) so concurrent durable runs
            # do not fan out unbounded requests at vendor quotas. Per-process;
            # cluster-wide limiting needs a shared limiter (see provider_limiter).
            with provider_slot(profile.concurrency_key, profile.provider_id):
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
        # SSRF / key-exfiltration guard (defense in depth). The AUTHORITATIVE gate
        # is at provider-profile create/patch (apps/api/services/providers.py),
        # which rejects an off-list base_url before it is ever persisted — that
        # fully covers the user-supplied vector. This gateway-level re-check is an
        # OPT-IN belt-and-suspenders layer that re-asserts the host allow-list just
        # before the adapter delivers the secret, catching a row tampered with
        # post-persist. It is OFF by default so test fixtures / seeds that
        # construct profiles directly with synthetic hosts keep working; enable in
        # production via CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST=1.
        if build_providers_settings().enforce_host_allowlist:
            from packages.ai.netpolicy import assert_options_hosts_allowed

            try:
                assert_options_hosts_allowed(profile.default_options)
            except ValueError as exc:
                return ProviderError(
                    code=ErrorCode.provider_unsupported_option,
                    message=str(exc),
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
            elif item.unit == "provider_credit" and result.provider_credits is not None:
                # Providers that bill in their own credits/coins (e.g. RunningHub
                # HeyGem ``consumeCoins``) report the consumed amount as
                # ``provider_credits``; unit_price is the CNY value per credit.
                amount += item.unit_price.amount * result.provider_credits
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
