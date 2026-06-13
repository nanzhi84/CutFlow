"""LoadCaseContext node: assemble case profile, memories, scripts, performance."""

from __future__ import annotations

from packages.core.contracts import ArtifactKind
from packages.core.contracts.artifacts import CaseContextArtifact
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext


def run(ctx: NodeContext) -> NodeOutput:
    repository = ctx.repository
    case = repository.cases[ctx.state.request.case_id]
    payload = CaseContextArtifact(
        case_id=case.id,
        case_profile=case.model_dump(mode="json"),
        active_memories=[
            memory.model_dump(mode="json")
            for memory in repository.memories.values()
            if memory.case_id == case.id and memory.status == "active"
        ],
        recent_script_versions=[
            script
            for script in repository.scripts.values()
            if script.case_id == case.id
        ][-10:],
        performance_summary={
            "observations": [
                obs.model_dump(mode="json")
                for obs in repository.performance_observations.values()
                if obs.case_id == case.id
            ][-50:]
        },
    ).model_dump(mode="json")
    return NodeOutput(
        artifacts=[ctx.artifact(ArtifactKind.case_context, payload, "CaseContextArtifact.v1")]
    )
