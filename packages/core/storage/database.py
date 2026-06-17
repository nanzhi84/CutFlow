from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import UserDefinedType

from packages.core.config import build_settings


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Vector(UserDefinedType):
    cache_ok = True

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **_: object) -> str:
        return f"vector({self.dimensions})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="v1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UserRow(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")


class SessionRow(TimestampMixin, Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyRecordRow(Base):
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    method: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (UniqueConstraint("key", "method", "path"),)


class RegistrationCodeRow(TimestampMixin, Base):
    __tablename__ = "registration_codes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purpose: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UploadSessionRow(TimestampMixin, Base):
    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    filename: Mapped[str] = mapped_column(String, nullable=False)
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    object_uri: Mapped[str | None] = mapped_column(Text)
    local_temp_path: Mapped[str | None] = mapped_column(Text)
    stabilize: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stabilized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SecretRow(TimestampMixin, Base):
    __tablename__ = "secrets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    environment: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    secret_ref: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    rotated_from_secret_id: Mapped[str | None] = mapped_column(String)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseRow(TimestampMixin, Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    description: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(String)
    product: Mapped[str | None] = mapped_column(String)
    target_audience: Mapped[str | None] = mapped_column(Text)
    key_selling_points: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    ip_persona: Mapped[str | None] = mapped_column(Text)
    brand_voice: Mapped[str | None] = mapped_column(Text)
    strategy_tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    brand_keywords: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    competitor_names: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)


class ArtifactRow(TimestampMixin, Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    run_id: Mapped[str | None] = mapped_column(String)
    node_run_id: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    uri: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    oss_uri: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retention_policy: Mapped[str] = mapped_column(String, nullable=False, default="default")
    sha256: Mapped[str | None] = mapped_column(String)
    media_info: Mapped[dict | None] = mapped_column(JSONB)
    payload_schema: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONB)
    created_by_node_run_id: Mapped[str | None] = mapped_column(String)


class JobRow(TimestampMixin, Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    request_schema: Mapped[str] = mapped_column(String, nullable=False)
    request: Mapped[dict] = mapped_column(JSONB, nullable=False)
    active_run_id: Mapped[str | None] = mapped_column(String)
    latest_finished_video_id: Mapped[str | None] = mapped_column(String)


class WorkflowRunRow(TimestampMixin, Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    workflow_template_id: Mapped[str] = mapped_column(String, nullable=False)
    workflow_version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    run_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resume_from_run_id: Mapped[str | None] = mapped_column(String)
    retry_of_run_id: Mapped[str | None] = mapped_column(String)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    experiment_assignment_id: Mapped[str | None] = mapped_column(String)
    public_report_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    debug_report_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NodeRunRow(TimestampMixin, Base):
    __tablename__ = "node_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False)
    node_id: Mapped[str] = mapped_column(String, nullable=False)
    node_version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_manifest_hash: Mapped[str] = mapped_column(String, nullable=False)
    output_artifact_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    provider_invocation_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    error: Mapped[dict | None] = mapped_column(JSONB)
    skipped_reason: Mapped[str | None] = mapped_column(Text)
    degradation_reason: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    degradations: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaAssetRow(TimestampMixin, Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    source_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    annotation_status: Mapped[str] = mapped_column(String, nullable=False)
    usable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    thumbnail_uri: Mapped[str | None] = mapped_column(Text)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)


class SelectionLedgerRow(Base):
    __tablename__ = "selection_ledger"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    medium: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    clip_id: Mapped[str | None] = mapped_column(String)
    slot_phase: Mapped[str] = mapped_column(String, nullable=False)
    diversity_key: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "case_id",
            "run_id",
            "medium",
            "asset_id",
            "clip_id",
            "slot_phase",
            postgresql_nulls_not_distinct=True,
        ),
    )


class SelectionReservationRow(Base):
    __tablename__ = "selection_reservations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(String, nullable=False)
    medium: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    diversity_key: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="reserved")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("run_id", "medium", "asset_id"),
    )


class AnnotationRow(TimestampMixin, Base):
    __tablename__ = "annotations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False)
    etag: Mapped[str] = mapped_column(String, nullable=False)
    canonical_schema: Mapped[str] = mapped_column(String, nullable=False)
    canonical: Mapped[dict] = mapped_column(JSONB, nullable=False)
    projection_schema: Mapped[str] = mapped_column(String, nullable=False)
    projection: Mapped[dict] = mapped_column(JSONB, nullable=False)
    editable_paths: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)


class VoiceProfileRow(TimestampMixin, Base):
    __tablename__ = "voice_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    provider_profile_id: Mapped[str | None] = mapped_column(String)
    preview_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ProviderProfileRow(TimestampMixin, Base):
    __tablename__ = "provider_profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    capability: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    environment: Mapped[str] = mapped_column(String, nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(String)
    concurrency_key: Mapped[str] = mapped_column(String, nullable=False, default="default")
    timeout_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    retry_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    cost_policy_id: Mapped[str | None] = mapped_column(String)
    options_schema_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    default_options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(String, nullable=False, default="v1")


class ProviderCapabilityRow(TimestampMixin, Base):
    __tablename__ = "provider_capabilities"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    capability_id: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False, default="*")
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    input_schema_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    output_schema_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    options_schema_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    supports_async_job: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    supports_cancel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_payload_bytes: Mapped[int | None] = mapped_column(Integer)
    max_duration_sec: Mapped[float | None] = mapped_column(Float)
    default_timeout_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    input_schema_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    output_schema_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (UniqueConstraint("provider_id", "capability_id"),)


class ProviderBalanceSnapshotRow(TimestampMixin, Base):
    __tablename__ = "provider_balance_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    account_group: Mapped[str | None] = mapped_column(String)
    balance_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    currency: Mapped[str | None] = mapped_column(String(3))
    quota_remaining: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderInvocationRow(TimestampMixin, Base):
    __tablename__ = "provider_invocations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    node_run_id: Mapped[str | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    provider_profile_id: Mapped[str] = mapped_column(String, nullable=False)
    capability_id: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    price_item_id: Mapped[str | None] = mapped_column(ForeignKey("provider_price_items.id"))
    billing_status: Mapped[str] = mapped_column(String, nullable=False, default="estimated")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[dict | None] = mapped_column(JSONB)
    actual_cost: Mapped[dict | None] = mapped_column(JSONB)
    request_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    response_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    external_job_id: Mapped[str | None] = mapped_column(String)
    error: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageMeterRecordRow(TimestampMixin, Base):
    __tablename__ = "usage_meter_records"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_invocation_id: Mapped[str] = mapped_column(
        ForeignKey("provider_invocations.id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    capability_id: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    audio_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    video_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_credits: Mapped[Decimal | None] = mapped_column(Numeric)
    raw_usage: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ProviderPriceCatalogRow(TimestampMixin, Base):
    __tablename__ = "provider_price_catalogs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)


class ProviderPriceItemRow(TimestampMixin, Base):
    __tablename__ = "provider_price_items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    catalog_id: Mapped[str] = mapped_column(
        ForeignKey("provider_price_catalogs.id", ondelete="CASCADE"), nullable=False
    )
    provider_id: Mapped[str] = mapped_column(String, nullable=False)
    model_id: Mapped[str] = mapped_column(String, nullable=False)
    capability_id: Mapped[str] = mapped_column(String, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)
    unit_price: Mapped[dict] = mapped_column(JSONB, nullable=False)
    active_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PromptTemplateRow(TimestampMixin, Base):
    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    variables_schema_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    output_schema_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)


class PromptVersionRow(TimestampMixin, Base):
    __tablename__ = "prompt_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    prompt_template_id: Mapped[str] = mapped_column(
        ForeignKey("prompt_templates.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    changelog: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PromptBindingRow(TimestampMixin, Base):
    __tablename__ = "prompt_bindings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    prompt_template_id: Mapped[str] = mapped_column(ForeignKey("prompt_templates.id"), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(ForeignKey("prompt_versions.id"), nullable=False)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    node_id: Mapped[str | None] = mapped_column(String)
    provider_profile_id: Mapped[str | None] = mapped_column(String)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PromptInvocationRow(TimestampMixin, Base):
    __tablename__ = "prompt_invocations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    prompt_template_id: Mapped[str] = mapped_column(ForeignKey("prompt_templates.id"), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(ForeignKey("prompt_versions.id"), nullable=False)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    node_run_id: Mapped[str | None] = mapped_column(ForeignKey("node_runs.id", ondelete="SET NULL"))
    provider_invocation_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_invocations.id", ondelete="SET NULL")
    )
    variables_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    output_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    status: Mapped[str] = mapped_column(String, nullable=False)


class PromptExperimentRow(TimestampMixin, Base):
    __tablename__ = "prompt_experiments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    prompt_template_id: Mapped[str] = mapped_column(ForeignKey("prompt_templates.id"), nullable=False)
    variants: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    traffic_split: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScriptVersionRow(TimestampMixin, Base):
    __tablename__ = "script_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    creative_intent_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    adopted_from_draft_id: Mapped[str | None] = mapped_column(String)


class VideoVersionRow(TimestampMixin, Base):
    __tablename__ = "video_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    script_version_id: Mapped[str | None] = mapped_column(ForeignKey("script_versions.id"))
    finished_video_id: Mapped[str | None] = mapped_column(String)
    timeline_plan_artifact_id: Mapped[str] = mapped_column(String, nullable=False)
    style_plan_artifact_id: Mapped[str] = mapped_column(String, nullable=False)


class CaseAgentSourceBindingRow(TimestampMixin, Base):
    __tablename__ = "case_agent_source_bindings"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String)


class CaseAgentRunRow(TimestampMixin, Base):
    __tablename__ = "case_agent_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    goal: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source_binding_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)


class CreativeBriefRow(TimestampMixin, Base):
    __tablename__ = "creative_briefs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    source_binding_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    topic: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str | None] = mapped_column(Text)
    key_insights: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    source_refs: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    generated_by_run_id: Mapped[str | None] = mapped_column(String)


class ScriptDraftRow(TimestampMixin, Base):
    __tablename__ = "script_drafts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    script: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    memory_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)


class CaseMemoryRow(TimestampMixin, Base):
    __tablename__ = "case_memories"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    memory_type: Mapped[str] = mapped_column(String, nullable=False, default="script_pattern")
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scope_key: Mapped[str | None] = mapped_column(String)
    insight: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    supersedes_memory_id: Mapped[str | None] = mapped_column(String)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding: Mapped[object | None] = mapped_column(Vector(1536))

    __table_args__ = (CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),)


class MemoryProposalRow(TimestampMixin, Base):
    __tablename__ = "memory_proposals"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    memory_type: Mapped[str] = mapped_column(String, nullable=False, default="script_pattern")
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scope_key: Mapped[str | None] = mapped_column(String)
    insight: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    supersedes_memory_id: Mapped[str | None] = mapped_column(String)
    proposed_by_reflection_run_id: Mapped[str | None] = mapped_column(String)


class ReflectionRunRow(TimestampMixin, Base):
    __tablename__ = "reflection_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    window: Mapped[str] = mapped_column(String, nullable=False)
    report_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    input_observation_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    input_feature_vector_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    memory_proposal_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PublishRecordRow(TimestampMixin, Base):
    __tablename__ = "publish_records"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    video_version_id: Mapped[str | None] = mapped_column(ForeignKey("video_versions.id"))
    publish_package_id: Mapped[str | None] = mapped_column(String)
    publish_batch_id: Mapped[str | None] = mapped_column(String)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    cover_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PerformanceObservationRow(TimestampMixin, Base):
    __tablename__ = "performance_observations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    publish_record_id: Mapped[str] = mapped_column(String, nullable=False)
    video_version_id: Mapped[str | None] = mapped_column(String)
    platform: Mapped[str | None] = mapped_column(String)
    account_id: Mapped[str | None] = mapped_column(String)
    window: Mapped[str | None] = mapped_column(String)
    metric_name: Mapped[str] = mapped_column(String, nullable=False)
    metric_value: Mapped[float] = mapped_column(Float, nullable=False)
    impressions: Mapped[int | None] = mapped_column(Integer)
    views: Mapped[int | None] = mapped_column(Integer)
    avg_watch_sec: Mapped[float | None] = mapped_column(Float)
    completion_rate: Mapped[float | None] = mapped_column(Float)
    like_rate: Mapped[float | None] = mapped_column(Float)
    comment_rate: Mapped[float | None] = mapped_column(Float)
    share_rate: Mapped[float | None] = mapped_column(Float)
    follow_rate: Mapped[float | None] = mapped_column(Float)
    conversion_count: Mapped[int | None] = mapped_column(Integer)
    conversion_rate: Mapped[float | None] = mapped_column(Float)
    raw_metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CreativeFeatureVectorRow(TimestampMixin, Base):
    __tablename__ = "creative_feature_vectors"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    script_version_id: Mapped[str | None] = mapped_column(String)
    video_version_id: Mapped[str | None] = mapped_column(String)
    hook_type: Mapped[str | None] = mapped_column(String)
    script_structure: Mapped[str | None] = mapped_column(String)
    topic_tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    cta_type: Mapped[str | None] = mapped_column(String)
    angle: Mapped[str | None] = mapped_column(String)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    broll_density: Mapped[float | None] = mapped_column(Float)
    cut_density: Mapped[float | None] = mapped_column(Float)
    subtitle_style_id: Mapped[str | None] = mapped_column(String)
    bgm_id: Mapped[str | None] = mapped_column(String)
    cover_style: Mapped[str | None] = mapped_column(String)
    material_ids: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    broll_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PerformanceScoreRow(TimestampMixin, Base):
    __tablename__ = "performance_scores"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    observation_id: Mapped[str] = mapped_column(String, nullable=False)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    video_version_id: Mapped[str | None] = mapped_column(String)
    platform: Mapped[str | None] = mapped_column(String)
    account_id: Mapped[str | None] = mapped_column(String)
    window: Mapped[str] = mapped_column(String, nullable=False)
    primary_metric: Mapped[str] = mapped_column(String, nullable=False)
    normalized_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_reason: Mapped[str | None] = mapped_column(String)


class CaseKnowledgeItemRow(TimestampMixin, Base):
    __tablename__ = "case_knowledge_items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    ref_id: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    embedding_ref: Mapped[str | None] = mapped_column(String)
    score: Mapped[float | None] = mapped_column(Float)


class FinishedVideoRow(TimestampMixin, Base):
    __tablename__ = "finished_videos"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_runs.id"))
    title: Mapped[str] = mapped_column(String, nullable=False)
    video_artifact: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cover_artifact: Mapped[dict | None] = mapped_column(JSONB)
    subtitle_artifact: Mapped[dict | None] = mapped_column(JSONB)
    duration_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    qc_status: Mapped[str] = mapped_column(String, nullable=False)
    # LipSync provider attribution (migration 0011). All optional / defaulted so the
    # add is safe on populated tables and create_all DBs.
    lipsync_provider_id: Mapped[str | None] = mapped_column(String)
    lipsync_fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lipsync_fallback_reason: Mapped[str | None] = mapped_column(Text)


class PublishPackageRow(TimestampMixin, Base):
    __tablename__ = "publish_packages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    source_finished_video_id: Mapped[str | None] = mapped_column(ForeignKey("finished_videos.id"))
    upload_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    video_artifact: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cover_artifact: Mapped[dict | None] = mapped_column(JSONB)
    platform_defaults: Mapped[dict] = mapped_column(JSONB, nullable=False)


class PublishBatchRow(TimestampMixin, Base):
    __tablename__ = "publish_batches"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)


class PublishBatchItemRow(TimestampMixin, Base):
    __tablename__ = "publish_batch_items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("publish_batches.id", ondelete="CASCADE"), nullable=False)
    publish_package_id: Mapped[str] = mapped_column(ForeignKey("publish_packages.id"), nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    # §28.1 publish-copy + cover + platform-payload fields (migration 0007).
    publish_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cover_title: Mapped[str] = mapped_column(String, nullable=False, default="")
    cover_subtitle: Mapped[str] = mapped_column(String, nullable=False, default="")
    cover_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    location: Mapped[str | None] = mapped_column(String)
    account_group: Mapped[str | None] = mapped_column(String)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PublishAttemptRow(TimestampMixin, Base):
    __tablename__ = "publish_attempts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("publish_batches.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[str] = mapped_column(ForeignKey("publish_batch_items.id"), nullable=False)
    platforms: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    adapter_id: Mapped[str] = mapped_column(String, nullable=False, default="sandbox.publish")
    external_task_id: Mapped[str | None] = mapped_column(String)
    results: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    error: Mapped[dict | None] = mapped_column(JSONB)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class YieldFunnelEventRow(TimestampMixin, Base):
    __tablename__ = "yield_funnel_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    finished_video_id: Mapped[str | None] = mapped_column(ForeignKey("finished_videos.id", ondelete="SET NULL"))
    publish_package_id: Mapped[str | None] = mapped_column(ForeignKey("publish_packages.id", ondelete="SET NULL"))
    publish_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("publish_attempts.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    affects_true_yield: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CostRollupRow(TimestampMixin, Base):
    __tablename__ = "cost_rollups"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    group_key: Mapped[str] = mapped_column(String, nullable=False)
    group_by: Mapped[str | None] = mapped_column(String)
    estimated_cost: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actual_cost: Mapped[dict | None] = mapped_column(JSONB)
    invocations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BudgetRow(TimestampMixin, Base):
    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(String)
    period: Mapped[str] = mapped_column(String, nullable=False, default="day")
    limit: Mapped[dict] = mapped_column(JSONB, nullable=False)
    alert_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enforce: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class ProviderBillingReconciliationRow(TimestampMixin, Base):
    __tablename__ = "provider_billing_reconciliations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str | None] = mapped_column(String)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    estimated_cost: Mapped[dict] = mapped_column(JSONB, nullable=False)
    recorded_usage_cost: Mapped[dict] = mapped_column(JSONB, nullable=False)
    variance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    line_items: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    request_id: Mapped[str] = mapped_column(String, nullable=False)


class OpsAlertRuleRow(TimestampMixin, Base):
    __tablename__ = "ops_alert_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    metric: Mapped[str] = mapped_column(String, nullable=False)
    condition: Mapped[str] = mapped_column(String, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    channels: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    severity: Mapped[str] = mapped_column(String, nullable=False, default="warning")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class OpsAlertEventRow(TimestampMixin, Base):
    __tablename__ = "ops_alert_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, nullable=False)
    rule_id: Mapped[str | None] = mapped_column(ForeignKey("ops_alert_rules.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FailureTaxonomyRow(TimestampMixin, Base):
    __tablename__ = "failure_taxonomy"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    failure_class: Mapped[str] = mapped_column(String, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    node_id: Mapped[str | None] = mapped_column(String)
    message: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String, unique=True)


class ProductionQualityCheckRow(TimestampMixin, Base):
    __tablename__ = "production_quality_checks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    check_type: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[str] = mapped_column(String, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String)
    evidence_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))
    affects_true_yield: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ApprovalRequestRow(TimestampMixin, Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)


class AuditEventRow(TimestampMixin, Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    actor: Mapped[str | None] = mapped_column(String)
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ImportBatchReportRow(TimestampMixin, Base):
    __tablename__ = "import_batch_reports"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    import_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    mapping_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("artifacts.id"))


class OutboxEventRow(TimestampMixin, Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    topic: Mapped[str] = mapped_column(String, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    payload_schema: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict | list | str | int | float | bool] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("aggregate_type", "aggregate_id", "topic", "dedupe_key"),
    )


Index("idx_artifacts_case_run", ArtifactRow.case_id, ArtifactRow.run_id)
Index("idx_artifacts_run", ArtifactRow.run_id)
Index("idx_artifacts_run_kind", ArtifactRow.run_id, ArtifactRow.kind)
Index("idx_node_runs_run", NodeRunRow.run_id)
Index(
    "idx_provider_balance_snapshots_provider",
    ProviderBalanceSnapshotRow.provider_id,
    ProviderBalanceSnapshotRow.account_group,
)
Index("idx_selection_ledger_case_medium", SelectionLedgerRow.case_id, SelectionLedgerRow.medium)
Index("idx_selection_ledger_asset", SelectionLedgerRow.medium, SelectionLedgerRow.asset_id)
# §17: selection_reservations must have a TTL index so expiry sweeps + active-slot
# lookups stay index-backed (status to scope to live reservations, expires_at for TTL).
Index(
    "idx_selection_reservations_active",
    SelectionReservationRow.case_id,
    SelectionReservationRow.medium,
    SelectionReservationRow.status,
)
Index("idx_selection_reservations_ttl", SelectionReservationRow.status, SelectionReservationRow.expires_at)
Index("idx_provider_invocations_case", ProviderInvocationRow.case_id, ProviderInvocationRow.provider_id)
Index("idx_usage_meter_provider", UsageMeterRecordRow.provider_id, UsageMeterRecordRow.capability_id)
Index("idx_case_memories_case_status", CaseMemoryRow.case_id, CaseMemoryRow.status)
Index("idx_case_memories_case_type", CaseMemoryRow.case_id, CaseMemoryRow.memory_type)
Index("idx_performance_case_metric", PerformanceObservationRow.case_id, PerformanceObservationRow.metric_name)
Index("idx_performance_video", PerformanceObservationRow.video_version_id)
Index("idx_feature_vectors_case", CreativeFeatureVectorRow.case_id)
Index("idx_feature_vectors_video", CreativeFeatureVectorRow.video_version_id)
Index("idx_performance_scores_case", PerformanceScoreRow.case_id, PerformanceScoreRow.window)
Index("idx_performance_scores_observation", PerformanceScoreRow.observation_id)
Index("idx_knowledge_items_case_kind", CaseKnowledgeItemRow.case_id, CaseKnowledgeItemRow.kind)
Index("idx_outbox_pending", OutboxEventRow.status, OutboxEventRow.available_at, OutboxEventRow.created_at, OutboxEventRow.id)
Index("idx_failure_taxonomy_class", FailureTaxonomyRow.failure_class, FailureTaxonomyRow.created_at)
Index("idx_failure_taxonomy_run", FailureTaxonomyRow.run_id)
Index("idx_ops_alert_events_status", OpsAlertEventRow.status, OpsAlertEventRow.code)


def database_url() -> str:
    value = build_settings().storage.database_url
    if value:
        return value
    raise RuntimeError(
        "CUTAGENT_DATABASE_URL is required when CUTAGENT_STORAGE_BACKEND=sqlalchemy. "
        "For local development, use "
        "postgresql+psycopg://cutagent:cutagent@127.0.0.1:55432/cutagent."
    )


def create_database_engine(url: str | None = None) -> Engine:
    resolved = url or database_url()
    if make_url(resolved).get_backend_name() == "sqlite":
        # sqlite uses StaticPool/no pooling; pool sizing args do not apply and
        # would break in-memory unit-test engines, so keep the original behavior.
        return create_engine(resolved, pool_pre_ping=True)
    pool = build_settings().storage
    return create_engine(
        resolved,
        pool_pre_ping=True,
        pool_size=pool.pool_size,
        max_overflow=pool.max_overflow,
        pool_recycle=pool.pool_recycle,
        pool_timeout=pool.pool_timeout,
    )


def create_session_factory(engine: Engine | None = None) -> sessionmaker:
    return sessionmaker(bind=engine or create_database_engine(), expire_on_commit=False)


def table_names() -> set[str]:
    return set(Base.metadata.tables)
