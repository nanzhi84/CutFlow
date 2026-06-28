# packages/publishing

发布与分发领域：把发布物（package/batch/item/attempt/record）落库并按状态机推进，同时承载平台适配、文案/封面生成、账号匹配。**不是只有仓储。**

## 职责
- 持久化：`sqlalchemy_repository.py` 的 `SqlAlchemyPublishingRepository` —— package/batch/item/attempt 的 CRUD、`submit_batch` 状态机、发布漏斗事件落库。
- 平台适配：`platform_adapter.py` 的 `PublishPlatformAdapter` 端口 + 两个适配器——`SandboxPublishAdapter`（`adapter_id="sandbox.publish"`，沙盒/测试，不触达平台）与 `XiaoVmaoPublishAdapter`（`adapter_id="xiaovmao.cdp"`，**生产默认**），经 `select_adapter()` / `resolve_adapter_id()` 选择实现。
- 真实发布：`XiaoVmaoPublishAdapter` 经 CDP 驱动小V猫桌面端真实发布到抖音/快手/视频号/小红书 四平台（无 bilibili）；小V猫不可达或 adapter id 未知（`_UnregisteredPublishAdapter`）均显式失败、绝不伪造成功。
- 文案/封面：`copy_node.py`（`generate_publish_copy`/`derive_publish_copy` + `LlmChatPort` + 确定性 fallback）、`cover_node.py`（generate-cover / preview-cover-frame：`generate_publish_cover`/`preview_cover_frame` + `AiCoverPort`）。
- 发布执行：`publish_executor.py` 的 `run_item_publish` —— 多账号发布执行编排。
- 账号：`account_matching.py`（账号组过滤、账号匹配、`normalize_scheduled_at`/`normalize_publish_tags`）+ `accounts_repository.py`/`accounts_mappers.py`（Client/PublishAccount/CasePublishTarget 仓储与映射）。

## 关键文件
- `sqlalchemy_repository.py` / `sqlalchemy_mappers.py` —— 仓储 + Row→contract 映射。
- `platform_adapter.py` —— adapter 端口与 `SandboxPublishAdapter` / `XiaoVmaoPublishAdapter` 实现、`select_adapter` / `_PUBLISH_ADAPTERS` 注册表。
- `connectors/xiaovmao_cdp.py` —— CDP 驱动小V猫核心实现 + 登录会话管理 `XiaoVmaoLoginManager`（`probe_xiaovmao_accounts` / `publish_via_xiaovmao`）。
- `publish_executor.py` —— `run_item_publish` 多账号发布执行编排。
- `copy_node.py` / `cover_node.py` —— 文案 / 封面生成（LLM/AI 端口 + fallback）；`copy_llm.py` 把 copy_node 接真实 `ProviderGateway` + `PublishingCopy` 提示词。
- `accounts_repository.py` / `accounts_mappers.py` —— Client/PublishAccount/CasePublishTarget 仓储与映射。
- `account_matching.py` —— 账号匹配与发布参数校验。

## 约定与要求
- 状态流转一律经 `assert_transition`（`publish_batch`/`publish_item`/`publish_attempt`，来自 `packages.core.contracts.state_machines`）；`dry_run` 走 review_ready / manual_review_ready 分支而非 published。
- 平台逻辑必须放在 adapter 之后，经 `select_adapter()`/`resolve_adapter_id()` 选择；生产默认 `xiaovmao.cdp`（env `CUTAGENT_PUBLISH_ADAPTER` 可切换），sandbox 仅在显式 `CUTAGENT_PUBLISH_ADAPTER=sandbox.publish` 或 `CUTAGENT_ALLOW_SANDBOX_FALLBACK=1` 时使用。新增平台加 adapter，别在仓储里写平台分支。
- 文案/封面生成走 `LlmChatPort`/`AiCoverPort`，缺 provider 时用确定性 fallback，不静默失败。
- 漏斗事件（`publish_started`/`published`，带 `dedupe_key`）在 submit 事务 commit **之后** best-effort 落库（`persist_funnel_event_rows`，来自 `packages.core.observability`），否则 SQL 后端 `true_yield_rate` 结构性为 0。

## 测试
- `pytest tests/publishing`（`test_platform_adapter.py` / `test_xiaovmao_cdp.py` / `test_publish_executor.py` / `test_copy_node.py` / `test_copy_llm.py` / `test_account_matching.py` / `test_accounts_repository.py`）
- `pytest tests/integration/test_sqlalchemy_publishing.py`、`tests/api/test_publishing_funnel.py`

## 注意 / 坑
- 生产发布走 `XiaoVmaoPublishAdapter`（CDP 驱动小V猫桌面端），CDP 地址经 env `CUTAGENT_XIAOVMAO_CDP_HOST`（默认 `127.0.0.1`）、`CUTAGENT_XIAOVMAO_CDP_PORT`（默认 `9222`）配置。
- 改发布相关 contract 后须重生成 openapi.json + schema.d.ts。
