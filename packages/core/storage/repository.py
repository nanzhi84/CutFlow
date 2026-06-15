from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar
from uuid import uuid4

from pydantic import BaseModel

from packages.core.registration_codes import hash_registration_code
from packages.core.storage.prompt_groups import seed_prompt_groups
from packages.core.storage.provider_seed import seed_real_provider_configuration
from packages.core.storage.selection_ledger import material_usage_ranking_from_entries
from packages.core.contracts import (
    AnnotationEditorVm,
    AnnotationV4,
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
    MaterialUsageRankingReport,
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
    ProviderBalanceSnapshot,
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
    RunEvent,
    ScriptDraft,
    ScriptVersion,
    SecretPreview,
    SelectionLedgerEntry,
    SelectionMedium,
    SelectionReservationRecord,
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
        self.selection_ledger: dict[str, SelectionLedgerEntry] = {}
        self.selection_reservations: dict[str, SelectionReservationRecord] = {}
        self.annotations: dict[str, AnnotationEditorVm] = {}
        self.voices: dict[str, VoiceProfile] = {}
        self.prompt_templates: dict[str, PromptTemplate] = {}
        self.prompt_versions: dict[str, PromptVersion] = {}
        self.prompt_bindings: dict[str, PromptBinding] = {}
        self.prompt_invocations: dict[str, PromptInvocation] = {}
        self.prompt_experiments: dict[str, PromptExperiment] = {}
        self.provider_profiles: dict[str, ProviderProfile] = {}
        self.provider_capabilities: dict[str, ProviderCapability] = {}
        self.provider_balance_snapshots: dict[str, ProviderBalanceSnapshot] = {}
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
            content=(
                "你是资深短视频创意策划。基于下面的口播脚本，提炼创意结构。\n\n"
                "严格要求：直接输出一个 JSON 对象（以左花括号开头、右花括号结尾）；"
                "禁止使用 markdown 代码块；禁止任何前后缀说明文字。\n\n"
                "JSON 必须且只能包含以下字段：\n"
                "- hook：字符串，一句话开场钩子。\n"
                "- tone：字符串，整体语气风格。\n"
                "- audience：字符串，目标受众。\n"
                "- beats：字符串数组，3 到 6 条，按顺序列出脚本的关键叙事节拍。\n\n"
                "脚本：\n"
                "{script}"
            ),
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
        seed_prompt_groups(self)
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

    def patch(self, table: dict[str, TModel], item_id: str, updates: dict) -> TModel:
        item = table[item_id]
        updates["updated_at"] = utcnow()
        patched = item.model_copy(update=updates)
        table[item_id] = patched
        return patched

    def record_selection_ledger_entries(
        self, entries: Iterable[SelectionLedgerEntry]
    ) -> list[SelectionLedgerEntry]:
        existing = {
            (entry.case_id, entry.run_id, entry.medium, entry.asset_id, entry.slot_phase)
            for entry in self.selection_ledger.values()
        }
        recorded: list[SelectionLedgerEntry] = []
        for entry in entries:
            key = (entry.case_id, entry.run_id, entry.medium, entry.asset_id, entry.slot_phase)
            if key in existing:
                continue
            self.selection_ledger[entry.id] = entry
            existing.add(key)
            recorded.append(entry)
        return recorded

    # --- selection reservations (§6.6 reserve -> commit -> release/expire) -----
    def active_selection_reservations(
        self,
        *,
        case_id: str | None,
        medium: SelectionMedium,
        exclude_run_id: str | None = None,
    ) -> list[SelectionReservationRecord]:
        """Reservations that still hold a slot for ``case_id``/``medium``.

        Expires lazily as it scans (TTL): a ``reserved`` lease past its
        ``expires_at`` is reclaimed and excluded. ``exclude_run_id`` drops this
        run's own reservations so a run never collides with itself.
        """
        now = utcnow()
        active: list[SelectionReservationRecord] = []
        for reservation in list(self.selection_reservations.values()):
            if reservation.medium != medium:
                continue
            if case_id is not None and reservation.case_id != case_id:
                continue
            if reservation.status == "reserved" and reservation.expires_at <= now:
                self.selection_reservations[reservation.id] = reservation.model_copy(
                    update={"status": "expired", "released_at": now}
                )
                continue
            if not reservation.is_active(now=now):
                continue
            if exclude_run_id is not None and reservation.run_id == exclude_run_id:
                continue
            active.append(reservation)
        return active

    def reserve_selections(
        self,
        *,
        case_id: str,
        run_id: str,
        medium: SelectionMedium,
        asset_ids: Iterable[str],
        diversity_keys: dict[str, str | None] | None = None,
    ) -> list[SelectionReservationRecord]:
        """Reserve a TTL lease over each (run, medium, asset) slot.

        Idempotent per (run, medium, asset): re-reserving an existing live lease
        returns it unchanged (so a retried planning node does not duplicate). Assets
        another live run already holds are SKIPPED here — the caller decides whether a
        skip is fatal (portrait coverage) or a soft demotion already applied upstream.
        Returns the reservations this run owns for the given assets.
        """
        keys = diversity_keys or {}
        blocked = {
            reservation.asset_id
            for reservation in self.active_selection_reservations(
                case_id=case_id, medium=medium, exclude_run_id=run_id
            )
        }
        owned: list[SelectionReservationRecord] = []
        for asset_id in asset_ids:
            if not isinstance(asset_id, str) or not asset_id:
                continue
            existing = next(
                (
                    reservation
                    for reservation in self.selection_reservations.values()
                    if reservation.run_id == run_id
                    and reservation.medium == medium
                    and reservation.asset_id == asset_id
                    and reservation.status in {"reserved", "committed"}
                ),
                None,
            )
            if existing is not None:
                owned.append(existing)
                continue
            if asset_id in blocked:
                continue
            reservation = SelectionReservationRecord(
                case_id=case_id,
                run_id=run_id,
                medium=medium,
                asset_id=asset_id,
                diversity_key=keys.get(asset_id),
            )
            self.selection_reservations[reservation.id] = reservation
            owned.append(reservation)
        return owned

    def commit_selection_reservation(
        self,
        *,
        run_id: str,
        medium: SelectionMedium,
        asset_id: str,
    ) -> SelectionReservationRecord | None:
        """Promote this run's live reservation on a slot to ``committed`` (a hard hold).

        Called from the per-medium production node when the asset actually ships.
        Returns the committed record, or ``None`` when this run held no live
        reservation for the slot (e.g. it was reclaimed by TTL before commit).
        """
        now = utcnow()
        for reservation in self.selection_reservations.values():
            if (
                reservation.run_id == run_id
                and reservation.medium == medium
                and reservation.asset_id == asset_id
                and reservation.status == "reserved"
            ):
                committed = reservation.model_copy(
                    update={"status": "committed", "committed_at": now}
                )
                self.selection_reservations[reservation.id] = committed
                return committed
        return None

    def release_run_reservations(
        self,
        *,
        run_id: str,
        only_uncommitted: bool = True,
    ) -> list[SelectionReservationRecord]:
        """Release a run's reservations (cancel/failure path, §6.6).

        By default only ``reserved`` (uncommitted) leases are freed — a committed
        pick stays a hard hold for diversity memory even if the run later fails
        (§6.6: "失败任务默认保留用于多样性记忆"). ``only_uncommitted=False`` also
        releases committed holds (ops cleanup).
        """
        now = utcnow()
        released: list[SelectionReservationRecord] = []
        for reservation in list(self.selection_reservations.values()):
            if reservation.run_id != run_id:
                continue
            if reservation.status not in {"reserved", "committed"}:
                continue
            if only_uncommitted and reservation.status == "committed":
                continue
            updated = reservation.model_copy(update={"status": "released", "released_at": now})
            self.selection_reservations[reservation.id] = updated
            released.append(updated)
        return released

    def expire_stale_selection_reservations(self) -> list[SelectionReservationRecord]:
        """Sweep ``reserved`` leases past their TTL into ``expired`` (ops/scheduler)."""
        now = utcnow()
        expired: list[SelectionReservationRecord] = []
        for reservation in list(self.selection_reservations.values()):
            if reservation.status == "reserved" and reservation.expires_at <= now:
                updated = reservation.model_copy(
                    update={"status": "expired", "released_at": now}
                )
                self.selection_reservations[reservation.id] = updated
                expired.append(updated)
        return expired

    def material_usage_ranking(
        self,
        *,
        kind: SelectionMedium,
        case_id: str | None = None,
        top_n: int = 20,
    ) -> MaterialUsageRankingReport:
        entries = [
            entry
            for entry in self.selection_ledger.values()
            if entry.medium == kind and (case_id is None or entry.case_id == case_id)
        ]
        return material_usage_ranking_from_entries(
            entries=entries,
            assets=self.media_assets,
            kind=kind,
            case_id=case_id,
            top_n=top_n,
        )

    def recent_selections(
        self,
        *,
        case_id: str | None,
        medium: SelectionMedium,
        limit: int = 12,
    ) -> list[SelectionLedgerEntry]:
        """Return this case's recent selections for ``medium``, most-recent first.

        Drives usage-aware recency demotion: a clip selected in an earlier run is
        demoted below a fresh one on the next run.
        """
        entries = [
            entry
            for entry in self.selection_ledger.values()
            if entry.medium == medium and (case_id is None or entry.case_id == case_id)
        ]
        entries.sort(key=lambda entry: entry.created_at, reverse=True)
        return entries[: max(0, limit)]

    def annotation_v4_for_asset(self, asset_id: str | None) -> AnnotationV4 | None:
        """Parse the stored canonical annotation for ``asset_id`` into AnnotationV4.

        Returns ``None`` when there is no annotation, the canonical is not a V4
        payload, or parsing fails — so material planning sees "no real
        annotation" and soft-degrades rather than fabricating a pick.
        """
        if not asset_id:
            return None
        vm = self.annotations.get(asset_id)
        if vm is None:
            return None
        # AnnotationEditorVm.canonical is now a AnnotationV4 | dict union: a stored
        # V4 payload is coerced to the model on load, while the minimal editor dict
        # ({labels, kind}) stays a dict. Accept the already-parsed model directly and
        # only re-validate when it is still a raw dict.
        if isinstance(vm.canonical, AnnotationV4):
            return vm.canonical
        if not isinstance(vm.canonical, dict):
            return None
        try:
            return AnnotationV4.model_validate(vm.canonical)
        except Exception:
            return None

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
