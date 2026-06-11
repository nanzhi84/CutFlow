from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import (
    ApprovePromptVersionRequest,
    CreatePromptBindingRequest,
    CreatePromptExperimentRequest,
    CreatePromptTemplateRequest,
    CreatePromptVersionRequest,
    ErrorCode,
    PatchPromptBindingRequest,
    PatchPromptExperimentRequest,
    PromptBinding,
    PromptBindingView,
    PromptExperiment,
    PromptExperimentScope,
    PromptSchemaRef,
    PromptTemplate,
    PromptTemplateView,
    PromptVersion,
    PromptVersionView,
    PublishPromptVersionRequest,
    RollbackPromptRequest,
    utcnow,
)
from packages.core.storage.database import (
    PromptBindingRow,
    PromptExperimentRow,
    PromptTemplateRow,
    PromptVersionRow,
)
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.core.contracts.state_machines import assert_transition


def prompt_template_row_to_contract(row: PromptTemplateRow) -> PromptTemplate:
    return PromptTemplate(
        id=row.id,
        name=row.name,
        purpose=row.purpose,
        variables_schema_ref=PromptSchemaRef.model_validate(row.variables_schema_ref),
        output_schema_ref=PromptSchemaRef.model_validate(row.output_schema_ref),
        status=row.status,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def prompt_version_row_to_contract(row: PromptVersionRow) -> PromptVersion:
    return PromptVersion(
        id=row.id,
        prompt_template_id=row.prompt_template_id,
        content=row.content,
        status=row.status,
        changelog=row.changelog,
        approved_at=row.approved_at,
        published_at=row.published_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def prompt_binding_row_to_contract(row: PromptBindingRow) -> PromptBinding:
    return PromptBinding(
        id=row.id,
        prompt_template_id=row.prompt_template_id,
        prompt_version_id=row.prompt_version_id,
        case_id=row.case_id,
        node_id=row.node_id,
        provider_profile_id=row.provider_profile_id,
        priority=row.priority,
        enabled=row.enabled,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def prompt_experiment_row_to_contract(row: PromptExperimentRow) -> PromptExperiment:
    return PromptExperiment(
        id=row.id,
        prompt_template_id=row.prompt_template_id,
        variants=row.variants,
        traffic_split=row.traffic_split,
        scope=PromptExperimentScope.model_validate(row.scope),
        status=row.status,
        start_at=row.start_at,
        end_at=row.end_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyPromptRuntimeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def resolve_published_version(
        self,
        *,
        node_id: str,
        case_id: str | None = None,
        provider_profile_id: str | None = None,
    ) -> tuple[PromptBinding, PromptVersion]:
        with self.session_factory() as session:
            statement = (
                select(PromptBindingRow, PromptVersionRow)
                .join(PromptVersionRow, PromptVersionRow.id == PromptBindingRow.prompt_version_id)
                .where(PromptBindingRow.enabled.is_(True))
                .where(PromptVersionRow.status == "published")
                .where(or_(PromptBindingRow.node_id.is_(None), PromptBindingRow.node_id == node_id))
                .where(or_(PromptBindingRow.case_id.is_(None), PromptBindingRow.case_id == case_id))
                .where(
                    or_(
                        PromptBindingRow.provider_profile_id.is_(None),
                        PromptBindingRow.provider_profile_id == provider_profile_id,
                    )
                )
                .order_by(PromptBindingRow.priority.asc(), PromptBindingRow.created_at.asc())
                .limit(1)
            )
            row = session.execute(statement).first()
            if row is None:
                raise NodeExecutionError(
                    ErrorCode.prompt_version_not_published,
                    f"No published prompt version is bound to {node_id}.",
                )
            binding_row, version_row = row
            return prompt_binding_row_to_contract(binding_row), prompt_version_row_to_contract(version_row)

    def get_template_for_version(self, prompt_version_id: str) -> PromptTemplate:
        with self.session_factory() as session:
            version = session.get(PromptVersionRow, prompt_version_id)
            if version is None:
                raise NodeExecutionError(ErrorCode.prompt_version_not_published, "Prompt version not found.")
            template = session.get(PromptTemplateRow, version.prompt_template_id)
            if template is None:
                raise NodeExecutionError(ErrorCode.prompt_version_not_published, "Prompt template not found.")
            return prompt_template_row_to_contract(template)


class SqlAlchemyPromptRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def list_templates(
        self,
        *,
        status: str | None = None,
        purpose: str | None = None,
        limit: int = 50,
    ) -> list[PromptTemplateView]:
        with self.session_factory() as session:
            statement = select(PromptTemplateRow)
            if status:
                statement = statement.where(PromptTemplateRow.status == status)
            if purpose:
                statement = statement.where(PromptTemplateRow.purpose == purpose)
            statement = statement.order_by(PromptTemplateRow.updated_at.desc()).limit(limit)
            return [self._template_view(session, row) for row in session.scalars(statement)]

    def create_template(self, payload: CreatePromptTemplateRequest) -> PromptTemplateView:
        with self.session_factory() as session:
            row = PromptTemplateRow(
                id=new_id("prompt"),
                name=payload.name,
                purpose=payload.purpose,
                variables_schema_ref=payload.variables_schema_ref.model_dump(mode="json"),
                output_schema_ref=payload.output_schema_ref.model_dump(mode="json"),
                status="draft",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return PromptTemplateView(template=prompt_template_row_to_contract(row))

    def list_versions(self, template_id: str, *, limit: int = 50) -> list[PromptVersionView]:
        with self.session_factory() as session:
            template = session.get(PromptTemplateRow, template_id)
            if template is None:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Prompt template not found.")
            statement = (
                select(PromptVersionRow)
                .where(PromptVersionRow.prompt_template_id == template_id)
                .order_by(PromptVersionRow.created_at.desc())
                .limit(limit)
            )
            template_contract = prompt_template_row_to_contract(template)
            return [
                PromptVersionView(
                    version=prompt_version_row_to_contract(row),
                    template=template_contract,
                )
                for row in session.scalars(statement)
            ]

    def create_version(self, template_id: str, payload: CreatePromptVersionRequest) -> PromptVersionView:
        with self.session_factory() as session:
            template = session.get(PromptTemplateRow, template_id)
            if template is None:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Prompt template not found.")
            row = PromptVersionRow(
                id=new_id("pver"),
                prompt_template_id=template_id,
                content=payload.content,
                status="draft",
                changelog=payload.changelog,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return PromptVersionView(
                version=prompt_version_row_to_contract(row),
                template=prompt_template_row_to_contract(template),
            )

    def approve_version(
        self, template_id: str, version_id: str, payload: ApprovePromptVersionRequest
    ) -> PromptVersionView:
        return self._patch_version(template_id, version_id, {"status": "approved", "approved_at": utcnow()})

    def publish_version(
        self, template_id: str, version_id: str, payload: PublishPromptVersionRequest
    ) -> PromptVersionView:
        return self._patch_version(template_id, version_id, {"status": "published", "published_at": utcnow()})

    def rollback(self, template_id: str, payload: RollbackPromptRequest) -> PromptVersionView:
        return self._patch_version(
            template_id,
            payload.target_version_id,
            {"status": "published", "published_at": utcnow()},
        )

    def list_bindings(self, *, limit: int = 50) -> list[PromptBindingView]:
        with self.session_factory() as session:
            statement = select(PromptBindingRow).order_by(PromptBindingRow.priority.asc()).limit(limit)
            return [self._binding_view(session, row) for row in session.scalars(statement)]

    def create_binding(self, payload: CreatePromptBindingRequest) -> PromptBindingView:
        with self.session_factory() as session:
            self._require_template_and_version(session, payload.prompt_template_id, payload.prompt_version_id)
            row = PromptBindingRow(
                id=new_id("pbind"),
                prompt_template_id=payload.prompt_template_id,
                prompt_version_id=payload.prompt_version_id,
                case_id=payload.case_id,
                node_id=payload.node_id,
                priority=payload.priority,
                enabled=True,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._binding_view(session, row)

    def patch_binding(self, binding_id: str, payload: PatchPromptBindingRequest) -> PromptBindingView:
        with self.session_factory() as session:
            row = session.get(PromptBindingRow, binding_id)
            if row is None:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Prompt binding not found.")
            updates = payload.model_dump(exclude_none=True)
            if "prompt_version_id" in updates:
                self._require_template_and_version(session, row.prompt_template_id, updates["prompt_version_id"])
            for key, value in updates.items():
                setattr(row, key, value)
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return self._binding_view(session, row)

    def list_experiments(
        self,
        *,
        prompt_template_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[PromptExperiment]:
        with self.session_factory() as session:
            statement = select(PromptExperimentRow)
            if prompt_template_id is not None:
                statement = statement.where(PromptExperimentRow.prompt_template_id == prompt_template_id)
            if status is not None:
                statement = statement.where(PromptExperimentRow.status == status)
            statement = statement.order_by(PromptExperimentRow.updated_at.desc()).limit(limit)
            return [prompt_experiment_row_to_contract(row) for row in session.scalars(statement)]

    def create_experiment(self, payload: CreatePromptExperimentRequest) -> PromptExperiment:
        with self.session_factory() as session:
            template = session.get(PromptTemplateRow, payload.prompt_template_id)
            if template is None:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Prompt template not found.")
            row = PromptExperimentRow(
                id=new_id("pexp"),
                prompt_template_id=payload.prompt_template_id,
                variants=payload.variants,
                traffic_split=payload.traffic_split,
                scope=payload.scope.model_dump(mode="json"),
                status="draft",
                start_at=payload.start_at,
                end_at=payload.end_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return prompt_experiment_row_to_contract(row)

    def patch_experiment(
        self, experiment_id: str, payload: PatchPromptExperimentRequest
    ) -> PromptExperiment:
        with self.session_factory() as session:
            row = session.get(PromptExperimentRow, experiment_id)
            if row is None:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Prompt experiment not found.")
            updates = payload.model_dump(exclude_none=True)
            for key, value in updates.items():
                setattr(row, key, value)
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return prompt_experiment_row_to_contract(row)

    def _patch_version(self, template_id: str, version_id: str, updates: dict) -> PromptVersionView:
        with self.session_factory() as session:
            template = session.get(PromptTemplateRow, template_id)
            version = session.get(PromptVersionRow, version_id)
            if template is None or version is None or version.prompt_template_id != template_id:
                raise NodeExecutionError(ErrorCode.validation_invalid_options, "Prompt version not found.")
            if "status" in updates:
                if version.status == "draft" and updates["status"] == "approved":
                    assert_transition("prompt_version", version.status, "reviewing")
                    version.status = "reviewing"
                    version.updated_at = utcnow()
                assert_transition("prompt_version", version.status, updates["status"])
            for key, value in updates.items():
                setattr(version, key, value)
            version.updated_at = utcnow()
            if updates.get("status") == "published":
                template.status = "active"
                template.updated_at = utcnow()
            session.commit()
            session.refresh(version)
            session.refresh(template)
            return PromptVersionView(
                version=prompt_version_row_to_contract(version),
                template=prompt_template_row_to_contract(template),
            )

    def _template_view(self, session: Session, template: PromptTemplateRow) -> PromptTemplateView:
        published = session.scalar(
            select(PromptVersionRow)
            .where(PromptVersionRow.prompt_template_id == template.id)
            .where(PromptVersionRow.status == "published")
            .order_by(PromptVersionRow.published_at.desc().nullslast(), PromptVersionRow.updated_at.desc())
            .limit(1)
        )
        return PromptTemplateView(
            template=prompt_template_row_to_contract(template),
            published_version=prompt_version_row_to_contract(published) if published else None,
        )

    def _binding_view(self, session: Session, binding: PromptBindingRow) -> PromptBindingView:
        version = session.get(PromptVersionRow, binding.prompt_version_id)
        return PromptBindingView(
            binding=prompt_binding_row_to_contract(binding),
            resolved_version=prompt_version_row_to_contract(version) if version else None,
        )

    def _require_template_and_version(
        self, session: Session, template_id: str, version_id: str
    ) -> None:
        template = session.get(PromptTemplateRow, template_id)
        version = session.get(PromptVersionRow, version_id)
        if template is None or version is None or version.prompt_template_id != template_id:
            raise NodeExecutionError(ErrorCode.validation_invalid_options, "Prompt version not found.")
