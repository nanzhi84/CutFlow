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
    WorkflowRun,
    WorkflowTemplate,
)
from packages.core.contracts.artifacts import NarrationUnit
from packages.production.pipeline._run_state import RunState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from packages.ai.gateway import ProviderGateway
    from packages.ai.prompts import PromptRegistry
    from packages.core.storage import Repository
    from packages.production.pipeline.digital_human import LocalRuntimeAdapter


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
    def provider_gateway(self) -> "ProviderGateway":
        return self.adapter.provider_gateway

    @property
    def prompt_registry(self) -> "PromptRegistry":
        return self.adapter.prompt_registry

    @property
    def template(self) -> WorkflowTemplate:
        return self.adapter.template

    @property
    def request(self) -> DigitalHumanVideoRequest:
        return self.state.request

    def object_store(self):
        """Resolve the object store through the adapter so the
        ``digital_human.get_object_store`` symbol stays monkeypatchable."""
        return self.adapter._object_store()

    # --- artifact + media helpers (shared across nodes) ----------------------
    def artifact(
        self,
        kind: ArtifactKind,
        payload,
        payload_schema: str,
        uri: str | None = None,
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
            sha256=sha256,
            media_info=media_info,
        )

    def source_artifact_for_asset(self, asset_id: str | None) -> Artifact:
        return self.adapter._source_artifact_for_asset(asset_id)

    def artifact_path(self, artifact: Artifact) -> Path:
        return self.adapter._artifact_path(artifact)

    def first_available_provider_profile(self, capability: str, *, include_sandbox: bool = True):
        return self.adapter._first_available_provider_profile(capability, include_sandbox=include_sandbox)

    def tts_provider_profile_id(self, request: DigitalHumanVideoRequest) -> str:
        return self.adapter._tts_provider_profile_id(request)

    def narration_units_from_segments(self, segments, fallback_duration: float) -> list[NarrationUnit]:
        return self.adapter._narration_units_from_segments(segments, fallback_duration)

    def write_report(self, *, failed: bool) -> tuple[Artifact, Artifact]:
        return self.adapter._write_report(self.run, self.state, failed=failed, node_run=self.node_run)
