# 删除 memory 存储后端 · 全部测试连真实 Postgres — 设计文档

- 日期：2026-06-29
- 分支：`worktree-remove-mock-backend`（基于 `origin/main`）
- 状态：已与用户对齐范围（字面彻底版 + 保留 sandbox），待转 writing-plans

## 1. 背景与问题

仓库当前是**双存储后端**架构：

- **memory 后端**：`packages/core/storage/repository.py` 的内存版 `Repository`（god-object，~67 属性 + ~15 方法）。测试默认走它（`tests/conftest.py:27` `CUTAGENT_STORAGE_BACKEND=memory`）。
- **sqlalchemy 后端**：~13 个分域 `SqlAlchemy*Repository`，生产唯一后端。

测试套件（~220 个 `test_*.py`）绝大多数跑在 memory 后端上，只有 `tests/integration/test_sqlalchemy_*`（15）+ `test_parity_*`（2）在 `CUTAGENT_RUN_DB_TESTS=1` gate 后连真 Postgres。两套后端靠 `test_parity_*` 维持语义一致，存在「memory 测试绿、真库/生产路径有 bug」的分裂风险。

**用户目标**：删除「mock 后端」与「mock 测试」，让所有碰存储的测试都连真实数据库跑；验收 = 全部子代理审核通过 + CI 绿 → 提 PR。

## 2. 决定性架构事实（已查证）

1. **内存 `Repository` 不是纯 mock，是生产运行态基底，删不掉。**
   - `apps/api/app.py:123` 与 `apps/worker/main.py:38` 在**任何后端下**都 `runtime_repository = Repository()`。
   - `ProviderGateway`、`PromptRegistry`、`build_digital_human_workflow`、`TemporalRuntimeAdapter` 全建在它上面。
   - worker 注释（`main.py:60-65`）：SQL 后端下，**每个 Temporal activity 新建一个隔离的内存 `Repository` 承载单次 run 的可变状态**，跑完经 `sync_workflow_snapshot()` 落盘到 SQLAlchemy。
   - 结论：内存 `Repository` = 单次工作流执行的临时运行态；SQLAlchemy repos = 持久层。两者设计上**并存**，不是二选一。"删掉内存 `Repository` 类"= 重写整个工作流执行引擎，非本任务范围。

2. **"memory 作为存储后端选项"才是要删的东西。** 选型在 `settings.py StorageSettings.backend`（memory|sqlalchemy|postgres）+ `bootstrap.py`（`sqlalchemy_backend_enabled()` / `get_sqlalchemy_session_factory_if_enabled()`）。memory 时 `session_factory=None`，app/worker 走"内存降级分支"（各 `sqlalchemy_*` 置 None、`AuthService(memory)`、内存 `OutboxDispatcher`、`MemoryAccountsRepository`）。**这条分支只有测试在走**，生产恒为 SQL 后端（缺 `CUTAGENT_DATABASE_URL` 直接启动失败）。

3. **schema 用 Postgres 专属类型**（`JSONB`/`ARRAY`/`Vector(1536)` pgvector）。"真实数据库"只能是 **Postgres**，不能用 SQLite 替身。CI 已用 `pgvector/pgvector:pg16`（端口 55432）。

4. **sandbox provider/publish 不是 DB mock，是外部 API 替身。** `SandboxProvider`（`packages/ai/gateway/provider_gateway.py:115`）、`sandbox.publish`（`packages/publishing/platform_adapter.py:94`）。受 `sandbox_fallback_allowed()` 控制，生产默认 OFF（无真 provider 显式报错，非静默降级），是**合法降级路径**；CI 无法真打火山/OpenAI/小V猫，必须保留。**本任务不删 sandbox。**

5. **对外部 HTTP 的测试替身（`FakeDriver` / `_FakeLlmProvider` / monkeypatch）必须保留**——CI 不能真打第三方 API。这些不是"数据库 mock"。

## 3. 范围（Scope）

### 删除 / 改造
- `CUTAGENT_STORAGE_BACKEND=memory` 作为存储后端选项 → 不再支持（显式拒绝或移除合法值）。
- `apps/api/app.py` / `apps/worker/main.py` 的 `session_factory is None` 整条内存降级分支。
- memory-only 实现的**使用接线**：`AuthService(runtime_repository)`、内存 `OutboxDispatcher`、`MemoryAccountsRepository`（确认无其它生产引用后删类）。
- `bootstrap.py` 的 `warn_if_memory_backend` / memory 分支语义。
- `tests/integration/test_parity_*`（只剩一个后端，parity 失去意义）。
- 纯 memory 后端专属、且 SQL 侧已覆盖的存储行为测试。
- `tests/conftest.py:27` 默认 `memory` → `sqlalchemy`；移除/调整临时 objectstore 接线。
- `tests/integration/test_sqlalchemy_*` 的 `CUTAGENT_RUN_DB_TESTS` gate（现在默认就是真库）。

### 保留（不在本任务删）
- 内存 `Repository` 类本身（生产运行态基底）。
- `LocalSecretStore`（SQL 模式下仍是 `SqlAlchemySecretStore` 的 fallback）。
- sandbox provider / `sandbox.publish` 适配器及 `sandbox_fallback_allowed()` 机制。
- 测试对外部 HTTP 的 `FakeDriver` / monkeypatch。
- 纯函数测试（不碰任何存储）——它们本就没用后端，**不受影响**。

### 自然边界
"所有测试连真库" = **没有任何测试拿内存 `Repository` 当数据库替身；凡需要存储的测试一律连真 Postgres**。不碰存储的纯逻辑测试无需"打开一个 DB 连接"。

## 4. 设计

### 4.1 Keystone：真库测试夹具（`tests/conftest.py` 重写）
这是整个改造的承重点，所有迁移依赖它。

- **session 级**：连 `CUTAGENT_DATABASE_URL`（默认指向 CI/本地 55432 pgvector），session 开始 `alembic upgrade head` 建表 + `seed_database()` + `seed_media_assets()`；连不上或迁移失败 → fail-fast（不再静默退内存）。
- **用例级隔离**：function 级 autouse fixture，用例前/后 **TRUNCATE 所有业务表（`RESTART IDENTITY CASCADE`）+ 重新 seed 基线**。
  - 选 truncate 而非事务回滚的原因：被测 app 自带 `session_factory`，会在**独立连接**上 commit；测试侧的事务/savepoint 回滚管不到 app 连接里已提交的数据，必然泄漏。truncate 是跨连接可靠的清理方式。
- **统一夹具**：
  - `db_session_factory` — 真 Postgres `session_factory`。
  - `seeded_app` — 用 SQL 后端 + 该 session_factory 接线好的 FastAPI app（替代当前全局 memory app）。
  - `client` — `TestClient(seeded_app)`。
  - 直接操作分域 SQLAlchemy 仓库的 helper（用于 setup/断言）。
- conftest 顶部 env：`CUTAGENT_STORAGE_BACKEND` 默认 `sqlalchemy`；`CUTAGENT_ALLOW_SANDBOX_FALLBACK=1` / `CUTAGENT_PUBLISH_ADAPTER=sandbox.publish` 保留；objectstore 指向 env 配置的 MinIO/本地路径。

### 4.2 移除生产 memory 分支
- `apps/api/app.py configure_app_state`：删 `session_factory is None` 整个分支；SQL 后端强制（无 DATABASE_URL → 启动失败）。`runtime_repository` 保留（run-state）。各 `sqlalchemy_*` 恒非 None。
- `apps/worker/main.py async_main`：同样删 None 降级三元分支。
- `packages/core/storage/bootstrap.py`：`sqlalchemy_backend_enabled()` 恒真化 / `get_sqlalchemy_session_factory_if_enabled()` 恒返回真 factory；memory 显式拒绝；删 `warn_if_memory_backend`。
- `packages/core/config/settings.py StorageSettings`：移除 `memory` 合法值（或 build 时拒绝并给清晰报错）。
- 删 memory-only 实现的使用与类：`MemoryAccountsRepository`、events.py 内存 `OutboxDispatcher` 走 None 分支的接线、`AuthService(runtime_repository)`。逐个确认无其它生产引用后再删类（保留 `LocalSecretStore`）。
- 同步 `*/CLAUDE.md` 里"无 session_factory 时退化到内存 Repository"等描述。

### 4.3 分域迁移测试（约 80–120 个碰存储的文件，分批）
按域分批（api / production / media / providers / publishing / ops / creative / planning / contract / golden / workflow / storage / core / import / connectors / observability / prompts / voices）：

- **走 app/API 的测试**：换用 `client` / `seeded_app` / `db_session_factory` 夹具；断言改走 API 响应或分域 SQLAlchemy 仓库查询，不再读 `app.state.repository` 内部 dict。
- **直接 `Repository()` 的节点/算法测试**：改走生产数据流——「从 Postgres 水合 run-state Repository → 跑节点 → 快照回 Postgres → 查 Postgres」，或上提为 app 端到端。这是最硬的一批。
- **parity 测试**：删（只剩一个后端）。
- **memory 专属存储行为测试**：SQL 侧已覆盖的删；未覆盖的转测 SQL 仓库。
- **现有 `test_sqlalchemy_*`**：去掉 `CUTAGENT_RUN_DB_TESTS` gate，纳入默认套件。

### 4.4 CI / 门禁
- `.github/workflows/ci.yml` `unit` job：加 `pgvector/pgvector:pg16`（+ 视情况 Temporal / MinIO）service，设 `CUTAGENT_DATABASE_URL` / `CUTAGENT_STORAGE_BACKEND=sqlalchemy`，pytest 前跑 `scripts/bootstrap_database.py`。可考虑把 unit 与 integration 合并为单一真库 job，或 unit job 复用 integration 的 service 定义。
- `scripts/ci_gate.sh`：第一段 unit 也接真库 env + bootstrap。
- **三个 required check（frontend / integration / unit）名称保持不变**，避免触碰 main 分支保护 ruleset。

## 5. 测试隔离策略（详）
- 默认：每用例 `TRUNCATE ... RESTART IDENTITY CASCADE` 全业务表 + 重 seed 基线种子（users / registration_codes / providers / media）。
- seed 基线必须与当前 `seed_database()` / `seed_media_assets()` 一致，保证 API 登录账号（`admin@local.cutagent` 等）可用。
- 评估优化：seed 基线可在 session 级建一次「干净快照」，用例级只 truncate 业务数据再轻量 re-seed，平衡正确性与速度。

## 6. 风险
- **节点/算法单测架构摩擦**（最大）：生产节点跑在内存 run-state Repository 再快照 SQL；这类单测"上真库"要走完整水合-快照链路或上提端到端，工期主要花在这。
- **套件耗时**：每用例 truncate+seed，几百用例从秒级涨到数分钟级，CI unit job 时间显著上升。
- **隔离正确性**：必须 truncate 而非纯事务回滚（app 跨连接 commit）。
- **单 PR 体量**：~100+ 文件，分域 commit 让 diff 可读，review 负担仍重。
- **OpenAPI 漂移**：若动到 API contract 形状，需重生成 `openapi.json` + `schema.d.ts`（CI `git diff --exit-code` 校验）。本任务以测试/接线为主，应尽量不改 contract。

## 7. 验收标准
1. 删除 memory 存储后端选项；生产与测试统一走真 Postgres；sandbox 与外部 HTTP 替身保留。
2. 凡碰存储的测试连真 Postgres；不再有任何测试用内存 `Repository` 当数据库。
3. 全量 `scripts/ci_gate.sh` 本地绿；GitHub CI 三个 required check 绿。
4. 双子代理审核通过：一审正确性（无静默语义改动 / 隔离正确 / 红线未误删），一审范围完整与可删性（memory 后端选项确实删净 / sandbox 等红线确实保留）。
5. 提交一个 PR（分域 commit）。

## 8. 交付/执行编排（writing-plans 细化）
1. keystone 真库夹具（我亲自做，高风险、强交叉依赖）。
2. 移除生产 memory 分支 + 删 memory-only 实现。
3. 分域迁移测试（子代理按域并行，每域本地真库跑绿）。
4. 删 parity / 冗余 memory 测试 + 去 gate。
5. CI / ci_gate 改造。
6. 全量真库门禁绿 → 双子代理审 → 修复 → 提 PR。
