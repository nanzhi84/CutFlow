from __future__ import annotations

from dataclasses import dataclass
import re

from fastapi import Request

from apps.api.common import get_case, provider_repository, repository
from packages.ai.gateway import ProviderCall
from packages.ai.prompts.registry import (
    case_prompt_variables,
    extract_script_from_output,
    extract_script_title_from_output,
)
from packages.core import contracts as c
from packages.core.config.settings import sandbox_fallback_allowed
from packages.core.workflow import NodeExecutionError

_FALLBACK_SCRIPT_NODE_ID = "CaseAgentScriptGenerate"

_FALLBACK_ERROR_CODES = frozenset(
    {c.ErrorCode.prompt_version_not_published, c.ErrorCode.prompt_render_error}
)

# Spec §2.3: prompt 输出不符合 schema 时重试，耗尽后 hard_fail: prompt.output_invalid.
# Total attempts = 1 initial + _SCRIPT_OUTPUT_MAX_RETRIES re-tries.
_SCRIPT_OUTPUT_MAX_RETRIES = 2
_RECENT_SCRIPT_CONTEXT_LIMIT = 5
_RECENT_SCRIPT_SNIPPET_CHARS = 220


_RESPONSE_CONTRACT = (
    '请只返回一个 JSON 对象：{"script": "<一条可直接拍摄的完整中文口播脚本纯文本>"}。'
    "script 必须是数字人主播从头到尾逐字念出来的口播台词，"
    "严禁出现任何括号（包括中文（）和英文()）以及括号内的镜头、画面、景别、动作、表情、"
    "手势、语气、停顿、节奏、配乐、音效、字幕、分镜或旁白等提示——"
    "不要写「（停顿）」「（直视镜头）」「（语气转沉稳）」「（靠近镜头）」这类内容，只输出要念出来的话本身。"
    "只输出这一个脚本，不要输出思考过程、解释说明或多余文字。"
)


@dataclass(frozen=True)
class GeneratedScript:
    script: str
    title: str | None = None


def generate_script_with_llm(
    case_id: str,
    brief: str,
    memory_ids: list[str],
    memories: list[str],
    request: Request,
    persona_mode: str = "hard_ad",
    operation: str = "generate",
    strategy_tags: list[str] | None = None,
    reference_script: str | None = None,
    duration: str | None = None,
    recent_script_texts: list[str] | None = None,
) -> GeneratedScript | None:
    profile = _select_real_llm_profile(request)
    if profile is None:
        if not sandbox_fallback_allowed():
            raise NodeExecutionError(
                c.ErrorCode.provider_unsupported_option,
                "未配置可用的真实 LLM 供应商（llm.chat），无法生成脚本。请在「设置」中配置并启用真实 LLM 供应商及密钥。",
        )
        return None
    tags = strategy_tags or []
    scene_label = "硬广投流" if persona_mode == "hard_ad" else "IP人设号"
    variables: dict[str, object] = {
        "brief": brief,
        "memories": " / ".join(memories) if memories else "暂无",
        "persona_mode": persona_mode,
        "operation": operation,
        "variation_count": "1",
        "scene_type": persona_mode,
        "scene_label": scene_label,
        "generation_mode": operation,
        "strategy_tags": "、".join(tags),
        "duration": duration or "",
        "user_input": reference_script or "",
        "original_script": reference_script or "",
        "style": "",
        "title": "",
        "script": "",
        "publish_content": "",
    }
    variables.update(case_prompt_variables(get_case(request, case_id)))
    if not str(variables.get("key_selling_points") or "").strip():
        variables["key_selling_points"] = variables.get("description") or ""
    variant_node_id = f"{_FALLBACK_SCRIPT_NODE_ID}.{persona_mode}.{operation}"
    prompt_invocation, rendered = _render_with_fallback(
        request,
        variant_node_id=variant_node_id,
        variables=variables,
        case_id=case_id,
        provider_profile_id=profile.id,
    )
    rendered = _append_generation_context(
        rendered,
        brief=brief,
        memories=memories,
        strategy_tags=tags,
        reference_script=reference_script,
        duration=duration,
        recent_script_texts=recent_script_texts or [],
    )
    rendered = f"{rendered}\n\n{_RESPONSE_CONTRACT}"
    registry = request.app.state.prompt_registry
    last_invalid: NodeExecutionError | None = None
    # No-silent-degrade (Spec §2.3): the model reply must validate against the
    # script output schema (non-empty口播 script). On prompt.output_invalid we retry
    # up to the bound, then hard_fail with prompt.output_invalid -- we never let a
    # malformed-but-non-empty reply slip through as a usable script.
    for attempt in range(_SCRIPT_OUTPUT_MAX_RETRIES + 1):
        invocation, result = request.app.state.provider_gateway.invoke(
            ProviderCall(
                case_id=case_id,
                provider_profile_id=profile.id,
                capability_id="llm.chat",
                prompt_version_id=prompt_invocation.prompt_version_id,
                input={
                    "prompt": rendered,
                    "brief": brief,
                    "memory_ids": memory_ids,
                    "memories": memories,
                    "recent_script_texts": recent_script_texts or [],
                    "attempt": attempt,
                },
            )
        )
        if result is None or invocation.error:
            raise NodeExecutionError(
                invocation.error.code if invocation.error else c.ErrorCode.provider_remote_failed,
                invocation.error.message if invocation.error else "Case agent LLM provider failed.",
            )
        repository(request).prompt_invocations[prompt_invocation.id] = prompt_invocation.model_copy(
            update={"provider_invocation_id": invocation.id, "updated_at": c.utcnow()}
        )
        try:
            registry.validate_output(
                prompt_version_id=prompt_invocation.prompt_version_id,
                output=result.output,
            )
        except NodeExecutionError as exc:
            if exc.error.code != c.ErrorCode.prompt_output_invalid:
                raise
            last_invalid = exc
            continue
        script = _strip_stage_cues(extract_script_from_output(result.output))
        if script:
            title = extract_script_title_from_output(result.output) or None
            return GeneratedScript(script=script, title=title)
        last_invalid = NodeExecutionError(
            c.ErrorCode.prompt_output_invalid, "Case agent LLM output missing script."
        )
    raise NodeExecutionError(
        c.ErrorCode.prompt_output_invalid,
        last_invalid.error.message if last_invalid else "Case agent LLM output failed schema validation.",
    )


def _render_with_fallback(
    request: Request,
    *,
    variant_node_id: str,
    variables: dict[str, object],
    case_id: str,
    provider_profile_id: str,
):
    registry = request.app.state.prompt_registry
    try:
        return registry.render(
            node_id=variant_node_id,
            variables=variables,
            case_id=case_id,
            provider_profile_id=provider_profile_id,
        )
    except NodeExecutionError as exc:
        if exc.error.code not in _FALLBACK_ERROR_CODES:
            raise
    return registry.render(
        node_id=_FALLBACK_SCRIPT_NODE_ID,
        variables=variables,
        case_id=case_id,
        provider_profile_id=provider_profile_id,
    )


def _select_real_llm_profile(request: Request) -> c.ProviderProfile | None:
    gateway = request.app.state.provider_gateway
    provider_repo = provider_repository(request)
    if provider_repo is not None:
        candidates = provider_repo.list_profiles(capability="llm.chat", limit=200)
    else:
        candidates = [
            profile
            for profile in repository(request).provider_profiles.values()
            if profile.capability == "llm.chat"
        ]
    for profile in candidates:
        if not profile.enabled or profile.provider_id == "sandbox":
            continue
        if profile.provider_id not in gateway.plugins:
            continue
        if profile.secret_ref and not gateway._secret_is_active(profile.secret_ref):
            continue
        return profile
    return None


def _append_generation_context(
    rendered: str,
    *,
    brief: str,
    memories: list[str],
    strategy_tags: list[str],
    reference_script: str | None,
    duration: str | None,
    recent_script_texts: list[str],
) -> str:
    lines = [
        "",
        "【本轮用户要求】",
        brief.strip() or "未填写",
    ]
    if strategy_tags:
        lines.extend(["", f"【策略标签】{'、'.join(strategy_tags)}"])
    if duration:
        lines.extend(["", f"【时长偏好】{duration}"])
    if reference_script and reference_script.strip():
        lines.extend(["", "【用户补充/参考文本】", reference_script.strip()])
    if memories:
        lines.extend(["", "【案例记忆】", " / ".join(memories)])
    recent = _recent_script_snippets(recent_script_texts)
    if recent:
        lines.extend(
            [
                "",
                "【历史避重要求】",
                "下面是同一 case 最近已经生成或采用过的脚本。禁止复用或近似改写这些脚本的开头句、核心场景、段落顺序、CTA 和连续短语；本次必须换一个明显不同的切口、叙述结构或痛点路径。",
                *[f"{index}. {text}" for index, text in enumerate(recent, start=1)],
            ]
        )
    lines.extend(
        [
            "",
            "【差异化硬约束】",
            "- 如果本轮用户要求里包含版本序号，必须让该版本拥有独立切口，不能只替换几个同义词。",
            "- 全新创作必须优先新开场、新场景、新结构；不得把最近脚本压缩、扩写或轻微改写后输出。",
            "- 只能使用当前 case 信息、用户补充、案例记忆中明确给出的事实，不得编造价格、距离、优惠、销量、资质或承诺。",
        ]
    )
    context = "\n".join(lines)
    return f"{rendered}\n{context}"


def _recent_script_snippets(texts: list[str]) -> list[str]:
    snippets: list[str] = []
    seen: set[str] = set()
    for text in texts:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            continue
        key = normalized[:80]
        if key in seen:
            continue
        seen.add(key)
        if len(normalized) > _RECENT_SCRIPT_SNIPPET_CHARS:
            normalized = f"{normalized[:_RECENT_SCRIPT_SNIPPET_CHARS]}..."
        snippets.append(normalized)
        if len(snippets) >= _RECENT_SCRIPT_CONTEXT_LIMIT:
            break
    return snippets


_STAGE_CUE_KEYWORDS = (
    "停顿", "镜头", "语气", "画面", "景别", "特写", "近景", "远景", "全景", "转场",
    "配乐", "bgm", "音效", "字幕", "分镜", "旁白", "画外", "动作", "表情", "手势",
    "节奏", "语速", "拉近", "靠近", "直视", "对镜", "微笑", "点头", "停留", "切到",
    "运镜", "光线", "出镜", "入镜",
)


def _strip_stage_cues(text: str) -> str:
    def _repl(match: re.Match) -> str:
        inner = match.group(1).lower()
        return "" if any(keyword in inner for keyword in _STAGE_CUE_KEYWORDS) else match.group(0)

    text = re.sub(r"（([^（）]*)）", _repl, text)
    text = re.sub(r"\(([^()]*)\)", _repl, text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
