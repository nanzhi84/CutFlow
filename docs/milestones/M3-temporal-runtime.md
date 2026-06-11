# M3 施工简报：执行迁出 API（Temporal Runtime）

负责：Codex（执行）/ Claude（架构 + 验收）
分支：`feat/m3-temporal-runtime`
Spec：1A.2（行 124-132）、3.3（行 343-356）、第 6 章执行语义（行 840-927）、19.1。
审计依据：workflow-runtime 审计两条 HIGH——整条流水线在 API 请求 handler 内同步执行完毕（架构倒置，
cancel 结构上不可能为真）；`_reuse_prefix` 的 missing-artifact 处理 bug（artifact 丢失被静默跳过而非重跑）。

## Goal

生产执行从 API 进程迁出到 Temporal worker；API 经 `WorkflowRuntimeAdapter` 提交后立即返回；
cancel/resume 成为真实语义；修复 resume 复用契约。

## 关键设计决定（架构师已定，不要重新发明）

- 领域 node / pipeline / planning 代码**不得 import temporalio**（spec 3.3）。SDK 只出现在
  `packages/core/workflow/temporal_adapter.py`（或同级模块）和 `apps/worker`。
- 保留进程内 `LocalRuntimeAdapter`（现有同步执行路径改名收编）：单测/golden 默认走它，保持秒级测试。
  Temporal adapter 与 local adapter 实现同一个 `WorkflowRuntimeAdapter` 接口。
- Temporal workflow 只编排（确定性代码：按 WorkflowTemplate 顺序调 activity、处理 cancel signal），
  每个 Node 对应一个 activity；activity 内调用现有 NodeRunner，IO/DB 都在 activity 里。
- run/node_run/artifact 的持久化发生在 worker（activity）侧，API 读 DB 看状态——不再有"POST 返回时已终态"。
- 你的 sandbox 连不上 Temporal 服务端和 Postgres：Temporal 集成测试写好后用
  `CUTAGENT_RUN_TEMPORAL_TESTS=1` 门控，留给验收官跑（本机 127.0.0.1:7233 已有可用 Temporal 1.26 + UI）。

## 改动清单（逐条核销）

### A. 依赖与配置

- A1 `pyproject.toml` 加 `temporalio>=1.28`（venv 已预装 1.28.0，可直接 import）。
- A2 settings：`CUTAGENT_WORKFLOW_RUNTIME`（`local`|`temporal`，默认 local，prod 文档注明应为 temporal）、
  `CUTAGENT_TEMPORAL_ADDRESS`（默认 127.0.0.1:7233）、`CUTAGENT_TEMPORAL_NAMESPACE`（default）、
  `CUTAGENT_TEMPORAL_TASK_QUEUE`（cutagent-production）。

### B. WorkflowRuntimeAdapter 接口定版

- B1 接口方法：`start_run(job, run, template) -> None`（异步提交）、`cancel_run(run_id, force)`、
  `resume_run(source_run_id, new_run, reuse_plan)`、`get_run_status(run_id)`（可选 query）。
  API services 只持有 adapter 实例（app.state），按 CUTAGENT_WORKFLOW_RUNTIME 在启动时装配。
- B2 `LocalRuntimeAdapter`：现有同步执行路径包装成该接口；为兼容单测可同步完成，但 API service
  层代码不得假设提交后即终态（断言只看契约状态机）。

### C. Temporal workflow + activities + worker

- C1 `DigitalHumanVideoWorkflow`（Temporal workflow 类）：输入 run_id/job_id/template id+version；
  按模板节点序依次 `workflow.execute_activity("run_node", node_id, ...)`；retry policy 从 NodeSpec
  映射到 Temporal RetryPolicy；activity timeout 取 NodeSpec/profile 的 timeout。
- C2 `run_node` activity：构造仓储/gateway（worker 进程的 DI 上下文），调 NodeRunner 执行单节点，
  持久化 node_run/artifacts/provider_invocations，返回 node 状态摘要（小对象，不传大 payload）。
- C3 cancel：workflow 接收 Temporal cancellation/signal，把 run 置 cancelling；正在跑的 activity
  通过 heartbeat + cancellation token 感知；node/provider 收到 cancel 后按 spec 6.5 行为；
  最终 run 置 cancelled。force cancel = Temporal terminate + DB 状态收敛。
- C4 `apps/worker/main.py`：真 worker——连 Temporal、注册 workflow + activities、跑 task queue；
  `python -m apps.worker` 可启动；docker-compose 加 `worker` 服务定义（build 本仓库或 command 直跑，
  依赖 postgres + temporal）。
- C5 API job/run services 改走 adapter：POST 创建 job+run（queued）→ adapter.start_run → 立即返回；
  cancel/resume endpoints 调 adapter 对应方法。temporal 模式下 run 状态由 worker 写。

### D. Resume 契约修真

- D1 重写 reuse 判定为纯函数 `compute_reuse_plan(source_run, template, artifacts) -> ReusePlan`，
  按 spec 6.3 六条件逐节点判定（node_id、node_version、input_manifest_hash、文件存在、sha256、
  kind+schema_version 匹配）；**第一个不满足条件的节点即复用前缀终点，其后全部重跑**——修掉现有
  `_reuse_prefix` 把 missing-artifact 节点静默跳过的 bug。
- D2 副作用节点（NodeSpec.side_effects 非空）不自动复用/重放，除非声明 idempotency key（spec 6.3）。
- D3 单测覆盖：完整前缀复用、中间 artifact 丢失（必须从该节点重跑且不吞后续）、sha256 不匹配、
  node_version 变化、provider profile 变化导致 hash 变化、副作用节点不复用。

### E. 测试

- E1 既有单测/golden 全部走 LocalRuntimeAdapter，不改断言（接口收编后允许改装配代码）。
- E2 `tests/temporal/test_temporal_runtime.py`（`CUTAGENT_RUN_TEMPORAL_TESTS=1` 门控）：
  真 Temporal + 真 DB 跑通：提交 → worker 执行 sandbox 流水线 → run succeeded + finished video 落库；
  cancel 中途生效（提交后立刻 cancel，最终状态 cancelled 且无 finished video）；
  resume 复用合法前缀（先跑一个成功 run，删一个中间 artifact 文件，resume 后该节点重跑）。
  测试内可用 `temporalio.worker.Worker` 在测试进程里起 worker（同进程即可，不要求 docker worker）。

## 边界（Out of scope）

- 不做 admission control/schedule/reconciliation 的完整版（M4+）；不做 WebSocket（M4）；
- 不改 provider/媒体处理语义；不动发布/标注。
- API 进程不 import temporalio 的硬规则**本批暂放宽为**：API 可经 adapter 模块间接依赖 SDK client
  （spec 行 352 的"API 不安装 SDK"在单仓库形态下以"API 代码不直接 import temporalio"执行，
  记入 docs/spec-questions.md）。

## Verification（sandbox 内）

- `timeout -k 5 600 /home/nanzhi/projects/cutagent-genesis/.venv/bin/python -m pytest -q` 全绿（基线 69）。
- D3 的 reuse 纯函数单测全绿（不需要 Temporal/DB）。
- OpenAPI 导出无意外 diff（新增字段允许，删改不允许）。
- Temporal/DB 集成测试连接失败属 sandbox 环境限制，记录留验收官。

## 验收门（验收官执行，真 Temporal + 真 DB）

1. `CUTAGENT_WORKFLOW_RUNTIME=temporal` 下 POST job：立即返回 queued/admitted，非终态。
2. worker 执行完成：run succeeded、node_runs/artifacts/finished_video 落库、Temporal UI 可见执行历史。
3. 提交后立即 cancel：run 最终 cancelled，无成片产出。
4. resume：删中间 artifact 后 resume，新 run 复用合法前缀并从缺失节点重跑（对照 D1）。
5. 领域包 grep 无 temporalio import（packages/production、packages/planning、packages/media、packages/creative）。
6. 全量 + DB 集成 + temporal 集成测试三套全绿。

---

## 验收记录（2026-06-11，验收官：Claude）

**判定：通过**（merge `d998d48`）。证据：78 单测 + 22 DB 集成 + 3 条真 Temporal 集成全绿；提交立即返回非终态、worker 真执行并落库（Temporal UI 可见）、中途 cancel 最终 cancelled 无成片、删中间 artifact 后 resume 从该节点重跑；领域包 grep 无 temporalio import。

核销：A1-E2 全部 done。`_reuse_prefix` 静默吞节点的 bug 已由 `compute_reuse_plan` 纯函数替换并有六情形单测。

验收修复（3 处实弹 bug，codex sandbox 连不上 Temporal 测不出）：同步 activity 需要 Worker 配 `activity_executor`；workflow 沙箱确定性校验需要领域 import 走 `workflow.unsafe.imports_passed_through()`；`TemporalRuntimeAdapter._run` 桥接改为事件循环安全（已有 loop 时用私有线程跑）。

遗留（记入 M4+）：adapter 每次调用都 `Client.connect`，应复用连接；workflow 启动经 `_template_from_run` 重建模板的路径待收紧。
