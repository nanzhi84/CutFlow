"""Provider domain: invocations, usage metering, capabilities/profiles, pricing, usage/balance reports."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from pydantic import Field, JsonValue, model_validator

from .base import ContractModel, EntityMeta, ErrorCode, Money, ProviderStatus, utcnow


class SpeechTokenTiming(ContractModel):
    """Provider-neutral word/character timing in seconds."""

    text: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    # Provider adapters are not required to know script ownership. The
    # NarrationAlignment boundary enriches the normalized display-token stream with
    # these fields so downstream caption planning never has to infer ownership from
    # overlapping time windows.
    token_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    char_span: tuple[int, int] | None = Field(default=None, exclude_if=lambda value: value is None)
    source_unit_id: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_range(self) -> "SpeechTokenTiming":
        if self.end <= self.start:
            raise ValueError("speech token end must be greater than start")
        if self.char_span is not None:
            start, end = self.char_span
            if start < 0 or end <= start:
                raise ValueError("speech token char_span must be a positive half-open range")
        return self


class SpeechSegmentTiming(ContractModel):
    """Provider-neutral sentence/segment timing in seconds."""

    text: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> "SpeechSegmentTiming":
        if self.end <= self.start:
            raise ValueError("speech segment end must be greater than start")
        return self


class SpeechTiming(ContractModel):
    """Canonical speech timing emitted by TTS and ASR adapters.

    Provider-private response fields are normalized at the adapter boundary.  The
    workflow only consumes this shape and therefore never branches on provider id.
    """

    segments: list[SpeechSegmentTiming] = Field(default_factory=list)
    tokens: list[SpeechTokenTiming] = Field(default_factory=list)
    granularity: Literal["segment", "token", "character"] = "segment"
    text_basis: Literal["original", "normalized"] = "original"


class TtsSpeechOutput(ContractModel):
    """Canonical output contract for the ``tts.speech`` capability."""

    audio_artifact_id: str
    audio_uri: str | None = None
    duration_sec: float
    timing: SpeechTiming | None = None
    voice_id: str | None = None


class ProviderError(ContractModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    raw_error_artifact_id: str | None = None


class UsageMeterRecord(EntityMeta):
    provider_invocation_id: str
    provider_id: str
    model_id: str
    capability_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    audio_seconds: float = 0
    video_seconds: float = 0
    image_count: int = 0
    provider_credits: Decimal | None = None
    raw_usage: dict[str, Any] = Field(default_factory=dict)


class ProviderInvocation(EntityMeta):
    case_id: str | None = None
    run_id: str | None = None
    node_run_id: str | None = None
    idempotency_key: str | None = None
    provider_id: str
    model_id: str
    provider_profile_id: str
    capability_id: str
    prompt_version_id: str | None = None
    status: ProviderStatus
    usage: UsageMeterRecord | None = None
    price_item_id: str | None = None
    billing_status: Literal["estimated", "reconciled", "unpriced", "ignored"] = "estimated"
    duration_ms: int = 0
    retry_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: Money | None = None
    actual_cost: Money | None = None
    request_artifact_id: str | None = None
    response_artifact_id: str | None = None
    external_job_id: str | None = None
    error: ProviderError | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class ProviderOptionsSchemaRef(ContractModel):
    schema_id: str
    schema_version: str = "v1"
    dialect: Literal["json_schema_2020_12", "pydantic"] = "pydantic"
    sha256: str = "dev-unpinned"


class ProviderCapability(EntityMeta):
    capability: str
    provider_id: str
    model_id: str
    display_name: str
    input_schema_id: str
    output_schema_id: str
    options_schema_id: str
    supports_async_job: bool
    supports_cancel: bool
    max_payload_bytes: int | None = None
    max_duration_sec: float | None = None
    default_timeout_sec: int


class ProviderProfile(EntityMeta):
    provider_id: str
    model_id: str
    capability: str
    display_name: str
    environment: Literal["local", "dev", "staging", "prod"]
    secret_ref: str | None = None
    enabled: bool = True
    concurrency_key: str = "default"
    timeout_sec: int = 30
    cost_policy_id: str | None = None
    options_schema_ref: ProviderOptionsSchemaRef
    default_options: dict[str, JsonValue] = Field(default_factory=dict)
    version: str = "v1"


class CreateProviderProfileRequest(ContractModel):
    provider_id: str
    model_id: str
    capability: str
    display_name: str
    environment: Literal["local", "dev", "staging", "prod"]
    secret_ref: str | None = None
    concurrency_key: str = "default"
    timeout_sec: int = 30
    cost_policy_id: str | None = None
    options_schema_ref: ProviderOptionsSchemaRef
    default_options: dict[str, JsonValue] = Field(default_factory=dict)
    version: str = "v1"


class PatchProviderProfileRequest(ContractModel):
    display_name: str | None = None
    enabled: bool | None = None
    secret_ref: str | None = None
    concurrency_key: str | None = None
    timeout_sec: int | None = None
    cost_policy_id: str | None = None
    default_options: dict[str, JsonValue] | None = None


class TestProviderProfileRequest(ContractModel):
    sample_input: dict[str, JsonValue] = Field(default_factory=dict)


class ProviderHealthCheckResponse(ContractModel):
    profile_id: str
    ok: bool
    latency_ms: int | None = None
    error: ProviderError | None = None


class ProviderPriceCatalog(EntityMeta):
    provider_id: str
    status: Literal["draft", "approved", "published", "deprecated"] = "draft"
    currency: str = "CNY"


class ProviderPriceItem(EntityMeta):
    catalog_id: str
    provider_id: str
    model_id: str
    capability_id: str
    unit: Literal["input_token", "output_token", "media_second", "call", "provider_credit"]
    unit_price: Money
    active_from: datetime = Field(default_factory=utcnow)
    active_to: datetime | None = None


class UpsertPriceCatalogRequest(ContractModel):
    catalog: ProviderPriceCatalog
    items: list[ProviderPriceItem]


class ProviderUsageReport(ContractModel):
    invocations: int
    estimated_cost: Money
    actual_cost: Money | None = None
    unpriced_invocation_count: int


class ProviderUsageMetricsItem(ContractModel):
    provider_id: str
    capability_id: str
    model_id: str | None = None
    calls: int
    success_count: int
    success_rate: float
    estimated_cost: Money
    window_hours: int
    p50_duration_ms: float | None = None


class ProviderUsageMetricsReport(ContractModel):
    items: list[ProviderUsageMetricsItem]
    window_hours: int
    generated_at: datetime
    request_id: str


class GovernedActionRequest(ContractModel):
    reason: str


class ProviderBalanceItem(ContractModel):
    provider_id: str
    account_group: str | None = None
    balance: Money | None = None
    quota_remaining: float | None = None
    unit: str | None = None
    checked_at: datetime
    status: Literal["ok", "unconfigured", "unsupported", "unauthorized", "error", "pending"]
    detail: str | None = None


class ProviderBalanceReport(ContractModel):
    items: list[ProviderBalanceItem]
    request_id: str
    status: Literal["ok", "pending"] = "ok"


class RefreshProviderBalancesRequest(ContractModel):
    reason: str | None = None


class ProviderBalanceSnapshot(EntityMeta):
    provider_id: str
    account_group: str | None = None
    balance: Money | None = None
    quota_remaining: float | None = None
    unit: str | None = None
    status: Literal["ok", "unconfigured", "unsupported", "unauthorized", "error", "pending"]
    detail: str | None = None
    checked_at: datetime


class ReconcileBillingRequest(ContractModel):
    provider_id: str | None = None
    window_start: datetime
    window_end: datetime
    dry_run: bool = False


class ReconcileBillingLineItem(ContractModel):
    provider_id: str
    capability_id: str
    estimated_cost: Money
    recorded_usage_cost: Money
    variance: Money


class ReconcileBillingResponse(ContractModel):
    reconciliation_run_id: str
    status: Literal["queued", "running", "completed"]
    estimated_cost: Money
    recorded_usage_cost: Money
    variance: Money
    line_items: list[ReconcileBillingLineItem] = Field(default_factory=list)
    request_id: str
