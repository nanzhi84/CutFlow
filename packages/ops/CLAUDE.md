# packages/ops

Ops / 成本 / 成品率 / 治理域：既有**指标计算**（成本、成品率、预算、失败分类），也有 provider 调用前的**预算硬阻断 + 熔断**、持久化门面与 provider 余额轮询。支撑 Ops 控制台：降级与成本必须显式上报，不得隐藏。

## 职责
- 指标计算（纯函数，均在 `__init__.py` 导出）：`cost_metrics.py`（`compute_cost_metrics`）、`yield_rates.py`（`compute_yield_rates`，基于 `yield_funnel_events`）、`budget_evaluation.py`（`evaluate_budget`/`period_start`）、`failure_taxonomy.py`（`classify_error_code`，薄再导出）。
- 调用前闸门（被 `apps/api` 与 `apps/worker` 接线注入 gateway）：`budget_guard.py` 的 `BudgetEnforcementGuard`（provider 调用前预算硬阻断，超额返回 `ProviderError`/`provider_quota_exceeded`、`retryable=False`）、`circuit_breaker.py` 的 `ProviderCircuitBreaker`（按 `provider_profile` 错误率熔断，返回 `provider_circuit_open`；受 env `CUTAGENT_PROVIDER_CIRCUIT_BREAKER`（默认 OFF）/ 阈值 0.5 / 窗口 24h 控制）。
- 持久化门面：`sqlalchemy_repository.py` 的 `SqlAlchemyOpsRepository` 聚合 dashboard / cost rollups / yield funnel / budgets / alerts / QC / approval / audit / billing reconcile（`evaluate_budgets`/`_sync_budget_alerts` 生成 `budget.exceeded` 告警行）；`sqlalchemy_mappers.py` 做 Row→contract；`provider_usage_metrics.py` 用 SQL 聚合 provider 成功率/成本，并产出 `ProviderProfileHealthMetrics` 作为熔断输入。
- Provider 余额：`balance/`（`port.py` PORT、`registry.py` `build_pollers`/`query_balance`、`service.py` `refresh_balances` + 可选后台 `BalancePollerService`、`base.py` 共享助手、`providers/` 各家插件）拉真实余额，失败 graceful degrade（unconfigured/unsupported/unauthorized/error），绝不编造、绝不抛。

## 关键文件
- 计算：`cost_metrics.py` / `yield_rates.py` / `budget_evaluation.py` / `failure_taxonomy.py`。
- 调用前闸门：`budget_guard.py` / `circuit_breaker.py`。
- 持久化：`sqlalchemy_repository.py` / `sqlalchemy_mappers.py` / `provider_usage_metrics.py`。
- 余额：`balance/`。

## 约定与要求
- contract-first：I/O 走 `packages.core.contracts` 类型，money 用 `Money`（CNY）。
- 依赖方向：`production` / `core` **不得** import `ops`。漏斗 taxonomy 与 `compute_true_yield_rate`/`record_funnel_event` 的实现在 `packages.core.observability.funnel`（ops `__init__.py` 再导出复用），ops 只在其上做指标计算——别在 ops 重写漏斗底座。
- 余额 poller 失败映射为状态字段、不抛异常；后台轮询默认 OFF（`settings.balance.poller_enabled`），无 key / 测试环境不外呼。

## 测试
- `pytest tests/ops tests/observability`（指标计算 / 漏斗 / outbox / metrics）。

## 注意 / 坑
- `reconcile_billing` 现做真实对账：按窗口聚合 ProviderInvocation 的 estimated_cost 与已记账用量，算出 estimated/recorded/variance（按 provider+capability 出 line_items），返回 `status="completed"`；`dry_run=True` 仅返回计算结果不落库不写审计，`dry_run=False` 落 `ProviderBillingReconciliationRow` + 写 `billing.reconcile_completed` 审计。
- 改 Ops contract 后须重新生成 openapi.json + schema.d.ts（`scripts/export_openapi.py`）。
