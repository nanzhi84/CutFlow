# 施工 Roadmap（2026-06-11 审计后定版）

依据：`docs/audit/spec-gap-audit-2026-06-11.json`（14 路子系统审计 + 横切 critic）。
基线：commit `eb68023`，54 测试绿（34 默认 + 20 DB 集成），前端可构建。

## 总判断

骨架与契约方向正确（contract-first、强类型 union、OpenAPI 生成 client），但存在四个**结构性**问题，
必须先于一切业务功能修复，否则每天都在制造要迁移的脏数据和假绿测试：

1. **契约层漂移**：Money 用 float、ProviderStatus 整套换掉、ArtifactKind 持久化值与 spec 32 章终值冲突、
   请求无 schema_version、状态机只有枚举没有迁移表、32.6 明令禁止的裸 dict 多处存在。
2. **双真相源脑裂**：默认内存 repo + 可选 SQLAlchemy 并行；`_GATEWAY`/`PromptRegistry`/`main.py repo`
   等 module 级单例永远绑内存——后台写 DB 的 prompt/profile 对运行时不可见。Idempotency 只在进程内存。
3. **架构倒置**：16 节点流水线在 API 请求 handler 内同步跑完，POST 返回时 run 已终态；
   temporalio 不在依赖里，worker 是 print 占位——cancel/resume 结构上不可能为真。
4. **事件与观测面为零**：无 WebSocket、无结构化日志、/metrics 硬编码 0、Redis 零引用、
   outbox 只覆盖 run 状态一种事件。

另有一批"假语义"（发布无条件 succeeded、annotation rerun 伪造 completed、timeline validation 硬编码
全过、true_yield 公式用错分母）在地基修好后逐一修真。

## Milestones

| # | 名称 | 内容 | 验收门 |
|---|---|---|---|
| M1 | 契约定版冻结 | `packages/core/contracts` 按 spec 修复全部漂移；共享状态机迁移表；统一错误体（含 422 handler、request_id 贯穿）；artifact payload 类型注册表 | 全测试绿；契约 diff 对照 spec 逐条核销；此后契约改动需架构师签字 |
| M2 | 单一真相源 | Postgres 默认后端；内存 repo 降级为测试 fixture；消灭 module 级单例（DI）；idempotency 落表；registration_code→code_hash；secrets→secret_ref 模型；main.py 单体拆 routers + use-case services | DB 重启后 idempotency 仍生效；gateway/registry 读 DB；main.py < 300 行 |
| M3 | 执行迁出 API | temporalio 依赖 + 真 worker + WorkflowRuntimeAdapter 的 Temporal 实现；API 提交后立即返回 queued；cancel/resume 走 signal；修 `_reuse_prefix` missing-artifact bug | 跑通：提交→worker 执行→cancel 中途生效→resume 复用合法前缀 |
| M4 | 事件面 + 观测面 | 全事件落 DB outbox（dedupe_key/append-only/稳定排序）；WebSocket run events；结构化日志按 1A.5 最低字段；真 Prometheus 指标 | WS 能实时收到 node_update；outbox 重放幂等 |
| M5 | 验收闸门 | CI（真 Postgres、integration 默认必跑）；golden ≥12 条；写接口 contract tests（2xx/4xx/权限/idempotency）；OpenAPI 导出 diff 检查 | CI 全绿且覆盖率达 spec 20 章清单 |
| M6+ | 语义修真系列 | 发布走真 adapter 边界、annotation 真排队、timeline 真校验+30fps 帧量化、planning 节点归位 packages/planning、媒体处理真 FFmpeg、ops 公式修正、前端 caseId 路由 | 按 spec 20.6 最终验收八条逐项过 |

## 施工纪律（验收官口径）

- 每个 milestone 一个 worktree 分支（Codex 任务用 `.codex/worktrees/<id>/<repo>`；Claude 验收/协作任务用 `.claude/worktrees/<slug>`；分支如 `feat/<slug>`），Codex 执行，Claude 验收后合 main。
- 验收 = 测试绿 + 对照简报逐条核销 + 抽查代码语义（不信"端点存在"，只信"语义为真"）。
- 禁止：为让测试绿而放宽契约；新增 module 级单例；在 API handler 里做重活；裸 dict 当业务契约。
- 任何 spec 冲突或不明确之处，记入 `docs/spec-questions.md`，由架构师裁决后写回 spec 或本文件，不口头协商。
