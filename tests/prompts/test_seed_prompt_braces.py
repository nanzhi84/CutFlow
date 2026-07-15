from __future__ import annotations

from string import Formatter

from packages.core import contracts as c
from packages.core.storage.repository import Repository
from packages.core.storage.prompt_groups import prompt_variable_hints, seed_prompt_groups


EXPECTED_PROMPT_GROUPS = {
    "script": {
        "prompt_script_hard_ad_polish": "prompt.script.hard_ad.polish",
        "prompt_script_hard_ad_fresh_generate": "prompt.script.hard_ad.fresh_generate",
        "prompt_script_hard_ad_remix_generate": "prompt.script.hard_ad.remix_generate",
        "prompt_script_hard_ad_clone_generate": "prompt.script.hard_ad.clone_generate",
        "prompt_script_hard_ad_semantic": "prompt.script.hard_ad.semantic",
        "prompt_script_ip_persona_polish": "prompt.script.ip_persona.polish",
        "prompt_script_ip_persona_fresh_generate": "prompt.script.ip_persona.fresh_generate",
        "prompt_script_ip_persona_remix_generate": "prompt.script.ip_persona.remix_generate",
        "prompt_script_ip_persona_clone_generate": "prompt.script.ip_persona.clone_generate",
        "prompt_script_ip_persona_semantic": "prompt.script.ip_persona.semantic",
    },
    "vlm": {
        "prompt_vlm_broll_analysis": "prompt.vlm.broll_analysis",
        "prompt_vlm_broll_portrait": "prompt.vlm.broll_portrait",
        "prompt_vlm_broll_scenery": "prompt.vlm.broll_scenery",
    },
    "cover": {
        "prompt_cover_ai_cover": "prompt.cover.ai_cover",
        "prompt_cover_reference_style": "prompt.cover.reference_style",
    },
    "editing": {
        # Clean-slate caption composition (#209) leaves the Agent chain with two
        # narrow LLM responsibilities: media selection and BGM selection. Caption
        # composition itself is deterministic and has no prompt.
        "prompt_media_selection_agent": "prompt.media_selection.agent",
        "prompt_bgm_agent": "prompt.bgm.agent",
        "prompt_window_query": "prompt.window_query.planning",
    },
}


def _format_fields(content: str) -> set[str]:
    return {
        field_name for _, field_name, _, _ in Formatter().parse(content) if field_name is not None
    }


def test_production_seed_prompts_only_use_declared_format_variables():
    repository = Repository()
    cases = {
        "prompt_creative_intent_v1": {"script": "示例脚本"},
        "prompt_case_agent_script_v1": {"brief": "示例 brief", "memories": "示例记忆"},
        "prompt_vlm_annotation_v1": {"asset_id": "asset_1", "asset_kind": "video"},
    }

    for version_id, variables in cases.items():
        content = repository.prompt_versions[version_id].content
        assert _format_fields(content) == set(variables)
        content.format(**variables)


def test_prompt_group_seeds_create_four_groups_with_brace_safe_content():
    repository = Repository()

    for expected in EXPECTED_PROMPT_GROUPS.values():
        for template_id, purpose in expected.items():
            template = repository.prompt_templates[template_id]
            version_id = (
                "prompt_media_selection_agent_v2"
                if template_id == "prompt_media_selection_agent"
                else f"{template_id}_v1"
            )
            version = repository.prompt_versions[version_id]
            hints = prompt_variable_hints(template_id)

            assert template.purpose == purpose
            assert template.status == "active"
            assert version.status == "published"
            assert version.prompt_template_id == template_id
            assert _format_fields(version.content) <= set(hints)
            version.content.format(**{name: f"示例 {name}" for name in hints})


def test_prompt_group_seed_does_not_change_existing_bindings():
    repository = Repository()

    expected_bindings = {
        "prompt_binding_global_intent": (
            "prompt_creative_intent",
            "prompt_creative_intent_v1",
            "ResolveCreativeIntent",
        ),
        "prompt_binding_vlm_annotation": (
            "prompt_vlm_annotation",
            "prompt_vlm_annotation_v1",
            "MediaAssetAnnotation",
        ),
        "prompt_binding_case_agent_script": (
            "prompt_case_agent_script",
            "prompt_case_agent_script_v1",
            "CaseAgentScriptGenerate",
        ),
    }
    for binding_id, expected in expected_bindings.items():
        binding = repository.prompt_bindings[binding_id]
        assert (binding.prompt_template_id, binding.prompt_version_id, binding.node_id) == expected


def test_clean_slate_agent_prompts_bind_to_narrow_responsibility_nodes():
    repository = Repository()

    expected_bindings = {
        "prompt_binding_prompt_media_selection_agent": (
            "prompt_media_selection_agent",
            "prompt_media_selection_agent_v2",
            "MediaSelectionAgentPlanning",
        ),
        "prompt_binding_prompt_bgm_agent": (
            "prompt_bgm_agent",
            "prompt_bgm_agent_v1",
            "BgmAgentPlanning",
        ),
    }
    for binding_id, expected in expected_bindings.items():
        binding = repository.prompt_bindings[binding_id]
        assert (binding.prompt_template_id, binding.prompt_version_id, binding.node_id) == expected


def test_media_v2_seed_does_not_overwrite_an_existing_binding_choice():
    repository = Repository()
    binding_id = "prompt_binding_prompt_media_selection_agent"
    repository.prompt_bindings[binding_id] = repository.prompt_bindings[binding_id].model_copy(
        update={"prompt_version_id": "prompt_media_selection_agent_v1"}
    )

    seed_prompt_groups(repository)

    assert repository.prompt_bindings[binding_id].prompt_version_id == (
        "prompt_media_selection_agent_v1"
    )


def test_seeded_ai_cover_prompt_targets_9_16():
    content = Repository().prompt_versions["prompt_cover_ai_cover_v1"].content

    assert "9:16" in content
    assert "3:4" not in content
    assert "生成一张" in content
    assert "不是普通截图贴字" in content
    assert "由模型自主设计" in content
    assert "Main headline" not in content
    assert "selected video frame" not in content


def test_seeded_cover_reference_style_prompt_requests_chinese_style_guide():
    content = Repository().prompt_versions["prompt_cover_reference_style_v1"].content

    assert "中文风格说明" in content
    assert "商业包装感" in content
    assert "English style guide" not in content


def test_media_selection_agent_prompt_is_media_only():
    repository = Repository()
    content = repository.prompt_versions["prompt_media_selection_agent_v2"].content
    legacy_content = repository.prompt_versions["prompt_media_selection_agent_v1"].content
    hints = prompt_variable_hints("prompt_media_selection_agent")
    output_example = content.rsplit("只输出如下 JSON：", 1)[1]

    assert _format_fields(content) == set(hints)
    assert "bgm_candidates" not in hints
    assert "caption_windows" not in hints
    assert "portrait_plan" in output_example
    assert "broll_plan" in output_example
    assert "analysis" in output_example
    assert "legal_candidates" in content
    assert "不需要再查全局候选表" in content
    assert "{portrait_candidates}" not in content
    assert "{broll_candidates}" not in content
    assert "allowed_slot_ids" not in content
    assert "broll_uniqueness_rule" in hints
    assert "diversity_key" in content
    assert '"candidate_id": "pc_000"' in output_example
    assert "legal_window_ids" not in content
    assert '"window_id"' not in output_example
    assert '"source_mode"' not in output_example
    assert '"confidence"' not in output_example
    assert '"matched_keywords"' not in output_example
    assert "bgm_id" not in output_example
    assert "bgm_plan" not in output_example
    assert "严格职责边界" in content
    assert "{portrait_candidates}" in legacy_content
    assert "{broll_candidates}" in legacy_content


def test_bgm_agent_prompt_selects_only_bgm():
    repository = Repository()
    content = repository.prompt_versions["prompt_bgm_agent_v1"].content
    hints = prompt_variable_hints("prompt_bgm_agent")
    output_example = content.rsplit("只输出如下 JSON：", 1)[1]

    assert hints == ["script", "bgm_candidates", "repair_feedback"]
    assert _format_fields(content) == set(hints)
    assert '"bgm_id"' in output_example
    assert '"analysis"' in output_example
    assert "caption" not in output_example
    assert '"font"' not in output_example
    assert '"rect"' not in output_example
    assert '"animation_id"' not in output_example
    assert "只能输出 bgm_id 和 analysis" in content


def test_prompt_template_view_exposes_seed_variable_hints():
    repository = Repository()
    template = repository.prompt_templates["prompt_script_ip_persona_fresh_generate"]
    view = c.PromptTemplateView(
        template=template,
        published_version=repository.prompt_versions["prompt_script_ip_persona_fresh_generate_v1"],
        variable_hints=prompt_variable_hints(template.id),
    )

    assert view.variable_hints
    assert "ip_persona" in view.variable_hints
    assert "duration" in view.variable_hints


def test_creative_intent_seed_prompt_requests_top_level_contract():
    content = Repository().prompt_versions["prompt_creative_intent_v1"].content

    assert content.count("{script}") == 1
    assert "hook" in content
    assert "tone" in content
    assert "audience" in content
    assert "beats" in content
    assert "bgm_mood" in content
    assert "display_mode" in content
    assert "inline" in content
    assert "whole_cue" in content
    assert "沉稳 / 温暖 / 轻快 / 励志 / 高能 / 紧张 / 高级 / 俏皮" in content
    assert "禁止使用 markdown 代码块" in content
    assert "不要再嵌套 intent" not in content


def test_window_query_seed_prompt_requests_scene_hint():
    content = Repository().prompt_versions["prompt_window_query_v1"].content

    assert "scene_hint" in content
    assert "窗口 JSON（每项含 window_id、kind、narration_text、scene_hint）" in content
