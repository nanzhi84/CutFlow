# Cutagent（树影） · Clean-Slate

Case-first 数字人短视频内容生产系统。Python（FastAPI + Temporal）+ TypeScript（React/Vite）monorepo，**contract-first**：FastAPI 是 OpenAPI 唯一事实源。产品/上手详见 `README.md`，长期文档入口见 `docs/README.md`，能力边界以当前代码、README 与关键设计决策为准。

## 仓库地图（改对应代码前先读该目录的 CLAUDE.md）

- `apps/`：`api`（FastAPI）· `worker`（Temporal worker，独立进程）· `web`（React/Vite SPA）· `connectors`（OceanEngine 离线 ETL CLI）
- `packages/`：`core`（contracts/storage【对象存储 local/S3/tiered + presigned upload、secret 信封加密】/config/auth/observability/workflow）· `ai`（gateway/prompts/providers）· `creative`（Case/脚本/自进化）· `media` · `planning` · `production`（活动流水线：主链 `digital_human_v2` 19 节点、Agent 链 `digital_human_editing_agent_v2` 20 节点、`seedance_t2v_v1` 5 节点；v1 Agent 已删除且不可新建/重试/恢复；纯 B-roll 画外音走主链 `broll.mode="full_coverage"`）· `publishing` · `ops` · `migrations`（保留目录约定，**非** Alembic）
- `tests/`（按域）· `scripts/` · `deploy/`（Temporal 配置）· `docs/`（入口：`docs/README.md`）

## 关键命令

```bash
scripts/dev_up.sh up                 # 一键起 infra+API+worker+web（down|status|logs api|worker|web）
pip install -e ".[dev]" ; (cd apps/web && npm install)
docker compose up -d postgres redis minio temporal temporal-ui
python scripts/bootstrap_database.py # alembic upgrade head + 种子（仅迁移：scripts/migrate.py）
python -m uvicorn apps.api.main:app --reload --port 8000
python -m apps.worker                # 独立进程
(cd apps/web && npm run dev)
python -m pytest -q                  # 默认套件（含 SQL 集成；Temporal 未置 flag 会 skip）
uv run --extra dev python scripts/export_openapi.py && (cd apps/web && npm run generate:api)   # 改契约后重生成
CUTAGENT_ENV=production python scripts/preflight.py   # 部署前配置预检；API/worker 生产启动也会 fail closed
python scripts/provision_oss_cors.py # S3/OSS 浏览器直传上传前配置 CORS + staging lifecycle
```

## 全局约定（必须遵守）

- **Contract-first**：改任何 API 形状 → 必须重生成 `apps/web/src/api/openapi.json` + `schema.d.ts`（CI 校验漂移）。`schema.d.ts` 是生成物，**禁止手改**。
- 领域类型唯一来源 `packages/core/contracts`（Pydantic v2），跨包共享走它。
- DB schema 迁移**只**在 `packages/core/storage/alembic/versions/`（当前 `0001…0062`，单一 head `0062_drop_v1_prompts`；`0058` 增加可恢复上传，`0059` 发布 BGM Agent prompt，`0060` 收敛 CreativeIntent 字幕提示，`0061` 清理旧字幕/花字/PostProcess 数据，`0062` 删除 v1 Agent prompt 与诊断；历史 prompt 迁移必须内联冻结、不得读取可变 seed JSON；`0014` 合并过早期双 `0012` 分支，两个 `0018` 文件是线性顺接、非分叉）。
- 存储/运行时/对象存储后端由 `Settings`（`CUTAGENT_*` env）切换，清单见 `.env.example`。
- 浏览器上传走 `/api/uploads/prepare` → object-store presigned PUT → `/api/uploads/complete`；API 不代理文件字节，complete 阶段验证 HEAD/sha256/content-type/媒体探测并登记产物。
- 外部 AI/媒体调用一律经 `ProviderGateway` 按能力分发；prompt 不得硬编码，经 registry + binding，生产只解析 published 版本。
- 真实 provider 未配置时**显式报错**；`CUTAGENT_ALLOW_SANDBOX_FALLBACK=1` 才回退 sandbox。
- Secret（provider key）只进 `SecretStore`/`ProviderProfile`，**绝不**进 env/代码。
- 降级必须显式上报（分级 degradations），不静默降级；素材选择确定性、不随机（ledger 近期降权）。
- CI workflow `CI` 包含 `unit`、`production-preflight`、`integration`、`frontend`、`redis-coordination`；本地全量门禁入口仍是 `scripts/ci_gate.sh`。

## 关键坑

- `worker` 是独立长驻进程：改 `packages/production` / 节点代码后要**重启 worker**（不只是 API）。
- Postgres 主机端口是 **55432**（避让本地 5432）；MinIO 9000/9001、Temporal 7233 / UI 8080。
- 存储后端只支持 `sqlalchemy`/`postgres`（内存后端已移除，配 `CUTAGENT_STORAGE_BACKEND=memory` 会显式报错）；缺 `CUTAGENT_DATABASE_URL` 会显式启动失败；测试全连真实 Postgres（见 `tests/CLAUDE.md`）。
- `LocalObjectStore` 的 presign 是 dev/test 替身；生产直传上传需 `CUTAGENT_OBJECTSTORE_BACKEND=s3`/OSS，并先配置 bucket CORS/lifecycle。
- `CUTAGENT_REDIS_URL` 让事件 fanout、stream token、provider limiter、登录/注册防爆破计数跨进程协调（可选协调层，非查询缓存）；其中**前三项**退化到进程内且 `CUTAGENT_REDIS_REQUIRED=1` 时会让 `/api/health/ready` 返回 503 摘流，认证限流不在 readiness 判定内（无 30s 重连、无 degraded 遥测）。详见 `docs/architecture/redis-coordination.md`。
- Temporal 测试需指向**共享 MinIO** 的 ephemeral 桶，节点本地 ephemeral 会被 fail-fast 拒绝。
- lint：ruff（line-length 100，配置在 `pyproject.toml`）。
