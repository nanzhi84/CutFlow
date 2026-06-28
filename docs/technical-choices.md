# 关键技术选型

这个文件只记录会长期影响系统形态的技术选型。临时方案、调研草稿和 PR 证据不要放在这里。

## API 与契约

FastAPI 是 API 事实源。OpenAPI 从后端应用导出，再生成前端 TypeScript 类型。

为什么这样选：

- 后端统一拥有请求和响应形状。
- 前端消费生成类型，而不是手写 DTO。
- CI 可以发现 OpenAPI 或生成 schema 漂移。

关键路径：

- `apps/api/app.py`
- `scripts/export_openapi.py`
- `apps/web/src/api/openapi.json`
- `apps/web/src/api/schema.d.ts`
- `packages/core/contracts`

## 数据存储

PostgreSQL + SQLAlchemy 是默认持久化路径。`postgres` 后端名是 SQLAlchemy 的别名，`memory` 后端只用于测试和 demo。

为什么这样选：

- 生产状态必须跨进程重启保留。
- 幂等、outbox、artifact、provider 用量、预算和审计都需要持久化。
- 测试仍需要快速的内存路径。

关键路径：

- `packages/core/storage/database.py`
- `packages/core/storage/bootstrap.py`
- `packages/core/storage/alembic/versions/`

## 工作流运行时

Temporal 是生产长流程运行时。Local runtime 保留给测试和受控本地开发。

为什么这样选：

- 视频生产太长、太容易受外部失败影响，不适合绑在请求处理器里。
- 取消、重试、恢复和节点级可见性需要工作流语义。
- API 和 worker 应该能独立重启。

关键路径：

- `apps/worker/main.py`
- `packages/core/workflow/runtime.py`
- `packages/core/workflow/temporal_adapter.py`
- `packages/production/pipeline/`

## 前端

控制台使用 React + Vite + TypeScript，配合 TanStack Query、React Router 和 Tailwind。

为什么这样选：

- 产品是高频操作控制台，不是一次性页面。
- 生成 API 类型能让前后端漂移可见。
- Query 缓存和路由级页面符合工作台形态。

关键路径：

- `apps/web/src/App.tsx`
- `apps/web/src/routes.ts`
- `apps/web/src/api/client.ts`
- `apps/web/src/api/r6.ts`

## 对象存储

生成媒体和中间 artifact 通过 ObjectStore 抽象保存，支持本地与 S3 兼容实现。Tiered storage 把长期输出和临时 scratch artifact 分开。

为什么这样选：

- 大媒体不应该塞进数据库行。
- 云 provider 经常需要可访问 URL。
- Temporal 多 worker 场景下，连续 activity 可能落在不同主机，所以临时存储也需要共享。

关键路径：

- `packages/core/storage/object_store.py`
- `packages/core/storage/object_store_env.py`
- `packages/core/storage/tiered_object_store.py`

## 媒体栈

媒体处理以 ffmpeg/ffprobe 为底座，由 Python 编排。标注和规划优先走确定性逻辑，配置后再接入 provider-backed VLM。

为什么这样选：

- ffmpeg 是音视频处理的稳定底座。
- 规划必须确定、可复现、可审计。
- provider 增强能力必须显式且可观测。

关键路径：

- `packages/media/`
- `packages/planning/`
- `packages/production/pipeline/nodes/`

## Provider 层

外部 AI 和媒体供应商都封装为 `ProviderGateway` 后面的 provider plugin。

当前真实 provider 家族包括 MiniMax、Volcengine、DashScope、RunningHub HeyGem、Ark Seedance 和 OpenAI image generation。

为什么这样选：

- 节点调用能力，不直接调用 vendor API。
- Provider profile、active secret、预算保护和熔断可以集中执行。
- Sandbox 保留给测试和 demo，但不伪装成生产能力。

关键路径：

- `packages/ai/gateway/`
- `packages/ai/providers/`
- `packages/ops/budget_guard.py`
- `packages/ops/circuit_breaker.py`

## 发布

发布侧采用 adapter 边界。生产默认小V猫 CDP，sandbox publish 需要显式打开。

为什么这样选：

- 平台自动化应该和发布状态机隔离。
- 真实发布失败必须暴露，不能变成假成功。
- 文案与封面生成可以独立于平台提交演进。

关键路径：

- `packages/publishing/platform_adapter.py`
- `packages/publishing/connectors/xiaovmao_cdp.py`
- `packages/publishing/publish_executor.py`
- `packages/publishing/copy_node.py`
- `packages/publishing/cover_node.py`

## 验证

本地和远端门禁覆盖不同风险带：

- `python -m pytest -q` for default unit and contract safety.
- `tests/integration` for SQLAlchemy/Postgres behavior.
- `tests/temporal` for Temporal runtime behavior.
- `npm run build` and strict `tsc` for frontend.
- `scripts/ci_gate.sh` as the local broad gate.
