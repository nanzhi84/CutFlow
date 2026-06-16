# packages/publishing

发布与分发领域（spec §13，隔离边界）：把发布物（package/batch/item/attempt/record）落库并按状态机推进，同时承载平台适配、文案/封面生成、账号匹配，以及小V猫真机 connector。**不是只有仓储。**

## 职责
- 持久化：`sqlalchemy_repository.py` 的 `SqlAlchemyPublishingRepository` —— package/batch/item/attempt 的 CRUD、`submit_batch` 状态机、§9.5 漏斗事件落库。
- 平台适配：`platform_adapter.py` 的 `PublishPlatformAdapter` 端口 + `SandboxPublishAdapter` / `XiaoVmaoPublishAdapter`，经 `select_adapter()` / `resolve_adapter_id()` 选择实现。
- 真机驱动：`connectors/xiaovmao_cdp.py` —— 小V猫 CDP **进程外**真机 connector（M6c，代码自标 UNVERIFIED）。
- 文案/封面：`copy_node.py`（§28.3 generate-copy：`generate_publish_copy`/`derive_publish_copy` + `LlmChatPort` + 确定性 fallback）、`cover_node.py`（generate-cover / preview-cover-frame：`generate_publish_cover`/`preview_cover_frame` + `AiCoverPort`）。
- 账号：`account_matching.py` —— 账号组过滤、账号匹配、`normalize_scheduled_at`/`normalize_publish_tags`。

## 关键文件
- `sqlalchemy_repository.py` / `sqlalchemy_mappers.py` —— 仓储 + Row→contract 映射。
- `platform_adapter.py` —— adapter 端口与 Sandbox/小V猫 实现、`select_adapter`。
- `connectors/xiaovmao_cdp.py` —— 小V猫真机 CDP 驱动（进程外、UNVERIFIED）。
- `copy_node.py` / `cover_node.py` —— 文案 / 封面生成（LLM/AI 端口 + fallback）。
- `account_matching.py` —— 账号匹配与发布参数校验。

## 约定与要求
- 状态流转一律经 `assert_transition`（`publish_batch`/`publish_item`/`publish_attempt`，来自 `packages.core.contracts.state_machines`）；`dry_run` 走 review_ready / manual_review_ready 分支而非 published。
- 平台逻辑必须放在 adapter 之后，经 `select_adapter()`/`resolve_adapter_id()` 选择；新增平台加 adapter，别在仓储里写平台分支。
- 文案/封面生成走 `LlmChatPort`/`AiCoverPort`，缺 provider 时用确定性 fallback，不静默失败。
- 漏斗事件（`publish_started`/`published`，带 `dedupe_key`）在 submit 事务 commit **之后** best-effort 落库（`persist_funnel_event_rows`，来自 `packages.core.observability`），否则 SQL 后端 `true_yield_rate` 结构性为 0。

## 测试
- `pytest tests/publishing`（`test_platform_adapter.py` / `test_copy_node.py` / `test_account_matching.py`）
- `pytest tests/integration/test_sqlalchemy_publishing.py`、`tests/api/test_publishing_funnel.py`

## 注意 / 坑
- `connectors/xiaovmao_cdp.py` 是 UNVERIFIED 真机驱动（进程外、依赖 CDP），勿当作已验证的生产路径。
- 改发布相关 contract 后须重生成 openapi.json + schema.d.ts。
