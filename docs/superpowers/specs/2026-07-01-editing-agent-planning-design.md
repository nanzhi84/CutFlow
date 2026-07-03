# EditingAgentPlanning MVP 设计（技术 spec）

- 日期：2026-07-01
- 状态：已批准，进入实现
- 来源：GitHub Issue [#136](https://github.com/nanzhi84/lead-gen-video-workflow/issues/136)
- 前置依赖：#135（NarrationBoundaryPlanning，已合入 main）
- 产品 PRD：`docs/superpowers/plans/2026-07-01-editing-agent-planning-prd.md`

## 1. 核心思想

新增一个 LLM 综合剪辑节点 `EditingAgentPlanning` + 一个新工作流模板
`digital_human_editing_agent_v1`。该节点用一次 LLM 调用（qwen3.7-plus）综合规划
A-roll / B-roll / 字体 / BGM，产出与现有 `PortraitPlanning` / `BrollPlanning` /
`StylePlanning` 三节点**完全同构**的三个下游 artifact：`plan.portrait` /
`plan.broll` / `plan.style`。

关键设计约束：**LLM 只做"选哪个候选 ID"的语义决策；所有精确到帧的时间线由本地
确定性 materializer 复用现有帧网格纯函数（`packages/planning/editing/frame_grid.py`）
算出。** `TimelinePlanning` 及其下游渲染链、前端渲染因此零改动。

旧 `digital_human_v2` 模板与其确定性规划链路完全不动，作为稳定灰度基线。

## 2. 数据流

```
上游（已有节点，复用）:
  TTS → MaterialPackPlanning → NarrationAlignment → NarrationBoundaryPlanning

EditingAgentPlanning（新）:
  读 case_context / creative_intent / narration_units
    / plan.narration_boundary（safe_cut_boundaries + portrait_slots + broll_slots）
    / plan.material_pack（portrait/broll/font/bgm candidates）
    / request.edit.instruction / request.subtitle|bgm|broll options
  ↓
  ① 组装编号化 LLM 输入
  ② 调 qwen3.7-plus（sandbox/无 provider → 确定性兜底）
  ③ 本地 validator 校验 ID 合法性/覆盖/不重叠
  ④ repair（≤ max_repair_attempts）
  ⑤ materializer：ID → 帧网格纯函数 → 三个 artifact
  ↓
  产出 plan.portrait + plan.broll + plan.style + plan.editing_diagnostics

下游（已有节点，零改动）:
  TimelinePlanning(verify-only) → PortraitTrackBuild → LipSync
    → RenderFinalTimeline → SubtitleAndBgmMix → ExportFinishedVideo → FinalizeRunReport
```

## 3. 契约变更

全部在 `packages/core/contracts/`。

### 3.1 新增 `EditPlanningOptions`（jobs.py）

```python
class EditPlanningOptions(ContractModel):
    instruction: str = ""
    max_repair_attempts: int = Field(1, ge=0, le=3)
```

### 3.2 `DigitalHumanVideoRequest` 增字段（jobs.py）

```python
    edit: EditPlanningOptions = Field(default_factory=EditPlanningOptions)
```

有默认值 → 对旧请求向后兼容，前端可选填。

### 3.3 新增 `ArtifactKind.plan_editing_diagnostics`（base.py）

新增枚举值，供节点产出 debug/复盘 artifact。会进 OpenAPI（枚举扩展），但对前端非破坏性。

### 3.4 契约导出与 regen

- 同步 `packages/core/contracts/__init__.py` 的 re-export + `__all__`。
- **必须重生成** `apps/web/src/api/openapi.json` + `apps/web/src/api/schema.d.ts`
  （`scripts/export_openapi.py` + `apps/web && npm run generate:api`），用项目
  `.venv`。CI 有两道 `git diff --exit-code` 漂移闸。
- 零 DB 迁移（migration head 保持 `0027`）：`edit` 是 request JSON、diagnostics 是
  JSON payload artifact，都不触 DB schema。

## 4. 节点内部设计（`packages/production/pipeline/nodes/editing_agent_planning.py`）

标准节点函数 `def run(ctx: NodeContext) -> NodeOutput`。

### 4.1 输入组装

从 `ctx.state.require(...)` 读取上游 artifact，构造编号化 LLM 输入（见 §5）。
所有 slot / candidate 都带稳定 ID，候选携带 `packages/planning` 已算好的语义 metadata
（穿搭/关键词/scene_name/mood/energy/script_fit/scene_fit…）。

### 4.2 LLM 调用

复用 `ResolveCreativeIntent` 的范式：
1. `ctx.first_available_provider_profile("llm.chat", include_sandbox=False)`；
   为空且 `sandbox_fallback_allowed()` → sandbox profile。
2. `prompt_registry.render(node_id="EditingAgentPlanning", variables=...)`。
3. `provider_gateway.invoke(...)`（capability `llm.chat`，model 由 profile.model_id 决定）。
4. `prompt_registry.validate_output(...)` 校验 JSON 形状。

### 4.3 Validator（本地硬约束）

见 §7。校验失败产出结构化错误列表。

### 4.4 Repair

校验失败 → 把错误摘要回喂 LLM 重选，最多 `request.edit.max_repair_attempts` 次
（默认 1）。仍失败：
- 有真实 provider（生产）→ `NodeExecutionError`（fail-fast，`render_invalid_timeline`
  或新 `editing_plan_invalid` 语义码）。
- sandbox / 无 provider → 走确定性兜底（§8），并上报 `DegradationNotice`。

### 4.5 Materializer

见 §6。产出三个下游 artifact + `plan.editing_diagnostics`（记录 LLM 输入摘要、原始
输出、repair trace、validator 结果、是否走兜底）。

## 5. LLM 输入/输出协议

### 5.1 输入（编号化，禁裸秒/裸帧）

```json
{
  "script": "...", "title": "...", "edit_instruction": "...",
  "video_duration": 18.6, "creative_intent": {...},
  "narration_units": [{"unit_id","text","start","end","pause_after_ms",
                       "portrait_cut_allowed","boundary_score","boundary_reason"}],
  "safe_cut_boundaries": [{"cut_id","time","frame","after_unit_id","source"}],
  "portrait_slots": [{"slot_id","start_frame","end_frame","unit_ids"}],
  "broll_slots": [{"slot_id","start_frame","end_frame","unit_ids","text"}],
  "portrait_candidates": [{"candidate_id","asset_id","clip_id","source_start",
                           "source_end","score","reason","tags"}],
  "broll_candidates": [{"candidate_id","asset_id","clip_id","source_start",
                        "source_end","matched_keywords","scene_name","score"}],
  "font_candidates": [{"font_id","score","reason"}],
  "bgm_candidates": [{"bgm_id","mood","energy_profile","script_fit","scene_fit","score"}]
}
```

### 5.2 输出（仅 ID，禁裸帧/禁虚构 ID）

```json
{
  "portrait_plan": [{"slot_id","window_id","source_mode","reason"}],
  "broll_plan": [{"slot_id","candidate_id","reason","confidence","matched_keywords"}],
  "font_plan": {"font_id","reason"} ,
  "bgm_plan": {"bgm_id","reason"},
  "analysis": "..."
}
```

- `window_id` 指向 `portrait_candidates[].candidate_id`（一个 portrait 素材源窗口）。
- `broll_plan` 只对需要覆盖的 `broll_slots` 子集给出，可为空。
- `font_plan` / `bgm_plan` 允许为 null（候选为空或关闭时）。

## 6. Materializer（复用帧网格纯函数）

### 6.1 Portrait → `PortraitPlanArtifact`

对每个 `portrait_slot`（已带 `start_frame`/`end_frame`，相邻共享帧、覆盖全时间线）：
1. `window = FrameWindow(slot.start_frame, slot.end_frame)`（timeline 帧已定）。
2. 取 LLM 选的 `window_id` → `portrait_candidate`（含 `source_start`/`source_end`）。
3. `slice_source_window(source_start_seconds=cand.source_start,
   length_frames=window.length_frames, source_window_start_seconds=cand.source_start,
   source_window_end_seconds=cand.source_end)` → source 帧 + pad_end。
4. 构造 `PortraitSegment`（`timeline_start_frame`/`timeline_end_frame` 来自 slot，
   `source_start_frame`/`source_end_frame` 来自 slice，`asset_id`/`clip_id`/`unit_ids`
   /`source_mode`/`boundary_source` 从 slot+candidate 填）。

**不跑 beam-search packer**，直接复用 `frame_grid` 纯函数，LLM 的逐槽选择被 100% 尊重。

### 6.2 B-roll → `BrollPlanArtifact`

- 取 portrait plan 的所有 cut frames（portrait segment 的 timeline_start/end frame 集合）。
- 对 LLM 选中的每个 broll_slot + candidate，复用
  `packages/planning/material/broll_plan.py` 的对齐逻辑
  （`align_insertions_to_portrait_cuts` 或等价路径）把 overlay 帧对齐到 portrait cut
  grid，产出 `BrollOverlay`（4 帧字段 + pad_start/pad_end + reason/confidence/
  matched_keywords/scene_name），保证不重叠、不越 broll_slot、不越素材可用 source 窗口。
  具体接口在 writing-plans 阶段依 §12 探查结论定稿。

### 6.3 Style → `StylePlanArtifact`

- `font_plan.font_id` → `SubtitleStylePlan.font_id` + `FontPlan.font_id` +
  `font_asset_id`；非法/为空 → 默认 sentinel `case_default_font`（复用
  `style_planning` 的默认字体逻辑）。
- `bgm_plan.bgm_id` → 从 `bgm_candidates` 按 id 定位，填 `BgmPlan` 全字段
  （section_type/mood/energy_profile/loopable/source_start/end/…，复用
  `style_planning` 从 candidate.metadata 填充的映射）；BGM 关闭/候选空 → `bgm=None`。
- `overlay_events` 复用 `style_planning._derive_overlay_events`（从 creative_intent.emphasis 派生）。

## 7. Validator 规则

- 所有 `portrait_plan[].slot_id` 覆盖**全部** `portrait_slots`（缺一即失败）。
- 所有 `window_id` / `candidate_id` / `font_id` / `bgm_id` 必须来自对应候选池。
- 每个 portrait `window_id` 的 source window（`source_end - source_start`）足够覆盖
  slot 帧长（允许 source > timeline；不足即失败）。
- `broll_plan` 之间不重叠；不超出其 `broll_slot`；不超出素材可用 source 窗口。
- broll overlay 的 4 个 frame 字段完整（供 `TimelinePlanning` verify-only 消费）。
- `font_plan` 候选为空时可为 null；`bgm_plan` 关闭或候选为空时可为 null。

## 8. Sandbox / 无 provider 确定性兜底

无真实 provider（sandbox）或 repair 后仍非法且允许兜底时：不调 LLM，按候选 `score`
降序做确定性默认选择（portrait 逐 slot 取最高分候选、broll 取锚点句最高分、font/bgm
取最高分），等价现有确定性节点的默认行为。保证：
- 新模板在 sandbox / 单测下能完整跑通（conftest 默认 `CUTAGENT_ALLOW_SANDBOX_FALLBACK=1`）。
- 走兜底必上报 `DegradationNotice`，绝不静默降级。

## 9. 新模板 `digital_human_editing_agent_v1`

节点序列（与 v2 上游顺序一致，仅把 3 个规划节点替换为 1 个）：

```
ValidateRequest, LoadCaseContext, ResolveCreativeIntent, TTS,
MaterialPackPlanning, NarrationAlignment, NarrationBoundaryPlanning,
EditingAgentPlanning,
TimelinePlanning, PortraitTrackBuild, LipSync, RenderFinalTimeline,
SubtitleAndBgmMix, ExportFinishedVideo, FinalizeRunReport
```

（注：与 issue 建议序列的唯一差异是 `MaterialPackPlanning` 沿用 v2 的靠前位置而非
挪到 `NarrationBoundaryPlanning` 之后——两者对 `EditingAgentPlanning` 的上游可得性
等价，靠前更贴近 v2、风险更低。）

注册点：
- `node_sequence.py`：新增 `EDITING_AGENT_SEQUENCE` + `WORKFLOW_GRAPHS` 条目 +
  `WORKFLOW_TEMPLATE_NODE_COUNTS`。
- 新建 `nodes/editing_agent_planning.py`；`nodes/__init__.py` 导入 + `__all__`。
- `digital_human.py`：`NODE_HANDLERS` 注册；`_NODE_OUTPUT_KINDS` 声明输出
  `[plan_portrait, plan_broll, plan_style, plan_editing_diagnostics]`；加入
  `_TIMELINE_REUSE_BREAK_NODES`（`reuse_policy=never`）；加入
  `_PROVIDER_SIDE_EFFECT_NODES`（带 `idempotency_key`）；新增
  `editing_agent_template()` + `_TEMPLATE_BUILDERS` 路由。

## 10. Prompt 改造

- 改造 `packages/core/storage/prompt_group_defaults.json` 的 `prompt_editing_agent`
  （v1）：输入协议从旧的 insert_time/asr_segments 改为 §5 的 ID 协议；输出 schema
  `prompt.editing.output` 改为 §5.2 的 ID-only 形状；明确禁止 LLM 输出裸帧/虚构 ID。
- 新增 prompt binding：`node_id=EditingAgentPlanning` → `prompt_editing_agent` published。
- MVP 只用这一个默认 prompt；`_steady`/`_balanced`/`_fast` 不绑定（不暴露节奏档位）。
- 生产经 prompt registry + provider gateway，不硬编码 prompt。

## 11. Provider 升级

`packages/core/storage/provider_seed.py`：`dashscope.llm.prod`（line ~89-91）的
`model_id` 已统一为 `qwen3.7-plus`（用户决策：共享升级，`ResolveCreativeIntent`
一并升级）。核查同 profile 的价格项或 prompt binding 默认 model 等其他引用时，
一并对齐。base_url compatible-mode 不变。

## 12. 前端（本 PR 一起做）

- `StudioCreateSteps.tsx`：新增第四个模板卡片"AI 综合剪辑"→ 新 contentMode。
- `StudioCreatePage.tsx`：新 mode 映射到 `workflow_template_id=digital_human_editing_agent_v1`；
  payload 构造 `edit: { instruction, max_repair_attempts }`。
- 新增"剪辑要求"长文本框（textarea，placeholder 用 PRD 示例），可选、默认空。
- `schema.d.ts` regen 后 `edit?` 类型自动可用。

## 13. 测试计划（逐条覆盖 issue 验收标准）

`tests/production/test_editing_agent_planning_node.py`（照 `test_portrait_planning_node.py`
范式：构造 `RunState` + 上游 artifacts + stub `LocalRuntimeAdapter`，走 sandbox）：

1. LLM 合法输出 → materialize 成 `plan.portrait`/`plan.broll`/`plan.style`，帧字段完整。
2. LLM 选非法 ID → validation/repair 生效（先红后绿）。
3. 无字体候选 → 默认字体路径（`case_default_font`）。
4. BGM 候选合法 → 生成 `StylePlanArtifact.bgm`。
5. B-roll overlay frame 字段完整且不重叠。
6. 新模板 smoke：sandbox 下 `template_for("digital_human_editing_agent_v1")` 构建 +
   `EditingAgentPlanning` 跑通产出三 artifact。

契约测试：`tests/contract` 的 settings/env 白名单不受影响；OpenAPI/schema 漂移闸靠
regen 提交。前端：`apps/web && npm run build` 绿。

## 14. 交付流程

1. ultracode Workflow 编排实现阶段（并行分工 + TDD 先红后绿；`rev-correct` +
   `rev-clean` 双审 → 修复）。
2. 主会话串行：落地 → `.venv/bin/python -m pytest` 相关域（真 PG:55432、串行、禁 xdist）
   全绿 → regen openapi/schema → commit → push `feat/editing-agent-planning-mvp`。
3. 开 PR → CI 绿（unit/integration/frontend/production-preflight）→ 子代理双审修复。
4. rebase 最新 main → `gh pr merge --squash --admin` → 关 #136。
5. 等 CI（约 11–12min）期间并行找有价值的事做，不空等。

## 15. 关键决策与风险

- **保留资产级唯一性**：`EditingAgentPlanning` 与 `PortraitPlanning` 一样，强制同一
  `asset_id` 在每个 run 的人像主轨里最多使用一次。用户要求"尽量用同样穿搭的人像"时，
  只能在风格相近但不同 `asset_id` 的候选之间选择，不能靠重复同一素材填满全片。
- **materializer 复用面**：portrait 复用 `frame_grid` 纯函数（已核实可行）；broll 复用
  `broll_plan` 对齐逻辑（接口在 writing-plans 阶段依探查定稿）；style 复用 `style_planning`
  的字体默认 + bgm 映射 + overlay 派生。
- **共享升级 qwen3.7-plus**：`ResolveCreativeIntent` 一并换模型；单测走 sandbox 不受
  影响，仅真实生产调用换模型。
- **契约漂移对环境敏感**：regen 用项目 `.venv`；只有本功能真实新增字段才提交 diff，
  不本地 regen 去"修"幻影 key-order 漂移。`schema.d.ts` 禁手改。

## 16. issue 验收标准 → 实现映射

| 验收标准 | 实现点 |
|---|---|
| 新增 `EditingAgentPlanning` 节点 | §4，`nodes/editing_agent_planning.py` |
| 新增 `digital_human_editing_agent_v1` 模板 | §9 |
| 新模板完整输出三 plan 并走通下游渲染 | §6 materializer + 下游零改动 + §13.6 smoke |
| LLM 输出只含 ID 选择、无权威 frame | §5.2 协议 + §7 validator |
| materializer 生成完整 frame 字段 | §6.1/6.2 |
| `TimelinePlanning` 仍 verify-only | 下游零改动 |
| 支持 `request.edit.instruction` | §3.1/3.2 + §12 |
| 不新增 rhythm_preset/节奏档位 | §10（只单默认 prompt） |
| 单测：合法输出 materialize / 非法 ID repair / 无字体默认 / bgm 生成 / broll 帧完整不重叠 | §13.1–13.5 |
