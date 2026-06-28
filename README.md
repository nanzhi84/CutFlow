# Cutagent（树影） · Clean-Slate

> **Case 优先（Case-first）的数字人短视频内容生产系统。**
> 围绕「Case（账号/品牌长期边界）→ 脚本生成 → 数字人成片 → 发布 → 数据回流 → 自进化」打通全链路，并以 **contract-first** 的方式把每一条能力落到可校验的接口契约上。

本仓库是对 [`docs/树影_Cutagent_CleanSlate重写Spec_v3_2026-06-11.md`](docs/树影_Cutagent_CleanSlate重写Spec_v3_2026-06-11.md) 所描述系统的一次 **clean-room 重写**：FastAPI 即 OpenAPI 的唯一事实源，领域类型用 Pydantic v2 表达，数据层用 SQLAlchemy 2 + Alembic，长流程编排走 Temporal，前端类型从 OpenAPI 自动生成。Spec §2「必须保留的用户流程」是能力保留的权威清单。

---

## 核心功能

| 能力域 | 说明 | 主要代码 |
| --- | --- | --- |
| **Case 工作台 & 自进化闭环** | Case 是所有脚本/成片/记忆/素材/指标的长期学习边界。Case Agent 生成 `ScriptDraft` 并采用为版本化脚本；生成时写入盲预测，采用/成片/发布/删片/指标回填写入奖励信号，复盘时用评分卡校准并在升版时人工确认。`CaseMemory` 仅保留为人工维护的品牌红线/硬约束，不再承载自动学习审批流。 | `apps/api/routers/{cases,case_agent,case_rubric}.py`、`packages/creative/cases/` |
| **16 节点数字人生产流水线** | 一个 `DigitalHumanVideo` Job 触发 `WorkflowRun`，按固定节点序执行：`Validate → LoadCaseContext → ResolveCreativeIntent → TTS → 素材规划 → 旁白对齐 → 立绘/B-roll/风格/时间线规划 → 立绘轨构建 → LipSync → 渲染 → 字幕+BGM 混音 → 导出 → 收尾报告`。产出**带类型的 artifacts**、运行报告、用量记录与**分级降级（不静默降级）**。 | `packages/production/pipeline/digital_human.py`、`packages/production/pipeline/nodes/` |
| **媒体内核** | TTS 字幕 → 强制对齐 → ASR 的对齐策略（strict 模式无估算回退）；字幕渲染为 ASS；B-roll 用 jieba 关键词/语义匹配脚本节拍（不足则软跳过）；BGM 混音；封面默认抽帧、可选 AI 封面。底层 ffmpeg/ffprobe 驱动。 | `packages/media/{audio,video,annotation}/`、`packages/planning/material/` |
| **LipSync providers** | 口型同步是一种 provider 能力（`lipsync.video`）：默认 RunningHub HeyGem，备选 DashScope VideoReTalk，禁用时节点可透传跳过。 | `packages/ai/providers/{runninghub,videoretalk}.py` |
| **Jobs / Runs 控制** | 单条或批量建 Job、查看 Run 详情/artifacts/报告；运行控制：cancel/force-cancel、retry（全新重跑）、resume（复用有效 artifacts）；WebSocket `/ws/runs/{id}` 实时进度。 | `apps/api/routers/jobs_runs.py`、`packages/production/pipeline/reuse.py` |
| **素材 / 标注 / 音色库** | 立绘与 B-roll 素材库（上传/筛选/稳定化/替换）+ 基于 VLM 的 AI 标注流水线与标注编辑器；音色库（克隆/设计/试听）。素材选择**确定性**、按 ledger 做近期降权，不随机（spec §2.4）。 | `apps/api/routers/{media,voices}.py`、`packages/media/`、`packages/planning/selection/` |
| **成片交付 & 发布中心** | 成片列表/预览/下载（签名 URL）；剪映（Jianying）草稿包与编辑器交接包；发布中心：构建发布包 → 组装批次（多平台目标）→ 提交 → 单条重试 → 查看 attempts，自动生成文案/封面。发布是隔离边界（spec §13）。 | `apps/api/routers/{finished_videos,publishing}.py`、`packages/{production,publishing}/` |
| **Prompt Registry & Ops** | 生产 prompt 全部在注册表中（节点内不硬编码）：版本走 `draft→reviewing→approved→published→deprecated/rolled_back` 生命周期，binding 把 published 版本绑定到 `node_id`（可按 case/provider/env 灰度），生产只解析 published，支持回滚与 A/B。 | `apps/api/routers/prompts.py`、`packages/ai/prompts/registry.py` |
| **Provider 网关 & 计费治理** | 所有外部 AI/媒体调用按能力（`llm.chat`/`vlm.annotation`/`tts.speech`/`asr.transcribe`/`lipsync.video`/`image.generate`）经 `ProviderGateway`；管理 provider profile、价格目录（受治理审批）、用量与余额、对账；secret 独立存储/轮换。 | `apps/api/routers/{providers,secrets}.py`、`packages/ai/gateway/`、`packages/ops/balance/` |
| **运营中台 & 可观测** | Ops 看板、成本汇总、成品率漏斗、provider 用量、预算（admin）、告警（ack/resolve）、生产质检、审批、审计日志；Prometheus `/metrics`。 | `apps/api/routers/ops.py`、`packages/ops/`、`packages/core/observability/` |
| **数据接入 & 账号** | 巨量引擎 / OceanEngine **离线导入 connector**（独立进程，把 XLSX 归档归一化为指标导入，喂给表现闭环）；竞品参考脚本提取（ASR，cookie 手动导入）；通用导入批次；Auth & 三级 RBAC（viewer/operator/admin）+ 分片上传。 | `apps/connectors/oceanengine/`、`apps/api/routers/{auth,uploads,imports,creative}.py`、`packages/core/auth/` |

---

## 架构概览

**Contract-first，可插拔**：

- **契约即事实源**：FastAPI 暴露 OpenAPI，前端 TS 类型由 `openapi-typescript` 从中生成；领域类型为 `packages/core/contracts` 的 Pydantic v2 模型；库表由 `packages/core/storage/alembic/versions/` 的 Alembic 迁移（当前 `0001…0022`，单一 head `0022_drop_publish_hashtags`）定义。
- **可插拔存储**：`CUTAGENT_STORAGE_BACKEND=memory|sqlalchemy|postgres`（memory 仅供测试/演示，postgres 是 SQLAlchemy 后端别名）。
- **可插拔运行时**：`CUTAGENT_WORKFLOW_RUNTIME=local|temporal`；Temporal 模式下由独立 worker 在 `cutagent-production` 队列消费。
- **Provider 网关**：按能力分发，注册 sandbox 与真实插件；真实路径需「插件 + 已激活 secret」，否则**显式报错而非静默降级**（除非 `CUTAGENT_ALLOW_SANDBOX_FALLBACK=1`）。
- **分层对象存储**：`TieredObjectStore` 在 durable 与 ephemeral 两层之间路由（多 worker 下 ephemeral 必须是共享的 MinIO/S3，不能是节点本地）。
- **Secret 隔离**：provider API key 只存于 `SecretStore`/`ProviderProfile`，**永不**进入 env/Settings。

**端到端流**：HTTP 请求 → 准入 Job/Run → 运行时适配器 → 节点 runner（16 节点）→ 按能力解析 provider → 写入带类型 artifacts 到对象存储 → 报告/用量/降级 → outbox 去重派发到 SSE/WebSocket → Prometheus 指标。

---

## 技术栈

| 层 | 选型 |
| --- | --- |
| 语言 / API | **Python ≥3.12**、FastAPI ≥0.124、Pydantic v2、Uvicorn、httpx |
| 数据 | **PostgreSQL 16 + pgvector**、SQLAlchemy 2、Alembic、psycopg v3（`postgresql+psycopg`） |
| 编排 | **Temporal**（`temporalio` ≥1.28）+ 独立 worker |
| 对象存储 | S3 兼容分层存储（boto3）：本地 **MinIO**，可换 AWS S3 / 阿里云 OSS |
| 媒体 / AI | ffmpeg·ffprobe、opencv-headless、PySceneDetect、Silero VAD（`pysilero-vad`）、jieba、yt-dlp |
| Providers | MiniMax(TTS)、DashScope(ASR/VLM/LLM/VideoReTalk)、RunningHub HeyGem(LipSync)、OpenAI(image) + sandbox |
| 认证 / 观测 | argon2-cffi（口令哈希）、prometheus-client（`/metrics`） |
| 缓存 / 协调 | Redis 7（已在 compose 中预置，作为跨进程限流的目标后端；当前限流器为进程内实现） |
| 前端 | React 18、Vite 6、TypeScript 5.7、Tailwind 3、TanStack Query 5、react-router 6、openapi-typescript 7 |

> 版本为 `pyproject.toml`/`package.json` 的下限/范围约束；compose 固定具体镜像（`pgvector/pgvector:pg16`、`redis:7`、`minio/minio:RELEASE.2025-09-07T16-13-09Z`、`temporalio/auto-setup:1.26`、`temporalio/ui:2.34.0`）。

---

## 仓库结构

```
apps/
  api/          FastAPI 服务：routers/（接口）+ services/（业务）；ASGI 入口 apps.api.main:app
  worker/       Temporal worker（独立进程，消费 cutagent-production 队列）
  web/          React + Vite 单页控制台；src/api/ 内含从 OpenAPI 生成的类型
  connectors/   离线 ETL connector（OceanEngine/巨量引擎），独立 CLI 进程
packages/
  core/         地基：contracts（Pydantic）/ storage（SQLAlchemy + Alembic + seed）/ config / auth / observability / workflow / 对象存储 / secret store
  ai/           gateway（按能力分发）/ prompts（注册表 + 绑定）/ providers（各家插件 + sandbox）
  creative/     Case 领域、脚本生成、Case Agent、自进化学习、参考提取
  media/        素材、AI 标注（VLM 传感器）、音频（强制对齐/VAD）、视频（ffmpeg）、封面、音色桥接
  planning/     素材匹配（jieba）、确定性选择（recency ledger）、剪辑规划
  production/   生产流水线（16 节点）、复用、字幕、剪映草稿、编辑器交接
  publishing/   发布仓储与平台适配、文案/封面生成
  ops/          成品率漏斗、余额/对账、预算/告警、Ops 仓储
  migrations/   迁移目录约定（仅 CLAUDE.md，**非** Alembic；DB 迁移在 packages/core/storage/alembic）
tests/          按域组织的 pytest（约 140 文件）：api/core/creative/media/.../integration/temporal/golden/contract
scripts/        bootstrap_database · migrate · export_openapi · gc_objectstore · dev_up.sh · ci_gate.sh 等
deploy/         Temporal 动态配置（无 k8s/terraform 清单）
docs/           Spec、ROADMAP、milestones/、ops/、audit/
```

每个 `apps/*`、`packages/*` 和 `tests/` 目录下都有一份精简的 `CLAUDE.md`，说明该模块用途与约定。

---

## 快速开始

### 前置依赖

- **Python 3.12+**、**Node.js 22**
- **ffmpeg / ffprobe** 在 `PATH` 上（或用 `CUTAGENT_FFMPEG_BIN`/`CUTAGENT_FFPROBE_BIN` 指定）
- **Docker + Docker Compose v2**

### 方式 A：一键开发（推荐）

`scripts/dev_up.sh` 会幂等地拉起 infra（docker）、执行 DB 迁移与 seed，然后启动 API + worker + web：

```bash
cp .env.example .env.local        # 按需改 .env.local（dev_up 读取它，或 CUTAGENT_ENV_FILE）
python3.12 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
( cd apps/web && npm install )

scripts/dev_up.sh up              # 启动全部；默认 API :8000、web :8001
scripts/dev_up.sh status          # 查看 infra / 进程 / 端口
scripts/dev_up.sh logs api        # 跟踪 api|worker|web 日志
scripts/dev_up.sh down            # 停应用进程（infra 保留；down --infra 连 docker 一起停）
```

### 方式 B：手动分步

```bash
# 1) 安装依赖
python -m pip install -e ".[dev]"
( cd apps/web && npm install )

# 2) 拉起基础设施（端口：Postgres 55432→5432 · Redis 6379 · MinIO 9000/9001 · Temporal 7233 · Temporal UI 8080）
docker compose up -d postgres redis minio temporal temporal-ui

# 3) 配置 SQLAlchemy + Temporal + 共享 MinIO 对象存储
#    这些变量必须在 bootstrap/API/worker 启动前设置：Temporal 模式禁止 node-local ephemeral。
export CUTAGENT_STORAGE_BACKEND=sqlalchemy
export CUTAGENT_DATABASE_URL=postgresql+psycopg://cutagent:cutagent@localhost:55432/cutagent
export CUTAGENT_WORKFLOW_RUNTIME=temporal
export CUTAGENT_TEMPORAL_ADDRESS=localhost:7233
export CUTAGENT_TEMPORAL_NAMESPACE=default
export CUTAGENT_TEMPORAL_TASK_QUEUE=cutagent-production
export CUTAGENT_OBJECTSTORE_BACKEND=s3
export CUTAGENT_OBJECTSTORE_ENDPOINT=http://127.0.0.1:9000
export CUTAGENT_OBJECTSTORE_BUCKET=cutagent-local
export CUTAGENT_OBJECTSTORE_ACCESS_KEY=minioadmin
export CUTAGENT_OBJECTSTORE_SECRET_KEY=minioadmin
export CUTAGENT_OBJECTSTORE_ADDRESSING_STYLE=path
export CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND=s3
export CUTAGENT_EPHEMERAL_OBJECTSTORE_ENDPOINT=http://127.0.0.1:9000
export CUTAGENT_EPHEMERAL_OBJECTSTORE_BUCKET=cutagent-ephemeral
export CUTAGENT_EPHEMERAL_OBJECTSTORE_ACCESS_KEY=minioadmin
export CUTAGENT_EPHEMERAL_OBJECTSTORE_SECRET_KEY=minioadmin
export CUTAGENT_EPHEMERAL_OBJECTSTORE_ADDRESSING_STYLE=path

# 4) 初始化数据库：alembic upgrade head + 种子用户/媒体（仅迁移用 scripts/migrate.py）
python scripts/bootstrap_database.py

# 5) 启动 API（默认/推荐 SQLAlchemy 后端；缺 DATABASE_URL 会显式失败）
python -m uvicorn apps.api.main:app --reload --port 8000

# 6) 启动 Temporal worker（独立进程，改代码需重启；继承上面的 env）
python -m apps.worker

# 7) 启动前端
( cd apps/web && npm run dev )    # Vite http://127.0.0.1:5173
```

**改了 API 契约后**重新生成类型（CI 会校验漂移）：

```bash
uv run --extra dev python scripts/export_openapi.py  # 写 apps/web/src/api/openapi.json
( cd apps/web && npm run generate:api )           # 生成 src/api/schema.d.ts
```

### 本地种子账号

- `admin@local.cutagent` / `local-admin`（admin）
- `viewer@local.cutagent` / `local-viewer`（viewer）

---

## 配置 / 环境变量

所有变量映射到 `packages/core/config` 的 `Settings`（见 `build_settings()`）；完整清单与默认值见 [`.env.example`](.env.example)。常用：

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `CUTAGENT_STORAGE_BACKEND` | `sqlalchemy` | 存储后端：`sqlalchemy` \| `postgres` \| `memory`（memory 仅测试/演示，postgres 是 SQLAlchemy 后端别名） |
| `CUTAGENT_DATABASE_URL` | — | SQLAlchemy 后端必填 |
| `CUTAGENT_WORKFLOW_RUNTIME` | `local` | 运行时：`local` \| `temporal` |
| `CUTAGENT_OBJECTSTORE_BACKEND` | `local` | 对象存储：`local` \| `s3`（MinIO/S3/OSS） |
| `CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND` | `local` | scratch 对象存储：`local` \| `s3`；Temporal 模式必须指向共享 MinIO/S3 |
| `CUTAGENT_ALLOW_SANDBOX_FALLBACK` | `false` | `1` 才允许无真实 provider 时静默回退 sandbox；默认**显式报错不降级** |
| `CUTAGENT_REGISTRATION_OPEN` | `true` | 是否开放自助注册 |
| `CUTAGENT_DISABLE_BACKGROUND_DISPATCHER` | — | `1` 关闭进程内 outbox 派发 |

如需纯演示/测试用内存仓储，必须显式设置 `CUTAGENT_STORAGE_BACKEND=memory`；默认值是 `sqlalchemy`，因此默认启动需要 `CUTAGENT_DATABASE_URL`。
Temporal runtime 下 durable 与 ephemeral 对象存储都应使用共享 MinIO/S3（且 bucket 不同），否则启动时会 fail-fast，避免跨 worker 读不到 ephemeral artifacts。

> **Secret 不进 env**：provider API key 等敏感信息由 `SecretStore`/`ProviderProfile` 管理，`.env` 里只有基础设施连接参数。

---

## 测试与 CI

```bash
# 默认单测（不需基础设施）
timeout -k 5 600 python -m pytest -q

# DB 集成测试（opt-in，需 Postgres；复用“方式 B”的 SQLAlchemy env）
export CUTAGENT_RUN_DB_TESTS=1
python -m pytest -q tests/integration

# Temporal 测试（opt-in，需 Temporal + 共享 MinIO；复用“方式 B”的 Temporal/S3 env）
export CUTAGENT_RUN_TEMPORAL_TESTS=1
python -m pytest -q tests/temporal

# 完整本地验收门禁（镜像 .github/workflows/ci.yml；需 docker compose 已起）
scripts/ci_gate.sh
```

`scripts/ci_gate.sh` 会跑单测、校验 `openapi.json`、`npm ci` + 生成类型并检查 `schema.d.ts` 漂移、构建前端、初始化 DB、跑集成与 Temporal 测试。

---

## 运维脚本

| 脚本 | 用途 |
| --- | --- |
| `scripts/gc_objectstore.py --max-age-hours 24 --apply` | 回收旧的生成类对象（不带 `--apply` 为 dry-run）；见 [`docs/ops/objectstore-gc.md`](docs/ops/objectstore-gc.md) |
| `scripts/migrate.py` | 仅执行 Alembic `upgrade head` |

阿里云 OSS 后端（云 ASR strict 时间戳对齐）配置见 [`docs/ops/objectstore-oss.md`](docs/ops/objectstore-oss.md)。

---

## 实现状态

采用 contract-first：Spec §34 的接口与 16 节点编排已完整接通，前后端契约一致。当前阶段仍在推进：多数读写路径正逐步迁移到 DB 会话，生产级 Temporal SDK 适配与跨进程幂等仍在完善；部分 provider/媒体实现提供 **sandbox 模式**便于本地开发。真实接入通过 `ProviderProfile` + 已激活 `Secret` 启用，未配置且未开 `CUTAGENT_ALLOW_SANDBOX_FALLBACK` 时会显式报错而非静默降级。能力保留的权威清单见 Spec §2。
