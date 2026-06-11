# M4 施工简报：事件面 + 观测面

负责：Codex（执行）/ Claude（架构 + 验收）
分支：`feat/m4-events-observability`
Spec：1A.5（行 147-163，Observability Contract + outbox 最低字段）、15.3（RunEvent）、第 2.1 章「WebSocket 进度：必须」。
审计依据：critic 横切缝隙 a/d/e/g（WebSocket 整体缺失、request_id 不贯穿已在 M1 修、零结构化日志、/metrics 硬编码 0、Redis 零引用）；observability-deps 审计「事件双轨：pipeline 业务事件只进内存 dict」。

## Goal

所有业务事件统一落 DB outbox（可恢复、幂等、稳定排序），WebSocket 只消费 outbox；
结构化日志与 Prometheus 指标按 1A.5 最低契约真实化。

## 关键设计决定（架构师已定）

- 本批 fanout 用**进程内 dispatcher 轮询 outbox**（API 进程后台 task）。Redis/NATS fanout 是多副本
  部署时的扩展，本批不做，记入 docs/spec-questions.md（spec 1A.5 允许 Redis fanout，但消费端只读
  outbox 的硬规则本批就要成立）。
- 日志用标准库 logging + 自写 JSON formatter（不引第三方 structlog）。
- 指标用 prometheus_client（venv 已预装 0.25.0）。
- worker（Temporal activity）侧事件在与业务写入同一个 DB 事务里写 outbox。

## 改动清单（逐条核销）

### A. Outbox 统一

- A1 `outbox_events` 表对齐 spec 1A.5 最低字段全集：`id`、`topic`、`aggregate_type`、`aggregate_id`、
  `payload_schema`、`payload`(JSONB)、`status`(pending/published/failed)、`attempts`、`dedupe_key`、
  `available_at`、`created_at`、`published_at`、`last_error`；`(aggregate_type, aggregate_id, topic, dedupe_key)`
  唯一索引（幂等）；replay 按 `created_at, id` 稳定排序。
- A2 事件写入点全部收口到 `OutboxWriter`（与业务变更同事务）：run 状态变更、node_run 状态变更、
  artifact created、finished_video created、yield funnel 事件。内存模式提供同语义实现。
- A3 `YieldFunnelEvent` 补齐 spec 26.1 最低字段（`job_id`、`finished_video_id`、`publish_package_id`、
  `publish_attempt_id`、`event_type`、`event_time`、`dedupe_key`），经 outbox + 表双写收口为单链路
  （事件表由 outbox 消费者投影或直接同事务写，二选一但只能有一条写路径）。

### B. Dispatcher + WebSocket

- B1 进程内 `OutboxDispatcher`：API lifespan 启动的后台 asyncio task，按稳定排序轮询 pending 事件，
  投递到进程内 fanout hub，成功置 published/attempts+1/published_at；失败记 last_error、按 available_at 退避重试。
- B2 `GET /api/runs/{run_id}/events` 已返回 `EventStreamTokenResponse`（契约已有）：实现签发短时 token。
- B3 新增 WS endpoint `/ws/runs/{run_id}`（带 token 校验）：连接后先回放该 run 历史事件（从 outbox 读，
  稳定排序），再实时推送；消息体为 spec 15.3 `RunEvent` 契约（event_id/run_id/job_id/event_type/
  node_id/status/progress/message/created_at）。
- B4 前端不在本批改造；WS 可用 TestClient 的 websocket_connect 做进程内测试（local 模式）。

### C. 结构化日志

- C1 JSON logging formatter + `configure_logging()`：每条日志含 1A.5 最低字段
  （request_id/trace_id/user_id/case_id/job_id/run_id/node_run_id/provider_invocation_id/prompt_invocation_id），
  无上下文填 null——用 contextvars 注入，API middleware 设置 request 级上下文，worker activity 设置 run/node 级上下文。
- C2 API 访问日志（method/route/status/duration_ms）+ 错误日志接入统一 formatter；替换现存 print。

### D. Prometheus 指标真实化

- D1 用 prometheus_client 实现并暴露 `/metrics`：`api_request_duration_seconds`(histogram)、
  `api_request_errors_total`、`workflow_run_duration_seconds`、`node_run_duration_seconds`、
  `node_run_retries_total`、`provider_invocation_duration_seconds`、`provider_invocation_failures_total`、
  `provider_cost_estimated_total`、`provider_unpriced_invocations_total`、`yield_funnel_events_total`、
  `outbox_lag_seconds`(gauge)。删掉 telemetry.py 里硬编码 0 的假快照。
- D2 API middleware 记录请求指标；NodeRunner/gateway 记录 node/provider 指标；dispatcher 记录 outbox lag。

### E. 测试

- E1 单测（进程内，无需 DB/Temporal）：outbox 写入幂等（同 dedupe_key 不重复）、dispatcher 投递顺序稳定、
  WS 连接回放+实时（TestClient websocket）、日志字段完整性（capture handler 断言 JSON 字段）、
  /metrics 在请求后计数增长。
- E2 DB 集成（门控，验收官跑）：outbox 落表、重放幂等、dispatcher 在 sqlalchemy 模式下工作。
- E3 Temporal 集成（门控，验收官跑）：temporal 模式下 worker 写 outbox，API 侧 WS 能收到 node_update。

## 边界（Out of scope）

- Redis/NATS fanout、OpenTelemetry trace 导出、Sentry 接入（记 spec-questions，M5+ 裁决）；
- 前端 WS 消费改造；ops 公式修正（M6）；告警规则引擎（M6）。

## Verification（sandbox 内）

- `timeout -k 5 600 /home/nanzhi/projects/cutagent-genesis/.venv/bin/python -m pytest -q` 全绿（基线 78 passed, 16 skipped）。
- E1 全部可在你的 sandbox 内跑（进程内）。
- OpenAPI 导出：新增 WS/token 相关允许，不得删改既有。
- DB/Temporal 集成连接失败属环境限制，记录留验收官。

## 验收门（验收官执行）

1. local 模式：WS 进程内测试回放+实时推送全绿。
2. sqlalchemy 模式：outbox 落表、同 dedupe_key 幂等、按 created_at,id 顺序投递。
3. temporal 模式：真 worker 跑 run，API WS 实时收到 node_update 与 run_update。
4. /metrics 输出真实计数（发请求后增长），无硬编码 0 假指标。
5. 日志 JSON 含最低字段集（无上下文为 null 而非缺字段）。
6. 三套测试全绿。
