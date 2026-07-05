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
    """Seed the real provider PROFILES (and prompts/prices), but NEVER secret values.

    SAFETY INVARIANT: every real (paid) provider path is gated on an ACTIVE secret
    (gateway ``_validate_profile`` / ``_is_real_*_profile``). These seeded profiles
    carry a ``secret_ref`` but the secret VALUE is intentionally NOT seeded, so the
    paid paths (TTS / lipsync / VLM / image cover) stay inert until an operator
    activates a secret out of band. Do NOT seed secret values here, and do NOT remove
    a profile's ``secret_ref`` -- either would silently arm a paid path from the
    default seed. (Locked by test_seeded_image_profile_is_gated_without_an_active_secret.)
    """
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
            id="volcengine.tts.prod",
            provider_id="volcengine.tts",
            model_id="seed-icl-2.0",
            capability="tts.speech",
            display_name="火山豆包语音 Production",
            environment="prod",
            secret_ref="volcengine_tts_prod.secret",
            concurrency_key="volcengine:tts.speech",
            timeout_sec=120,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.tts.options"),
            # appid is the per-account public id (operator fills it in, e.g. 9635790622);
            # secret is the account AccessKeyId:SecretAccessKey, armed out of band.
            default_options={
                "appid": "",
                "cluster": "volcano_icl",
                "format": "mp3",
                "api_key_name": "cutagent-tts",
            },
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
            model_id="qwen3.7-plus",
            capability="llm.chat",
            display_name="DashScope Qwen LLM Production",
            environment="prod",
            secret_ref="dashscope_prod.secret",
            concurrency_key="dashscope:llm.chat",
            timeout_sec=120,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.llm.options"),
        ),
        ProviderProfile(
            id="dashscope.omni.prod",
            provider_id="dashscope.omni",
            model_id="qwen3.5-omni-plus",
            capability="audio.understanding",
            display_name="DashScope Qwen-Omni Audio Production",
            environment="prod",
            enabled=True,
            secret_ref="dashscope_prod.secret",
            concurrency_key="dashscope:audio.understanding",
            timeout_sec=120,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.audio.options"),
        ),
        ProviderProfile(
            id="dashscope.multimodal_embedding.prod",
            provider_id="dashscope.multimodal_embedding",
            model_id="qwen3-vl-embedding",
            capability="multimodal.embedding",
            display_name="DashScope Qwen3-VL Embedding Production",
            environment="prod",
            enabled=True,
            secret_ref="dashscope_prod.secret",
            concurrency_key="dashscope:multimodal.embedding",
            timeout_sec=60,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.embedding.options"),
            default_options={
                "base_url": "https://dashscope.aliyuncs.com/api/v1",
                "embedding_url": (
                    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
                    "multimodal-embedding/multimodal-embedding"
                ),
                "dimension": 1024,
                "normalization": "l2",
                "index_version": "clip-video-qwen3-v2",
            },
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
                "video_field_name": "file",
                "audio_node_id": "",
                "audio_field_name": "audio",
                "poll_interval": 2,
                "poll_max_attempts": 120,
            },
        ),
        ProviderProfile(
            id="dashscope.videoretalk.prod",
            provider_id="dashscope.videoretalk",
            model_id="videoretalk",
            capability="lipsync.video",
            display_name="DashScope VideoReTalk Production",
            environment="prod",
            secret_ref="dashscope_prod.secret",
            concurrency_key="dashscope:lipsync.video",
            timeout_sec=180,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.lipsync.options"),
            default_options={
                "base_url": "https://dashscope.aliyuncs.com/api/v1",
                "poll_interval": 8,
                "poll_max_attempts": 180,
            },
        ),
        ProviderProfile(
            id="volcengine.seedream.prod",
            provider_id="volcengine.seedream",
            model_id="doubao-seedream-5-0-260128",
            capability="image.generate",
            display_name="Volcengine Ark Seedream Production",
            environment="prod",
            # Ark Seedream and Seedance share the same Ark AK/SK; the provider
            # exchanges it for a temporary OpenAI-compatible API key at runtime.
            secret_ref="volcengine_seedance_prod.secret",
            concurrency_key="volcengine:image.generate",
            timeout_sec=180,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.image.options"),
            default_options={
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "auth_type": "auto",
                "project_name": "default",
                "temporary_api_key_ttl_seconds": 604800,
                "size": "1440x2560",
                "response_format": "url",
                "watermark": False,
                "n": 1,
            },
        ),
        ProviderProfile(
            id="openai.image.prod",
            provider_id="openai.image",
            model_id="gpt-image-2-all",
            capability="image.generate",
            display_name="OpenAI gpt-image (neuromash mirror) Production",
            environment="prod",
            secret_ref="openai_image_prod.secret",
            concurrency_key="openai:image.generate",
            timeout_sec=180,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.image.options"),
            default_options={
                "base_url": "https://neuromashv1.cn/v1",
                "provider_kind": "neuromash",
                "size": "1024x1536",
                "n": 1,
            },
        ),
        # Balance-only profile: NOT an invocation path (no gateway plugin / capability
        # is dispatched to ``volcengine.billing``). It exists so the balance poller has
        # an account to query. The secret is the Volcengine billing AK/SK pair
        # ("<access_key_id>:<access_key_secret>"); like every other seed it carries a
        # ``secret_ref`` but the value is armed out of band. The 火山 account balance is
        # account-level — one balance covers TTS / 方舟 Seedance / etc.
        ProviderProfile(
            id="volcengine.billing.prod",
            provider_id="volcengine.billing",
            model_id="account",
            capability="balance.monitor",
            display_name="火山引擎账户余额（TTS / 方舟 共享账户）",
            environment="prod",
            secret_ref="volcengine_billing_prod.secret",
            concurrency_key="volcengine:billing",
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.billing.options"),
        ),
        # Aliyun account-level balance (BSS QueryAccountBalance). The DashScope models
        # + OSS all bill to ONE Aliyun account; this profile surfaces that single
        # account balance. Its secret is the Aliyun AK/SK pair ("ak:sk", the SAME
        # credential as OSS) — NOT the DashScope sk- Bearer key. Balance-only, no
        # gateway plugin. The per-capability dashscope.* model profiles keep their
        # Bearer secret and degrade to ``unsupported`` for balance (not AK/SK).
        ProviderProfile(
            id="aliyun.billing.prod",
            provider_id="aliyun.billing",
            model_id="account",
            capability="balance.monitor",
            display_name="阿里云账户余额（DashScope / OSS 共享账户）",
            environment="prod",
            secret_ref="aliyun_billing_prod.secret",
            concurrency_key="aliyun:billing",
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.billing.options"),
        ),
        ProviderProfile(
            id="volcengine.seedance.prod",
            provider_id="volcengine.seedance",
            model_id="doubao-seedance-2-0-260128",
            capability="video.generate",
            display_name="Volcengine Ark Seedance Production",
            environment="prod",
            secret_ref="volcengine_seedance_prod.secret",
            concurrency_key="volcengine:video.generate",
            timeout_sec=600,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.video.options"),
            default_options={
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "ratio": "9:16",
                "resolution": "720p",
                "duration": 15,
                "param_style": "json_fields",
                "temporary_api_key_ttl_seconds": 604800,
                "poll_interval": 8,
                "poll_max_attempts": 180,
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
        ProviderPriceCatalog(id="price_volcengine_prod", provider_id="volcengine.tts", status="published"),
        ProviderPriceCatalog(id="price_dashscope_prod", provider_id="dashscope.llm", status="published"),
        ProviderPriceCatalog(id="price_dashscope_asr_prod", provider_id="dashscope.asr", status="published"),
        ProviderPriceCatalog(id="price_dashscope_vlm_prod", provider_id="dashscope.vlm", status="published"),
        ProviderPriceCatalog(
            id="price_dashscope_videoretalk_prod", provider_id="dashscope.videoretalk", status="published"
        ),
        ProviderPriceCatalog(
            id="price_dashscope_omni_prod",
            provider_id="dashscope.omni",
            status="published",
        ),
        ProviderPriceCatalog(
            id="price_dashscope_embedding_prod",
            provider_id="dashscope.multimodal_embedding",
            status="published",
        ),
        ProviderPriceCatalog(id="price_openai_image_prod", provider_id="openai.image", status="published"),
        ProviderPriceCatalog(
            id="price_volcengine_seedream_prod", provider_id="volcengine.seedream", status="published"
        ),
        ProviderPriceCatalog(
            id="price_runninghub_heygem_prod", provider_id="runninghub.heygem", status="published"
        ),
        ProviderPriceCatalog(
            id="price_volcengine_seedance_prod", provider_id="volcengine.seedance", status="published"
        ),
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
    # 火山豆包语音大模型约 6.5 元/万字符 = 0.00065 元/字符（input_token=len(text)）。
    repository.price_items["price_volcengine_tts_chars"] = ProviderPriceItem(
        id="price_volcengine_tts_chars",
        catalog_id="price_volcengine_prod",
        provider_id="volcengine.tts",
        model_id="seed-icl-2.0",
        capability_id="tts.speech",
        unit="input_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.00065")),
    )
    repository.price_items["price_dashscope_llm_input"] = ProviderPriceItem(
        id="price_dashscope_llm_input",
        catalog_id="price_dashscope_prod",
        provider_id="dashscope.llm",
        model_id="qwen3.7-plus",
        capability_id="llm.chat",
        unit="input_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.0000008")),
    )
    repository.price_items["price_dashscope_llm_output"] = ProviderPriceItem(
        id="price_dashscope_llm_output",
        catalog_id="price_dashscope_prod",
        provider_id="dashscope.llm",
        model_id="qwen3.7-plus",
        capability_id="llm.chat",
        unit="output_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.000002")),
    )
    repository.price_items["price_dashscope_omni_input"] = ProviderPriceItem(
        id="price_dashscope_omni_input",
        catalog_id="price_dashscope_omni_prod",
        provider_id="dashscope.omni",
        model_id="qwen3.5-omni-plus",
        capability_id="audio.understanding",
        unit="input_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.000002")),
    )
    repository.price_items["price_dashscope_omni_output"] = ProviderPriceItem(
        id="price_dashscope_omni_output",
        catalog_id="price_dashscope_omni_prod",
        provider_id="dashscope.omni",
        model_id="qwen3.5-omni-plus",
        capability_id="audio.understanding",
        unit="output_token",
        unit_price=Money(currency="CNY", amount=Decimal("0.000008")),
    )
    repository.price_items["price_dashscope_embedding_call"] = ProviderPriceItem(
        id="price_dashscope_embedding_call",
        catalog_id="price_dashscope_embedding_prod",
        provider_id="dashscope.multimodal_embedding",
        model_id="qwen3-vl-embedding",
        capability_id="multimodal.embedding",
        unit="call",
        unit_price=Money(currency="CNY", amount=Decimal("0")),
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
    repository.price_items["price_openai_image_call"] = ProviderPriceItem(
        id="price_openai_image_call",
        catalog_id="price_openai_image_prod",
        provider_id="openai.image",
        model_id="gpt-image-2-all",
        capability_id="image.generate",
        unit="call",
        unit_price=Money(currency="CNY", amount=Decimal("0.4")),
    )
    # Seedream pricing is account-side and can be tuned in the operator price
    # catalog; keep the existing image-call estimate until live billing is calibrated.
    repository.price_items["price_volcengine_seedream_call"] = ProviderPriceItem(
        id="price_volcengine_seedream_call",
        catalog_id="price_volcengine_seedream_prod",
        provider_id="volcengine.seedream",
        model_id="doubao-seedream-5-0-260128",
        capability_id="image.generate",
        unit="call",
        unit_price=Money(currency="CNY", amount=Decimal("0.4")),
    )
    # DashScope VideoReTalk (lipsync 备路) reports ``video_seconds`` (duration_sec).
    # Priced per media-second so the backup lipsync path is no longer ``unpriced``.
    # ⚠️ The rate below is a PLACEHOLDER estimate — set the real 百炼 VideoReTalk
    # unit price via the price catalog / operator (billing_status stays "estimated").
    repository.price_items["price_dashscope_videoretalk_second"] = ProviderPriceItem(
        id="price_dashscope_videoretalk_second",
        catalog_id="price_dashscope_videoretalk_prod",
        provider_id="dashscope.videoretalk",
        model_id="videoretalk",
        capability_id="lipsync.video",
        unit="media_second",
        unit_price=Money(currency="CNY", amount=Decimal("0.1")),
    )
    # RunningHub HeyGem (lipsync 主路) bills in coins (``consumeCoins``), reported by
    # the adapter as ``provider_credits``. Without this priced via ``provider_credit``,
    # the gateway records 0 cost for the most expensive node. The per-coin CNY rate
    # was derived from the live account (remainMoney/remainCoins ≈ ¥2.6/65952 ≈
    # ¥0.0000394/coin); it is an estimate — operators can re-tune via the price catalog.
    repository.price_items["price_runninghub_heygem_credit"] = ProviderPriceItem(
        id="price_runninghub_heygem_credit",
        catalog_id="price_runninghub_heygem_prod",
        provider_id="runninghub.heygem",
        model_id="heygem-webapp",
        capability_id="lipsync.video",
        unit="provider_credit",
        unit_price=Money(currency="CNY", amount=Decimal("0.0000394226")),
    )
    repository.price_items["price_volcengine_seedance_media_second"] = ProviderPriceItem(
        id="price_volcengine_seedance_media_second",
        catalog_id="price_volcengine_seedance_prod",
        provider_id="volcengine.seedance",
        model_id="doubao-seedance-2-0-260128",
        capability_id="video.generate",
        unit="media_second",
        unit_price=Money(currency="CNY", amount=Decimal("1.0")),
    )
