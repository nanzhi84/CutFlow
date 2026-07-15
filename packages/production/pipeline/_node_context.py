"""Shared execution context handed to every pipeline node handler.

Each node handler is a free ``def run(ctx: NodeContext) -> NodeOutput`` that
reads its inputs from ``ctx.state``, persists outputs via ``ctx.artifact(...)``
and reaches shared cross-node services (repository, provider gateway, prompt
registry, object store, media helpers) through this object.

Keeping these dependencies on a single context — rather than passing the whole
adapter around — gives every node module the same narrow, explicit seam and
lets the orchestrator (``digital_human.LocalRuntimeAdapter``) stay a thin
engine. ``object_store`` is resolved through the adapter so monkeypatching
``digital_human.get_object_store`` continues to work.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    MediaInfo,
    NodeRun,
    SelectionReservationRecord,
    WorkflowRun,
    WorkflowTemplate,
)
from packages.core.contracts.artifacts import NarrationUnit
from packages.core.provider_idempotency import (
    ProviderCallIdempotency,
    build_provider_call_idempotency,
)
from packages.production.pipeline._run_state import RunState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from packages.ai.gateway import ProviderGateway
    from packages.ai.prompts import PromptRegistry
    from packages.core.storage import Repository
    from packages.production.pipeline.digital_human import LocalRuntimeAdapter
    from packages.production.sqlalchemy_repository import SqlAlchemyProductionRepository


@dataclass
class NodeContext:
    """Everything a single node handler needs to run."""

    adapter: "LocalRuntimeAdapter"
    run: WorkflowRun
    node_run: NodeRun
    state: RunState

    # --- shared services (proxied from the adapter) --------------------------
    @property
    def repository(self) -> "Repository":
        return self.adapter.repository

    @property
    def production_repository(self) -> "SqlAlchemyProductionRepository | None":
        return getattr(self.adapter, "production_repository", None)

    @property
    def provider_gateway(self) -> "ProviderGateway":
        return self.adapter.provider_gateway

    @property
    def prompt_registry(self) -> "PromptRegistry":
        return self.adapter.prompt_registry

    @property
    def template(self) -> WorkflowTemplate:
        return self.adapter._template_for_run(self.run)

    @property
    def request(self) -> DigitalHumanVideoRequest:
        return self.state.request

    def object_store(self):
        """Resolve the object store through the adapter so the
        ``digital_human.get_object_store`` symbol stays monkeypatchable."""
        return self.adapter._object_store()

    def reserve_selection_candidates(
        self,
        *,
        case_id: str,
        asset_ids_by_medium: dict[str, list[str]],
        diversity_keys_by_medium: dict[str, dict[str, str | None]],
    ) -> dict[str, list[SelectionReservationRecord]]:
        """Reserve one MaterialPack candidate batch at the durable race boundary.

        SQL-backed runs acquire every slot in one transaction so the active-slot
        uniqueness race is raised inside the node's declared retry loop. Pure
        in-memory runs keep the same Repository path used by unit tests.
        """
        if self.production_repository is not None:
            return self.production_repository.reserve_selection_candidates(
                self.repository,
                case_id=case_id,
                run_id=self.run.id,
                asset_ids_by_medium=asset_ids_by_medium,
                diversity_keys_by_medium=diversity_keys_by_medium,
            )
        return {
            medium: self.repository.reserve_selections(
                case_id=case_id,
                run_id=self.run.id,
                medium=medium,
                asset_ids=asset_ids,
                diversity_keys=diversity_keys_by_medium.get(medium),
            )
            for medium, asset_ids in asset_ids_by_medium.items()
        }

    def provider_call_idempotency(
        self, *, logical_call_slot: str, provider_profile_id: str
    ) -> ProviderCallIdempotency:
        """Stable idempotency identity for one logical provider call in this node.

        This is the single place the coordinates are assembled, so every paid node
        gets the same identity by construction. They are Job-stable (job id, canonical
        node id, slot, profile, ``node_run.input_manifest_hash``) and deliberately free
        of ``run.id`` / ``node_run.id`` / Temporal attempt: an infrastructure retry OR
        an operator resume — which creates a whole new run — reuses the one durable
        provider-call identity instead of re-submitting and paying twice.
        ``logical_call_slot`` separates multiple calls inside a node (a repair loop's
        n-th attempt must carry a distinct slot)."""
        return build_provider_call_idempotency(
            job_id=self.run.job_id,
            run_id=self.run.id,
            canonical_node_id=self.node_run.node_id,
            logical_call_slot=logical_call_slot,
            provider_profile_id=provider_profile_id,
            input_manifest_hash=self.node_run.input_manifest_hash,
        )

    # --- artifact + media helpers (shared across nodes) ----------------------
    def artifact(
        self,
        kind: ArtifactKind,
        payload,
        payload_schema: str,
        uri: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
        media_info: MediaInfo | None = None,
    ) -> Artifact:
        return self.adapter._artifact(
            self.run,
            self.node_run,
            kind,
            payload,
            payload_schema,
            uri=uri,
            size_bytes=size_bytes,
            sha256=sha256,
            media_info=media_info,
        )

    def source_artifact_for_asset(self, asset_id: str | None) -> Artifact:
        return self.adapter._source_artifact_for_asset(asset_id)

    def artifact_path(self, artifact: Artifact) -> Path:
        return self.adapter._artifact_path(artifact)

    def first_available_provider_profile(self, capability: str, *, include_sandbox: bool = True):
        return self.adapter.provider_profiles.first_available(capability, include_sandbox=include_sandbox)

    def tts_provider_profile_id(self, request: DigitalHumanVideoRequest) -> str:
        return self.adapter.provider_profiles.tts_profile_id(request)

    def image_cover_profile_id(self, request: DigitalHumanVideoRequest) -> str | None:
        return self.adapter.provider_profiles.image_cover_profile_id(request)

    def image_cover_profile_ids(self, request: DigitalHumanVideoRequest) -> list[str]:
        return self.adapter.provider_profiles.image_cover_profile_ids(request)

    def resolve_lipsync_profile(self, request: DigitalHumanVideoRequest):
        return self.adapter.provider_profiles.resolve_lipsync(request)

    def select_lipsync_fallback_profile(self, current_profile, error_message: str):
        return self.adapter.provider_profiles.select_lipsync_fallback(current_profile, error_message)

    def narration_units_from_segments(
        self,
        segments,
        fallback_duration: float,
        *,
        script: str | None = None,
    ) -> list[NarrationUnit]:
        return self.adapter._narration_units_from_segments(segments, fallback_duration, script=script)

    def write_report(self, *, failed: bool) -> tuple[Artifact, Artifact]:
        return self.adapter._write_report(self.run, self.state, failed=failed, node_run=self.node_run)
