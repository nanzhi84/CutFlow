from __future__ import annotations

from decimal import Decimal

from packages.core.contracts import (
    Money,
    PromptBinding,
    PromptSchemaRef,
    PromptTemplate,
    PromptVersion,
    ProviderOptionsSchemaRef,
    ProviderPriceCatalog,
    ProviderPriceItem,
    ProviderProfile,
    utcnow,
)


def seed_real_provider_configuration(repository) -> None:
    profiles = [
        ProviderProfile(
            id="minimax.tts.prod",
            provider_id="minimax.tts",
            model_id="speech-02-hd",
            capability="tts.speech",
            display_name="MiniMax TTS Production",
            environment="prod",
            secret_ref="minimax_prod.secret",
            concurrency_key="minimax:tts.speech",
            timeout_sec=120,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.tts.options"),
            default_options={"group_id": "", "format": "mp3", "sample_rate": 32000},
        ),
        ProviderProfile(
            id="dashscope.asr.prod",
            provider_id="dashscope.asr",
            model_id="paraformer-v2",
            capability="asr.transcribe",
            display_name="DashScope Paraformer Production",
            environment="prod",
            secret_ref="dashscope_prod.secret",
            concurrency_key="dashscope:asr.transcribe",
            timeout_sec=120,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.asr.options"),
        ),
        ProviderProfile(
            id="dashscope.vlm.prod",
            provider_id="dashscope.vlm",
            model_id="qwen-vl-max",
            capability="vlm.annotation",
            display_name="DashScope Qwen-VL Production",
            environment="prod",
            secret_ref="dashscope_prod.secret",
            concurrency_key="dashscope:vlm.annotation",
            timeout_sec=120,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.vlm.options"),
        ),
        ProviderProfile(
            id="dashscope.llm.prod",
            provider_id="dashscope.llm",
            model_id="qwen-plus",
            capability="llm.chat",
            display_name="DashScope Qwen LLM Production",
            environment="prod",
            secret_ref="dashscope_prod.secret",
            concurrency_key="dashscope:llm.chat",
            timeout_sec=60,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.llm.options"),
        ),
        ProviderProfile(
            id="runninghub.heygem.prod",
            provider_id="runninghub.heygem",
            model_id="heygem-webapp",
            capability="lipsync.video",
            display_name="RunningHub HeyGem Production",
            environment="prod",
            secret_ref="runninghub_prod.secret",
            concurrency_key="runninghub:lipsync.video",
            timeout_sec=180,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.lipsync.options"),
            default_options={
                "base_url": "https://www.runninghub.ai",
                "webapp_id": "",
                "video_node_id": "",
                "audio_node_id": "",
                "poll_interval": 2,
                "poll_max_attempts": 120,
            },
        ),
    ]
    for profile in profiles:
        repository.provider_profiles[profile.id] = profile

    _seed_prompt(
        repository,
        template_id="prompt_case_agent_script",
        version_id="prompt_case_agent_script_v1",
        binding_id="prompt_binding_case_agent_script",
        node_id="CaseAgentScriptGenerate",
        name="Case Agent Script Generator",
        purpose="case_agent.script_generate",
        output_schema_id="case_agent_script.output",
        content=(
            "你是短视频脚本助手。根据 brief 和案例记忆生成或润色一版可直接拍摄的口播脚本。"
            "只返回一个 JSON 对象，字段 script 为脚本文本。\nBrief: {brief}\n案例记忆: {memories}"
        ),
    )
    _seed_prompt(
        repository,
        template_id="prompt_vlm_annotation",
        version_id="prompt_vlm_annotation_v1",
        binding_id="prompt_binding_vlm_annotation",
        node_id="MediaAssetAnnotation",
        name="Media Asset VLM Annotation",
        purpose="media.vlm_annotation",
        output_schema_id="media_annotation.output",
        content=(
            "分析素材并返回 JSON canonical 标注，包含 labels、kind、quality.valid、quality.issues、scenes。"
            "素材 ID: {asset_id}\n素材类型: {asset_kind}"
        ),
    )

    _seed_price_catalogs(repository)


def _seed_prompt(
    repository,
    *,
    template_id: str,
    version_id: str,
    binding_id: str,
    node_id: str,
    name: str,
    purpose: str,
    output_schema_id: str,
    content: str,
) -> None:
    template = PromptTemplate(
        id=template_id,
        name=name,
        purpose=purpose,
        variables_schema_ref=PromptSchemaRef(schema_id=f"{output_schema_id}.variables"),
        output_schema_ref=PromptSchemaRef(schema_id=output_schema_id),
        status="active",
    )
    version = PromptVersion(
        id=version_id,
        prompt_template_id=template.id,
        content=content,
        status="published",
        approved_at=utcnow(),
        published_at=utcnow(),
    )
    binding = PromptBinding(
        id=binding_id,
        prompt_template_id=template.id,
        prompt_version_id=version.id,
        node_id=node_id,
        priority=1,
    )
    repository.prompt_templates[template.id] = template
    repository.prompt_versions[version.id] = version
    repository.prompt_bindings[binding.id] = binding


def _seed_price_catalogs(repository) -> None:
    catalogs = [
        ProviderPriceCatalog(id="price_minimax_prod", provider_id="minimax.tts", status="published"),
        ProviderPriceCatalog(id="price_dashscope_prod", provider_id="dashscope.llm", status="published"),
        ProviderPriceCatalog(id="price_dashscope_asr_prod", provider_id="dashscope.asr", status="published"),
        ProviderPriceCatalog(id="price_dashscope_vlm_prod", provider_id="dashscope.vlm", status="published"),
    ]
    for catalog in catalogs:
        repository.price_catalogs[catalog.id] = catalog
    repository.price_items["price_minimax_tts_chars"] = ProviderPriceItem(
        id="price_minimax_tts_chars",
        catalog_id="price_minimax_prod",
        provider_id="minimax.tts",
        model_id="speech-02-hd",
        capability_id="tts.speech",
        unit="input_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.00015")),
    )
    repository.price_items["price_dashscope_llm_input"] = ProviderPriceItem(
        id="price_dashscope_llm_input",
        catalog_id="price_dashscope_prod",
        provider_id="dashscope.llm",
        model_id="qwen-plus",
        capability_id="llm.chat",
        unit="input_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.0000008")),
    )
    repository.price_items["price_dashscope_llm_output"] = ProviderPriceItem(
        id="price_dashscope_llm_output",
        catalog_id="price_dashscope_prod",
        provider_id="dashscope.llm",
        model_id="qwen-plus",
        capability_id="llm.chat",
        unit="output_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.000002")),
    )
    repository.price_items["price_dashscope_asr_media_second"] = ProviderPriceItem(
        id="price_dashscope_asr_media_second",
        catalog_id="price_dashscope_asr_prod",
        provider_id="dashscope.asr",
        model_id="paraformer-v2",
        capability_id="asr.transcribe",
        unit="media_second",
        unit_price=Money(currency="CNY", amount=Decimal("0.0005")),
    )
    repository.price_items["price_dashscope_vlm_input"] = ProviderPriceItem(
        id="price_dashscope_vlm_input",
        catalog_id="price_dashscope_vlm_prod",
        provider_id="dashscope.vlm",
        model_id="qwen-vl-max",
        capability_id="vlm.annotation",
        unit="input_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.000003")),
    )
    repository.price_items["price_dashscope_vlm_output"] = ProviderPriceItem(
        id="price_dashscope_vlm_output",
        catalog_id="price_dashscope_vlm_prod",
        provider_id="dashscope.vlm",
        model_id="qwen-vl-max",
        capability_id="vlm.annotation",
        unit="output_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.000009")),
    )
