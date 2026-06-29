# 删除 memory 存储后端 · 全部测试连真 Postgres — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（fan-out 迁移阶段）。Phase 1 由主代理 inline 完成。Steps 用 `- [ ]` 勾选跟踪。

**Goal:** 删除 memory 作为存储后端选项，让所有碰存储的测试连真实 Postgres，生产/测试统一走 SQLAlchemy 后端。

**Architecture:** 三阶段。Phase 1 主代理串行建「真库 conftest 夹具」keystone + 移除生产 memory 降级分支（强交叉依赖，高风险）。Phase 2 按域 fan-out 子代理迁移 ~75 个测试文件（每域独立、本地真库跑绿）。Phase 3 改 CI/门禁 + 双子代理审 + PR。

**Tech Stack:** Python 3 / pytest / FastAPI / SQLAlchemy 2 / Alembic / Postgres 16 (pgvector) / Temporal / MinIO。

## Global Constraints（每个任务都隐含遵守）

- 真实数据库 = **Postgres**（schema 用 `JSONB`/`ARRAY`/`Vector(1536)` pgvector，不能用 SQLite）。本地/CI 端口 **55432**，镜像 `pgvector/pgvector:pg16`。
- **保留**：内存 `Repository` 类（生产运行态基底）、`LocalSecretStore`（SQL 模式 fallback）、sandbox provider/`sandbox.publish`、`sandbox_fallback_allowed()` 机制、测试对外部 HTTP 的 `FakeDriver`/monkeypatch。
- **不改 API contract 形状**（本任务以测试/接线为主）；万一动到，必须 `uv run --extra dev python scripts/export_openapi.py` + `(cd apps/web && npm run generate:api)` 重生成，CI `git diff --exit-code` 校验。
- 迁移**不得静默改测试语义**：被测断言的业务含义保持不变，只换数据访问方式（memory→真 Postgres）。
- DB 迁移只在 `packages/core/storage/alembic/versions/`，单一 head `0022`。本任务**不新增迁移**。
- lint：ruff line-length 100。改 `packages/production`/节点代码后本地验证需重启 worker（仅集成测试相关）。
- 三个 required check 名称 `frontend`/`integration`/`unit` **保持不变**（别碰 main 分支保护 ruleset）。

## 受影响文件清单（已枚举，93 文件碰存储）

| 域 | 文件数 | 备注 |
|---|---|---|
| production | 25 | 最硬：节点/算法测试，多数直接 `Repository()` |
| integration | 17 | 已真库，仅去 `CUTAGENT_RUN_DB_TESTS` gate（parity 两个删） |
| providers | 15 | 直接 `Repository()` 居多 |
| api | 13 | 走 `app.state.repository` / TestClient |
| contract | 4 | 含 backend 选型测试，需改语义 |
| temporal | 3 | 已 gate，去 gate / 保留 Temporal gate |
| ops | 3 | |
| publishing / prompts / observability / media | 2 each | |
| workflow / import / golden | 1 each | golden 后端假设浅 |
| conftest.py | 1 | **keystone，Phase 1** |

精确清单见 `$CLAUDE_JOB_DIR/tmp/storage_touching_tests.txt`（执行时重新生成）。

---

## Phase 1 — Foundation（主代理 inline，串行）

### Task 1: 真库 conftest 夹具（keystone）+ 单文件 spike 验证

**Files:**
- Modify: `tests/conftest.py`
- 参考: `scripts/bootstrap_database.py`、`packages/core/storage/{database,seed,seed_media,bootstrap}.py`、`tests/integration/test_sqlalchemy_workflow.py`

**Interfaces — Produces（Phase 2 全程消费）:**
- `db_session_factory` (session/function fixture) → 真 Postgres `sessionmaker`（`get_sqlalchemy_session_factory_if_enabled()` 的产物）。
- `seeded_app` (fixture) → `create_app()` 在 SQL 后端 + 已迁移/已 seed 的库上接线好的 `FastAPI`。
- `client` (fixture) → `TestClient(seeded_app)`（沿用 conftest 的 `_ASGISyncTestClient`）。
- 每用例自动隔离：truncate 全业务表 + 重 seed 基线。

**设计:**
- conftest 顶部 env：`CUTAGENT_STORAGE_BACKEND` 默认 `sqlalchemy`（替换第 27 行 `memory`）。保留 `CUTAGENT_ALLOW_SANDBOX_FALLBACK=1`、`CUTAGENT_PUBLISH_ADAPTER=sandbox.publish`、`CUTAGENT_DISABLE_BACKGROUND_DISPATCHER=1`。`CUTAGENT_DATABASE_URL` 未设时给本地默认 `postgresql+psycopg://cutagent:cutagent@127.0.0.1:55432/cutagent`。
- session 级 `_database_ready` fixture（autouse, scope=session）：`alembic upgrade head`（in-process `command.upgrade`）建表 → 一次 `seed_database()` + `seed_media_assets()` 建基线快照。连不上/迁移失败 → fail-fast。
- function 级 `_db_isolation` fixture（autouse）：用例**后** `TRUNCATE <所有业务表> RESTART IDENTITY CASCADE` 再 `seed_database()`+`seed_media_assets()` 复位基线。表清单从 `Base.metadata.sorted_tables` 取，避免漏表。
- 评估：若每用例全量 re-seed 太慢，改为 session 建基线、用例后只 truncate「非基线表」+ 必要轻量补种；正确性优先。

- [ ] **Step 1**: 实现上述 fixtures（conftest 改写）。
- [ ] **Step 2**: 起本地 infra（`scripts/dev_up.sh up` 或 `docker compose up -d postgres`）；确认 55432 可连。
- [ ] **Step 3（spike）**: 选 1 个 api 测试（如 `tests/api/test_cases_profile.py`）和 1 个 production 节点测试，临时在新夹具上跑：`CUTAGENT_DATABASE_URL=... python -m pytest tests/api/test_cases_profile.py -q`，验证夹具可用、隔离正确（连跑两遍结果稳定）。
- [ ] **Step 4**: 提交 `test(conftest): 真库测试夹具 keystone + 用例间 truncate 隔离`。

### Task 2: 移除 `apps/api/app.py` 的 memory 降级分支

**Files:** Modify `apps/api/app.py:121-216`（`configure_app_state`）
**Interfaces — Consumes:** Task 1 夹具用于回归。

- [ ] **Step 1**: 删 `if session_factory is None:` 整条分支（148-164 行），各 `sqlalchemy_*` 恒实例化；`auth_service`/`secret_store`/`provider_reader`/`prompt_reader`/`budget_guard`/`circuit_breaker` 恒走 SQL 路径。`runtime_repository` 保留。`outbox_dispatcher` 恒 `SqlAlchemyOutboxDispatcher`。
- [ ] **Step 2**: `create_app()` → `configure_app_state(app)` 调用处确保从 `get_sqlalchemy_session_factory_if_enabled()` 取真 factory（lifespan 已如此；`configure_app_state` 默认参数 `session_factory=None` 改为必须有效，或在函数内取）。
- [ ] **Step 3**: 跑 `tests/api` + `tests/contract` 子集回归绿。
- [ ] **Step 4**: 提交 `refactor(api): app 强制 SQL 后端，删 memory 降级分支`。

### Task 3: 移除 `apps/worker/main.py` 的 memory 降级分支

**Files:** Modify `apps/worker/main.py:33-80`

- [ ] **Step 1**: 删各 `... if session_factory is not None else None/local` 三元降级（secret_store/provider_reader/prompt_reader/ops_repository/production_repository 恒 SQL）。`runtime_repository` 保留（per-activity run-state 模板）。
- [ ] **Step 2**: 跑 `tests/temporal/test_activity_repository_scoping.py`（纯单测）回归。
- [ ] **Step 3**: 提交 `refactor(worker): worker 强制 SQL 后端，删 memory 降级分支`。

### Task 4: 简化 `bootstrap.py` / `settings.py`（memory 不再是存储后端）

**Files:** Modify `packages/core/storage/bootstrap.py`、`packages/core/config/settings.py`

- [ ] **Step 1**: `settings.py StorageSettings.backend`：移除 `memory` 合法值（保留 `sqlalchemy`/`postgres`），或 `build_settings()` 校验时对 `memory` 显式报错并给清晰信息。
- [ ] **Step 2**: `bootstrap.py`：`sqlalchemy_backend_enabled()` 恒真 / `get_sqlalchemy_session_factory_if_enabled()` 恒返回真 factory；删 `warn_if_memory_backend`、`bootstrap_sqlalchemy_storage_if_enabled` 里的 memory 短路（可保留函数名，内部不再有 memory 分支）。
- [ ] **Step 3**: 改 `tests/contract/test_storage_backend_config.py` / `test_settings_config.py`：断言 memory 被拒绝（而非可选）。
- [ ] **Step 4**: 提交 `refactor(core): 存储后端只剩 sqlalchemy/postgres，memory 显式拒绝`。

### Task 5: 删 memory-only 实现（确认无生产引用后）

**Files:** `packages/publishing/accounts_repository.py`(MemoryAccountsRepository)、`packages/core/observability/events.py`(内存 OutboxDispatcher)、`packages/core/auth/service.py`(AuthService memory 用法)

- [ ] **Step 1**: 对每个候选 `grep -rn "<类名>" packages/ apps/`，确认仅测试/已删分支引用。
- [ ] **Step 2**: 删类/函数；`LocalSecretStore` **保留**。内存 `OutboxDispatcher` 若被 events 其它路径复用则保留接口、仅删 None-分支接线。
- [ ] **Step 3**: `python -c "import apps.api.main, apps.worker.main"` 冒烟导入；跑 `tests/observability`+`tests/publishing` 子集。
- [ ] **Step 4**: 提交 `refactor(core): 删除 memory-only 实现（MemoryAccountsRepository 等）`。

---

## Phase 2 — 分域迁移测试（fan-out 子代理，每域一个 commit）

### 迁移配方（Recipe，所有域通用）

**三类访问套路 → 替换法：**

1. **走 `TestClient(app)` / `app.state.repository`（api/golden）**
   - 用 `client` / `seeded_app` 夹具替代全局 app。
   - 断言：优先走 API 响应；需查库时用 `db_session_factory` + 分域 SQLAlchemy 仓库或 ORM `select`。**删除**读 `app.state.repository._xxx` 内部 dict 的断言，改为等价的 API/ORM 查询。

2. **直接 `repo = Repository(); repo.seed(); repo.create_*(...)`（providers/ops/prompts 等的存储型单测）**
   - 改为：`session_factory = db_session_factory` → 用对应 `SqlAlchemy*Repository(session_factory)` 写入/读取；断言走 ORM 查询。
   - 若该测试与已有 `tests/integration/test_sqlalchemy_<域>.py` 重复，则**删除**该单测（避免冗余），在 PR 描述里记一笔。

3. **节点/算法测试（production，最硬）**
   - 模式：`db_session_factory` seed 输入数据到 Postgres → 用生产水合接口建 run-state `Repository`（参考 `SqlAlchemyProductionRepository.hydrate_workflow_runtime_snapshot` / worker `TemporalActivityContext.build_runtime()`）→ 跑节点 → `sync_workflow_snapshot()` 落回 → `db_session_factory` 查库断言。
   - 纯算法函数（输入/输出不经存储，如 beam search、jieba 选材打分）若**完全不碰 Repository**，保持原样（本就不是 mock 后端测试）。

**收尾每个文件：** `python -m pytest tests/<域>/test_xxx.py -q` 本地真库**两连跑**绿（验隔离）。

### 域任务（按风险从易到难，子代理可并行不同域）

- [ ] **Task 6: contract（4）** — backend 选型/schema 守卫，配合 Task 4 调整。
- [ ] **Task 7: api（13）** — Recipe①。
- [ ] **Task 8: providers（15）** — Recipe②为主。
- [ ] **Task 9: ops / prompts / observability / media / publishing / workflow / import（~13）** — Recipe①②混合。
- [ ] **Task 10: golden（1）+ 端到端** — Recipe①，后端假设浅。
- [ ] **Task 11: production（25，最硬）** — Recipe③，拆 2-3 个子批。
- [ ] **Task 12: integration（17）** — 去 `CUTAGENT_RUN_DB_TESTS` module-skip / pytestmark；**删** `test_parity_run.py`+`test_parity_mapper.py`（单后端无 parity）。Temporal gate（`CUTAGENT_RUN_TEMPORAL_TESTS`）**保留**（需真 Temporal）。
- 每个 Task 末尾：`git commit -m "test(<域>): 迁移到真 Postgres"`。

---

## Phase 3 — CI / 门禁 / 审核 / PR

### Task 13: CI + ci_gate 改造

**Files:** `.github/workflows/ci.yml`、`scripts/ci_gate.sh`、`tests/CLAUDE.md`、相关 `CLAUDE.md`

- [ ] **Step 1**: `ci.yml` `unit` job 加 `services: postgres(pgvector/pgvector:pg16, 55432)`（+ 需要则 Temporal/MinIO），设 `CUTAGENT_DATABASE_URL`/`CUTAGENT_STORAGE_BACKEND=sqlalchemy`，pytest 前 `python scripts/bootstrap_database.py`。或合并 unit/integration job（保持 required check 名）。
- [ ] **Step 2**: `ci_gate.sh` 第一段 unit 接真库 env + bootstrap。
- [ ] **Step 3**: 更新 `tests/CLAUDE.md`/`packages/core/CLAUDE.md`/`apps/*/CLAUDE.md` 中"memory 后端/默认内存"描述。
- [ ] **Step 4**: 提交 `ci: 全部测试段接真 Postgres`。

### Task 14: 全量门禁 + 双子代理审 + 修复

- [ ] **Step 1**: 本地全量 `scripts/ci_gate.sh` 绿（或等价：bootstrap + 全 `pytest -q` 真库）。
- [ ] **Step 2**: 子代理 A（正确性审）：无静默语义改动 / 隔离正确（两连跑稳定）/ 红线未误删（Repository 类、LocalSecretStore、sandbox、外部 HTTP 替身仍在）。
- [ ] **Step 3**: 子代理 B（范围/可删性审）：memory 后端选项确实删净（无残留 `STORAGE_BACKEND=memory`、无 None 降级分支、parity 删除）；CLAUDE.md 同步。
- [ ] **Step 4**: 按审核意见修复，回到 Step 1 直到双审过 + 门禁绿。

### Task 15: 提 PR

- [ ] **Step 1**: rebase 到最新 `origin/main`，重跑门禁绿。
- [ ] **Step 2**: push 分支，`gh pr create`（标题/正文中文，列改动面、风险、验收）。
- [ ] **Step 3**: 等 GitHub CI 三 check 绿。

---

## Self-Review（对照 spec）

- 范围覆盖：spec §3 删除项 → Task 2/3/4/5/12；保留项 → Global Constraints；§4.1 夹具 → Task 1；§4.2 → Task 2-5；§4.3 → Task 6-12；§4.4 → Task 13；§7 验收 → Task 14-15。✓
- 无占位符：各 Task 给了文件/行号/命令/配方。节点测试给了水合-快照具体接口名。✓
- 类型/命名一致：夹具名 `db_session_factory`/`seeded_app`/`client` 全程一致。✓
- 已知风险（节点测试摩擦/套件耗时/隔离正确性/单 PR 体量）已在 spec §6 记录，Task 1/11 对应缓解。
