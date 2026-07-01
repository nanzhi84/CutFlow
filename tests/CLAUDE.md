# tests

按域组织的 pytest 套件。内存存储后端已移除：**所有碰存储的测试都跑在真实 Postgres 上**。纯逻辑测试不碰库、零开销、也不需要 Postgres。只有 Temporal 集成测试仍通过 env flag 显式 opt-in。

## 布局
- 按域分目录：`api` `core` `creative` `media` `planning` `production` `publishing` `ops` `observability` `providers` `prompts` `connectors` `import` `scripts` `workflow` `frontend` `storage`。其中 `media/annotation/` 是唯一的二级嵌套测试簇（16 个 `test_*.py`）。
- `contract/` — 契约/schema 守卫：OpenAPI 矩阵、DB schema、状态机、错误信封、单一依赖方向（`test_api_contract_matrix.py`/`test_openapi_matrix.py`/`test_database_schema.py`/`test_state_machines.py`/`test_single_source_dependencies.py` 等）。
- 上传/对象存储守卫分散在 `api`（direct upload、presign、object-store backends）、`core`（upload settings/preflight）、`contract`（upload contract/settings/storage backend）、`storage`（object-store lazy）与 `integration/test_oss_direct_upload_real.py`（需真实 S3/OSS env）。
- contract 字段清理守卫在 `contract/test_contract_field_defects.py`；对应迁移回归在 `storage/test_migration_0023_drop_lipsync_fields.py`、`test_migration_0024_drop_output_strictness_fields.py`、`test_migration_0025_drop_broll_overlay_allowed.py`。
- `golden/` — 端到端 golden 流程（用 seeded sandbox provider 跑，存储走真 Postgres）。
- `integration/` — `test_sqlalchemy_*.py`（SQLAlchemy 后端集成，需 Postgres）。已与默认套件合流：不再有 `CUTAGENT_RUN_DB_TESTS` gate，`pytest -q` 默认就跑。（memory↔SQL 的 `test_parity_*.py` 已随内存后端删除。）
- `temporal/` — Temporal 运行时集成（仍 opt-in，需真实 Temporal + 共享 MinIO）。
- `observability/` — Redis fanout/token/provider limiter 跨副本协调与 degrade/reconnect 回归；CI 的 `redis-coordination` job 用真实 Redis 跑 `test_redis_coordination.py` / `test_redis_reconnect.py`。
- `fixtures/` — 共享夹具（`fixtures/media.py` 的 `MediaFixtureFactory` 等）。

## 关键文件
- `conftest.py` — 全局夹具与默认 env：`CUTAGENT_STORAGE_BACKEND=sqlalchemy`、`CUTAGENT_DATABASE_URL`（默认指向一次性 `cutagent_test` 库，CI/ci_gate 显式覆盖）、`CUTAGENT_DISABLE_BACKGROUND_DISPATCHER=1`、`CUTAGENT_ALLOW_SANDBOX_FALLBACK=1`（golden/fallback 夹具走 seeded sandbox）、`CUTAGENT_PUBLISH_ADAPTER=sandbox.publish`（确定性发布适配器；生产默认 `xiaovmao.cdp`）、小连接池。内含进程内 ASGI 测试客户端 `_ASGISyncTestClient`，全局 monkeypatch `fastapi/starlette` 的 `TestClient` 指向它，配合 `_ASGIWebSocketSession` 支持 WebSocket。
- **真库夹具与隔离**：`db_session_factory`（真 Postgres sessionmaker）、`seeded_app`（SQL 后端 FastAPI app）、`client`（`TestClient(seeded_app)`）。隔离是 **autouse + 脏检测**：监听任意引擎的 `checkout` 事件，凡碰库的用例 teardown 自动 `TRUNCATE 全表 + 重 seed 基线`；纯逻辑测试从不 checkout → 零开销、免 DB。**conftest 不跑迁移**，靠外部先 `scripts/bootstrap_database.py` 建库（CI/ci_gate 已做）。

## 约定与要求
- 碰存储的测试默认连真 Postgres；隔离自动完成，**无需**手动加任何 reset/clean 夹具。
- 纯函数测试不碰存储就别引入存储依赖；这类测试不需要 Postgres 在跑。
- 对外部第三方 API（火山/OpenAI/小V猫/dashscope 等 HTTP）仍必须 mock（`FakeDriver`/`_FakeLlmProvider`/`monkeypatch`）——CI 不能真打。这类 mock 与"内存存储后端"无关，保留。
- Redis 行为测试只有设置 `CUTAGENT_REDIS_URL` 才检验跨进程语义；默认套件会覆盖 degrade-in-place/readiness 分支，真实 Redis 覆盖在 CI `redis-coordination` job。
- 契约类断言（`contract/`、`golden/`）是 OpenAPI/schema 漂移与单一事实源的护栏；改契约后这些会失败 → 先按根 `CLAUDE.md` 重新生成 `openapi.json` + `schema.d.ts`。
- 测试默认显式开了 sandbox fallback（与生产相反），勿据此以为生产会回退 sandbox。
- 默认套件**必须串行运行**，已禁用 pytest-xdist/`-n`（`pyproject.toml` 的 addopts 带 `-p no:xdist`，conftest 的 `pytest_configure` 探测到分布式运行即抛 `UsageError`）。原因是全套件共用一次性 `cutagent_test` 库、靠 autouse `TRUNCATE`+reseed 隔离，并行 worker 会互相清表、污染彼此数据。别加 `-n/--dist` 提速。

## 运行
- 前置：起 Postgres（55432）并 `python scripts/bootstrap_database.py` 建库 + seed（只需一次；用一次性测试库，勿指向开发库——conftest 会 TRUNCATE）。
- 默认：`CUTAGENT_DATABASE_URL=… python -m pytest -q`（含集成测试；Temporal 测试缺 flag 时 skip）。
- Temporal（opt-in）：`CUTAGENT_RUN_TEMPORAL_TESTS=1` + 真实 Temporal + 共享 MinIO 对象存储 env → `pytest -q tests/temporal`。
- 完整门禁：`scripts/ci_gate.sh`（镜像 `.github/workflows/ci.yml` 的本地可跑部分：bootstrap → 全量 `pytest -q` → production preflight → openapi/前端 → MinIO+Temporal 段；远端另有 `redis-coordination` 真 Redis job）。

## 注意 / 坑
- conftest **不建表**：必须先 bootstrap 一个已迁移+seed 的库；否则碰库的测试会报表不存在。
- 隔离用 `TRUNCATE`：测试库必须是一次性专用库（CI 用 service 的 `cutagent`，本地默认 `cutagent_test`），**绝不**指向开发库。
- `CUTAGENT_RUN_TEMPORAL_TESTS` 由 `tests/temporal/` 下用例读取；未置则 skip 而非失败。Temporal 测试要求 ephemeral 对象存储指向**共享 MinIO**，节点本地 ephemeral 会被 fail-fast 拒绝。
- 从 worktree 跑测试需正确设置 `PYTHONPATH`（仓库根）并复用主 checkout 的 `.venv`（worktree 默认不带依赖）。
