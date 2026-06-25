# packages/core

跨包共享的基础包：Pydantic v2 领域契约、状态机、存储/迁移、配置、认证、可观测性、工作流运行时适配。所有其他包（apps/api、worker、providers 等）都依赖它，是整个系统的"地基 + 单一事实源"。

## 职责
- 定义所有跨包领域类型：`contracts/` 下按域拆分（base/providers/jobs/auth/media/prompts/cases/publishing/ops/preferences/publish_accounts），经 `contracts/__init__.py` 统一 re-export，`__all__` 即权威公开面。**例外**：`contracts/artifacts.py`（`MaterialCandidate`/`CreativeIntentArtifact`/`NarrationUnit` 等）**不**走 `__init__.py` 出口（不 re-export/不入 `__all__`），下游一律直接 `from packages.core.contracts.artifacts import ...`。
- 定义全部状态机与合法迁移（job/run/node/provider/prompt_version/case_memory/case_rubric/rubric_bump/upload_session/publish_*），由 `assert_transition()` 强制。
- 提供持久化层：`storage/database.py`（SQLAlchemy ORM/`Base.metadata`）、`storage/repository.py`（内存 `Repository`）、seed 与 Alembic 迁移。
- 集中基础设施配置（`config/settings.py`）、argon2 认证与限流（`auth/`）、Prometheus 遥测与 outbox/funnel（`observability/`）、工作流运行时适配（`workflow/`）、对象存储与密钥库（`storage/`）。

## 关键文件 / 子目录
- `contracts/__init__.py` — 契约统一出口；新增/改契约后这里 re-export + `__all__` 必须同步。
- `contracts/base.py` — `ContractModel`(extra="forbid")、`ErrorCode`/`WarningCode`/`DegradationCode`、各 Status 枚举、`ArtifactKind`、`Money`。
- `contracts/state_machines.py` — 各域 `*_TRANSITIONS` 表 + `assert_transition(kind, from, to)`。
- `config/settings.py` — `build_settings()`/`Settings`（按域分组，frozen）、`sandbox_fallback_allowed()`；`CUTAGENT_*` env 在调用时读取，无模块级单例。
- `storage/database.py` / `repository.py` / `bootstrap.py` — ORM 后端 / 内存后端 / 按 `storage.backend`(memory|sqlalchemy|postgres) 选型。
- `storage/alembic/versions/` — 0001..0022（单一 head `0022_drop_publish_hashtags`），仓库内**唯一**的 Alembic 迁移目录。
- `storage/seed.py` / `seed_media.py` / `provider_seed.py` — 用户/注册码、媒体、provider 配置 seed。
- `storage/secret_store.py` — `SecretStore` 协议 + `LocalSecretStore`；自 `0017` 起做 **Fernet 信封加密**（`envelope_prefix = "fernet:v1:"`，key 来源 `CUTAGENT_SECRET_ENCRYPTION_KEY`，缺省落盘 `.db_encryption_key`），密钥不入 env/Settings。
- `storage/` 其余基建：对象存储 `object_store.py` / `tiered_object_store.py`；SQLAlchemy 后端实现 `sqlalchemy_secrets.py` / `sqlalchemy_uploads.py` / `sqlalchemy_idempotency.py`；`selection_ledger.py`（选材账本，确定性近期降权）、`row_mapper.py`（ORM 行 ↔ 契约映射）。
- `observability/` — `events.py`（SSE 事件 fanout hub + outbox dispatcher）、`outbox.py`（outbox writer + replay）、`failure_taxonomy.py`（失败分类）、`funnel.py`（漏斗）、`telemetry.py`（Prometheus 遥测）。
- `auth/` — `service.py`（认证服务 + argon2 哈希）/ `sqlalchemy_service.py`、`password_policy.py`（口令强度校验：长度/字符种类/弱口令黑名单）、`rate_limit.py`（限流）。
- `registration_codes.py`（包根）— 注册码盐 + 哈希（生成在 `auth/sqlalchemy_service.py`）。
- `workflow/runtime.py` — `WorkflowRuntimeAdapter` 协议、`NodeExecutionError`、`canonical_json`/`manifest_hash`；`temporal_adapter.py` 的 `TemporalRuntimeAdapter` 为 Temporal 实现。

## 约定与要求
- Contract-first：契约是跨包事实源，下游对照它生成 openapi.json / schema.d.ts；改了契约这些产物需重生成。
- `ContractModel` 设 `extra="forbid"`，未声明字段会报错；新契约务必继承它并在 `__init__.py` 同步导出。
- 状态变更一律走 `assert_transition()`，禁止绕过状态机直接改 status。
- 密钥只存 `SecretStore`/`ProviderProfile`，**永不**进 env 或 `Settings`（settings 仅放 infra/policy）。
- 所有 Alembic revision 只能放在 `storage/alembic/versions/`，保持单一 head（当前 `0022`）；链中存在过 merge revision（`0014` 合并两支）。
- 配置经 `build_settings()` 取快照（frozen、调用时读 env），勿引入缓存单例。

## 测试
- `pytest tests/core`（`test_password_policy.py` / `test_row_mapper.py`）；契约/状态机/DB schema 相关另见 `tests/contract`。多数测试以 `CUTAGENT_STORAGE_BACKEND=memory` 跑内存后端。

## 注意 / 坑
- `sandbox_fallback_allowed()` 默认 False：真实运行只走真 provider、无 provider 时显式报错，绝不静默降级到 sandbox；测试经 conftest 置 `CUTAGENT_ALLOW_SANDBOX_FALLBACK=1` 才走 sandbox 路径。
- 改契约只改 Pydantic 类还不够，须同步 `__init__.py` 的 import + `__all__`，否则下游 `from packages.core.contracts import X` 失败。
- `alembic/env.py` 优先读 `CUTAGENT_DATABASE_URL`，离线/CI 回退 alembic.ini 的 sqlalchemy.url。
