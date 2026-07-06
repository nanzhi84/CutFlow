# 检索与剪辑 Agent 智能化收口：query 生成、结构化输出、约束编译与外观一致性

> 2026-07-05 · 基于 PR #156→#169 演进线（head `579c059`）的架构评审与分阶段方案。
> 核心判断：**确定性执行 + 硬资格闸这半程已经修扎实了，下一步的杠杆点全在"智能前移"的前半程**——query 生成太笨（模板拼接）、instruction 太软（自由文本靠 LLM 自觉）、标注太粗（没有外观/身份维度）。

## 0. 背景：近 10 个 PR 的演进线

#161 权威 B-roll 窗口 → #162 MaterialPack 从"排序候选"改成"硬资格池" → #163/#167 clip 级
qwen3-vl-embedding 检索（pgvector HNSW）→ #169 prompt 压缩成 decision-only 字段 + 修复循环显式化。
整体方向是把"排序智能"从确定性规划层剥离，交给 embedding 检索 + LLM 决策，用硬资格闸和本地校验器兜底。

当前链路（digital_human_v2 / editing_agent 链共用）：

```
上传 → 标注(AnnotationV4 per-clip) → 离线 clip embedding 索引(qwen3-vl, 1024维, HNSW)
  → MaterialPackPlanning(硬资格池, 不排序)
  → TimelineWindowPlanning(帧精确权威窗口)
  → WindowQueryPlanning(每窗一条模板拼接的文本 retrieval_intent)
  → WindowMaterialRetrieval(文本query→向量, HNSW cosine, topK=12)
  → EditingAgentPlanning(LLM 在 topK 白名单内选 ID) / DeterministicEditingPlanning
  → materialize(本地纯函数算精确帧)
```

各层职责与关键锚点：

| 层 | 现状 | 锚点 |
|---|---|---|
| 标注 | PySceneDetect+VAD 切窗、VLM 判语义，四层 ClipV4 | `packages/core/contracts/media.py:646-716`、`packages/media/annotation/vlm.py:45-141` |
| 离线索引 | per-clip 干净源窗口一条向量；ffmpeg 裁片段→签公网 URL→DashScope video_url | `packages/planning/material/clip_embedding.py:51-77`、`apps/api/services/clip_embeddings.py:162,671` |
| 资格硬门 | usable/角色/person/avoid-span/reservation；LRU 缓存（#164） | `packages/production/pipeline/nodes/material_pack_planning.py:616,712` |
| query 生成 | 确定性模板拼接：固定英文前缀+旁白+instruction+case 上下文，900 字符截断 | `packages/production/pipeline/nodes/window_query_planning.py:41-66,100-126` |
| 检索 | eligible 池内 HNSW cosine，recall 10×topK，score=sim−0.1×recency+tiebreak，topK=12 | `packages/production/pipeline/nodes/window_material_retrieval.py:30,261-309` |
| LLM 决策 | 单次巨型 prompt 输出 portrait+broll+font+bgm 四类 ID；repair 默认 1 次；#169 本地 B-roll 修复 | `packages/production/pipeline/nodes/editing_agent_planning.py:391-473,528-668`、`packages/production/pipeline/_editing_agent.py:74-127,371-520,644-679` |

## 1. 问题清单

### A. 检索 query 端（智能缺位）

- **A1** `WindowQueryPlanning` 是纯模板拼接，是整条检索链里唯一没有智能的环节，检索质量上限被模板锁死（`window_query_planning.py:41-66`）。
- **A2** `_trim_intent` 900 字符截断，而 `Instruction: {request.edit.instruction}` 拼在**最尾部**；portrait 窗口 unit_text 为空时回退塞整个 script（`:44`），此时甲方 instruction 恰好最先被截掉（`window_query_planning.py:100-126`）。
- **A3** 单一 cosine 排序。jieba 关键词匹配体系（`packages/planning/material/matching.py`）目前只在 `DeterministicEditingPlanning` 的 broll 回退里当灾备（`deterministic_editing_planning.py:86-99`），没有作为常态融合信号。文本→视频跨模态检索对 `retrieval_sentence` 质量很敏感，缺关键词通道保险。
- **A4** 采样策略单一：`CLIP_SAMPLE_POLICY` 对长短 clip 一视同仁（`clip_embedding.py:25-30`），长 clip 语义被稀释。policy 已版本化进 embedding key，改动安全。

### B. LLM context 组织与输出合规

- **B1** 输出合规靠"只输出 JSON 不要 markdown"的裸文本约定 + `parse_selection` 容错 + repair 回喂（默认仅 1 次，`packages/core/contracts/jobs.py:117`）。**格式**错误（markdown 包裹/前后缀）本不该消耗 repair 预算。
- **B2** 单次巨型决策：一次输出 portrait+broll+font+bgm 四类，schema 复杂度直接推高出错率；其中 font/bgm 候选仅 6 个、几乎规则可判，智能含量与 portrait/broll 极不对称。
- **B3** portrait 的 ID 幻觉没有本地修复：#169 的 `_repair_broll_selection_to_constraints` 只修 B-roll，portrait 选了 topk 外但语义等价的 window 只能烧 LLM repair。
- **B4** token 大头是 narration_units（脚本本体）+ JSON 键名重复开销；units 还带着 slot 里已有的 start/end 冗余。

### C. 标注粒度与个性化约束（"衣服尽量相同"案例，三层全断）

- **C1 采集层**：`ClipV4` 全部语义字段无任何服饰/外观/人物身份维度（`media.py:646-677`），VLM 标注 prompt 从不要求描述外观（`vlm.py:64-98`）。讽刺的是 `EditPlanningOptions.instruction` 的 docstring 例子恰是"尽量用穿搭相近的人像"（`jobs.py:110-113`）——契约写了愿望，数据层无支撑。
- **C2 传递层**：`_compact_prompt_input` 把 portrait 候选砍到 candidate_id/asset_id/available_seconds/reason 四字段（`editing_agent_planning.py:436-445`），即使标注有外观，LLM 也无从比对。
- **C3 约束层**：现有跨 clip 机制**全是多样性（推开）**——资产唯一性、diversity_key 去重、recency 降权；没有任何"一致性（聚拢）"机制，且检索逐窗独立。`diversity_key = scene_type or narrative_role`（`packages/planning/material/broll_pack.py:124`），无人物/服装维度。
- **C4 第 0 环（产品决策）**：B-roll 池默认整段排除含人片段（`material_pack_planning.py:745`），"B-roll 里的人物"素材池现在根本是空的。
- **C5** instruction 生效只有两条弱通路：拼进检索 intent 尾部（还会被 A2 截断）+ 塞进剪辑 prompt 靠 LLM 自觉；未满足时静默吞掉，无 degradation 上报，违背"降级必须显式"的红线精神。

### D. 运维附带发现（顺手修）

- **D1** clip embedding 索引 job 状态存进程内 dict、不持久化（`clip_embeddings.py:124-162`）；API 重启丢进度，前端（#168）看不到延续状态。
- **D2** 索引硬性要求素材公网可取（`clip_embeddings.py:754`，localhost/私网 fatal），与 lipsync 公网 OSS 前提同类，应在文档/preflight 里显式声明。

## 2. 非目标（红线）

- **不做 full-agentic 检索**（LLM 逐窗迭代发查询-看结果-改写）：几十窗×多轮调用成本爆炸，且破坏确定性选材/resume 可复现/ledger 降权三条核心不变量。智能只进入 query 生成端和候选决策端，检索执行保持确定性 ANN。
- **不给 LLM 输出帧号/秒数**：#169 确立的"LLM 只选 ID、本地算帧"分工不动。
- **不逐个加服饰枚举字段**：甲方个性化维度无穷（今天衣服、明天背景竞品 logo），枚举字段永远追不上，走开放式描述 + 聚类 key。

## 3. 分阶段方案

### Stage 1 — 低成本高确定性（每项独立可交付）

1. **S1-1 结构化输出接线**：llm.chat 走 DashScope JSON mode / function calling，从解码层保证合法 JSON；`parse_selection` 保留作双保险。repair 预算只留给语义违规。改动面：`provider_gateway` + dashscope provider + `_invoke`。
2. **S1-2 修 A2 截断**：`_context_text` 的 instruction 挪到 narration 之前，或对 narration 单独限长；portrait 空 unit_text 时不回退整段 script。
3. **S1-3 portrait ID 幻觉本地修复**：仿照 `_repair_broll_selection_to_constraints`，在 legal_window_ids∩topk 内做最近邻替换（约束更硬，机制同构）。
4. **S1-4 混合检索**：把关键词匹配从灾备升级为常态融合信号，`retrieval_score` 加 keyword 通道（RRF 或加权和），权重可先保守（向量为主）。改动面只在 `window_material_retrieval.py` 打分处。
5. **S1-5 prompt 序列化减脂**：narration_units 去掉 start/end；候选池从 JSON 改行式紧凑文本（键名重复是纯浪费，预计再省 30-50% token）。
6. **S1-6 修 D1**：索引 job 状态落 DB（或 Redis），对齐现有 annotation job 的持久化模式。

### Stage 2 — 智能前移到 query/决策结构

1. **S2-1 WindowQueryPlanning agentic-lite**：一次 LLM 调用为**全部窗口批量**生成结构化检索意图（每窗更好的自然语言 query + 可选结构化过滤条件），产物落 artifact、可 resume 复现。模板拼接保留为 provider 缺失时的确定性回退。
2. **S2-2 决策拆分**：EditingAgentPlanning 拆成 portrait 选择与 broll 覆盖两次调用（各自 schema 减半），font/bgm 规则化或并入小调用。代价是多一次 LLM 调用，对分钟级 pipeline 可忽略。
3. **S2-3 零标注成本的视觉聚拢**：选定第一个 portrait 片段后，后续窗口检索分加一项"与已选片段 clip 向量的余弦相似度"（可由 instruction/约束开关控制方向与权重）。只改 `window_material_retrieval` 打分公式，一行不动标注，立刻得到"视觉风格聚拢"近似效果。

### Stage 3 — 约束系统与外观标注（回答"如何承接甲方个性化"）

1. **S3-1 开放式外观/实体标注层**：VLM 标注额外输出自由文本 `appearance` + 结构化实体列表（人物着装、产品、品牌元素），挂 `ClipRetrievalV4` 旁。标注一次，检索与一致性判断都能消费。需要 AnnotationV4→V5 或 V4 增量字段 + 存量素材重标注策略。
2. **S3-2 外观/人物聚类 key**：离线用人脸 embedding（已有 YuNet 基础）+ 服装区域特征聚类，给 clip 打 `person_cluster_id`/`outfit_cluster_id`。"聚拢"与现有"推开"（diversity_key）用同一套机制表达——eligibility 过滤、检索加权、validator 校验全部确定性，LLM 零开销。
3. **S3-3 约束编译**：扩展 ResolveCreativeIntent（或新节点），一次 LLM 调用把甲方自然语言 instruction 翻译成结构化约束，如 `{type: consistency, dimension: outfit_cluster, scope: portrait_track, strength: prefer}`，落 artifact；下游确定性节点消费（eligibility 过滤 / 检索加权 / validator 校验）。
4. **S3-4 约束挂 case 不挂 job**：甲方偏好持久化在 case_profile 层，每次生成自动注入，job 级 instruction 只做单次覆盖。
5. **S3-5 未满足约束显式上报**：新增 degradation/warning（如 `edit_instruction_unsatisfied`），diagnostics 记录哪条约束、差在哪一环，给运营一个和甲方对齐预期的抓手。注意：新增 WarningCode 属契约变更，必须重生成 openapi.json + schema.d.ts。
6. **S3-6（产品决策）**：是否放开 `broll_person_clip` 排除，让"B-roll 含人"素材进池——是 C4 的前提，需产品侧确认。

## 4. 依赖与风险

- S2-1/S2-2/S3-3 新增 LLM 调用点：均为一次性调用、产物落 artifact，不破坏确定性；需绑定 prompt registry（不得硬编码），并注意 prompt seed 迁移同步（参考 0029/0030 的坑）。
- S3-1/S3-2 涉及标注契约与存量重标注成本，是三阶段里唯一"重"的部分；S2-3 是它的廉价前菜，先验证"视觉聚拢"有无产品价值再投入。
- 改 EditingAgentPlanning/检索节点均属 `_TIMELINE_REUSE_BREAK_NODES` 域（reuse_policy=never），无 resume 兼容负担；改 WarningCode/契约需重生成前端 schema。
- worker 是独立进程：所有节点改动部署时须重启 worker。
