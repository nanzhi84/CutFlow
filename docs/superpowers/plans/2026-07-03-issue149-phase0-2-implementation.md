# Plan: issue149 Phase 0–2 实现计划（单一综合 PR）

> 配套设计：`docs/superpowers/specs/2026-07-03-issue149-editing-boundary-convergence.md`
> 分支：`feat/issue149-convergence`（基于 origin/main = 8e9b3161）
> 分工：Codex（gpt-5.5 / xhigh / fast）写全部代码；Claude 规划、验收、清 slop、commit。

## Goal

消除 `digital_human_v2` 与 `digital_human_editing_agent_v1` 两链在时间线几何、切镜节奏、素材指派上的规则漂移：几何政策归一为公共库、切镜权威唯一化为 `plan.timeline_windows`、落帧路径唯一化，Agent 降为 assignment 引擎。v2 链行为逐字节不变（现有测试期望值零改动是硬闸）。

## 全局硬约束（三个 Phase 通用，违反即打回）

1. **v2 链回归零改动**：`tests/production/test_portrait_planning_node.py`、`test_broll_planning_node.py`、`tests/workflow` 里 v2 的既有断言期望值一律不许改；只允许为新增上游 artifact 补测试装配（fixture plumbing）。
2. **prompt 变量名不许动**：`_JSON_VARS` 里的键（`portrait_slots`/`broll_slots`/`portrait_candidates`…）与 prompt seed 的占位符保持不变（0029/0030 迁移教训——prompt seed 变更需要 DB 迁移，本 PR 不做）。窗口条目在喂给 prompt 前把 `window_id` 别名成 `slot_id`。
3. **枚举变更 = 契约变更**：新增 `ArtifactKind`/`WarningCode` 后必须重生成 `apps/web/src/api/openapi.json` + `schema.d.ts`（终审阶段统一做一次，命令：`uv run --extra dev python scripts/export_openapi.py && (cd apps/web && npm run generate:api)`）。**禁止手改 schema.d.ts**。
4. **新节点必须八处同步**（codex 评审补齐）：①`node_sequence.py` 对应 `*_SEQUENCE` ②`NODE_HANDLERS` ③`_NODE_OUTPUT_KINDS` ④`_TIMELINE_REUSE_BREAK_NODES`（复用真闸是 reuse_policy=never，**不要 bump node_version**——那条闸未接线）⑤`packages/production/pipeline/nodes/__init__.py` 手写模块导入清单（漏掉会 AttributeError）⑥后端 `apps/api/services/jobs_runs.py:44` NODE_LABELS ⑦后端 `packages/production/sqlalchemy_repository.py:162` NODE_LABELS ⑧前端 `apps/web/src/components/runs/runModel.ts` 节点标签 + 阶段分组（顺带补齐缺失的 NarrationBoundaryPlanning / EditingAgentPlanning 条目）。新 WarningCode 需同步前端 `warningLabel()`。
5. **降级必须显式**：任何新的丢弃/放松/回退都要进 `DegradationNotice` 或 diagnostics，禁止静默。
6. **确定性**：不引入任何随机；排序用稳定 key。
7. **测试真 provider 路径**：Agent 行为变更必须有注入 fake gateway 的 llm 路径测试，不能只测 deterministic fallback（#136 血泪：sandbox 单测抓不到真路径 bug）。
8. **验证只用非变异命令**：`python -m pytest tests/planning tests/production tests/workflow -q`（worktree 内跑法见文末）。不跑 lint --fix、不跑 npm build。

---

## Phase 0 — 共享几何政策 + Agent 缺陷修复（零架构变化）

### Touch surface

- `packages/planning/material/broll_plan.py`
  - 新增 `BrollGeometryPolicy` frozen dataclass：`fps=30, min_insert_seconds=1.5, max_insert_seconds=4.0, min_visible_aroll_seconds=2.0, snap_max_frames=15, max_pad_seconds=0.15`。现有模块常量改为引用 policy 默认实例的别名（保持导入兼容）。
  - 把 `_accept_insertion_if_safe` 的能力公开为 `place_insertion_safely(existing, insert, *, window_start, window_end, fps, portrait_cut_frames, policy)`：起点变体搜索（`_timeline_start_variants`）+ 网格对齐（`_align_insertions_to_grid_if_safe`）+ 短残片拒绝（`_has_short_visible_portrait_gap`）。**返回值语义与 `_accept_insertion_if_safe` 相同：返回整组重对齐后的 accepted insertions（既有项的 snap/pad 可能被整体重算），调用方用返回值替换累计列表；拒绝返回 None**。内部私有函数保留，`plan_insertions()` 行为不变。
- `packages/production/pipeline/_editing_agent.py`
  - `materialize_broll()` 重写放置逻辑：每个 choice → 时长 clamp 到 `[min_insert, min(max_insert, slot跨度, 源可用)]`；**源窗语义与 deterministic 一致：仅 `0 < 源可用 < min_insert` 时 drop；`源可用 <= 0`（开放/未知源窗）按 max_insert 处理，不算不足**；然后逐条走 `place_insertion_safely`（窗口=slot 起止秒），返回整组替换累计列表，接受失败 → drop（记原因）。返回值改为 `(payload, drop_diagnostics)` 或等价结构，drops 含 `slot_id/candidate_id/reason`。
  - 保持函数纯净（无 IO），单测友好。
- `packages/production/pipeline/nodes/editing_agent_planning.py`
  - **人像可行性预检**（在 provider 调用与 deterministic fallback 之前）：任一 portrait slot 的 `legal_window_ids` 为空 → `NodeExecutionError(ErrorCode.material_insufficient_portrait, ...)`，details 含 `slot_id/required_frames/longest_available_frames/portrait_candidate_count`，措辞与 deterministic 链对齐（资产级/容量语义）。不再让这种失败烧 repair 后报 `prompt_output_invalid`。
  - broll drops 进 `plan_editing_diagnostics`（键名 `broll_drops`）；若有 drop → 新 `WarningCode.broll_insertions_dropped_geometry = "broll.insertions_dropped_geometry"` 的 graded degradation（`affects_true_yield=False`，除非全部 drop 且原本有选择 → `True`）。
- `packages/core/contracts/base.py`：新增上述 WarningCode（终审统一 regen openapi）。
- `packages/core/contracts/artifacts.py`：`NarrationBoundaryPlan` 的 `portrait_slots`/`broll_slots` 字段 description 收紧（"base/available windows, NOT final authority"）。
- `packages/production/CLAUDE.md`：追加职责矩阵小节（谁拥有哪类决策）。

### 不做（Out of scope for Phase 0）

- 不动 `portrait_slots` 的来源（Phase 1 做）；不动 shortlist；不动 deterministic_selection 的兜底逻辑（预检后其"无够长源"分支自然不可达，保留不删）。

### 新增测试（tests/production/test_editing_agent*.py + tests/planning）

- `test_agent_broll_rejects_unsnappable_short_residual`：构造 0.3s A-roll 残片场景，agent 路径 drop 且诊断记录。
- `test_agent_broll_repositions_inside_window_before_drop`：窗口内存在合法起点时保留。
- `test_agent_broll_honours_min_max_insert_seconds`：长 slot 被 clamp 到 4s；短源 <1.5s 被 drop。
- `test_agent_portrait_infeasible_slot_fails_with_material_insufficient`：空 legal ids → 错误码 `material_insufficient_portrait`，不消耗 repair 尝试（fake gateway 断言零调用）。
- `test_place_insertion_safely_matches_plan_insertions_geometry`：同输入下共享函数与 plan_insertions 接受/拒绝一致。
- 政策同源断言：两链引用的常量来自同一 policy 对象。

---

## Phase 1 — TimelineWindowPlanning + plan.timeline_windows（切镜权威唯一化）

### Touch surface

- `packages/core/contracts/base.py`：`ArtifactKind.plan_timeline_windows = "plan.timeline_windows"`。
- `packages/core/contracts/artifacts.py`：新 `TimelineWindowsPlan`（pydantic v2）：
  ```
  fps:int, total_frames:int,
  geometry_policy: dict（BrollGeometryPolicy 快照 + portrait_reuse 模式）,
  portrait_windows: [ {window_id, start_frame, end_frame, unit_ids, boundary_source, phase} ],
  broll_windows:    [ {window_id, start_frame, end_frame, host_unit_ids,
                       host_portrait_window_ids, text} ],
  default_assignment: { portrait: [ {window_id(planner 原始 window_id，即候选 "asset:clip" 键，
                                      Phase 2 assignment 的 candidate_id 就用它),
                                     segment_payload(完整 _segment_payload 输出)} ],
                        portrait_plan_payload: <PortraitPlanning 今天会产出的完整 artifact payload，
                                                含 fps/total_duration/asset_id/duration_sec/segments/
                                                diagnostics 九键：used_audio_pauses, audio_pause_count,
                                                segment_count, recovery_stage, recovery_attempts,
                                                capacity_controlled_split, longest_usable_source_window,
                                                audio_pause_capacity_cap, recently_used_segment_count>,
                        engine: "compiler_default" },
  compile_diagnostics: {recovery_stage, attempts, capacity_controlled_split,
                        longest_usable_source_window, audio_pause_capacity_cap,
                        requested_constraints, ...}
  ```
  **v2 逐字节不变的机制（codex 评审修订）**：TimelineWindowPlanning 在内部把今天 PortraitPlanning 的完整产物（顶层字段 + 九个 diagnostics 键 + segments）原样算好存进 `default_assignment.portrait_plan_payload`；瘦身后的 PortraitPlanning 将它**原样再发布**为 `plan.portrait`。`portrait[*].window_id` 保留 planner 原始 window_id（`_segment_payload` 会丢掉它，必须在拆解前记录），供 Phase 2 的 assignment `candidate_id` 使用。
- 新节点 `packages/production/pipeline/nodes/timeline_window_planning.py`：
  - 输入：`plan_material_pack` + `narration_units` + `plan_narration_boundary`。
  - 迁入 `portrait_planning.py` 的 `_portrait_window_candidates`（含 ctx.source_artifact_for_asset IO）与 `_plan_with_escalation`（三级 ladder）与「hard_fail 无候选」前置检查、`material_insufficient_portrait` 失败路径（含 details 结构原样）。
  - portrait_windows = 编译产物 segments 的切镜结构；default_assignment = 每 window 的 `_segment_payload`；broll_windows = boundary `broll_slots` 内容 + `host_portrait_window_ids`（含关系按帧区间求交）；geometry_policy 快照来自 Phase 0 policy。
- `packages/production/pipeline/nodes/portrait_planning.py` 瘦身：
  - require `plan_timeline_windows`，把 `default_assignment.portrait_plan_payload` **原样再发布**为 `plan.portrait`（零重算、零重组）。删除已迁走的函数。
- `packages/production/pipeline/nodes/broll_planning.py`：fps 与 `portrait_cut_frames` 改从 `plan_timeline_windows.portrait_windows` 边界帧派生（数值与今天从 plan_portrait 读完全一致）。
- `packages/production/pipeline/nodes/editing_agent_planning.py` + `_editing_agent.py`：
  - slots 来源从 `boundary.portrait_slots/broll_slots` 换成 `windows.portrait_windows/broll_windows`（喂 prompt 前 `window_id`→`slot_id` 别名，条目形状与今天一致，prompt 零改动）。
  - Agent 只校验 strict asset-level uniqueness；素材稀缺由 `TimelineWindowPlanning` 编译层 hard-fail，不在 Agent 层放宽复用。
- 新增 `packages/planning/material/shortlist.py`：纯函数
  `shortlist_for_windows(portrait_windows, broll_windows, material_candidates, *, portrait_per_window=12, broll_per_window=6)` → 每类 exposed 子集（按 legal→slot-fit→全局分排序的 top-k 并集，稳定 tie-break）+ 计数 `{raw, eligible, exposed, dropped}`。`index_candidates` 只对 exposed 子集编号；计数进 `plan_editing_diagnostics.shortlist_counts`。
- 注册同步（全局约束 4 的八处全走一遍）：两个模板序列在 `NarrationBoundaryPlanning` 之后插入 `TimelineWindowPlanning`；后端两处 NODE_LABELS + 前端 `runModel.ts` 标签（"编译时间线窗口"）+ material 阶段分组，并顺带补齐 NarrationBoundaryPlanning / EditingAgentPlanning 缺失的标签与分组条目。
- Temporal 路径复用 template_for，无需单独改。

### 验收锚（Phase 1 专属）

- `test_portrait_planning_node.py` 全部既有断言不改值直接过（只补上游 artifact 装配）。
- 新测试：
  - `test_timeline_windows_have_no_concrete_frames_invented`：windows 边界帧 == 编译 segments 边界帧。
  - `test_v2_portrait_plan_identical_through_windows_split`：同输入，重构前后 `plan.portrait` payload 相等（以现有节点测试期望值为基准）。
  - `test_agent_slots_come_from_compiled_windows_not_base_slots`：构造 beam 合并场景（相邻边界被合并），断言 agent 收到的 slot 数 < base portrait_slots 数。
  - `test_shortlist_applies_budget_and_reports_counts`。
  - `test_agent_windows_always_feasible`：编译成功 ⇒ 每个 portrait window 至少一个 legal 候选。

### 风险与对策

- **风险**：portrait 诊断字段搬运遗漏 → 现有节点测试断 diagnostics 会红。对策：diagnostics 键集合先 grep 齐全再搬。
- **风险**：agent 模板节点数 +1 影响进度 UI。对策：`WORKFLOW_TEMPLATE_NODE_COUNTS` 用 len() 自动，前端只吃 API 数字；仅 runModel.ts 标签要加。

---

## Phase 2 — media_assignment 契约 + 唯一落帧库

### Touch surface

- `packages/core/contracts/base.py`：`ArtifactKind.plan_media_assignment = "plan.media_assignment"`。
- `packages/core/contracts/artifacts.py`：新 `MediaAssignmentPlan`：
  ```
  engine: "editing_agent_llm" | "deterministic_default" | "deterministic_fallback",
  portrait: [ {window_id, candidate_id, source_mode, reason} ],
  broll:    [ {window_id, candidate_id, reason, confidence, matched_keywords} ],
  font_id, bgm_id,
  diagnostics: { repair_trace, shortlist_counts, fallback_used, broll_drops }
  ```
- 新共享落帧库 `packages/production/pipeline/_materialize.py`（纯函数，从 `_editing_agent.py` 迁移+归并）：
  - `materialize_portrait_from_assignment(windows, assignment, candidates)` → PortraitPlanArtifact payload。
  - `materialize_broll_from_assignment(windows, assignment, candidates, cut_frames, policy)` → BrollPlanArtifact payload（内部走 Phase 0 的 `place_insertion_safely`）。
  - `overlays_from_insertions(insertions)`：统一 BrollOverlay 构造，**以 v2 语义为准**（`clip_id=ins.clip_id`、`scene_name=ins.scene_name` 原样传，不做 `or None` 归一化——agent 侧输出向 v2 对齐，v2 输出不变）。
  - `materialize_style_from_selection(...)`：归并 `style_planning` 与 agent 的 style 构造，**返回 `(payload, warnings, degradations)` 三元组**——v2 的 BGM/font 降级 warning 语义必须原样保留并由两个调用方线程到 NodeOutput（agent 节点由此获得与 v2 一致的 style 降级上报，这是收敛目标的一部分）。
- `packages/production/pipeline/nodes/editing_agent_planning.py`：
  - 产出 `plan_media_assignment`（第 5 个 artifact）+ 既有四个 artifact 不变（下游零改动）。
  - **删除** `_editing_agent.py` 里的 `materialize_portrait/materialize_broll/materialize_style`，节点改调 `_materialize.py`。
  - LLM 不可修复时的兜底（codex 评审修订）：**portrait 部分改用 default_assignment 回退**（windows 自带、必然合法）；**broll/font/bgm 部分保留 `deterministic_selection` 的对应逻辑**（default_assignment 不覆盖这三类）。`engine="deterministic_fallback"` + 既有 degradation 语义保留；`deterministic_selection` 里 portrait"复用 top-ranked 短源 clone-pad"的分支删除（预检+default 回退后不可达）。
- v2 节点：PortraitPlanning / BrollPlanning / StylePlanning 改调 `_materialize.py` 对应函数（输出不变；BrollPlanning 的选择引擎 `plan_insertions` 保持原样，只换 overlay 构造）。

### 验收锚（Phase 2 专属）

- `test_same_assignment_same_frames_across_engines`：同一份 assignment + windows，走 `_materialize.py` 得到的 portrait/broll 帧字段与 agent 节点产物一致（纯函数断言）。
- `test_agent_fallback_uses_default_assignment`：LLM 永远回错 JSON → 兜底产物 == default_assignment 落帧，engine 标记正确。
- `test_style_selection_cannot_mutate_visual_windows`：style 产出不含/不改 portrait、broll 帧字段。
- `test_timeline_planning_stays_verify_only`：TimelinePlanning 源码不 import 任何 snap/推导 helper（守卫测试，AST 或 import 检查）。
- `grep` 断言：`_editing_agent.py` 无 materialize_* 定义残留。
- v2 三节点既有测试期望值零改动。

### Alternatives considered

- **agent 模板加独立 PlanMaterialization 节点**：被否——节点拓扑再 +1、进度/注册/前端连带，收益只是名义上的"节点级分离"；单一共享库已满足"无第二套隐藏政策"的验收本质。
- **BrollPlanning 拆成 assignment→materialize 两段**：被否——`plan_insertions` 的 anchor/cursor/jitter 是选择与放置交织的确定性引擎，硬拆有行为漂移风险；几何已在 Phase 0 共享，Phase 2 只统一 overlay 构造。

## Edge cases & risks（全局）

- Agent 重指派在唯一性预算下的完整可行性由 default_assignment 存在性保证；validator+repair+default 回退三层兜底。
- windows artifact 是 JSON payload，零 DB 迁移；ArtifactKind/WarningCode 枚举进 openapi → 终审统一 regen。
- worker 独立进程：本 PR 合并部署时需重启 worker（写进 PR 描述）。
- Out of scope：Phase 3（intent/sketch/工具环）、旧纯 B-roll 独立链路、MaterialPack 检索化、prompt seed 变更、`_creative_intent` 接线。

## Verification（终审前全绿）

- [ ] `python -m pytest tests/planning tests/production tests/workflow -q`（worktree：`PYTHONPATH=$WT /Users/yoryon/Projects/cutflow/.venv/bin/python -m pytest ...`）
- [ ] `python -m pytest -q` 全量（等 CI 前本地过一遍单测域）
- [ ] openapi regen 后 `git diff --stat` 只含预期文件
- [ ] Codex 终审整 diff + Claude 自审（slop / 死代码 / 过度防御 / 注释噪音）
