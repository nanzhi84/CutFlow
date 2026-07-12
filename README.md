# Cutagent（树影） · Clean-Slate

树影是一个 Case-first 的数字人短视频生产系统。它把账号/品牌的长期边界、脚本生成、素材选择、数字人成片、发布、数据回流和复盘学习放进同一条可审计的生产链路里。

它不是一个“生成一次视频”的小工具，而是一套面向重复生产的内容操作系统：

- 以 Case 作为长期记忆和策略边界。
- 以 FastAPI OpenAPI 作为前后端契约事实源。
- 以 Temporal 承载长流程生产，API 只负责准入和控制。
- 以 ProviderGateway 管理外部 AI/媒体能力，真实 provider 未配置时显式失败。
- 以 typed artifacts、运行报告、用量记录和降级事件保留每次生产的证据链。

当前文档入口在 [docs/README.md](docs/README.md)。长期设计记录只保留 milestone、关键技术选型和关键设计决策。

## 产品能力

**Case 工作台**

Case 是账号、品牌、素材、脚本、指标和学习反馈的长期边界。Case Agent 生成脚本草稿，成片和发布结果回流到评分卡与复盘流程，帮助下一轮内容更贴近目标账号。

**数字人生产流水线**

一条 `DigitalHumanVideo` job 会生成 `WorkflowRun`，按固定节点完成校验、Case 上下文加载、创作意图解析、TTS、素材规划、旁白对齐、立绘和 B-roll 规划、LipSync、渲染、字幕与 BGM 混音、成片导出和最终报告。

系统保留主链模板和专用生成模板；纯 B-roll 画外音不再是独立模板，而是主链的 `broll.mode="full_coverage"`：

- `digital_human_v2`：主链数字人成片。
- `digital_human_editing_agent_v2`：活动剪辑 Agent 链；媒体选择与成片字幕/BGM 后处理拥有独立规划、调用、修复和复用边界。
- `digital_human_editing_agent_v1`：仅保留给历史任务恢复的 legacy 模板，不再接受新任务。
- `seedance_t2v_v1`：Seedance 文生视频链路。

**素材和媒体内核**

素材库覆盖立绘、B-roll、BGM、字体、封面模板和音色。选择策略是确定性的：脚本节拍与素材标注匹配，近期使用通过 ledger 降权，不随机抽取。

媒体侧以 ffmpeg/ffprobe 为底座，支持 TTS、ASR/强制对齐、ASS 字幕、BGM 混音、封面抽帧、AI 封面和剪映/编辑器交接包。

**Provider 与 Prompt 治理**

所有外部 AI/媒体调用都经 `ProviderGateway` 按能力分发，例如 `llm.chat`、`vlm.annotation`、`tts.speech`、`asr.transcribe`、`lipsync.video`、`image.generate`、`video.generate`。

生产 prompt 不写死在节点里，而是通过 Prompt Registry、版本生命周期和 binding 解析。Provider key 只进入 `SecretStore`/`ProviderProfile`，不进入 env 或代码。

**发布和运营**

成片可预览、下载、打包、生成发布文案和封面，并进入发布中心。发布侧以 adapter 隔离平台能力，当前生产默认走小V猫 CDP，sandbox 只在显式配置时使用。

Ops 提供成本、成品率、provider 用量、余额、预算、告警、质检、审批和审计事件。降级和成本必须显式记录，不允许静默吞掉。

## 架构骨架

树影的工程边界很清晰：

- `apps/api` 是 FastAPI 服务，注册所有 router，装配 auth、storage、workflow、provider、prompt、outbox 和 balance poller。
- `apps/worker` 是独立 Temporal worker，消费生产任务队列。
- `apps/web` 是 React/Vite 控制台，类型来自后端 OpenAPI。
- `apps/connectors` 是离线 ETL connector，目前覆盖 OceanEngine/XLSX。
- `packages/core` 放契约、配置、存储、鉴权、观测、工作流抽象、对象存储和 secret store。
- `packages/ai` 放 provider gateway、prompt registry 和真实 provider 插件。
- `packages/creative` 放 Case、脚本、自进化、评分卡和参考提取。
- `packages/media` 放素材、标注、音频、视频、封面、渲染辅助。
- `packages/planning` 放素材匹配、确定性选择和剪辑规划纯函数。
- `packages/production` 放生产流水线、节点、复用、成片仓储、剪映草稿和编辑器交接。
- `packages/publishing` 放发布仓储、账号、文案/封面节点和平台 adapter。
- `packages/ops` 放成本、成品率、预算、告警、余额和熔断。

数据库迁移只在 `packages/core/storage/alembic/versions/`。`packages/migrations` 只是目录约定占位，不是 Alembic 目录。

## 本地启动

前置依赖：

- Python 3.12+
- Node.js 22
- Docker + Docker Compose v2
- `ffmpeg` / `ffprobe` 在 `PATH` 上，或用 `CUTAGENT_FFMPEG_BIN` / `CUTAGENT_FFPROBE_BIN` 指定

推荐一键启动：

```bash
cp .env.example .env.local
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
( cd apps/web && npm install )

scripts/dev_up.sh up
scripts/dev_up.sh status
scripts/dev_up.sh logs api
scripts/dev_up.sh logs worker
scripts/dev_up.sh logs web
```

停止应用进程：

```bash
scripts/dev_up.sh down
```

连本地 infra 一起停：

```bash
scripts/dev_up.sh down --infra
```

手动启动时，先拉起基础设施：

```bash
docker compose up -d postgres redis minio temporal temporal-ui
```

常用端口：

- API: `8000`
- Web: `8001`（`scripts/dev_up.sh` 默认）或 Vite 直跑 `5173`
- Postgres host port: `55432`
- Redis: `6379`
- MinIO: `9000` / `9001`
- Temporal: `7233`
- Temporal UI: `8080`

本地种子账号：

- `admin@local.cutagent` / `local-admin`
- `viewer@local.cutagent` / `local-viewer`

## 关键配置

默认存储后端是 SQLAlchemy，所以启动 API 前需要数据库连接：

```bash
export CUTAGENT_STORAGE_BACKEND=sqlalchemy
export CUTAGENT_DATABASE_URL=postgresql+psycopg://cutagent:cutagent@localhost:55432/cutagent
```

Temporal runtime 需要 API 和 worker 使用同一个 task queue：

```bash
export CUTAGENT_WORKFLOW_RUNTIME=temporal
export CUTAGENT_TEMPORAL_ADDRESS=localhost:7233
export CUTAGENT_TEMPORAL_NAMESPACE=default
export CUTAGENT_TEMPORAL_TASK_QUEUE=cutagent-production
```

多 worker 或 Temporal 模式下，ephemeral 对象存储必须是共享 MinIO/S3，不能是某个节点本地目录：

```bash
export CUTAGENT_OBJECTSTORE_TIERED=1
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
```

真实 provider 未配置时默认显式失败。只有在本地 demo 或测试需要时才打开 sandbox fallback：

```bash
export CUTAGENT_ALLOW_SANDBOX_FALLBACK=1
```

Redis 是**可选的跨进程协调依赖**，不是查询缓存，也不是生产任务的必需存储。单进程 / 单副本部署可以完全不启用；配了 `CUTAGENT_REDIS_URL` 后，provider 限流、事件实时 fanout、stream token、登录防爆破计数才跨 API / worker / 多副本共享，Redis 故障会 fail-safe 回退到进程内状态。多副本生产（`CUTAGENT_REPLICA_COUNT > 1`）下 preflight 强制要求 Redis，缺失即拒绝启动。业务事实仍在 PostgreSQL / Temporal / 对象存储。细节见 [docs/architecture/redis-coordination.md](docs/architecture/redis-coordination.md)。

```bash
# 可选：启用跨进程协调
export CUTAGENT_REDIS_URL=redis://127.0.0.1:6379/0
```

完整 env 清单以 [.env.example](.env.example) 和 `packages/core/config/settings.py` 为准。

## 开发命令

安装依赖：

```bash
pip install -e ".[dev]"
( cd apps/web && npm install )
```

初始化数据库：

```bash
python scripts/bootstrap_database.py
```

只跑迁移：

```bash
python scripts/migrate.py
```

macOS 本地数据库定时备份：

```bash
# 安装两个 LaunchAgent，并立即触发首份备份
.venv/bin/python scripts/install_database_backup_launchd.py

# 手动备份；默认会先在隔离临时库完整恢复，再发布备份文件
scripts/backup_database.sh

# 只预览清理结果
.venv/bin/python scripts/prune_database_backups.py --dry-run
```

默认每 6 小时备份、每天 04:30 清理，自动备份同时受 14 天、32 份、2 GiB
三重上限约束，并至少保留最新 4 份。文件位于 `.data/db-backups/`，每份包含
`.dump`、`.sha256` 和 `.dump.json` 恢复校验清单；脚本只清理
`cutagent-auto-*`，不会删除人工恢复点。日志也会在 10 MiB 时保留尾部并轮转。
可用安装器参数调整策略，用 `--uninstall` 卸载定时任务。

这是数据库备份，不包含 MinIO/OSS 对象和 `.data/secrets/`；这些持久化数据需要各自的
备份策略。

启动 API：

```bash
python -m uvicorn apps.api.main:app --reload --port 8000
```

启动 worker：

```bash
python -m apps.worker
```

启动前端：

```bash
( cd apps/web && npm run dev )
```

改 API 形状后重新生成契约产物：

```bash
uv run --extra dev python scripts/export_openapi.py
( cd apps/web && npm run generate:api )
```

不要手改 `apps/web/src/api/openapi.json` 或 `apps/web/src/api/schema.d.ts`。

## 验证

默认单测：

```bash
python -m pytest -q
```

前端构建：

```bash
( cd apps/web && npm run build )
```

前端严格类型扫描：

```bash
( cd apps/web && npx tsc -p tsconfig.json --noEmit --noUnusedLocals --noUnusedParameters )
```

完整本地门禁：

```bash
scripts/ci_gate.sh
```

DB 集成测试已随默认套件运行（不再需要开关，只要 Postgres:55432 起着）；单独跑该子集：

```bash
python -m pytest -q tests/integration
```

Temporal 测试需要真实 Temporal 和共享 MinIO/S3：

```bash
export CUTAGENT_RUN_TEMPORAL_TESTS=1
python -m pytest -q tests/temporal
```

## 文档

长期文档分两类：`docs/` 根下的三个概览文件，加上 `docs/architecture/` 子目录里的子系统权威参考。

- [Milestones](docs/milestones.md)
- [关键技术选型](docs/technical-choices.md)
- [关键设计决策](docs/design-decisions.md)
- [Redis 跨进程协调层](docs/architecture/redis-coordination.md)（架构参考）

文档索引见 [docs/README.md](docs/README.md)。
