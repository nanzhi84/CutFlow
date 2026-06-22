from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from packages.core.auth.service import create_password_hasher
from packages.core.config import build_settings
from packages.core.storage.database import (
    CaseRow,
    MediaAssetRow,
    OpsAlertEventRow,
    PromptBindingRow,
    PromptTemplateRow,
    PromptVersionRow,
    ProviderCapabilityRow,
    ProviderPriceCatalogRow,
    ProviderPriceItemRow,
    ProviderProfileRow,
    RegistrationCodeRow,
    UserRow,
    VoiceProfileRow,
)
from packages.core.storage.repository import Repository


LOCAL_AUTH_SEED_USER_IDS = {"usr_admin", "usr_viewer"}
LOCAL_AUTH_SEED_REGISTRATION_CODE_IDS = {"reg_seed_local_admin"}


def seed_rows(
    repository: Repository | None = None, *, include_local_auth_seed: bool = True
) -> list[object]:
    source = repository or Repository()
    password_hasher = create_password_hasher()
    registration_hash_by_id = {
        code_id: code_hash for code_hash, code_id in source.registration_code_hashes.items()
    }
    rows: list[object] = []
    source_users = list(source.users.values())
    source_registration_codes = list(source.registration_codes.values())
    if not include_local_auth_seed:
        source_users = [
            user for user in source_users if user.id not in LOCAL_AUTH_SEED_USER_IDS
        ]
        source_registration_codes = [
            code
            for code in source_registration_codes
            if code.id not in LOCAL_AUTH_SEED_REGISTRATION_CODE_IDS
        ]
    rows.extend(
        [
            UserRow(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                password_hash=password_hasher.hash(
                    "local-admin" if user.id == "usr_admin" else "local-viewer"
                ),
                role=user.role.value,
                status=user.status,
                schema_version=user.schema_version,
                created_at=user.created_at,
                updated_at=user.updated_at,
            )
            for user in source_users
        ]
    )
    rows.extend(
        [
            RegistrationCodeRow(
                id=code.id,
                code_hash=registration_hash_by_id[code.id],
                role=code.role.value,
                status=code.status,
                max_uses=code.max_uses,
                used_count=code.used_count,
                expires_at=code.expires_at,
                schema_version="v1",
                created_at=code.created_at,
                updated_at=code.created_at,
            )
            for code in source_registration_codes
        ]
    )
    rows.extend(
        [
            CaseRow(
                id=case.id,
                name=case.name,
                owner_user_id=(
                    None
                    if not include_local_auth_seed
                    and case.owner_user_id in LOCAL_AUTH_SEED_USER_IDS
                    else case.owner_user_id
                ),
                status=case.status,
                description=case.description,
                industry=case.industry,
                product=case.product,
                target_audience=case.target_audience,
                schema_version=case.schema_version,
                created_at=case.created_at,
                updated_at=case.updated_at,
            )
            for case in source.cases.values()
        ]
    )
    rows.extend(
        [
            MediaAssetRow(
                id=asset.id,
                case_id=asset.case_id,
                title=asset.title,
                kind=asset.kind,
                source_artifact_id=asset.source_artifact_id,
                tags=asset.tags,
                annotation_status=asset.annotation_status,
                usable=asset.usable,
                schema_version=asset.schema_version,
                created_at=asset.created_at,
                updated_at=asset.updated_at,
            )
            for asset in source.media_assets.values()
        ]
    )
    rows.extend(
        [
            VoiceProfileRow(
                id=voice.id,
                display_name=voice.display_name,
                source=voice.source,
                vendor=voice.vendor,
                provider_profile_id=voice.provider_profile_id,
                preview_artifact_id=voice.preview_artifact_id,
                enabled=voice.enabled,
                status=voice.status,
                schema_version=voice.schema_version,
                created_at=voice.created_at,
                updated_at=voice.updated_at,
            )
            for voice in source.voices.values()
        ]
    )
    rows.extend(
        [
            ProviderProfileRow(
                id=profile.id,
                provider_id=profile.provider_id,
                model_id=profile.model_id,
                capability=profile.capability,
                display_name=profile.display_name,
                environment=profile.environment,
                secret_ref=profile.secret_ref,
                concurrency_key=profile.concurrency_key,
                timeout_sec=profile.timeout_sec,
                retry_policy=profile.retry_policy.model_dump(mode="json"),
                cost_policy_id=profile.cost_policy_id,
                options_schema_ref=profile.options_schema_ref.model_dump(mode="json"),
                default_options=profile.default_options,
                enabled=profile.enabled,
                version=profile.version,
                schema_version=profile.schema_version,
                created_at=profile.created_at,
                updated_at=profile.updated_at,
            )
            for profile in source.provider_profiles.values()
        ]
    )
    rows.extend(
        [
            ProviderCapabilityRow(
                id=capability.id,
                provider_id=capability.provider_id,
                capability_id=capability.capability,
                model_id=capability.model_id,
                display_name=capability.display_name,
                input_schema_id=capability.input_schema_id,
                output_schema_id=capability.output_schema_id,
                options_schema_id=capability.options_schema_id,
                supports_async_job=capability.supports_async_job,
                supports_cancel=capability.supports_cancel,
                max_payload_bytes=capability.max_payload_bytes,
                max_duration_sec=capability.max_duration_sec,
                default_timeout_sec=capability.default_timeout_sec,
                input_schema_ref={
                    "schema_id": capability.input_schema_id,
                    "schema_version": "v1",
                    "dialect": "pydantic",
                    "sha256": "dev-unpinned",
                },
                output_schema_ref={
                    "schema_id": capability.output_schema_id,
                    "schema_version": "v1",
                    "dialect": "pydantic",
                    "sha256": "dev-unpinned",
                },
                enabled=True,
                schema_version=capability.schema_version,
                created_at=capability.created_at,
                updated_at=capability.updated_at,
            )
            for capability in source.provider_capabilities.values()
        ]
    )
    rows.extend(
        [
            PromptTemplateRow(
                id=template.id,
                name=template.name,
                purpose=template.purpose,
                variables_schema_ref=template.variables_schema_ref.model_dump(mode="json"),
                output_schema_ref=template.output_schema_ref.model_dump(mode="json"),
                status=template.status,
                schema_version=template.schema_version,
                created_at=template.created_at,
                updated_at=template.updated_at,
            )
            for template in source.prompt_templates.values()
        ]
    )
    rows.extend(
        [
            PromptVersionRow(
                id=version.id,
                prompt_template_id=version.prompt_template_id,
                content=version.content,
                status=version.status,
                changelog=version.changelog,
                approved_at=version.approved_at,
                published_at=version.published_at,
                schema_version=version.schema_version,
                created_at=version.created_at,
                updated_at=version.updated_at,
            )
            for version in source.prompt_versions.values()
        ]
    )
    rows.extend(
        [
            PromptBindingRow(
                id=binding.id,
                prompt_template_id=binding.prompt_template_id,
                prompt_version_id=binding.prompt_version_id,
                case_id=binding.case_id,
                node_id=binding.node_id,
                provider_profile_id=binding.provider_profile_id,
                priority=binding.priority,
                enabled=binding.enabled,
                schema_version=binding.schema_version,
                created_at=binding.created_at,
                updated_at=binding.updated_at,
            )
            for binding in source.prompt_bindings.values()
        ]
    )
    rows.extend(
        [
            ProviderPriceCatalogRow(
                id=catalog.id,
                provider_id=catalog.provider_id,
                status=catalog.status,
                currency=catalog.currency,
                schema_version=catalog.schema_version,
                created_at=catalog.created_at,
                updated_at=catalog.updated_at,
            )
            for catalog in source.price_catalogs.values()
        ]
    )
    rows.extend(
        [
            ProviderPriceItemRow(
                id=item.id,
                catalog_id=item.catalog_id,
                provider_id=item.provider_id,
                model_id=item.model_id,
                capability_id=item.capability_id,
                unit=item.unit,
                unit_price=item.unit_price.model_dump(mode="json"),
                active_from=item.active_from,
                active_to=item.active_to,
                schema_version=item.schema_version,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in source.price_items.values()
        ]
    )
    rows.extend(
        [
            OpsAlertEventRow(
                id=alert.id,
                code=alert.code,
                status=alert.status,
                message=alert.message,
                severity=alert.severity,
                schema_version=alert.schema_version,
                created_at=alert.created_at,
                updated_at=alert.updated_at,
            )
            for alert in source.alerts.values()
        ]
    )
    return rows


def seed_database(session: Session, rows: Iterable[object] | None = None) -> int:
    inserted = 0
    effective_rows = (
        rows
        if rows is not None
        else seed_rows(include_local_auth_seed=build_settings().auth.seed_local_auth)
    )
    for row in effective_rows:
        existing = session.get(type(row), row.id)
        if existing is None:
            session.add(row)
            inserted += 1
        elif isinstance(row, UserRow) and row.id in {"usr_admin", "usr_viewer"}:
            existing.password_hash = row.password_hash
    session.commit()
    return inserted
