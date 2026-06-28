from __future__ import annotations

from packages.core.contracts import ArtifactKind
from packages.core.contracts.artifacts import RenderPlanArtifact, TimelinePlanArtifact
from packages.core.workflow import NodeOutput
from packages.production.pipeline._node_context import NodeContext


def timeline_output(
    ctx: NodeContext,
    *,
    fps: int,
    total_frames: int,
    tracks: list,
    validation,
) -> NodeOutput:
    timeline = TimelinePlanArtifact(
        fps=fps,
        total_frames=total_frames,
        tracks=tracks,
        validation=validation,
    )
    render_plan = RenderPlanArtifact(
        timeline_artifact_id="pending",
        render_size=(ctx.state.request.output.width, ctx.state.request.output.height),
        fps=fps,
        tracks=tracks,
    )
    timeline_artifact = ctx.artifact(
        ArtifactKind.plan_timeline,
        timeline.model_dump(mode="json"),
        "TimelinePlanArtifact.v1",
    )
    render_plan = render_plan.model_copy(update={"timeline_artifact_id": timeline_artifact.id})
    return NodeOutput(
        artifacts=[
            timeline_artifact,
            ctx.artifact(
                ArtifactKind.plan_render,
                render_plan.model_dump(mode="json"),
                "RenderPlanArtifact.v1",
            ),
        ]
    )
