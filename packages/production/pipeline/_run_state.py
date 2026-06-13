"""In-memory state carried across nodes during a single workflow run."""

from __future__ import annotations

from dataclasses import dataclass, field

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DegradationNotice,
    DigitalHumanVideoRequest,
    ErrorCode,
    WarningCode,
)
from packages.core.workflow import NodeExecutionError


@dataclass
class RunState:
    request: DigitalHumanVideoRequest
    artifacts: dict[ArtifactKind, Artifact] = field(default_factory=dict)
    provider_invocation_ids: list[str] = field(default_factory=list)
    warnings: list[WarningCode] = field(default_factory=list)
    degradations: list[DegradationNotice] = field(default_factory=list)

    def require(self, kind: ArtifactKind) -> Artifact:
        if kind not in self.artifacts:
            raise NodeExecutionError(ErrorCode.artifact_missing, f"Missing artifact {kind.value}.")
        return self.artifacts[kind]


def degradation_notice(
    code: WarningCode,
    message: str,
    *,
    node_id: str | None = None,
    affects_true_yield: bool = False,
) -> DegradationNotice:
    return DegradationNotice(
        code=code,
        message=message,
        node_id=node_id,
        affects_true_yield=affects_true_yield,
    )
