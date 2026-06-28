# packages/ai

外部 AI/媒体能力的统一接入层：所有对厂商模型的调用经 ProviderGateway 按 capability 分发，所有 prompt 经 registry+bindings 解析（节点内不得硬编码）。

## 职责
- ProviderGateway 按 capability 分发并落 ProviderInvocation 状态机（`prepared→submitted→{polling,succeeded,failed,timed_out,cancelled}`，async 经 `polling`），产出 UsageMeterRecord + estimated_cost，无价目时记 `cost.unpriced` 告警。
- 8 类 capability：`llm.chat` `vlm.annotation` `tts.speech` `asr.transcribe` `lipsync.video` `image.generate` `video.generate`（Seedance 文/图生视频）`audio.understanding`（DashScope Omni）。其中 `lipsync.video` 有两个 provider 共存：`runninghub.heygem` 与 `dashscope.videoretalk`。
- providers/ 各厂商插件实现 `invoke_with_context`（异步任务用 `mark_polling` + external_job_id）。
- prompts/ 管理 PromptTemplate/Version/Binding/Experiment 生命周期与渲染、输出校验。
- 安全护栏：base_url SSRF 白名单（netpolicy）、secret 读审计、per-key 并发上限。

## 关键文件 / 子目录
- `gateway/provider_gateway.py` — ProviderGateway/ProviderCall/ProviderResult；内置 SandboxProvider，`__post_init__` 经 `auto_register_real_plugins`（默认 True）自动注册真实插件
- `gateway/provider_context.py` — ProviderInvocationContext：取 secret（含审计）、`mark_polling`、存媒体产物
- `gateway/provider_limiter.py` — `provider_slot`：按 concurrency_key 的进程内 BoundedSemaphore（非 token bucket，非跨进程）
- `providers/__init__.py` — `register_real_provider_plugins`（共注册 10 个：minimax.tts / volcengine.tts / dashscope.{asr,vlm,llm,omni,videoretalk} / runninghub.heygem / volcengine.seedance / openai.image）
- `providers/seedance.py` — ArkSeedanceProvider（火山方舟 Ark，`video.generate` 文/图生视频）
- `providers/volcengine_tts.py` — VolcengineTTSProvider（火山豆包 TTS，`tts.speech`，data/management 双 auth plane）
- `providers/volc_openapi.py` — 火山管理面 OpenAPI（speech_saas_prod，AK/SK V4 签名；音色列表/签发 x-api-key，余额账单口径见 ops）
- `providers/_volc_sigv4.py` — 火山 V4 签名（HMAC-SHA256）集中实现，seedance / volc_openapi / 余额 poller 共用
- `providers/common.py` — HTTP 封装：`map_http_status`（HTTP 状态→ProviderRuntimeError 映射）、`require_secret`
- `gateway/sqlalchemy_repository.py` — `SqlAlchemyProviderRuntimeRepository`：gateway 的 DB 后端运行时读取（profile / price / secret 活性）
- `prompts/registry.py` — PromptRegistry：`resolve_published_version`/`render`/`validate_output` + `extract_script_from_output`；仅解析 status==published 的绑定版本
- `prompts/sqlalchemy_repository.py` — 模板/版本/绑定/实验的 DB 实现与 create/approve/publish/rollback
- `netpolicy.py` — 出站 host 白名单（SSRF 护栏），`is_host_allowed`/`assert_options_hosts_allowed`

## 约定与要求
- 一切外部调用走 gateway 按 capability_id；profile.capability 必须匹配，否则 `provider_unsupported_option`。
- prompt 生命周期严格 `draft→reviewing→approved→published→deprecated/rolled_back`（见 `packages/core/contracts/state_machines.py` 的 PROMPT_VERSION_TRANSITIONS），转换经 `assert_transition`；prod 只解析 published 版本，binding 钉死某一版本。
- 节点禁止硬编码 prompt，全部经 registry+bindings；缺变量/输出不合契约必须显式失败（`prompt_render_error`/`prompt_output_invalid`），不得静默降级。
- 真实路径要求 enabled profile + active secret，否则 fail loudly（`provider_auth_failed`）。
- 改 ProviderResult/usage 字段需同步计费逻辑 `_estimated_cost_from_usage`。

## 测试
- `pytest tests/providers tests/prompts`；契约 `tests/contract/test_provider_*`，DB 集成 `tests/integration/test_sqlalchemy_{providers,prompts}.py`，host 白名单 `tests/providers/test_netpolicy.py`。
- host 白名单测试可用 `CUTAGENT_ALLOWED_API_HOSTS`；并发上限 `CUTAGENT_PROVIDER_MAX_INFLIGHT`。

## 注意 / 坑
- sandbox 回退的开关 `CUTAGENT_ALLOW_SANDBOX_FALLBACK` 不在本模块判定——gateway 始终注册 `sandbox` 插件；该 flag 由调用方经 `packages/core/config/settings.py` 的 `sandbox_fallback_allowed()` 控制（默认 OFF=按真实 profile 失败）。
- gateway-level host 白名单复检默认关闭，需 `CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST=1`；权威拦截在 provider-profile create/patch（`apps/api/services/providers.py`）。
- 并发限流是进程内的；跨 worker/pod 不生效。
- secret.read 审计为 best-effort（log+swallow），不阻断热路径；只记访问元数据不记密文。
