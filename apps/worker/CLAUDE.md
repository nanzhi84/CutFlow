# apps/worker

Temporal worker 进程：连接 Temporal，注册数字人成片（digital-human production）的 workflow 与 activity，在任务队列 `cutagent-production` 上长驻消费。与 API 是**两个独立进程**。

## 职责
- `Client.connect` 连接 Temporal，在 `settings.temporal_task_queue` 上启动 `Worker`，注册 `temporal_workflows()` / `temporal_activities()`。
- `bootstrap_sqlalchemy_storage()` + `get_sqlalchemy_session_factory()` 初始化 SQLAlchemy 存储并取得 `session_factory`。
- 装配 worker 级「无状态模板」运行时：`ProviderGateway`（secret store：SQL 后端用 `SqlAlchemySecretStore`（密钥活性以 DB 为准），否则 `LocalSecretStore`；并挂 `BudgetEnforcementGuard`（预算硬阻断）+ `ProviderCircuitBreaker`（熔断））、`PromptRegistry`、`build_digital_human_workflow(...)`（默认 `seed_media=True`，会一次性 ffmpeg 生成 demo 媒体）。
- 经 `configure_temporal_activity_context(TemporalActivityContext(...))` 注入 repository / local_runtime / production_repository。
- 就绪后打日志 `Cutagent Temporal worker ready`（logger `cutagent.worker`，event `worker_ready`）。

## 关键文件
- `main.py` — `async_main()` 装配并 `worker.run()`；全部逻辑在此。
- `__main__.py` — `python -m apps.worker` 入口，转调 `main()`。

## 约定与要求
- worker 全局 runtime 只是**无状态服务模板**；每个 activity 经 `TemporalActivityContext.build_runtime()` 建**全新隔离的 `Repository`**，并发 run 不得共享可变 run-state（见 `main.py` 注释）。
- provider/prompt reader 仅在 `session_factory` 非空（SQL 后端启用）时挂载，否则降级为 `None`。
- prompt/provider 不在此硬编码，统一走 `PromptRegistry` / `ProviderGateway` 读 DB 绑定。

## 测试
- `tests/temporal/test_activity_repository_scoping.py` — 纯单测（monkeypatch，无外部依赖），无条件运行。
- `tests/temporal/test_temporal_runtime.py` — 集成用例，需 `CUTAGENT_RUN_TEMPORAL_TESTS=1` 才运行，且需 `CUTAGENT_STORAGE_BACKEND=sqlalchemy` + 真实 Temporal。

## 注意 / 坑
- **独立长驻进程，改代码必须重启 worker**，不随 API 热更。
- 设置经 `load_workflow_runtime_settings()`（`packages/core/workflow/runtime.py` 的 `WorkflowRuntimeSettings`）从 `WorkflowSettings`（`packages/core/config/settings.py`，`settings.workflow.*`）读取；env：`CUTAGENT_WORKFLOW_RUNTIME` + `CUTAGENT_TEMPORAL_ADDRESS`/`_NAMESPACE`/`_TASK_QUEUE`（默认 `127.0.0.1:7233` / `default` / `cutagent-production`）。
- activity 执行用 `ThreadPoolExecutor(max_workers=8)`，activity 实现需线程安全。
- task queue 两端必须一致：API 派发与 worker 消费的 `CUTAGENT_TEMPORAL_TASK_QUEUE` 不匹配则 run 永远 pending。
