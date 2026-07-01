# packages/production

数字人视频生产引擎：执行 3 套工作流模板——`digital_human_v2`（16 节点主链）、`broll_only_v1`（13 节点纯空镜）、`seedance_t2v_v1`（5 节点文生视频），以及成片的 SQL 仓储、剪映草稿包、剪辑师交接包导出。

## 职责
- 定义并执行三套工作流模板：`node_sequence.py` 给出三套序列（`NODE_SEQUENCE` 16 节点 / `BROLL_ONLY_SEQUENCE` 13 节点 / `SEEDANCE_T2V_SEQUENCE` 5 节点，外加 `WORKFLOW_TEMPLATE_NODE_COUNTS` 模板节点数）；`digital_human.py` 的 `_TEMPLATE_BUILDERS`/`template_for()` 按 `workflow_template_id` 路由三模板；`NODE_HANDLERS`（21 项，覆盖三模板全部节点，`digital_human_v2` 走其中 16 个）分发到 `pipeline/nodes/` 下一文件一节点的 `run(ctx)`。
- `LocalRuntimeAdapter` 是 thin engine：跑节点循环、run/node 状态机迁移（`assert_transition`）、事件/漏斗/可观测埋点、写 public+debug run report，并向节点提供共享服务（artifact 创建、media 解析、provider profile 选取、object store）。
- resume 复用既有有效产物（`reuse.py` 校验 node_status/node_version/input_manifest_hash/schema_version/sha256），retry 则全新跑。
- 节点产出 TYPED artifacts + provider invocation + warnings + GRADED degradations；选材落 selection ledger（`_selection.py`，驱动下一次 recency 降权）。当前 ledger 只由 `MaterialPackPlanning` 读取并写入候选 metadata，B-roll/Portrait 后续节点不再直接查 ledger。
- 人像主轨执行资产级唯一性：`PortraitPlanning`/editing planner 把 `template_id` 作为资产 id，每个 run 最多使用一次；覆盖不足是 `material_insufficient_portrait` hard fail，capacity-controlled split 只能用更多不同资产恢复，不能复用同一资产。
- 成片侧出口：`jianying_draft.py`/`jianying_draft_json.py`（剪映草稿包）、`editor_handoff.py`（zip 交接包）、`sqlalchemy_repository.py` + `sqlalchemy_mappers.py`（成片/草稿/交接的 SQL 持久化）。

## 关键文件 / 子目录
- `pipeline/digital_human.py` — 编排引擎、模板路由、状态机、共享节点服务（最重）
- `pipeline/node_sequence.py` — 三套节点序列 + 模板节点数的唯一真源（轻量、无重依赖，供 UI/进度复用）
- `pipeline/nodes/` — 每节点一个 `run(ctx: NodeContext)`，能力开发改这里
- `pipeline/_node_context.py` — 节点拿到的 `NodeContext`（repository/provider_gateway/prompt/object store/artifact 助手）
- `pipeline/_provider_profiles.py` — 真实 vs sandbox profile 选取、应用 `sandbox_fallback_allowed()` 闸门（函数定义在 `packages/core/config`，逻辑从 adapter 抽出）
- `pipeline/reuse.py` — resume 复用计划；`pipeline/_run_state.py` — 跨节点 `RunState` + `degradation_notice`
- `pipeline/degradation_policies.py` — 具名降级策略（lipsync 故障转移 / ASR 估算回退 / 封面回退等版本化策略对象）；`pipeline/ephemeral_gc.py` — 终态 run 的 ephemeral 资产 GC
- `pipeline/_timeline_grid.py` — 帧网格 helper（fps 由调用方传入，`TIMELINE_FPS=30` 在 `planning/editing/frame_grid.py`）；`pipeline/_subtitles.py` — ASS 字幕；`pipeline/_selection.py` — 选材 ledger 条目；`_broll_overlays.py` — `BrollPlanArtifact` 读边界，`overlays` 为 canonical，legacy `segments` 只在这里兼容。
- `finished_video_numbering.py` — 成片编号（`V-NNN`）

## 约定与要求
- 节点是纯 `run(ctx)`：输入读 `ctx.state`，输出经 `ctx.artifact(...)` 落库，跨节点服务只走 `NodeContext`，不直接传 adapter。
- 降级必须显式上报为 `DegradationNotice`，禁止静默降级；节点 succeeded + 有 degradation 自动标 `degraded`。
- 确定性选材，不得随机；失败/取消时只释放 uncommitted 预留，committed picks 保留作多样性记忆。
- `BrollPlanArtifact` 新写入只用 `overlays`；下游读取统一走 `broll_overlays_from_plan()`，不要再写 `segments` 双结构。
- 真实 vs sandbox 由 provider profile 选取判定；无真实供应商时是否回退 sandbox 受 `sandbox_fallback_allowed()`（即 `CUTAGENT_ALLOW_SANDBOX_FALLBACK`，默认 OFF=显式报错）控制。
- 有 provider 副作用的节点（TTS/ResolveCreativeIntent/LipSync/ExportFinishedVideo/SeedanceGenerateVideo）必须带 `idempotency_key`，否则 reuse 拒绝复用。
- 增删节点须同步三处（`digital_human_template()` 已数据驱动、只调 `_build_template`，无需手改）：①对应模板的 `*_SEQUENCE`（`node_sequence.py`）②`NODE_HANDLERS` ③`_NODE_OUTPUT_KINDS`（声明每节点 `output_artifact_kinds`）。节点有 provider 副作用还需加入 `_PROVIDER_SIDE_EFFECT_NODES`，会破坏时间线复用还需加入 `_TIMELINE_REUSE_BREAK_NODES`。

## 测试
- `pytest tests/production tests/workflow`。人像唯一性/恢复诊断重点见 `test_portrait_planning_node.py`；B-roll canonical overlays 见 `test_broll_overlays_helper.py`、`test_broll_planning_node.py`、`test_broll_coverage_planning.py`。

## 注意 / 坑
- worker 是独立进程，改完节点逻辑要重启 worker，不只是重启 API。
- `seed_media=True`（LocalRuntimeAdapter 默认）会在构造时用 ffmpeg 生成 demo 媒体；Temporal per-activity 路径用 `seed_media=False`（见 `packages/core/workflow/temporal_adapter.py`）从 SQL 重水化真实资产。
- `get_object_store` 在 `digital_human` 命名空间被刻意保留为可 monkeypatch；测试 patch 的是 `digital_human.get_object_store`，节点经 `ctx.object_store()` 解析。
- lipsync 成片输入需可下载的持久化 OSS + presigned URL（非本地 MinIO）。
