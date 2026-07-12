"""The durable payload of a completed provider call.

A Run-scoped provider call can be re-entered after the worker dies between the
vendor's response and the node's completion snapshot. The envelope is what makes
that re-entry free: it is written onto ``provider_invocations.result_payload`` in
the same transaction that marks the row succeeded, and a later attempt rebuilds
the node's view of the call from it instead of paying the vendor again.

Media bytes are deliberately absent. By the time the provider returns, every byte
it produced is already in the object store (``ProviderInvocationContext``
uploads + HEAD-verifies before it hands an Artifact back), so only the artifact
METADATA has to travel — uri, sha256, size, media_info.

Gateway-internal: this is not a wire type and never enters
``packages/core/contracts`` / OpenAPI.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, JsonValue

from packages.core.contracts import Artifact, Money, UsageMeterRecord, zero_money


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


class ProviderResultEnvelope(BaseModel):
    """Everything a re-run needs to reconstruct one succeeded provider call."""

    result: ProviderResult
    # Carries its own id: a replay re-uses it verbatim so the usage row stays a
    # primary-key upsert and the call is never billed twice.
    usage: UsageMeterRecord
    artifacts: list[Artifact] = Field(default_factory=list)
    price_item_id: str | None = None
    billing_status: str = "estimated"
    duration_ms: int = 0
    estimated_cost: Money = Field(default_factory=zero_money)

    def media_uris(self) -> list[str]:
        return [artifact.uri for artifact in self.artifacts if artifact.uri]
