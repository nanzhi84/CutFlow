# packages/production

数字人视频生产引擎：把 `DigitalHumanVideoRequest` 跑成成片的 16 节点流水线，以及成片的 SQL 仓储、剪映草稿包、剪辑师交接包导出。

## 职责
- 定义并执行 `digital_human_v2` 工作流：`NODE_SEQUENCE`（16 节点 ValidateRequest…FinalizeRunReport）→ `NODE_HANDLERS` 分发到 `pipeline/nodes/` 下一文件一节点的 `run(ctx)`。
- `LocalRuntimeAdapter` 是 thin engine：跑节点循环、run/node 状态机迁移（`assert_transition`）、事件/漏斗/可观测埋点、写 public+debug run report，并向节点提供共享服务（artifact 创建、media 解析、provider profile 选取、object store）。
- resume 复用既有有效产物（`reuse.py` 校验 node_status/node_version/input_manifest_hash/schema_version/sha256），retry 则全新跑。
- 节点产出 TYPED artifacts + provider invocation + warnings + GRADED degradations；选材落 selection ledger（`_selection.py`，驱动下一次 recency 降权）。
- 成片侧出口：`jianying_draft.py`/`jianying_draft_json.py`（剪映草稿包）、`editor_handoff.py`（zip 交接包）、`sqlalchemy_repository.py` + `sqlalchemy_mappers.py`（成片/草稿/交接的 SQL 持久化）。

## 关键文件 / 子目录
- `pipeline/digital_human.py` — 编排引擎、模板定义、状态机、共享节点服务（最重）
- `pipeline/node_sequence.py` — 节点顺序的唯一真源（轻量、无重依赖，供 UI/进度复用）
- `pipeline/nodes/` — 每节点一个 `run(ctx: NodeContext)`，能力开发改这里
- `pipeline/_node_context.py` — 节点拿到的 `NodeContext`（repository/provider_gateway/prompt/object store/artifact 助手）
- `pipeline/reuse.py` — resume 复用计划；`pipeline/_run_state.py` — 跨节点 `RunState` + `degradation_notice`
- `pipeline/_subtitles.py` — ASS 字幕；`pipeline/_selection.py` — 选材 ledger 条目

## 约定与要求
- 节点是纯 `run(ctx)`：输入读 `ctx.state`，输出经 `ctx.artifact(...)` 落库，跨节点服务只走 `NodeContext`，不直接传 adapter。
- 降级必须显式上报为 `DegradationNotice`（spec §9，禁止静默降级）；节点 succeeded + 有 degradation 自动标 `degraded`。
- 确定性选材，不得随机；失败/取消时只释放 uncommitted 预留（committed picks 保留作多样性记忆，§6.6）。
- 真实 vs sandbox 由 provider profile 选取判定；无真实供应商时是否回退 sandbox 受 `sandbox_fallback_allowed()`（即 `CUTAGENT_ALLOW_SANDBOX_FALLBACK`，默认 OFF=显式报错）控制。
- 有 provider 副作用的节点（TTS/ResolveCreativeIntent/LipSync/ExportFinishedVideo）必须带 `idempotency_key`，否则 reuse 拒绝复用。
- 增删节点须同步 `NODE_SEQUENCE`、`NODE_HANDLERS`、`digital_human_template()` 三处。

## 测试
- `pytest tests/production tests/workflow`。

## 注意 / 坑
- worker 是独立进程，改完节点逻辑要重启 worker，不只是重启 API。
- `seed_media=True`（LocalRuntimeAdapter 默认）会在构造时用 ffmpeg 生成 demo 媒体；Temporal per-activity 路径用 `seed_media=False`（见 `packages/core/workflow/temporal_adapter.py`）从 SQL 重水化真实资产。
- `get_object_store` 在 `digital_human` 命名空间被刻意保留为可 monkeypatch；测试 patch 的是 `digital_human.get_object_store`，节点经 `ctx.object_store()` 解析。
- lipsync 成片输入需可下载的持久化 OSS + presigned URL（非本地 MinIO）。
