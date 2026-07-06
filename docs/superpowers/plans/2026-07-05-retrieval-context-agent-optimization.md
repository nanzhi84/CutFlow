# 检索与剪辑 Agent 智能化收口：query 生成、结构化输出与 V4.5 外观标注

> 2026-07-05 · 基于 PR #156→#169 演进线（head `579c059`）的架构评审与分阶段方案。
> 修订 r2（同日）：与 owner 讨论后收敛范围——去掉决策拆分/视觉聚拢/聚类 key/约束编译/case 偏好持久化/放开 broll 含人池；
> V4.5 开放式外观标注升级为核心项，语义描述必须进 LLM prompt（qwen3.7-plus 256k 上下文充足），embedding key 与标注解耦。
>
> 核心判断：**确定性执行 + 硬资格闸这半程已经修扎实了，下一步的杠杆点在"智能前移"的前半程**——query 生成太笨（模板拼接）、
> 标注太粗（没有外观/实体维度）、语义描述被 prompt 压缩误伤。

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
| 离线索引 | per-clip 干净源窗口一条向量；ffmpeg 裁片段→签公网 URL→DashScope **纯视频** embedding（文本标注不参与向量） | `packages/planning/material/clip_embedding.py:51-77`、`apps/api/services/clip_embeddings.py:162,671`、`packages/ai/providers/dashscope.py:265-279` |
| 资格硬门 | usable/角色/person/avoid-span/reservation；LRU 缓存（#164） | `packages/production/pipeline/nodes/material_pack_planning.py:616,712` |
| query 生成 | 确定性模板拼接：固定英文前缀+旁白+instruction+case 上下文，900 字符截断 | `packages/production/pipeline/nodes/window_query_planning.py:41-66,100-126` |
| 检索 | eligible 池内 HNSW cosine，recall 10×topK，score=sim−0.1×recency+tiebreak，topK=12 | `packages/production/pipeline/nodes/window_material_retrieval.py:30,261-309` |
| LLM 决策 | 单次综合 prompt 输出 portrait+broll+font+bgm 四类 ID；repair 默认 1 次；#169 本地 B-roll 修复；prompt 侧 topk 限 6 | `packages/production/pipeline/nodes/editing_agent_planning.py:74-75,391-473,528-668`、`packages/production/pipeline/_editing_agent.py:74-127,371-520,644-679` |

## 1. 问题清单

### A. 检索 query 端（智能缺位）

- **A1** `WindowQueryPlanning` 是纯模板拼接，是整条检索链里唯一没有智能的环节，检索质量上限被模板锁死（`window_query_planning.py:41-66`）。
- **A2** `_trim_intent` 900 字符截断，而 `Instruction: {request.edit.instruction}` 拼在**最尾部**；portrait 窗口 unit_text 为空时回退塞整个 script（`:44`），此时甲方 instruction 恰好最先被截掉（`window_query_planning.py:100-126`）。注意此截断作用于 **embedding query**（非 LLM prompt），修法是调顺序而非拉长——query 过长本身会稀释向量语义。
- **A3** 单一 cosine 排序。jieba 关键词匹配体系（`packages/planning/material/matching.py`）目前只在 `DeterministicEditingPlanning` 的 broll 回退里当灾备（`deterministic_editing_planning.py:86-99`），没有作为常态融合信号。文本→视频跨模态检索对标注质量很敏感，缺关键词通道保险。
- **A4** 采样策略单一：`CLIP_SAMPLE_POLICY` 对长短 clip 一视同仁（`clip_embedding.py:25-30`），长 clip 语义被稀释。policy 已版本化进 embedding key，改动安全。

### B. LLM context 组织与输出合规

- **B1** 输出合规靠"只输出 JSON 不要 markdown"的裸文本约定 + `parse_selection` 容错 + repair 回喂（默认仅 1 次，`packages/core/contracts/jobs.py:117`）。**格式**错误（markdown 包裹/前后缀）本不该消耗 repair 预算。决策保持单次综合（剪辑是综合判断，portrait/broll/BGM 互相牵制，不拆分），因此解码层保障是唯一可加的防线，优先级最高。
- **B2** portrait 的 ID 幻觉没有本地修复：#169 的 `_repair_broll_selection_to_constraints` 只修 B-roll，portrait 选了 topk 外但语义等价的 window 只能烧 LLM repair。
- **B3** #169 的 compact 策略"误伤"了语义：砍簿记字段（帧号/源区间/score）是对的——那是 LLM 不该看的噪音；但语义文本也被一起穷省（portrait 候选只剩 4 字段、topk 限 6 而检索层给 12）。qwen3.7-plus 有 256k 上下文，语义描述完全装得下。
- **B4** JSON 键名重复是纯 token 浪费（候选池可改行式紧凑序列化）。narration_units 的 start/end **保留不动**（owner 决定：节奏感知信号）。

### C. 标注粒度（"衣服尽量相同"案例的根因）

- **C1 采集层**：`ClipV4` 全部语义字段无任何服饰/外观/人物身份维度（`media.py:646-677`），VLM 标注 prompt 从不要求描述外观（`vlm.py:64-98`）。而 `EditPlanningOptions.instruction` 的 docstring 例子恰是"尽量用穿搭相近的人像"（`jobs.py:110-113`）——契约写了愿望，数据层无支撑。
- **C2 传递层**：`_compact_prompt_input` 把 portrait 候选砍到 candidate_id/asset_id/available_seconds/reason 四字段（`editing_agent_planning.py:436-445`），即使标注有外观，LLM 也无从比对。
- **C3 耦合成本坑**：`patch_annotation` 会 bump `asset.updated_at`（`packages/media/sqlalchemy_repository.py:~500`），而 clip embedding key 把 `updated_at` 编进 `asset_revision`（`clip_embedding.py:43-48`）→ 任何标注文本改动都让该资产全部向量 key 失效、触发重嵌入。但索引侧 embedding 是**纯视频输入**（`dashscope.py:265-279`、`clip_embeddings.py:635-650`），文本从不参与向量——重标注触发重嵌入是纯浪费，全库 V4.5 重标注前必须解耦。

### D. 运维附带发现（顺手修）

- **D1** clip embedding 索引 job 状态存进程内 dict、不持久化（`clip_embeddings.py:124-162`）；API 重启丢进度，前端（#168）看不到延续状态。
- **D2** 索引硬性要求素材公网可取（`clip_embeddings.py:754`，localhost/私网 fatal），与 lipsync 公网 OSS 前提同类，应在文档/preflight 里显式声明。

## 2. 非目标（红线，含 r2 讨论明确不做的）

- **不做 full-agentic 检索**（LLM 逐窗迭代发查询）：成本爆炸，破坏确定性选材/resume/ledger 三条不变量。智能只进入 query 生成端和候选决策端，检索执行保持确定性 ANN。
- **不给 LLM 输出帧号/秒数**：#169 确立的"LLM 只选 ID、本地算帧"分工不动。
- **不拆分 LLM 决策**：剪辑是综合判断，portrait/broll/字体/BGM 一次综合决策保持不动（r2 决定）。
- **不做基于向量/聚类的一致性机制**：不加 person/outfit cluster key，不做"与已选片段向量相似度"聚拢加分（r2 决定）。一致性走"V4.5 详细外观描述进 prompt + LLM 综合判断"路线。
- **不做约束编译 DSL、不做 case 级偏好持久化**（r2 决定，后者留待后续单独评估）。
- **不放开 broll 含人片段池**（r2 决定：`material_pack_planning.py:745` 的排除保持）。
- **不逐个加服饰枚举字段**：走 VLM 开放式自由文本描述。
- **narration_units 的 start/end 保留**（r2 决定）。

## 3. 分阶段方案（r2 修订版）

### Stage 1 — 低成本高确定性（每项独立可交付）

1. **S1-1 结构化输出接线（优先级最高）**：llm.chat 走 DashScope JSON mode / function calling，从解码层保证合法 JSON；`parse_selection` 保留作双保险。repair 预算只留给语义违规。决策不拆分的前提下，这是 B 组问题唯一的根治手段。改动面：`provider_gateway` + dashscope provider + `_invoke`。
2. **S1-2 修 A2 截断顺序**：`_context_text` 的 instruction 挪到 narration 之前；portrait 空 unit_text 时不回退整段 script。不拉高 900 字符上限（embedding query 过长稀释向量）。
3. **S1-3 portrait ID 幻觉本地修复**：仿照 `_repair_broll_selection_to_constraints`，在 legal_window_ids∩topk 内做最近邻替换（约束更硬，机制同构）。
4. **S1-4 混合检索**：把关键词匹配从灾备升级为常态融合信号，`retrieval_score` 加 keyword 通道（RRF 或加权和），权重先保守（向量为主）。改动面只在 `window_material_retrieval.py` 打分处。
5. **S1-5 prompt 序列化与阈值校准**（r2 修订）：
   - 候选池从 JSON 改行式紧凑文本（键名重复是纯浪费，预计省 30-50% token）；**start/end 等语义字段一律保留**。
   - `_PROMPT_RETRIEVAL_TOPK_LIMIT` 6→12，对齐检索层 topK，中间不再折损（`editing_agent_planning.py:74`）。
   - compact 策略重新校准：**簿记字段（帧号/源区间/score）照砍，语义文本全保留**——为 Stage 3 的 `visual_detail` 进候选字段留好位置。qwen3.7-plus 256k 上下文充足。
   - prompt 变大后按需上调 provider profile `timeout_sec`（LLM 调用超时来源，`dashscope.py:65` 等）。
6. **S1-6 修 D1**：索引 job 状态落 DB（或 Redis），对齐现有 annotation job 的持久化模式。

### Stage 2 — query 端智能前移

1. **S2-1 WindowQueryPlanning agentic-lite**：一次 LLM 调用为**全部窗口批量**生成结构化检索意图（每窗更好的自然语言 query），产物落 artifact、可 resume 复现。模板拼接保留为 provider 缺失时的确定性回退。绑定 prompt registry（不硬编码），注意 prompt seed 迁移同步（参考 0029/0030 的坑）。

### Stage 3 — AnnotationV4 → V4.5：开放式外观/实体标注（核心项）

设计原则（r2 讨论确定）：**充分发挥 VLM 自由文本标注能力**，不设枚举、不做聚类；描述必须一路到达 LLM prompt。

1. **S3-1 契约：`ClipEntitiesV45` 新子层**（挂在 `ClipV4` 上，全部可选、带默认值，旧 V4 标注零迁移依然合法）：
   - `visual_detail: str` — VLM 自由发挥的详细画面描述：人物着装（颜色/款式/材质）、发型、配饰、产品、品牌元素、环境细节。鼓励写细，不限长。
   - `entity_keywords: list[str]` — 从描述中抽取的轻量实体词，并入现有 keyword 匹配通道（喂 S1-4 的混合检索）。
   - `AnnotationVersion` 枚举加 `v4_5`；`retrieval_sentence` 保持"一句话检索摘要"职责不稀释。
2. **S3-2 VLM 标注 prompt 增强**（`vlm.py:45-141`）：portrait 段重点要求描述人物外观（衣服颜色/款式、发型、配饰、背景环境）；broll 段重点描述实体（产品、品牌露出、场景物件、画面中人物外观）。每 clip 多产几十至上百字，VLM 成本增幅小。
3. **S3-3 embedding key 与标注解耦**（前置于全库重标注）：已核实索引 embedding 是纯视频输入、文本标注不参与向量（`dashscope.py:265-279`），故 `asset_revision_token` 从 `asset.updated_at` 改绑**媒体内容指纹**（上传 complete 登记的 sha256；`clip_embedding.py:43-48`）。clip 切分变动天然产新 key（key 已含 `source_start/end`+`clip_id`），源文件未换则纯文本重标注**零重嵌入成本**。需 bump `index_version` 一次 + 全库重建（一次性成本换永久解耦）。
4. **S3-4 语义描述进 LLM 候选字段**（V4.5 生效的前提，修 C2）：`_compact_prompt_input` 的 portrait 候选加 `visual_detail` 摘要，broll 候选在 `scene_name` 旁加实体描述。与 S1-5 的"语义全保留"校准配套。
5. **S3-5 重标注策略**：V4.5 字段可选、旧标注不失效；按需重标走 #168 现成的 per-asset reprocess 通路；解耦（S3-3）落地后批量重标注只有 VLM 成本、无重嵌入成本。

### 落地顺序建议

S1 六项并行可做 → S3-3（解耦，重标注的前置）→ S3-1/S3-2（契约+标注 prompt）→ S3-4（进 prompt）+ S1-5 配套 → S2-1（query agentic-lite，可与 Stage 3 并行）。

## 4. 依赖与风险

- S2-1 新增 LLM 调用点：一次性调用、产物落 artifact，不破坏确定性；prompt 经 registry 绑定。
- S3-1 契约变更连带 openapi.json + schema.d.ts 重生成（CI 校验漂移）；标注编辑器前端需展示/可编辑新字段。
- S3-3 改 embedding key 组成属一次性全库重建，需与 D1（job 状态持久化）先后配合，避免重建中途 API 重启丢进度。
- "衣服尽量相同"这类一致性诉求在本方案下是 **best-effort**（LLM 依据 visual_detail 综合判断），无确定性保证——这是 r2 讨论中有意选择的取舍（不做聚类 key/约束编译）。
- 改 EditingAgentPlanning/检索节点均属 `_TIMELINE_REUSE_BREAK_NODES` 域（reuse_policy=never），无 resume 兼容负担。
- worker 是独立进程：所有节点改动部署时须重启 worker。

## 附：r2 讨论删除项存档（含理由）

| 原编号 | 内容 | 不做的理由 |
|---|---|---|
| S2-2 | EditingAgent 决策拆分（portrait/broll 两次调用） | 剪辑是综合判断，拆分丢全局协调性 |
| S2-3 | 零标注成本视觉聚拢（检索分加已选片段向量相似度） | 不走向量一致性路线 |
| S3-2(r1) | person/outfit 聚类 key | 不做确定性一致性机器，走自由文本+LLM 综合判断 |
| S3-3(r1) | instruction→结构化约束 DSL 编译 | 同上 |
| S3-4(r1) | case 级剪辑偏好持久化 | 暂不做，后续单独评估 |
| S3-5(r1) | 未满足约束显式 degradation | 依赖约束编译，随之删除 |
| S3-6(r1) | 放开 broll 含人片段池 | 产品决策：不放开 |
| S1-5(r1) 部分 | narration_units 去掉 start/end | 保留：节奏感知信号，256k 上下文不差这点 token |
