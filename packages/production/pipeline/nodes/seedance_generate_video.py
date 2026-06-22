"""SeedanceGenerateVideo node: one-shot text/image-to-video via Volcengine Ark.

Builds the Seedance prompt from the request script (falling back to the case
profile), resolves any reference-image assets to their source artifact URIs, and
invokes the ``video.generate`` capability. The provider downloads + stores the
result, so the real path returns a ``video.rendered`` artifact id; the sandbox
path returns only a fake uri, which this node bridges into a uri-only artifact so
the downstream export node has something to reference.
"""

from __future__ import annotations

from packages.ai.gateway import ProviderCall
from packages.core.config.settings import sandbox_fallback_allowed
from packages.core.contracts import ArtifactKind, ErrorCode
from packages.core.workflow import NodeExecutionError, NodeOutput
from packages.production.pipeline._node_context import NodeContext

_SEEDANCE_DURATION_SEC = 15
_SEEDANCE_RATIO = "9:16"
_SEEDANCE_RESOLUTION = "720p"


def run(ctx: NodeContext) -> NodeOutput:
    state = ctx.state
    run = ctx.run
    node_run = ctx.node_run
    request = state.request

    prompt = (request.script or "").strip() or _compose_prompt_from_case(ctx)
    if not prompt:
        raise NodeExecutionError(
            ErrorCode.validation_missing_script,
            "Seedance 生成缺少提示词（脚本为空且无法从案例信息拼出）。",
        )

    references = _resolve_references(ctx)

    profile = ctx.first_available_provider_profile(
        "video.generate", include_sandbox=sandbox_fallback_allowed()
    )
    if profile is None:
        raise NodeExecutionError(
            ErrorCode.provider_unsupported_option,
            "未配置可用的真实文生视频（Seedance）供应商。请在「设置」中配置并启用 "
            "capability=video.generate 的供应商及密钥。",
        )

    invocation, result = ctx.provider_gateway.invoke(
        ProviderCall(
            case_id=run.case_id,
            run_id=run.id,
            node_run_id=node_run.id,
            provider_profile_id=profile.id,
            capability_id="video.generate",
            input={
                "prompt": prompt,
                "duration_sec": _SEEDANCE_DURATION_SEC,
                "ratio": _SEEDANCE_RATIO,
                "resolution": _SEEDANCE_RESOLUTION,
                "references": references,
            },
            idempotency_key=f"{run.id}:{node_run.id}:seedance",
        )
    )
    if result is None or invocation.error:
        # Video generation is not idempotent (a retry re-bills + re-generates), so
        # surface a hard failure for a human to act on rather than auto-retrying.
        raise NodeExecutionError(
            invocation.error.code if invocation.error else ErrorCode.provider_remote_failed,
            invocation.error.message if invocation.error else "Seedance 视频生成失败。",
            retryable=False,
        )

    artifact = _resolve_video_artifact(ctx, result)
    return NodeOutput(artifacts=[artifact], provider_invocation_ids=[invocation.id])


def _resolve_references(ctx: NodeContext) -> list[dict[str, str]]:
    """Map request.reference_asset_ids -> [{uri, role}] (presigned later by provider).

    ``source_artifact_for_asset`` raises ``artifact_missing`` when the asset or its
    source artifact uri is absent, so a missing reference fails the node loudly."""
    references: list[dict[str, str]] = []
    for asset_id in getattr(ctx.state.request, "reference_asset_ids", None) or []:
        artifact = ctx.source_artifact_for_asset(asset_id)
        references.append({"uri": artifact.uri, "role": "reference_image"})
    return references


def _resolve_video_artifact(ctx: NodeContext, result):
    """Real path: provider already stored the video and returned an artifact id.
    Sandbox path: only a fake uri exists -> wrap it in a uri-only artifact."""
    artifact_id = result.output.get("video_artifact_id")
    if isinstance(artifact_id, str) and artifact_id in ctx.repository.artifacts:
        return ctx.repository.artifacts[artifact_id]
    video_uri = result.output.get("video_uri")
    if not isinstance(video_uri, str) or not video_uri:
        raise NodeExecutionError(
            ErrorCode.provider_remote_failed,
            "Seedance 供应商未返回可用的视频产物。",
        )
    return ctx.artifact(ArtifactKind.video_rendered, None, "uri-only", uri=video_uri)


def _compose_prompt_from_case(ctx: NodeContext) -> str:
    case = ctx.repository.cases.get(ctx.state.request.case_id)
    if case is None:
        return ""
    selling = getattr(case, "key_selling_points", None) or []
    bits = [
        getattr(case, "product", None),
        "、".join(selling),
        getattr(case, "ip_persona", None),
        getattr(case, "brand_voice", None),
    ]
    return "，".join(b for b in bits if b)
