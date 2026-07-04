# packages/planning

纯函数、确定性的「规划层」：把 `AnnotationV4` 素材 + 旁白单元 +（可选）选择台账，算成素材打分/匹配、确定性选片、以及帧对齐的口播时间线计划。无 IO、无随机、不调用任何 provider；被 `packages/production/pipeline/nodes/`（portrait / material / broll / coverage / narration_alignment 等节点）消费。

## 职责
- material：jieba 关键词 + 同义扩展把脚本节拍与 b-roll 片段做 Jaccard 变体相似度匹配（`matching.py`），并据此对 b-roll（`broll_pack.py`）/ portrait·bgm·font（`portrait_pack.py`）候选打真实分。
- material：把排好序的 b-roll 候选锚定到真实旁白窗口内、生成不重叠的插入计划（`broll_plan.py` 的 `plan_insertions`，供 broll_planning 消费）/ 确定性 b-roll 覆盖规划（同文件 `plan_coverage`，供 broll_coverage_planning 消费），素材不足时返回空（上游软降级）。
- selection：从选择台账（`SelectionLedgerEntry`）算「近期使用」惩罚，让上一轮用过的素材这一轮被压到新素材之下（`recency.py`/`recency_context.py`）。生产链路只在 `MaterialPackPlanning` 读 ledger，再把 `recent_usage`/`recency_penalty` 写进候选 metadata，后续 planning 节点不再读 ledger。
- editing：把旁白单元 + portrait 源窗口候选，经语义/停顿边界 → beam 搜索 → 容量打包 → 一次性量化到固定 30fps 帧网格，产出帧精确的 `BoundaryTimelinePlan`；人像主轨强制资产级唯一性（`template_id` 每 run 最多用一次，`max_uses=1`）。

## 关键文件 / 子目录
- `material/keywords.py` — 确定性 jieba 关键词抽取 + 脚本分句（匹配的底料）。
- `material/matching.py` — `score_segment`/`best_match`；`MatchResult.has_overlap` 区分真实语义重叠 vs. 仅时长契合的 tie-breaker。
- `selection/recency.py` — `compute_recency`/`recency_penalty_for`，台账驱动的衰减惩罚；`selection/recency_context.py` 产出人像 `recent_usage`（近期使用 + opening guard）。
- `editing/plan.py` — 顶层入口 `plan_boundary_timeline`；`editing/frame_grid.py` 是 30fps 帧网格的唯一真源。子步骤：`boundary.py`（语义+停顿边界装配）→ `packing.py`（容量打包，内部跑 `beam.py` 定宽搜索 + `rescue.py` 回溯救援）；`constants.py` 是 beam 宽度/容量上限/救援上限等权重常量的集中真源。
- `material/__init__` / `selection/__init__` / `editing/__init__` — 各子域公开 API（从这里 import）。

## 约定与要求
- 选片必须确定性 + 台账支撑，禁止任何随机；排序用稳定 key（如 `(-score, asset_id, clip_id)`）。
- 多样性/近期性本身是软惩罚（demote），不是硬过滤；但人像主轨的资产级唯一性是硬约束，同一 `template_id` 不可通过多个窗口重复补时长。
- 素材/标注不足时返回空结果由上游软降级，绝不伪造选片或机械占位（不要回到 `index*3` / `score=1` 老种子）。
- 全部为纯函数：台账/停顿等都作为入参传入（most-recent-first），本层不查 DB、不跑 ffmpeg、不做音频检测。
- editing 只支持固定 30fps 网格（`TIMELINE_FPS`），fps 不符会 raise；移植自 origin 的权重/常量要与其标定一致。

## 测试
- `pytest tests/planning`（覆盖 b-roll 覆盖/动作/人物过滤、portrait clip/recency、material、editing 各子域），纯单测无需 env flag。

## 注意 / 坑
- 改 portrait recency 语义要同步 `recency_context.py` 里 ledger 字段映射：`asset_id`→template_id、`slot_phase=="portrait_opening"`→开场守卫、`diversity_key`→相似簇。
- `TimelineWindowPlanning` 的 capacity-controlled split 只缩短过长 chunk，让不同资产参与覆盖；它不能放宽 `max_uses=1`，覆盖不足应 hard fail 而不是复用同一资产。
- editing 里 timeline 边界连续（段 i 的 end frame == 段 i+1 的 start frame）是不变量；量化后 <1 帧的退化段会被丢弃并记入 trace，别改成静默平移后续段。
