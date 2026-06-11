# M2 施工简报：单一真相源

负责：Codex（执行）/ Claude（架构 + 验收）
分两阶段串行：**M2a 行为改造**（本文件主体）→ **M2b 结构拆分**（main.py 拆 routers/services，另行派发）。
Spec：仓库根 `树影_Cutagent_CleanSlate重写Spec_v3_2026-06-11.md`（1A.3、3.2、11.3、14 章、15.1、19.4）。

## M2a Goal

Postgres 成为唯一真相源；消灭内存/DB 双轨与 module 级单例；治理三处安全债。
审计依据：storage-db / providers-secrets / prompts 三路审计的 blocker（读写脑裂：API 写 DB 的
profile/prompt 对运行时 gateway/registry 不可见；idempotency 进程内存；registration_code 明文当主键）。

## 改动清单（逐条核销）

### A. 默认后端切换

- A1 `CUTAGENT_STORAGE_BACKEND` 默认值改为 `sqlalchemy`；显式 `memory` 仅供测试/演示，启动时打印明显的非生产警告。
- A2 sqlalchemy 模式下无 `CUTAGENT_DATABASE_URL` 时 fail-fast，报错信息给出本地默认连接串示例（55432）。
- A3 进程内单测（tests/api、tests/golden、tests/workflows 等）通过 conftest 显式设 memory fixture 保持快速；tests/integration 照旧走真 DB。**单测不允许因默认值切换而静默变成连 DB。**

### B. 消灭 module 级单例（DI 改造）

- B1 删除 `packages/ai/gateway` 的 `_GATEWAY` 模块单例：ProviderGateway 由 app 启动时按当前 backend 构造，挂 `app.state`，handler 经依赖注入获取。sqlalchemy 模式下 gateway 的 profile/secret/capability 读 DB（写后立即可见）。
- B2 同样消灭 `PromptRegistry` 模块单例与硬编码种子绑定进程内存的行为：sqlalchemy 模式下 registry 读 DB 的 template/version/binding；种子 prompt 通过 `scripts/bootstrap_database.py` 落库，不再 import 时注入。
- B3 `apps/api/main.py` 顶层 `repo` 单例改为 app.state + 依赖注入（本阶段不拆文件，只改获取方式）。
- B4 workflow pipeline 取仓储/gateway 的路径同步改 DI（不得从模块全局拿）。

### C. Idempotency 落表

- C1 新表 `idempotency_records`：`key`、`method`、`path`、`request_hash`、`response_status`、`response_body`(JSONB)、`created_at`、`expires_at`；`(key, method, path)` 唯一索引。
- C2 写接口的 Idempotency-Key 中间件在 sqlalchemy 模式读写该表：命中且 request_hash 一致 → 返回原响应（200 语义照 spec 32.11）；命中但 request_hash 不一致 → 409 统一错误体；过期记录可清理。memory 模式保持现有进程内实现（同语义）。
- C3 集成测试：用两个独立构造的 app/session 实例模拟"进程重启"，第二实例重放同 Idempotency-Key 必须返回原响应而非重复执行。

### D. registration_code 治理

- D1 `registration_codes` 表存 `code_hash`（sha256(code + 全局盐) 即可，盐进 env），不再以明文 code 作主键/查询键；注册路径按 hash 查找。seed 同步更新。

### E. Secrets 真实模型

- E1 新建 `SecretStore` 抽象（`put/get/disable`），local 实现把密文写 `.data/secrets/<secret_ref>`（文件权限 0600，沿用 dev 封装格式即可，生产实现留 M6+）。
- E2 DB 的 `secrets` 表删除 `encrypted_value` 列——DB 只存 `secret_ref` + 元数据（spec 11.3 硬规则）。
- E3 ProviderGateway 经 SecretStore 按 `secret_ref` 解析密钥；secret 缺失/禁用时报 `provider.auth_failed`，引用被禁用 secret 的 prod profile 不可调度（spec 1899）。

### F. 顺带核销 M1 残留

- F1 D1 残留：`uploaded.file` / `import.mapping` 两个兼容 ArtifactKind——裁决：分别改为 spec 体系内的命名 `upload.file.v?`→ 不，直接登记进 ArtifactSchemaRegistry 并在 docs/spec-questions.md 记录为 spec 补充提案（值保留，但必须有 schema 与 registry 项，不得游离）。
- F2 D7 残留：pipeline 里剩余手拼 dict payload 全部替换为 `packages/core/contracts/artifacts.py` 模型。

## 边界（Out of scope）

- 不拆 main.py 文件结构（M2b）；不引 temporalio（M3）；不动 WebSocket/observability（M4）；
  不实现生产级 secret manager；不改发布/标注的业务语义（M6）。

## Verification

- 全量：`timeout -k 5 600 /home/nanzhi/projects/cutagent-genesis/.venv/bin/python -m pytest -q`
- DB 集成（sandbox 内连不上 DB 属环境限制，连接报错时记录留给验收官，不要当代码错误修）：
  `timeout -k 5 300 env CUTAGENT_RUN_DB_TESTS=1 CUTAGENT_STORAGE_BACKEND=sqlalchemy CUTAGENT_DATABASE_URL='postgresql+psycopg://cutagent:cutagent@127.0.0.1:55432/cutagent' /home/nanzhi/projects/cutagent-genesis/.venv/bin/python -m pytest tests/integration -q`
- 新增集成测试：C3 跨实例 idempotency；B1 写 profile 后 gateway 可见；E2 表结构无 encrypted_value；D1 明文码不落库。
- OpenAPI 可导出。

## 验收门（验收官执行）

1. 默认环境变量下启动即要求 Postgres（不静默退化内存）。
2. API 创建 provider profile / 发布 prompt 后，运行时 gateway/registry 立即可见（DB 模式）。
3. idempotency 重放跨"重启"生效。
4. DB 中无明文注册码、无密钥派生物。
5. 全测试绿 + OpenAPI 导出 OK。

---

## M2a 验收记录（2026-06-11，验收官：Claude）

**判定：通过**（merge `778fbee`）。证据：重建 schema 后 69 单测 + 22 DB 集成全绿；无 DB URL 时启动 fail-fast（报错带本地连接串提示）；`registration_codes` 仅存 `code_hash`、`secrets` 表无 `encrypted_value`；gateway/registry 写后可见与跨实例 idempotency 由新增集成测试覆盖；OpenAPI 无 diff；前端构建通过。

核销：A1-F2 全部 done（含 M1 残留 D1/D7 清账；`uploaded.file`/`import.mapping` 已登记 registry 并记入 `docs/spec-questions.md`）。

过程：直驱 codex exec 一轮完成（457k tokens），无假死；commit 由验收官代办（codex sandbox 对 .git 只读是常态，流程已固化）。

## M2b 施工简报：main.py 拆分

目标：`apps/api/main.py`（约 2300 行）拆为 `apps/api/routers/*`（按域：auth、uploads、secrets、cases、jobs_runs、media、voices、prompts、providers、case_agent、finished_videos、publishing、ops、imports）+ `apps/api/services/*`（use case 层，路由只做参数绑定/权限/调 service）+ `apps/api/app.py`（create_app 工厂）。约束：
- 行为零变化：OpenAPI 导出 diff 必须为空（路由路径、模型、状态码全不变）。
- main.py 收敛为入口（目标 < 100 行：create_app + uvicorn 入口）。
- 依赖规则照 spec 3.2：routers 不直接 import provider HTTP/ffmpeg；service 经 app.state 仓储。
- 全部测试不改断言通过（允许只改 import 路径）。
