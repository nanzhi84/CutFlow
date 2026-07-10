# packages/production

数字人视频生产引擎：执行 4 套工作流模板——`digital_human_v2`（18 节点主链）、`digital_human_editing_agent_v2`（20 节点、媒体选择与字幕/BGM 后处理分离的活动 Agent 链）、仅供历史 run 恢复的 `digital_human_editing_agent_v1`（18 节点 legacy 链）、`seedance_t2v_v1`（5 节点文生视频）。纯 B-roll 画外音是主链的 `broll.mode="full_coverage"` 模式；成片侧包含 SQL 仓储、剪映草稿包、剪辑师交接包导出。

## 职责
- 定义并执行四套工作流模板：`node_sequence.py` 给出主链、活动 Agent v2、legacy Agent v1、Seedance 四套序列及 `WORKFLOW_TEMPLATE_NODE_COUNTS`；`digital_human.py` 的 `_TEMPLATE_BUILDERS`/`template_for()` 按 `workflow_template_id` 路由；`NODE_HANDLERS` 分发到 `pipeline/nodes/` 下一文件一节点的 `run(ctx)`。
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
- Caption Display v2（issue #188）确定性模块群：`pipeline/_caption_display.py`（cue 合并/DP 中文断行/超长拆 cue/花字时段去重的纯编译器）、`pipeline/_font_metrics.py`（fontTools 读 hmtx/hhea，libass cell-height 折算；不可读走 EAW fallback + `font.metrics_fallback`）、`pipeline/_caption_window_planner.py`（最终帧上的权威字幕窗口与完整表现 option）、`pipeline/_caption_visual_safety.py`（人脸/场内文字/繁忙区硬过滤）、`pipeline/_huazi_layout.py`（归一化候选框生成）、`pipeline/_huazi_candidates.py`（卖点短语候选派生）。`CaptionWindowPlanning` 产 `plan.caption_windows`，`PostProcessAgentPlanning` 只填 BGM/caption option ID，`SubtitleAndBgmMix` 纯消费并产 `plan.caption_display`。
- `finished_video_numbering.py` — 成片编号（`V-NNN`）

## 约定与要求
- **确定性剪辑链路已冻结（issue #188）**：`digital_human_v2` / `DeterministicEditingPlanning` 不生产花字，除阻断性缺陷、安全、依赖兼容和普通字幕/BGM 维护外不再新增剪辑能力。所有新 Agent 能力只在 `digital_human_editing_agent_v2` 演进：`MediaSelectionAgentPlanning` 只选择 portrait/B-roll；`CaptionWindowPlanning` 只用固定算法和最终底片生成合法时间/空间候选；`PostProcessAgentPlanning` 只选择 BGM/caption option ID 并物化 `plan.style`。三个节点拥有独立 prompt/provider/repair/diagnostics/reuse 域，严禁互相代做。`digital_human_editing_agent_v1` 与其 HuaziPlanningSubagent 仅为历史 run 恢复冻结保留，不得新增能力。`SubtitleAndBgmMix` 不按 template ID 分支，只按是否存在 `plan.caption_windows` 选择新 artifact 消费或 legacy 读兼容。
- 节点是纯 `run(ctx)`：输入读 `ctx.state`，输出经 `ctx.artifact(...)` 落库，跨节点服务只走 `NodeContext`，不直接传 adapter。
- 降级必须显式上报为 `DegradationNotice`，禁止静默降级；节点 succeeded + 有 degradation 自动标 `degraded`。
- 确定性选材，不得随机；失败/取消时只释放 uncommitted 预留，committed picks 保留作多样性记忆。
- `BrollPlanArtifact` 新写入只用 `overlays`；下游读取统一走 `broll_overlays_from_plan()`，不要再写 `segments` 双结构。
- 真实 vs sandbox 由 provider profile 选取判定；无真实供应商时是否回退 sandbox 受 `sandbox_fallback_allowed()`（即 `CUTAGENT_ALLOW_SANDBOX_FALLBACK`，默认 OFF=显式报错）控制。
- 有 provider 副作用的节点（TTS/ResolveCreativeIntent/LipSync/ExportFinishedVideo/SeedanceGenerateVideo）必须带 `idempotency_key`，否则 reuse 拒绝复用。
- 增删节点须同步八处（`digital_human_template()` 已数据驱动、只调 `_build_template`，无需手改）：①对应模板的 `*_SEQUENCE`（`node_sequence.py`）②`NODE_HANDLERS` ③`_NODE_OUTPUT_KINDS`（声明每节点 `output_artifact_kinds`）④`pipeline/nodes/__init__.py` 手写模块导入清单（漏掉会 AttributeError）⑤`apps/api/services/jobs_runs.py` 的 `NODE_LABELS` ⑥`sqlalchemy_repository.py` 的 `NODE_LABELS` ⑦前端 `runModel.ts` 节点标签+阶段分组 ⑧节点有 provider 副作用加 `_PROVIDER_SIDE_EFFECT_NODES`、会破坏时间线复用加 `_TIMELINE_REUSE_BREAK_NODES`。

## 剪辑职责矩阵
- `NarrationBoundaryPlanning` 只产出安全切点事实和 base/available windows；`portrait_slots` / `broll_slots` 不是最终帧权威。
- `TimelineWindowPlanning` 拥有人像主轨最终窗口与资产级容量判定，并同时发布 `plan_timeline_windows` + `plan_portrait`；素材不足用 `material_insufficient_portrait` hard fail。
- `BrollPlanning` 和 editing planner 的 B-roll 落点必须共用 `packages/planning/material/broll_plan.py` 的几何政策与安全放置函数。
- `MediaSelectionAgentPlanning`（活动 v2）只做 portrait/B-roll 候选指派和本地校验，不读写字幕/BGM；LLM 不输出最终帧，任何 B-roll 几何丢弃必须进入 diagnostics / degradation。`EditingAgentPlanning` 仅指 legacy v1 恢复节点。
- `TimelinePlanning` 保持 verify-only，只校验并组装上游已经决定的帧边界。

## 测试
- `pytest tests/production tests/workflow`。人像唯一性/恢复诊断重点见 `test_timeline_window_planning_node.py`；B-roll canonical overlays 见 `test_broll_overlays_helper.py`、`test_broll_planning_node.py`。

## 注意 / 坑
- worker 是独立进程，改完节点逻辑要重启 worker，不只是重启 API。
- `seed_media=True`（LocalRuntimeAdapter 默认）会在构造时用 ffmpeg 生成 demo 媒体；Temporal per-activity 路径用 `seed_media=False`（见 `packages/core/workflow/temporal_adapter.py`）从 SQL 重水化真实资产。
- `get_object_store` 在 `digital_human` 命名空间被刻意保留为可 monkeypatch；测试 patch 的是 `digital_human.get_object_store`，节点经 `ctx.object_store()` 解析。
- lipsync 成片输入需可下载的持久化 OSS + presigned URL（非本地 MinIO）。
