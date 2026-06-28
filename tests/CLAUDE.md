# tests

按域组织的 pytest 套件。默认只跑不依赖外部基础设施的单测；DB / Temporal 集成测试通过 env flag 显式 opt-in。

## 布局
- 按域分目录：`api` `core` `creative` `media` `planning` `production` `publishing` `ops` `observability` `providers` `prompts` `connectors` `import` `scripts` `workflow` `frontend` `storage`。其中 `media/annotation/` 是唯一的二级嵌套测试簇（16 个 `test_*.py`）。
- `contract/` — 契约/schema 守卫：OpenAPI 矩阵、DB schema、状态机、错误信封、单一依赖方向（`test_api_contract_matrix.py`/`test_openapi_matrix.py`/`test_database_schema.py`/`test_state_machines.py`/`test_single_source_dependencies.py` 等）。
- `golden/` — 端到端 golden 流程（用 seeded sandbox provider 跑）。
- `integration/` — `test_sqlalchemy_*.py`（SQLAlchemy 后端集成）与 `test_parity_*.py`（parity 集成：`test_parity_run.py` 后端 / `test_parity_mapper.py` mapper），均需 Postgres，同走 `CUTAGENT_RUN_DB_TESTS` gate。
- `temporal/` — Temporal 运行时集成。
- `fixtures/` — 共享夹具（`fixtures/media.py` 的 `MediaFixtureFactory` 等）。

## 关键文件
- `conftest.py` — 全局夹具与默认 env：`CUTAGENT_STORAGE_BACKEND=memory`、`CUTAGENT_DISABLE_BACKGROUND_DISPATCHER=1`、`CUTAGENT_ALLOW_SANDBOX_FALLBACK=1`（让 golden/fallback 夹具走 seeded sandbox）、`CUTAGENT_PUBLISH_ADAPTER=sandbox.publish`（测试用确定性发布适配器；生产默认 `xiaovmao.cdp`），并把对象存储指向临时目录；内含进程内 ASGI 测试客户端 `_ASGISyncTestClient`，并全局 monkeypatch `fastapi/starlette` 的 `TestClient` 指向它，配合 `_ASGIWebSocketSession` 支持 WebSocket。

## 约定与要求
- 默认套件**不得**依赖外部 infra：用内存后端 + sandbox provider，跑在 `pytest -q`（`pyproject.toml` 已配 `pythonpath=["."]` / `testpaths=["tests"]`）。
- 需要真实 infra 的测试必须在文件内自带 env gate（`pytest.skip` 当 flag 未置），不要无条件依赖 DB/Temporal/MinIO。
- 契约类断言（`contract/`、`golden/`）是 OpenAPI/schema 漂移与单一事实源的护栏；改契约后这些会失败 → 先按根 `CLAUDE.md` 重新生成 `openapi.json` + `schema.d.ts`。
- 测试默认显式开了 sandbox fallback（与生产相反），勿据此以为生产会回退 sandbox。

## 运行
- 默认：`python -m pytest -q`；完整门禁脚本会给 pytest 段加 600s 超时保护。
- DB 集成（opt-in）：`CUTAGENT_RUN_DB_TESTS=1` + `CUTAGENT_STORAGE_BACKEND=sqlalchemy` + `CUTAGENT_DATABASE_URL=…` → `pytest -q tests/integration`
- Temporal（opt-in）：`CUTAGENT_RUN_TEMPORAL_TESTS=1` + 真实 Temporal + 共享 MinIO 对象存储 env → `pytest -q tests/temporal`
- 完整门禁：`scripts/ci_gate.sh`（镜像 `.github/workflows/ci.yml`）。

## 注意 / 坑
- `CUTAGENT_RUN_DB_TESTS` 由 `tests/integration/` 下用例（`test_sqlalchemy_*.py` + `test_parity_*.py`）读取，`CUTAGENT_RUN_TEMPORAL_TESTS` 由 `tests/temporal/` 下用例（`test_temporal_runtime.py` + `test_parity_temporal.py`）读取；未置则这些用例 skip 而非失败。
- Temporal 测试要求 ephemeral 对象存储指向**共享 MinIO**，节点本地 ephemeral 会被 fail-fast 拒绝。
- 从 worktree 跑测试需正确设置 `PYTHONPATH`（仓库根）并复用主 checkout 的 `.venv`（worktree 默认不带依赖）。
