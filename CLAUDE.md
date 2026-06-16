# Cutagent（树影） · Clean-Slate

Case-first 数字人短视频内容生产系统。Python（FastAPI + Temporal）+ TypeScript（React/Vite）monorepo，**contract-first**：FastAPI 是 OpenAPI 唯一事实源。产品/上手详见 `README.md`，能力权威清单见 `docs/树影_Cutagent_CleanSlate重写Spec_v3_2026-06-11.md`（§2 / §34）。

## 仓库地图（改对应代码前先读该目录的 CLAUDE.md）

- `apps/`：`api`（FastAPI）· `worker`（Temporal worker，独立进程）· `web`（React/Vite SPA）· `connectors`（OceanEngine 离线 ETL CLI）
- `packages/`：`core`（contracts/storage/config/auth/observability/workflow/对象存储/secret）· `ai`（gateway/prompts/providers）· `creative`（Case/脚本/自进化）· `media` · `planning` · `production`（16 节点流水线）· `publishing` · `ops` · `migrations`（遗留资产导入，**非** Alembic）
- `tests/`（按域）· `scripts/` · `deploy/`（Temporal 配置）· `docs/`

## 关键命令

```bash
scripts/dev_up.sh up                 # 一键起 infra+API+worker+web（down|status|logs api|worker|web）
pip install -e ".[dev]" ; (cd apps/web && npm install)
docker compose up -d postgres redis minio temporal temporal-ui
python scripts/bootstrap_database.py # alembic upgrade head + 种子（仅迁移：scripts/migrate.py）
python -m uvicorn apps.api.main:app --reload --port 8000
python -m apps.worker                # 独立进程
(cd apps/web && npm run dev)
python -m pytest -q                  # 单测；完整门禁 scripts/ci_gate.sh
python scripts/export_openapi.py && (cd apps/web && npm run generate:api)   # 改契约后重生成
```

## 全局约定（必须遵守）

- **Contract-first**：改任何 API 形状 → 必须重生成 `apps/web/src/api/openapi.json` + `schema.d.ts`（CI 校验漂移）。`schema.d.ts` 是生成物，**禁止手改**。
- 领域类型唯一来源 `packages/core/contracts`（Pydantic v2），跨包共享走它。
- DB schema 迁移**只**在 `packages/core/storage/alembic/versions/`（当前 `0001…0011`）。
- 存储/运行时/对象存储后端由 `Settings`（`CUTAGENT_*` env）切换，清单见 `.env.example`。
- 外部 AI/媒体调用一律经 `ProviderGateway` 按能力分发；prompt 不得硬编码，经 registry + binding，生产只解析 published 版本。
- 真实 provider 未配置时**显式报错**；`CUTAGENT_ALLOW_SANDBOX_FALLBACK=1` 才回退 sandbox。
- Secret（provider key）只进 `SecretStore`/`ProviderProfile`，**绝不**进 env/代码。
- 降级必须显式上报（分级 degradations），不静默降级；素材选择确定性、不随机（ledger 近期降权）。

## 关键坑

- `worker` 是独立长驻进程：改 `packages/production` / 节点代码后要**重启 worker**（不只是 API）。
- Postgres 主机端口是 **55432**（避让本地 5432）；MinIO 9000/9001、Temporal 7233 / UI 8080。
- 默认存储后端是 `sqlalchemy`：缺 `CUTAGENT_DATABASE_URL` 会显式启动失败；演示/测试内存模式需显式设 `CUTAGENT_STORAGE_BACKEND=memory`。
- Temporal 测试需指向**共享 MinIO** 的 ephemeral 桶，节点本地 ephemeral 会被 fail-fast 拒绝。
- lint：ruff（line-length 100，配置在 `pyproject.toml`）。
