# packages/production

数字人视频生产引擎：执行 3 套工作流模板——`digital_human_v2`（19 节点主链）、`digital_human_editing_agent_v2`（20 节点、媒体选择与 BGM 选择分离的活动 Agent 链）、`seedance_t2v_v1`（5 节点文生视频）。纯 B-roll 画外音是主链的 `broll.mode="full_coverage"` 模式；成片侧包含 SQL 仓储、剪映草稿包、剪辑师交接包导出。

## 职责
- 定义并执行三套工作流模板：`node_sequence.py` 给出主链、活动 Agent v2、Seedance 三套序列及 `WORKFLOW_TEMPLATE_NODE_COUNTS`；`digital_human.py` 的 `_TEMPLATE_BUILDERS`/`template_for()` 按 `workflow_template_id` 路由；`NODE_HANDLERS` 分发到 `pipeline/nodes/` 下一文件一节点的 `run(ctx)`。
- `LocalRuntimeAdapter` 是 thin engine：跑节点循环、run/node 状态机迁移（`assert_transition`）、事件/漏斗/可观测埋点、写 public+debug run report，并向节点提供共享服务（artifact 创建、media 解析、provider profile 选取、object store）。
- resume 复用既有有效产物（`reuse.py` 校验 node_status/node_version/input_manifest_hash/schema_version/sha256），retry 则全新跑。
- 节点产出 TYPED artifacts + provider invocation + warnings + GRADED degradations；选材落 selection ledger（`_selection.py`，驱动下一次 recency 降权）。当前 ledger 只由 `MaterialPackPlanning` 读取并写入候选 metadata，B-roll/Portrait 后续节点不再直接查 ledger。
- 人像主轨执行资产级唯一性：`TimelineWindowPlanning`/editing planner 把 `template_id` 作为资产 id，每个 run 最多使用一次；覆盖不足是 `material_insufficient_portrait` hard fail，capacity-controlled split 只能用更多不同资产恢复，不能复用同一资产。
- 成片侧出口：`jianying_draft.py`/`jianying_draft_json.py`（剪映草稿包）、`editor_handoff.py`（zip 交接包）、`sqlalchemy_repository.py` + `sqlalchemy_mappers.py`（成片/草稿/交接的 SQL 持久化）。

## 关键文件 / 子目录
- `pipeline/digital_human.py` — 编排引擎、模板路由、状态机、共享节点服务（最重）
- `pipeline/node_sequence.py` — 四套节点序列 + 模板节点数的唯一真源（轻量、无重依赖，供 UI/进度复用）
- `pipeline/nodes/` — 每节点一个 `run(ctx: NodeContext)`，能力开发改这里
- `pipeline/_node_context.py` — 节点拿到的 `NodeContext`（repository/provider_gateway/prompt/object store/artifact 助手）
- `pipeline/_provider_profiles.py` — 真实 vs sandbox profile 选取、应用 `sandbox_fallback_allowed()` 闸门（函数定义在 `packages/core/config`，逻辑从 adapter 抽出）
- `pipeline/reuse.py` — resume 复用计划；`pipeline/_run_state.py` — 跨节点 `RunState` + `degradation_notice`
- `pipeline/degradation_policies.py` — 具名降级策略（lipsync 故障转移 / ASR 估算回退 / 封面回退等版本化策略对象）；`pipeline/ephemeral_gc.py` — 终态 run 的 ephemeral 资产 GC
- `pipeline/_timeline_grid.py` — 帧网格 helper（fps 由调用方传入，`TIMELINE_FPS=30` 在 `planning/editing/frame_grid.py`）；`pipeline/_subtitles.py` — ASS 字幕；`pipeline/_selection.py` — 选材 ledger 条目；`_broll_overlays.py` — `BrollPlanArtifact` 读边界，`overlays` 为 canonical，legacy `segments` 只在这里兼容。
- Caption Clean-Slate（issue #209）只有一个字幕事实源：`pipeline/_caption_composition.py` 确定性完成 cue 合并、1–3 行 DP 断行、超长拆 cue、精确短语绑定与 run 级时间；`CaptionCompositionPlanning` 在固定字幕带内发布 `plan.caption_composition`。普通与强调 run 共用一条 cue/line 坐标系，不再读取成片像素、选择矩形或生成独立 overlay。无显式选择时固定使用 starter pack 的 Noto Serif CJK SC Regular / Noto Sans CJK SC Bold，缺资产明确失败，不得退回平台相关 generic family。`BgmAgentPlanning` 只选择 BGM，`SubtitleAndBgmMix` 只消费 composition 并渲染 ASS/混音。
- `finished_video_numbering.py` — 成片编号（`V-NNN`）

## 约定与要求
- **字幕与 Agent 职责已收敛（issue #209）**：主链和活动 Agent 链共享同一个 `CaptionCompositionPlanning`，禁止再引入字幕候选框、空间视觉分析、独立强调 overlay 或模板分支读兼容。`MediaSelectionAgentPlanning` 只选择 portrait/B-roll；`BgmAgentPlanning` 只选择 BGM；字幕提示只来自 `CreativeIntentArtifact.emphasis`，最终 run、行、帧和字体全部由本地确定性编排。`digital_human_editing_agent_v1` 不再注册，新建、重试或恢复都必须返回 4xx；失败节点已退出当前模板的 run 也不得恢复。
- 节点是纯 `run(ctx)`：输入读 `ctx.state`，输出经 `ctx.artifact(...)` 落库，跨节点服务只走 `NodeContext`，不直接传 adapter。
- 降级必须显式上报为 `DegradationNotice`，禁止静默降级；节点 succeeded + 有 degradation 自动标 `degraded`。
- 确定性选材，不得随机；失败/取消时只释放 uncommitted 预留，committed picks 保留作多样性记忆。
- SQL 快照提交先锁 `workflow_runs`：一旦 durable 状态进入 `cancelling/cancelled`，禁止新增成功节点输出、交付产物和成功事件；取消报告、provider 调用审计、用量记录与 reservation 释放仍可提交。resume 只水合 succeeded/degraded/skipped 节点显式声明的 output artifact。
- `BrollPlanArtifact` 新写入只用 `overlays`；下游读取统一走 `broll_overlays_from_plan()`，不要再写 `segments` 双结构。
- 真实 vs sandbox 由 provider profile 选取判定；无真实供应商时是否回退 sandbox 受 `sandbox_fallback_allowed()`（即 `CUTAGENT_ALLOW_SANDBOX_FALLBACK`，默认 OFF=显式报错）控制。
- 有 provider 副作用的节点（TTS/ResolveCreativeIntent/LipSync/ExportFinishedVideo/SeedanceGenerateVideo）必须带 `idempotency_key`，否则 reuse 拒绝复用。
- 增删节点须同步八处（`digital_human_template()` 已数据驱动、只调 `_build_template`，无需手改）：①对应模板的 `*_SEQUENCE`（`node_sequence.py`）②`NODE_HANDLERS` ③`_NODE_OUTPUT_KINDS`（声明每节点 `output_artifact_kinds`）④`pipeline/nodes/__init__.py` 手写模块导入清单（漏掉会 AttributeError）⑤`apps/api/services/jobs_runs.py` 的 `NODE_LABELS` ⑥`sqlalchemy_repository.py` 的 `NODE_LABELS` ⑦前端 `runModel.ts` 节点标签+阶段分组 ⑧节点有 provider 副作用加 `_PROVIDER_SIDE_EFFECT_NODES`、会破坏时间线复用加 `_TIMELINE_REUSE_BREAK_NODES`。

## 剪辑职责矩阵
- `NarrationBoundaryPlanning` 只产出安全切点事实和 base/available windows；`portrait_slots` / `broll_slots` 不是最终帧权威。
- `TimelineWindowPlanning` 拥有人像主轨最终窗口与资产级容量判定，只发布 `plan_timeline_windows`；编译默认人像计划保存在 `default_assignment.portrait_plan_payload`，由后续确定性/Agent 媒体选择节点发布唯一的最终 `plan_portrait`；素材不足用 `material_insufficient_portrait` hard fail。
- B-roll 落点的帧几何是单一真源：窗口由 `TimelineWindowPlanning` 经 `packages/planning/material/broll_plan.py` 的 `BROLL_GEOMETRY_POLICY` + `legalize_broll_window_frames`（内含切点吸附与短残片拒绝）合法化后发布；`_materialize.py` 的各编排器只消费已合法化的窗口帧，不得自行重算几何。
- `MediaSelectionAgentPlanning`（活动 v2）只做 portrait/B-roll 候选指派和本地校验，不读写字幕/BGM；prompt 候选必须按 slot 内嵌，本地须先在完整合法域求覆盖 witness，再裁剪展示候选，并保证跨 slot portrait asset 唯一及 insert-mode B-roll 直接兼容/数量/重叠约束，禁止退回全局候选表 + ID 列表的跨表联结；LLM 不输出最终帧，任何 B-roll 几何丢弃必须进入 diagnostics / degradation。
- `TimelineAssemblyValidation` 保持 assembly + verify-only，只组装、校验上游已经决定的帧边界，不重新规划时间线；退出当前节点图的历史节点 ID 不再注册别名。

## 测试
- `pytest tests/production tests/workflow`。人像唯一性/恢复诊断重点见 `test_timeline_window_planning_node.py`；B-roll canonical overlays 见 `test_broll_overlays_helper.py`。

## 注意 / 坑
- worker 是独立进程，改完节点逻辑要重启 worker，不只是重启 API。
- `seed_media=True`（LocalRuntimeAdapter 默认）会在构造时用 ffmpeg 生成 demo 媒体；Temporal per-activity 路径用 `seed_media=False`（见 `packages/core/workflow/temporal_adapter.py`）从 SQL 重水化真实资产。
- `get_object_store` 在 `digital_human` 命名空间被刻意保留为可 monkeypatch；测试 patch 的是 `digital_human.get_object_store`，节点经 `ctx.object_store()` 解析。
- lipsync 成片输入需可下载的持久化 OSS + presigned URL（非本地 MinIO）。
