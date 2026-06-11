from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel

from packages.core.registration_codes import hash_registration_code
from packages.core.storage.provider_seed import seed_real_provider_configuration
from packages.core.contracts import (
    AnnotationEditorVm,
    Artifact,
    ArtifactKind,
    ArtifactRef,
    AuthUser,
    Budget,
    CaseAgentRun,
    CaseAgentSourceBinding,
    CaseDetail,
    CaseMemory,
    CostRollup,
    CreativeBrief,
    CreativePattern,
    FinishedVideo,
    ImportBatchReport,
    Job,
    MediaInfo,
    MediaAssetRecord,
    MemoryProposal,
    Money,
    OpsAlertEvent,
    OutboxEvent,
    PerformanceObservation,
    PromptBinding,
    PromptExperiment,
    PromptInvocation,
    PromptSchemaRef,
    PromptTemplate,
    PromptVersion,
    ProviderCapability,
    ProviderInvocation,
    ProviderOptionsSchemaRef,
    ProviderPriceCatalog,
    ProviderPriceItem,
    ProviderProfile,
    PublishAttempt,
    PublishBatchItemVm,
    PublishBatchVm,
    PublishDefaults,
    PublishPackage,
    PublishRecord,
    ReflectionRun,
    RegistrationCodePreview,
    RunStatus,
    RunEvent,
    ScriptDraft,
    ScriptVersion,
    SecretPreview,
    UploadSession,
    UsageMeterRecord,
    UserRole,
    VideoVersion,
    VoiceProfile,
    WorkflowRun,
    YieldFunnelEvent,
    utcnow,
)


TModel = TypeVar("TModel", bound=BaseModel)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Repository:
    """In-process repository boundary for the first clean-slate implementation slice."""

    def __init__(self) -> None:
        self.users: dict[str, AuthUser] = {}
        self.sessions: dict[str, dict] = {}
        self.password_hashes: dict[str, str] = {}
        self.registration_codes: dict[str, RegistrationCodePreview] = {}
        self.registration_code_hashes: dict[str, str] = {}
        self.uploads: dict[str, UploadSession] = {}
        self.secrets: dict[str, SecretPreview] = {}
        self.cases: dict[str, CaseDetail] = {}
        self.jobs: dict[str, Job] = {}
        self.runs: dict[str, WorkflowRun] = {}
        self.node_runs: dict[str, list] = {}
        self.artifacts: dict[str, Artifact] = {}
        self.media_assets: dict[str, MediaAssetRecord] = {}
        self.annotations: dict[str, AnnotationEditorVm] = {}
        self.voices: dict[str, VoiceProfile] = {}
        self.prompt_templates: dict[str, PromptTemplate] = {}
        self.prompt_versions: dict[str, PromptVersion] = {}
        self.prompt_bindings: dict[str, PromptBinding] = {}
        self.prompt_invocations: dict[str, PromptInvocation] = {}
        self.prompt_experiments: dict[str, PromptExperiment] = {}
        self.provider_profiles: dict[str, ProviderProfile] = {}
        self.provider_capabilities: dict[str, ProviderCapability] = {}
        self.price_catalogs: dict[str, ProviderPriceCatalog] = {}
        self.price_items: dict[str, ProviderPriceItem] = {}
        self.provider_invocations: dict[str, ProviderInvocation] = {}
        self.usage_records: dict[str, UsageMeterRecord] = {}
        self.source_bindings: dict[str, CaseAgentSourceBinding] = {}
        self.case_agent_runs: dict[str, CaseAgentRun] = {}
        self.briefs: dict[str, CreativeBrief] = {}
        self.drafts: dict[str, ScriptDraft] = {}
        self.memories: dict[str, CaseMemory] = {}
        self.memory_proposals: dict[str, MemoryProposal] = {}
        self.reflection_runs: dict[str, ReflectionRun] = {}
        self.scripts: dict[str, ScriptVersion] = {}
        self.video_versions: dict[str, VideoVersion] = {}
        self.publish_records: dict[str, PublishRecord] = {}
        self.performance_observations: dict[str, PerformanceObservation] = {}
        self.finished_videos: dict[str, FinishedVideo] = {}
        self.publish_packages: dict[str, PublishPackage] = {}
        self.publish_batches: dict[str, PublishBatchVm] = {}
        self.publish_attempts: dict[str, PublishAttempt] = {}
        self.cost_rollups: dict[str, CostRollup] = {}
        self.yield_events: dict[str, object] = {}
        self.budgets: dict[str, Budget] = {}
        self.alerts: dict[str, OpsAlertEvent] = {}
        self.quality_checks: dict[str, object] = {}
        self.approvals: dict[str, object] = {}
        self.audit_events: dict[str, object] = {}
        self.import_reports: dict[str, ImportBatchReport] = {}
        self.outbox: dict[str, OutboxEvent] = {}
        self.outbox_writer = None
        self.creative_patterns: dict[str, CreativePattern] = {}
        self.idempotency_records: dict[str, dict] = {}
        self.seed()

    def seed(self) -> None:
        admin = AuthUser(
            id="usr_admin",
            email="admin@local.cutagent",
            display_name="Local Admin",
            role=UserRole.admin,
        )
        viewer = AuthUser(
            id="usr_viewer",
            email="viewer@local.cutagent",
            display_name="Local Viewer",
            role=UserRole.viewer,
        )
        self.users[admin.id] = admin
        self.users[viewer.id] = viewer
        local_admin_registration_code = RegistrationCodePreview(
            id="reg_seed_local_admin",
            role=UserRole.admin,
            status="active",
            max_uses=None,
            used_count=0,
            created_at=utcnow(),
        )
        self.registration_codes[local_admin_registration_code.id] = local_admin_registration_code
        self.registration_code_hashes[hash_registration_code("reg_local_admin")] = local_admin_registration_code.id
        case = CaseDetail(
            id="case_demo",
            name="Demo Case",
            owner_user_id=admin.id,
            description="A seeded case for local golden flows.",
            industry="AI content",
            product="Cutagent CleanSlate",
            target_audience="operators",
        )
        self.cases[case.id] = case
        for asset_id, title, kind in [
            ("asset_portrait_demo", "Demo portrait main track", "portrait"),
            ("asset_broll_demo", "Demo b-roll clip", "broll"),
            ("asset_bgm_demo", "Demo background music", "bgm"),
            ("asset_font_demo", "Demo subtitle font", "font"),
        ]:
            self.media_assets[asset_id] = MediaAssetRecord(
                id=asset_id,
                case_id=case.id,
                title=title,
                kind=kind,
                tags=["seed", "usable"],
                annotation_status="annotated",
                usable=True,
            )
        self.voices["voice_sandbox"] = VoiceProfile(
            id="voice_sandbox",
            display_name="Sandbox Mandarin Voice",
            source="builtin",
            provider_profile_id="sandbox.tts.default",
        )
        self.provider_profiles["sandbox.tts.default"] = ProviderProfile(
            id="sandbox.tts.default",
            provider_id="sandbox",
            model_id="tts.local",
            capability="tts.speech",
            display_name="Sandbox TTS",
            environment="local",
            concurrency_key="sandbox:tts.speech",
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.tts.options"),
        )
        self.provider_profiles["runninghub.heygem.default"] = ProviderProfile(
            id="runninghub.heygem.default",
            provider_id="sandbox",
            model_id="heygem.local",
            capability="lipsync.video",
            display_name="Sandbox HeyGem LipSync",
            environment="local",
            concurrency_key="sandbox:lipsync.video",
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.lipsync.options"),
        )
        self.provider_profiles["sandbox.llm.default"] = ProviderProfile(
            id="sandbox.llm.default",
            provider_id="sandbox",
            model_id="llm.local",
            capability="llm.chat",
            display_name="Sandbox LLM",
            environment="local",
            concurrency_key="sandbox:llm.chat",
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.llm.options"),
        )
        seed_real_provider_configuration(self)
        for profile in self.provider_profiles.values():
            cap = ProviderCapability(
                id=f"cap_{profile.id.replace('.', '_')}",
                capability=profile.capability,
                provider_id=profile.provider_id,
                model_id=profile.model_id,
                display_name=profile.display_name,
                input_schema_id=f"{profile.capability}.input",
                output_schema_id=f"{profile.capability}.output",
                options_schema_id=profile.options_schema_ref.schema_id,
                supports_async_job=profile.capability in {"lipsync.video"},
                supports_cancel=profile.capability in {"lipsync.video"},
                default_timeout_sec=profile.timeout_sec,
            )
            self.provider_capabilities[cap.id] = cap
        template = PromptTemplate(
            id="prompt_creative_intent",
            name="Creative Intent Resolver",
            purpose="production.resolve_creative_intent",
            variables_schema_ref=PromptSchemaRef(schema_id="creative_intent.variables"),
            output_schema_ref=PromptSchemaRef(schema_id="creative_intent.output"),
            status="active",
        )
        version = PromptVersion(
            id="prompt_creative_intent_v1",
            prompt_template_id=template.id,
            content="Summarize script intent as strict JSON.",
            status="published",
            approved_at=utcnow(),
            published_at=utcnow(),
        )
        binding = PromptBinding(
            id="prompt_binding_global_intent",
            prompt_template_id=template.id,
            prompt_version_id=version.id,
            node_id="ResolveCreativeIntent",
            priority=1,
        )
        self.prompt_templates[template.id] = template
        self.prompt_versions[version.id] = version
        self.prompt_bindings[binding.id] = binding
        catalog = ProviderPriceCatalog(
            id="price_sandbox",
            provider_id="sandbox",
            status="published",
            currency="CNY",
        )
        self.price_catalogs[catalog.id] = catalog
        self.price_items["price_sandbox_call"] = ProviderPriceItem(
            id="price_sandbox_call",
            catalog_id=catalog.id,
            provider_id="sandbox",
            model_id="*",
            capability_id="*",
            unit="call",
            unit_price=Money(currency="CNY", amount=0, amount_micro=0),
        )
        self.alerts["alert_unpriced"] = OpsAlertEvent(
            id="alert_unpriced",
            code="provider.cost_unpriced",
            message="No unpriced provider invocations have been observed.",
            severity="info",
        )

    def put(self, table: dict[str, TModel], model: TModel) -> TModel:
        table[getattr(model, "id")] = model
        return model

    def patch(self, table: dict[str, TModel], item_id: str, updates: dict) -> TModel:
        item = table[item_id]
        updates["updated_at"] = utcnow()
        patched = item.model_copy(update=updates)
        table[item_id] = patched
        return patched

    def page(self, values: Iterable[TModel], limit: int = 50) -> list[TModel]:
        return list(values)[:limit]

    def artifact_ref(self, artifact_id: str) -> ArtifactRef:
        artifact = self.artifacts[artifact_id]
        return ArtifactRef(
            artifact_id=artifact.id,
            kind=artifact.kind,
            uri=artifact.uri or f"artifact://{artifact.id}",
            schema_version=artifact.schema_version,
            sha256=artifact.sha256,
        )

    def create_artifact(
        self,
        *,
        kind: ArtifactKind,
        payload_schema: str,
        payload,
        case_id: str | None = None,
        run_id: str | None = None,
        node_run_id: str | None = None,
        uri: str | None = None,
        sha256: str | None = None,
        media_info: MediaInfo | None = None,
    ) -> Artifact:
        artifact = Artifact(
            id=new_id("art"),
            case_id=case_id,
            run_id=run_id,
            node_run_id=node_run_id,
            kind=kind,
            uri=uri,
            sha256=sha256,
            media_info=media_info,
            payload_schema=payload_schema,
            payload=payload,
            created_by_node_run_id=node_run_id,
        )
        self.artifacts[artifact.id] = artifact
        if run_id:
            self.create_event(
                "workflow.artifact.created",
                "run",
                run_id,
                {
                    "artifact_id": artifact.id,
                    "artifact_kind": artifact.kind.value if hasattr(artifact.kind, "value") else str(artifact.kind),
                    "node_run_id": node_run_id,
                },
                dedupe_key=f"artifact:{artifact.id}",
                event_type="artifact_created",
                node_id=node_run_id,
                message=f"Artifact {artifact.id} created.",
            )
        return artifact

    def create_publish_package_from_finished_video(
        self, finished_video: FinishedVideo, title: str | None = None, description: str = ""
    ) -> PublishPackage:
        package = PublishPackage(
            id=new_id("pkg"),
            case_id=finished_video.case_id,
            source_finished_video_id=finished_video.id,
            video_artifact=finished_video.video_artifact,
            cover_artifact=finished_video.cover_artifact,
            platform_defaults=PublishDefaults(title=title or finished_video.title, description=description),
        )
        self.publish_packages[package.id] = package
        return package

    def create_publish_batch(self, package_ids: list[str], platforms: list[str]) -> PublishBatchVm:
        items: list[PublishBatchItemVm] = []
        for package_id in package_ids:
            package = self.publish_packages[package_id]
            for platform in platforms:
                item = PublishBatchItemVm(
                    id=new_id("pub_item"),
                    publish_package_id=package_id,
                    platform=platform,
                    title=package.platform_defaults.title,
                    description=package.platform_defaults.description,
                )
                items.append(item)
        batch = PublishBatchVm(id=new_id("pub_batch"), items=items)
        self.publish_batches[batch.id] = batch
        return batch

    def create_event(
        self,
        topic: str,
        aggregate_type: str,
        aggregate_id: str,
        payload,
        *,
        dedupe_key: str | None = None,
        payload_schema: str | None = None,
        event_type: str | None = None,
        run_id: str | None = None,
        job_id: str | None = None,
        node_id: str | None = None,
        status: str | None = None,
        progress: float | None = None,
        message: str | None = None,
    ) -> OutboxEvent:
        from packages.core.observability.outbox import OutboxWriter

        writer = OutboxWriter.in_memory(self)
        payload_data = payload if isinstance(payload, dict) else {}
        effective_run_id = run_id or (aggregate_id if aggregate_type in {"run", "workflow_run"} else None)
        run = self.runs.get(effective_run_id or "")
        effective_job_id = job_id or (run.job_id if run is not None else str(payload_data.get("job_id", "")))
        effective_status = status or str(payload_data.get("status") or (run.status.value if run is not None else ""))
        effective_type = event_type or ("node_update" if ".node." in topic else "run_update")
        created_at = utcnow()
        if effective_type in {"run_update", "node_update", "artifact_created", "warning", "error"} and effective_run_id:
            event_payload = RunEvent(
                event_id=new_id("evt"),
                run_id=effective_run_id,
                job_id=effective_job_id,
                event_type=effective_type,
                node_id=node_id or payload_data.get("node_id"),
                status=effective_status or None,
                progress=progress,
                message=message or str(payload_data.get("message") or topic),
                created_at=created_at,
            ).model_dump(mode="json")
        else:
            event_payload = payload
        event = writer.write(
            topic=topic,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload_schema=payload_schema or ("RunEvent.v1" if effective_run_id else f"{topic}.v1"),
            payload=event_payload,
            dedupe_key=dedupe_key
            or f"{aggregate_id}:{topic}:{payload_data.get('dedupe_key') or effective_status or len(self.outbox)}",
            created_at=created_at,
            event_id=event_payload.get("event_id") if isinstance(event_payload, dict) else None,
        )
        return event

    def record_yield_funnel_event(
        self,
        *,
        job_id: str | None,
        run_id: str | None,
        event_type: str,
        dedupe_key: str,
        finished_video_id: str | None = None,
        publish_package_id: str | None = None,
        publish_attempt_id: str | None = None,
        event_time=None,
    ) -> YieldFunnelEvent:
        event = YieldFunnelEvent(
            id=new_id("yield"),
            job_id=job_id,
            run_id=run_id,
            finished_video_id=finished_video_id,
            publish_package_id=publish_package_id,
            publish_attempt_id=publish_attempt_id,
            event_type=event_type,
            event_time=event_time or utcnow(),
            dedupe_key=dedupe_key,
        )
        for existing in self.yield_events.values():
            if getattr(existing, "dedupe_key", None) == dedupe_key:
                return existing
        self.yield_events[event.id] = event
        from packages.core.observability import record_yield_funnel_event

        record_yield_funnel_event()
        self.create_event(
            "ops.yield_funnel.event",
            "run" if run_id else "job",
            run_id or job_id or event.id,
            event.model_dump(mode="json"),
            dedupe_key=dedupe_key,
            payload_schema="YieldFunnelEvent.v1",
            event_type="run_update" if run_id else None,
            run_id=run_id,
            job_id=job_id,
            message=f"Yield funnel event {event_type}.",
        )
        return event


_REPOSITORY = Repository()


def get_repository() -> Repository:
    return _REPOSITORY
