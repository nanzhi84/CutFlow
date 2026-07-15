# Milestones

这个文件记录当前仍然有效的产品与架构里程碑。它不是实现流水账，也不保存一次性清理证据。

## M1 · 契约基础

目标：把接口、状态机、artifact、错误体和前后端类型收束到单一契约体系。

当前状态：

- FastAPI OpenAPI 是 API 事实源。
- 前端 `openapi.json` 和 `schema.d.ts` 从后端生成。
- 领域模型集中在 `packages/core/contracts`。
- API 形状变更必须重新导出 OpenAPI 并生成 TypeScript 类型。

## M2 · 持久化基础

目标：把运行时数据落到 SQLAlchemy/Postgres 路径，避免生产依赖内存态。

当前状态：

- 默认存储后端是 `sqlalchemy`。
- Alembic 迁移只在 `packages/core/storage/alembic/versions/`。
- 当前单一 migration head 是 `0062_drop_v1_prompts`。
- `memory` 后端保留给测试和 demo。

## M3 · 工作流运行时

目标：把长流程生产从 API 请求生命周期中拆出去，由 workflow runtime 承载。

当前状态：

- API 可在 local 和 Temporal runtime 间切换。
- `apps/worker` 是独立 Temporal worker。
- Worker 使用 `CUTAGENT_TEMPORAL_TASK_QUEUE` 消费任务。
- Temporal 模式要求共享 durable/ephemeral ObjectStore。

## M4 · 事件与可观测性

目标：生产过程可追踪、可回放、可审计。

当前状态：

- Run / node 状态、artifact、provider invocation、warning、degradation 都进入运行报告。
- Outbox dispatcher 支撑事件分发。
- `/ws/runs/{run_id}` 提供运行进度事件。
- `/metrics` 暴露 Prometheus 指标。
- Ops 域记录成本、成品率、预算、告警、质检、审批和审计事件。

## M5 · 验证门禁

目标：用本地和远端门禁守住契约、后端、前端和生产路径。

当前状态：

- 默认 pytest 覆盖不依赖外部基础设施的单测。
- DB 集成测试已并入默认 pytest 套件（需 Postgres，不再 opt-in）。
- Temporal 测试通过 `CUTAGENT_RUN_TEMPORAL_TESTS=1` opt-in。
- MinIO multipart 恢复测试通过 `CUTAGENT_RUN_S3_TESTS=1` 显式运行，并已进入本地/远端门禁。
- `scripts/ci_gate.sh` 镜像主要 CI 门禁。
- GitHub Actions 包含 `unit`、`integration`、`frontend` jobs。

## M6 · 生产语义

目标：把数字人生产、素材选择、真实 provider、发布和运营治理变成可重复运行的系统能力。

当前状态：

- 主生产模板是 `digital_human_v2`。
- 活动扩展模板包括 `digital_human_editing_agent_v2` 和 `seedance_t2v_v1`；历史 `digital_human_editing_agent_v1` 已退出注册且不可恢复；纯 B-roll 画外音走主链 `broll.mode="full_coverage"`。
- 素材选择确定性执行，并用 selection ledger 做近期降权。
- Provider 调用经 `ProviderGateway`，prompt 经 `PromptRegistry`。
- 真实 provider 需要 profile 和 active secret；sandbox fallback 只在显式配置时启用。
- 发布侧经 adapter 隔离，生产默认小V猫 CDP。

## M7 · 文档与仓库清洁度

目标：让仓库结构、文档和验证方式保持可读、可维护、可审查。

当前状态：

- 旧的详细规格长文、历史 milestone 日志、临时计划、审计 dump、旧 ops 小文档已移除。
- README 作为产品和上手入口。
- `docs/` 只保留 milestone、关键技术选型、关键设计决策。
- 清理 PR 不再把临时证据作为长期文档保存。
