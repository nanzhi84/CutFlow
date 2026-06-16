# packages/ops

Ops / 成本 / 成品率 / 治理域：既有 §9/§26 的**指标计算**（成本、成品率、预算、告警规则、失败分类），也有持久化门面与 provider 余额轮询。支撑 Ops 控制台（spec §9 / §1.6：降级与成本必须显式上报，不得隐藏）。

## 职责
- 指标计算（纯函数，均在 `__init__.py` 导出）：`cost_metrics.py`（§9.4/§26.2 `compute_cost_metrics`）、`yield_rates.py`（§9.5/§26.3 `compute_yield_rates`，基于 `yield_funnel_events`）、`budget_evaluation.py`（§9.8 `evaluate_budget`/`period_start`）、`alert_rules.py`（§9.2/§9.8 `evaluate_rules` + `ALERT_METRIC_CATALOG`）、`failure_taxonomy.py`（§9.6 `classify_error_code`/`classify_funnel_event`，薄再导出）。
- 持久化门面：`sqlalchemy_repository.py` 的 `SqlAlchemyOpsRepository` 聚合 dashboard / cost rollups / yield funnel / budgets / alerts / QC / approval / audit / billing reconcile；`sqlalchemy_mappers.py` 做 Row→contract；`provider_usage_metrics.py` 用 SQL 聚合 provider 成功率/成本。
- Provider 余额：`balance/`（`port.py` PORT、`registry.py` `build_pollers`/`query_balance`、`service.py` `refresh_balances` + 可选后台 `BalancePollerService`、`base.py` 共享助手、`providers/` 各家插件）拉真实余额，失败 graceful degrade（unconfigured/unsupported/unauthorized/error），绝不编造、绝不抛。

## 关键文件
- 计算：`cost_metrics.py` / `yield_rates.py` / `budget_evaluation.py` / `alert_rules.py` / `failure_taxonomy.py`。
- 持久化：`sqlalchemy_repository.py` / `sqlalchemy_mappers.py` / `provider_usage_metrics.py`。
- 余额：`balance/`。

## 约定与要求
- contract-first：I/O 走 `packages.core.contracts` 类型，money 用 `Money`（CNY）。
- §3.2 依赖方向：`production` / `core` **不得** import `ops`。漏斗 taxonomy 与 `compute_true_yield_rate`/`record_funnel_event` 的实现在 `packages.core.observability.funnel`（ops `__init__.py` 再导出复用），ops 在其上做 §9/§26 指标计算——别在 ops 重写漏斗底座。
- 余额 poller 失败映射为状态字段、不抛异常；后台轮询默认 OFF（`settings.balance.poller_enabled`），无 key / 测试环境不外呼。

## 测试
- `pytest tests/ops tests/observability`（指标计算 / 漏斗 / outbox / metrics）。

## 注意 / 坑
- `reconcile_billing` 当前只写审计并返回 `status="queued"`，是占位，勿当成已完成对账。
- 改 Ops contract 后须重新生成 openapi.json + schema.d.ts（`scripts/export_openapi.py`）。
