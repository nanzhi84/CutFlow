# apps/api

FastAPI 服务层:面向 Web 前端的 HTTP API,把 case-first 数字人生产能力暴露成 REST 接口;router 薄,业务逻辑落在 services,底层 domain 在 `packages/*`。

## 职责
- 注册并挂载所有 router(`create_app` / `ROUTER_MODULES`),统一鉴权中间件、错误处理、可观测日志。
- lifespan 在 bootstrap/连接外部依赖前运行 production preflight，发现不安全生产配置即 fail closed；`/api/health/ready` 在生产下也会把 preflight findings 映射成 503。
- `configure_app_state` 装配运行期依赖到 `app.state`:Repository（仅 workflow 临时 run-state）、各 SqlAlchemy repo（含 uploads/media/production/publishing/ops）、object_store、secret_store、ProviderGateway、PromptRegistry、workflow runtime(local 或 temporal)、outbox dispatcher;还装配 `xiaovmao_login_manager`(小V猫 CDP 登录管理,平台会话只在小V猫,不进 SecretStore/DB)、`event_hub`/`event_tokens`(可接 Redis 的 fanout + token store,事件流推送)。
- 通过 `dependencies.authenticate_api_request` 中间件做 session cookie 鉴权 + Idempotency-Key 去重/重放；Idempotency-Key 只支持小 JSON 控制面写请求，会按 `CUTAGENT_IDEMPOTENCY_MAX_BODY_BYTES`/`_MAX_RESPONSE_BYTES` 拒绝大 body、binary、chunked 超限或不缓存大响应；route 内用 `require_role` 做 RBAC(viewer/operator/admin)。
- 作为系统的 OpenAPI 唯一真源(FastAPI 自动生成),前端类型由它派生。
- 提供运维健康探针：`/api/health/ready`(不进 OpenAPI) 做 preflight/Redis-required readiness，`/api/health/network`(公开、超时保护) 探测 Postgres/Redis/OSS/Temporal 分段链路。

## 关键文件 / 子目录
- `app.py` — `create_app` / `configure_app_state` / lifespan(启动 outbox dispatcher + balance poller)
- `main.py` — ASGI 入口 `apps.api.main:app`;直跑绑定 `0.0.0.0:8000`
- `dependencies.py` — 鉴权中间件、`require_role`、异常处理器(NodeExecutionError/HTTPException/校验错误 → ErrorEnvelope)
- `common.py` — `app.state` 取值帮手(repo/store/auth/workflow…)、`request_id`、`get_case`、分页
- `routers/` — 薄路由,按域分组:auth/cases/creative/jobs_runs/media/voices/prompts/providers/case_agent/case_rubric/finished_videos/publish_accounts/publishing/ops/imports/secrets/uploads/core(health+metrics+network diagnostics)
- `services/` — 业务逻辑；`uploads.py`(presigned PUT 直传上传的 prepare/complete/cancel/get，complete 做 HEAD/sha256/content-type/媒体探测/可选 normalize+stabilize 后登记 artifact/asset/package)、`media_processing.py`(编排 ffmpeg 稳像/剪辑 + 标注时长对账)、`case_agent_llm.py`(case agent 脚本生成)、`publish_login.py`(发布登录,驱动小V猫 CDP)、`publishing_nodes.py`(发布节点编排)

## 约定与要求
- Contract-first:contracts 来自 `packages.core.contracts`(`as c`),不在本层另立 schema。
- router 保持薄,只做鉴权/参数绑定后转调 `services.*`;副作用走 service。
- 上传必须保持浏览器直传:后端只签发 `PrepareUploadResponse.put_url` 并在 complete 阶段验证对象存储结果;不要新增 API 二进制/FormData 代理路径。
- `/api/runs/{run_id}/events` 只签发短期 stream token；WebSocket `/ws/runs/{run_id}` 可带 `?after=<event_id>`，服务端从 SQL outbox cursor 之后 replay，未知 cursor 回退全量 replay。
- 任何改动 API contract(路由/请求/响应模型)后,必须重新生成 `apps/web/src/api/openapi.json`(`uv run --extra dev python scripts/export_openapi.py`)+ `schema.d.ts`(`npm run generate:api`);CI(`scripts/ci_gate.sh`)用 `git diff --exit-code` 校验二者无漂移。
- 错误一律抛 `NodeExecutionError(ErrorCode, …)`,由 handler 映射到 HTTP 状态 + `ErrorEnvelope`,不要直接构造响应。

## 测试
- `pytest tests/api`(conftest 默认置 `CUTAGENT_ALLOW_SANDBOX_FALLBACK=1`)；上传/对象存储重点见 `test_upload_direct_flow.py`、`test_upload_object_store.py`、`test_object_store_presign.py`、`test_upload_failure_cleanup.py`、`test_upload_complete_reject_branches.py`；幂等 body/response 上限见 `test_idempotency_body_cap.py`，Redis readiness 见 `test_health_redis_required.py`。

## 注意 / 坑
- 进程内 outbox dispatcher 随 lifespan 启停;`CUTAGENT_DISABLE_BACKGROUND_DISPATCHER=1`(→ `settings.api.disable_background_dispatcher`)关闭(测试/外部 worker 场景)。
- Redis 是跨副本协调层：`CUTAGENT_REDIS_URL` 配置后 fanout/token/limiter 共享；`CUTAGENT_REDIS_REQUIRED=1` 且任一组件退化时 readiness 503，但单请求路径仍 fail-safe 回进程内模式。
- balance poller 默认 no-op,受 `CUTAGENT_BALANCE_POLLER_ENABLED` / `settings.balance.poller_enabled` 控制。
- SQL 后端强制:`configure_app_state` 必有 session_factory(缺 `CUTAGENT_DATABASE_URL` 显式启动失败),各 sqlalchemy_* 恒挂载;`runtime_repository` 保留为工作流运行态基底(非存储后端)。内存存储后端已移除。
- sandbox 回退由 `sandbox_fallback_allowed()` / `CUTAGENT_ALLOW_SANDBOX_FALLBACK` 控制(默认 OFF=显式失败)。
- workflow 是否走 Temporal 取决于 `workflow_runtime_settings.runtime`;媒体/标注真实产出依赖独立 worker 进程。
