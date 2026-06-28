# 关键设计决策

这个文件记录会约束未来改动的关键设计决策。它刻意保持短、准、当前有效。

## Case 是产品边界

Case 是品牌约束、脚本、素材、生产运行、成片、指标和学习反馈的持久化边界。

影响：

- 当 Case 级状态可用时，生产流程不要依赖全局账号状态。
- CaseMemory 用来保存人工维护的硬约束和品牌红线。
- 表现反馈和评分校准属于 Case 闭环。

## Contract-first 意味着 FastAPI 拥有 API 形状

FastAPI OpenAPI 是唯一 API schema 来源。前端类型从它生成。

影响：

- 改 routes、request models 或 response models 后必须重新生成 `openapi.json` 和 `schema.d.ts`。
- 不要手改生成的 API 文件。
- 共享领域类型应放在 `packages/core/contracts`，不要散落到 app-specific copy。

## 长流程不跑在 API 进程里

API 请求负责创建和控制 jobs/runs。生产执行属于 workflow runtime 和 worker 进程。

影响：

- 修改 production node 逻辑后需要重启 worker。
- API 和 worker 必须共享同一个 Temporal namespace 与 task queue。
- 跨进程后仍要能观察 node state、artifact output 和 provider invocation。

## Provider 调用走能力接口

节点请求 `tts.speech`、`asr.transcribe`、`llm.chat`、`vlm.annotation`、`lipsync.video`、`image.generate`、`video.generate` 等能力。

影响：

- 节点不直接调用 vendor SDK。
- 真实 provider 路径需要 `ProviderProfile` 和 active secret。
- `CUTAGENT_ALLOW_SANDBOX_FALLBACK=1` 是 demo/test 选择，不是生产默认。
- Provider failure、quota error、budget block 和 circuit-breaker decision 必须可见。

## Prompt 运行时受治理

生产 prompt 文本通过 Prompt Registry 和 binding 解析。

影响：

- 节点不硬编码生产 prompt。
- 生产只解析 published 版本。
- Review、publish、rollback 和 experiment 路径都要保留审计性。

## 降级必须显式

只有当 fallback 被记录为分级 degradation 时，才允许降级。

影响：

- 不要静默跳过 ASR、LipSync、素材覆盖或 provider 失败。
- 一个 run 可以成功，同时仍然是 degraded。
- Public/debug report 都应该让降级路径可理解。

## 素材选择是确定性的

素材选择必须可复现。近期使用通过 ledger 信号降权，但不引入随机性。

影响：

- 不要对 B-roll、portrait、BGM、fonts 或 cover templates 做随机选择。
- 排序必须使用稳定 key。
- 素材覆盖不足时诚实降级，而不是伪造匹配。

## 对象存储属于运行时正确性

Artifacts 通过 URI 引用，可能跨进程或跨主机流转。

影响：

- Durable bucket 与 ephemeral bucket 应该分离。
- Temporal 多 worker 运行需要共享 ephemeral storage。
- Cloud ASR 等 provider 需要可访问或 presigned URL。

## 发布是 adapter 边界

发布状态机属于内部系统，平台自动化放在 adapter 后面。

影响：

- 新平台应新增 adapter，而不是在 repository 内部到处分支。
- 小V猫 CDP 是当前生产 adapter。
- Sandbox publish 必须显式启用，不应该掩盖生产失败。

## 文档是当前摘要，不是历史档案

仓库只保留简洁、长期有效的文档。

影响：

- 不要把带日期的调研 dump 加回 `docs/`。
- milestone 完成后，把长期结论折叠进 `milestones.md`、`technical-choices.md` 或 `design-decisions.md`。
- PR-specific evidence 放在 PR body 或外部 review notes，不放进永久文档。
