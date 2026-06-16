# apps/api

FastAPI 服务层:面向 Web 前端的 HTTP API,把 case-first 数字人生产能力暴露成 REST 接口;router 薄,业务逻辑落在 services,底层 domain 在 `packages/*`。

## 职责
- 注册并挂载所有 router(`create_app` / `ROUTER_MODULES`),统一鉴权中间件、错误处理、可观测日志。
- `configure_app_state` 装配运行期依赖到 `app.state`:Repository、各 SqlAlchemy repo、object_store、secret_store、ProviderGateway、PromptRegistry、workflow runtime(local 或 temporal)、outbox dispatcher。
- 通过 `dependencies.authenticate_api_request` 中间件做 session cookie 鉴权 + Idempotency-Key 去重/重放(spec 32.11);route 内用 `require_role` 做 RBAC(viewer/operator/admin)。
- 作为系统的 OpenAPI 唯一真源(FastAPI 自动生成),前端类型由它派生。

## 关键文件 / 子目录
- `app.py` — `create_app` / `configure_app_state` / lifespan(启动 outbox dispatcher + balance poller)
- `main.py` — ASGI 入口 `apps.api.main:app`;直跑绑定 `0.0.0.0:8000`
- `dependencies.py` — 鉴权中间件、`require_role`、异常处理器(NodeExecutionError/HTTPException/校验错误 → ErrorEnvelope)
- `common.py` — `app.state` 取值帮手(repo/store/auth/workflow…)、`request_id`、`get_case`、分页
- `routers/` — 薄路由,按域分组:auth/cases/creative/jobs_runs/media/voices/prompts/providers/case_agent/finished_videos/publishing/ops/imports/secrets/uploads/cost_estimate/core(health+metrics)
- `services/` — 业务逻辑;`media_processing.py`(编排 ffmpeg 稳像/剪辑 + 标注时长对账)、`case_agent_llm.py`(case agent 脚本生成)

## 约定与要求
- Contract-first:contracts 来自 `packages.core.contracts`(`as c`),不在本层另立 schema。
- router 保持薄,只做鉴权/参数绑定后转调 `services.*`;副作用走 service。
- 任何改动 API contract(路由/请求/响应模型)后,必须重新生成 `apps/web/src/api/openapi.json`(`python scripts/export_openapi.py`)+ `schema.d.ts`(`npm run generate:api`);CI(`scripts/ci_gate.sh`)用 `git diff --exit-code` 校验二者无漂移。
- 错误一律抛 `NodeExecutionError(ErrorCode, …)`,由 handler 映射到 HTTP 状态 + `ErrorEnvelope`,不要直接构造响应。

## 测试
- `pytest tests/api`(conftest 默认置 `CUTAGENT_ALLOW_SANDBOX_FALLBACK=1`)。

## 注意 / 坑
- 进程内 outbox dispatcher 随 lifespan 启停;`CUTAGENT_DISABLE_BACKGROUND_DISPATCHER=1`(→ `settings.api.disable_background_dispatcher`)关闭(测试/外部 worker 场景)。
- balance poller 默认 no-op,受 `CUTAGENT_BALANCE_POLLER_ENABLED` / `settings.balance.poller_enabled` 控制。
- 无 session_factory 时退化到内存 `Repository`(各 sqlalchemy_* 为 None);真实运行需 SqlAlchemy 存储已 bootstrap。
- sandbox 回退由 `sandbox_fallback_allowed()` / `CUTAGENT_ALLOW_SANDBOX_FALLBACK` 控制(默认 OFF=显式失败)。
- workflow 是否走 Temporal 取决于 `workflow_runtime_settings.runtime`;媒体/标注真实产出依赖独立 worker 进程。
