# BGM 按 clip（分段·精确到秒 + 真听语义）标注 — 设计文档

- 日期：2026-06-19
- 范围：**数据层 + UI 层 + 新增 `audio.understanding` provider 能力（Qwen-Omni 真听）**
  - 注：原定「纯数据+UI」，用户明确选择「整轨真听」，范围由其决定扩到含一个新 provider 能力。
- 状态：设计已与用户逐项对齐，待 spec 评审 → writing-plans

## 1. 背景与问题

### 现状
BGM 标注入口 `packages/media/annotation/bgm.py::annotate_bgm()` 对**整条文件**产出**一组全局标签**，无时间分段：

- 客观特征：`bpm` / `energy` / `tempo_bucket` (librosa)、整轨响度 `loudness_lufs` (ffmpeg loudnorm)。
- 「语义」：`mood` / `genre` / `scene_fit` / `avoid_scene` / `agent_caption`，由 `llm.chat` 产出。

全部写入 `quality_report["bgm"]`；**不写 `clips`，不写 `usage_windows`**。

### 「标注了跟没标注一样」的三层根因
1. BGM 产出 0 个时间分段；编辑器「结构化片段」对应 `canonical.clips`，BGM 永远空。
2. 前端 `AnnotationEditorModal.tsx` 只渲染 `clips`/`quality_events`；BGM 语义在 `quality_report.bgm`，**弹窗从没读没展示**（`apps/web/src` grep `mood/genre/bgm/scene_fit/适用` 无命中）。
3. 未配 `llm.chat` 时降级为「只有 bpm/响度」（`llm_unconfigured`/failed），语义全空。

### 语义其实是「没听就猜」（用户追问后查实）
`llm.chat` 绑的是 **DashScope `qwen-plus`（纯文本模型）**（`provider_seed.py:71`）。`_build_semantic_prompt` 只发了**曲名 + bpm/energy/tempo/loudness 几个数字**，**音频字节根本没发**。即情绪/曲风/场景是文本模型看文件名+数字猜的。系统里真正处理音频波形的只有 **librosa**（信号特征）；`asr.transcribe`(paraformer) 是语音转写、`vlm.annotation`(qwen-vl) 是看图，都不理解器乐 BGM。**当前 provider 清单无任何音频理解能力。**

## 2. 目标与非目标

### 目标
- **卡点/节奏对齐**：标出节拍/drop 的精确秒数（卡点网格）。
- **分段铺底选用**：标出 1-3 条「推荐使用片段」，供编辑 agent / 运营按视频部分挑合适 BGM 段。
- **和素材库结构对齐**：BGM 在标注编辑器里也呈现为带秒数、可手动编辑的片段。
- **语义真听**：情绪/曲风/场景由真正「听」过音频的模型产出，而非看文件名猜。

### 关键产品判断（用户确认）
短视频 ≤ 1 分钟，一般只用一首 BGM 的**一部分**。因此**不做整曲结构图谱**，只标少量「推荐使用片段」+ 客观卡点网格。

### 核心架构原则（用户定，与视频素材路径严格同构）
**确定性传感器掐时间，gated 语义模型只听不报秒**：
- librosa 做频率/响度/节拍/能量分析 → **切出 clip（拥有全部精确秒数）**；
- 切出 clip 后，**每段音频**交给 Qwen-Omni 听、回填语义；
- **绝不让音频大模型输出秒数**（音频 LLM 报精确秒不可靠）。

### 非目标（本次不做）
- ❌ 不改剪辑/时间轴节点去**真正卡点切镜**（消费层，今天空白，后续单独立项）。
- ❌ 不做数据迁移：新字段 additive，旧标注保持原样，点「重新分析」才出新结构。
- ❌ 不做整曲结构化分段（intro/verse/drop/outro 铺满整轨）。

## 3. 数据形状（契约）

文件：`packages/core/contracts/media.py`（+ `contracts/__init__.py` 同步 import/`__all__`）

```python
class BgmSegmentRole(str, Enum):
    hook = "hook"; climax = "climax"; outro = "outro"; general = "general"

class BgmUsageWindowV4(ContractModel):
    """BGM 推荐使用片段：整轨里值得用的一小段（非铺满整轨）。"""
    segment_id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    duration: float = Field(ge=0)
    role: BgmSegmentRole = BgmSegmentRole.general
    drop_anchor_sec: float | None = None        # 高潮/drop 秒，须落在 [start,end]
    energy: float = Field(0.0, ge=0, le=1)       # 客观 RMS 均值（librosa）
    mood: str = ""                               # Qwen-Omni
    scene_fit: list[str] = Field(default_factory=list)
    avoid_scene: list[str] = Field(default_factory=list)
    reason: str = ""                             # 给 agent 的推荐理由
    confidence: float = Field(0.8, ge=0, le=1)
    source: str = "sensor"                       # sensor | sensor+audio
    # validator: end>start；drop_anchor_sec ∈ [start,end] 或 None；duration 容差修正
```

挂载到 `AnnotationV4`：
- 新增 `bgm_usage_windows: list[BgmUsageWindowV4] = Field(default_factory=list)`；
- 扩 `_validate_time_bounds`：`bgm_usage_windows` 与 clips 同样卡 `[0, meta.duration]`。

**卡点网格**（节拍数量大、纯客观、不逐个编辑）入 `quality_report["bgm"]`：`beats: list[float]`、`drops: list[float]`。
**track 摘要**仍在 `quality_report["bgm"]`：`bpm/energy/tempo_bucket/loudness_lufs/genre/mood(整体)/scene_fit(整体)/librosa_available/status/source`。

改契约后重生成 `apps/web/src/api/openapi.json` + `schema.d.ts`（以 CI pinned venv 为准，不本地强修漂移 — 记忆 cutagent-openapi-drift-env-sensitive）。

## 4. 新增 provider 能力：`audio.understanding`（Qwen-Omni）

与视频 VLM 路径同构（确定性传感器 → gated 语义模型）。DashScope 按 provider_id 分插件类（`dashscope.asr/vlm/llm` 各一类），故新增一个 `dashscope.audio` 插件类。

**模型选型（据 DashScope 当前文档，2026-06 核实）**：
- ✅ **定用 `qwen3.5-omni-plus`（最新最强通用 omni）**（用户拍板）；可被 text prompt 指挥、输出我们结构化中文 JSON。`qwen3-omni-flash` 作降本备选，**由 provider profile `model_id` 配置、可热切**。
- ❌ 不用 `Qwen-Omni-Turbo`（文档标 discontinued）。
- ⚠️ 不用 `Qwen3-Omni-Captioner`(`qwen3-omni-30b-a3b-captioner`)：只自动出**英文 caption**、不能 prompt 指挥按字段出中文 JSON，不适合结构化标注（如要它，需二跳再过一次文本 LLM 转结构，本方案不取）。

实现要点：
- **插件**：`packages/ai/providers/dashscope.py` 新增 `DashScopeAudioPlugin`，`provider_id="dashscope.audio"`，处理 `capability_id=="audio.understanding"`。走 **OpenAI 兼容 chat.completions**：`messages` 含 `{"type":"input_audio","input_audio":{"data":"<presigned url>"}}` + 文本上下文（曲名/能量/位置/是否含drop + 要求输出的 JSON schema），`modalities=["text"]`（只取文字），**`stream=True`（通用 omni 强制流式，否则报错）→ 插件须累积流式 delta 再解析 JSON**。音频走公网 URL，复用 `asr.transcribe` 已验证的「按 URL 取音频」模式（`dashscope.py:31-40`）。
- **网关注册**：`provider_gateway.py:198` `self.plugins[plugin.provider_id]=plugin` 注册新插件。
- **能力/档案 seed**：`provider_seed.py` 加 `dashscope.audio.prod`（`capability="audio.understanding"`, `model_id="qwen3.5-omni-plus"`, `secret_ref="dashscope_prod.secret"`, `options_schema_ref=provider.audio.options`）+ `ProviderCapabilityRow` + pricing item；内存仓 `repository.py` 同步。
- **gating**：`bgm.py` 加 `resolve_audio_profile`（真档案 + 启用 + active secret，沿用 `_is_real_*` 模式），无则降级。

## 5. 检测流水线（`packages/media/annotation/bgm.py`）

### 5.1 librosa 传感器（确定性，拥有全部时间戳；librosa 可选依赖）
- `beats: list[float]`：复用 `beat_track` 的 beat frames → `frames_to_time`（现 `_beats` 被弃，改为保留）。
- `drops: list[float]`：RMS 能量曲线显著正跃迁 → 秒。
- **切 clip（1-3 条候选）**：能量曲线挑 drop 邻域窗 + 最高持续能量区，去重取前 3；整轨过短（≤~30s）整轨为一窗。每窗精确 `start/end`（snap 到最近 beat）+ `drop_anchor_sec`（snap beat）+ `mean_energy`。
- 全程 fail-open：librosa 缺失/解码失败 → 不含 beats/clips，绝不抛。

### 5.2 每段真听（gated Qwen-Omni）
- 对每条候选窗，用 ffmpeg trim 切出**该段音频**（短片段），`store_file` 落 OSS → **presigned URL**（远程模型须能拉取；本地 MinIO 不行 — 同 lipsync 约束）。
- 调 `audio.understanding`（`ProviderCall` 带 `idempotency_key` per asset/clip/audio-sha），Qwen-Omni 听完回 `mood/scene_fit/avoid_scene/role/reason`。**不向模型要秒数。**
- `role` clamp 到 `BgmSegmentRole`；列表裁剪；缺失不伪造。
- track 整体 `genre/mood`：可对整轨或首选片段额外听一次（实现时定，默认复用能量最高片段的听感聚合，避免多发整轨）。

### 5.3 组装
- `bgm_usage_windows=[BgmUsageWindowV4...]`（librosa 客观字段 + Qwen-Omni 语义，`source` 据此置位）；
- `quality_report["bgm"]` = track 摘要 + `beats` + `drops`；`meta.material_type="bgm"`，`meta.duration`=整轨。

## 6. 降级（沿用 fail-open / retry-never-fabricate）

| 情况 | 结果 |
|---|---|
| librosa 缺失 | 无 beats/clips → 退回整轨摘要（ffmpeg 响度）；记 `features_unavailable` 降级 |
| librosa 在、无 audio 档案 | **clip + beats/drops 照出**（卡点纯客观可用）；无 mood/scene，`role` 按启发式（含 drop→climax 否则 general），`source="sensor"` |
| 某片段音频切片/presign 失败 | 该片段退 sensor-only（同 VLM「无可读视频则降级」） |
| 全配 | 完整 `source="sensor+audio"` |

卡点价值在「无 audio 档案」时不丢，是本方案关键收益。

## 7. 前端

文件：`AnnotationEditorModal.tsx`、`utils/annotationV4.ts`（`BgmAssetCard.tsx` 入口「查看标注」已有，不改）。

editor 现有 `isPortrait`/b-roll 两分支，**新增 `isBgm` 分支**：
- **track 摘要条**：bpm/tempo/曲风/响度/整体情绪 chips。
- **节拍时间轴条**：beat 刻度 + drop 高亮；1-3 推荐片段按 `role` 上色为区间；复用现有音频试听播放（音频用 `<audio>` + 自绘时间轴条，不强塞 VideoPlayer）。
- **片段卡片**：`起止秒·时长·role·能量·drop锚点·scene_fit 标签·reason`（对齐 B-roll SegmentCard）。
- **编辑表单**：增删片段、start/end(步进0.1s)、role(select)、drop_anchor_sec、mood、scene（对齐 B-roll 表单）。
- `annotationV4.ts` 加 `canonicalToBgmWindows`/`bgmWindowsToCanonical`（防御式读 untyped canonical）。

## 8. 编辑 / Patch

文件：`apps/api/services/asset_annotation.py`、`annotation_patch.py`。
- BGM projection `editable_paths` 扩到含 `/canonical/bgm_usage_windows`、`/projection/bgm`。
- 保存按 `BgmUsageWindowV4` 重新校验（与 clips 同模式，非法→422）。
- `usable` 判定：沿用「有真实语义」口径 + 至少 1 条 window。

## 9. 测试

- `tests/media/annotation/`：注入返回 beats/drops/candidate_windows 的 `feature_extractor` + mock gateway 的 `audio.understanding` 返回按段语义 → 断言 `bgm_usage_windows` 类型化、时间有界、drop 锚点合法、role clamp、`source` 正确；断言降级三/四路径（librosa 缺失 / 无 audio 档案 / 单片段 presign 失败 / 全配）。
- `tests/ai`（或 provider 测试）：`DashScopeAudioPlugin` 用 mock HTTP，断言请求形状（音频 URL + 上下文）与 JSON 解析。
- 前端：`npm run build`（tsc + `contracts/*.typecheck.ts`）。
- 契约漂移与整体门禁：`scripts/ci_gate.sh`。
- **worker 是独立进程**：真实标注产出验证需重启 worker（本次主要走单测）。

## 10. 影响面清单

- `packages/core/contracts/media.py`、`contracts/__init__.py`（新类型 + 导出）
- `packages/ai/providers/dashscope.py`（`DashScopeAudioPlugin`）
- `packages/ai/gateway/provider_gateway.py`（注册新插件）
- `packages/core/storage/provider_seed.py`、`seed.py`、`repository.py`（capability/profile/pricing/options-schema seed）
- `packages/media/annotation/bgm.py`（librosa 切 clip + 每段 ffmpeg 切片 + presign + gated Qwen-Omni + 组装 + 降级）
- `apps/api/services/asset_annotation.py`、`annotation_patch.py`（editable_paths + patch 校验；BGM 走 audio gating）
- `apps/web/src/components/annotation/AnnotationEditorModal.tsx`、`utils/annotationV4.ts`（isBgm 分支 + 适配器）
- `apps/web/src/api/openapi.json`、`schema.d.ts`（重生成）
- 测试：`tests/media/annotation/`、provider 测试

## 11. 风险 / 待核定

- **Qwen-Omni 强制流式**：通用 omni 模型 `stream=True` 必填，插件须累积流式 delta 再解析 JSON（与现有 dashscope llm/vlm 的非流式路径不同，需单独实现+测）。
- **model alias**：已定 `qwen3.5-omni-plus`（备选 `qwen3-omni-flash`，profile 可切）；走 OpenAI 兼容、`input_audio` 支持公网 URL、单次音频上限 20min–3h（短片段绰绰有余）。实现首调时验一次该 alias 在账号下可用。
- **JSON 稳定性**：omni 流式自然语言里抠结构化 JSON 需稳健解析（复用 `_extract_json_object` 容错 + 必要字段校验，缺失即降级不伪造）。
- **presigned OSS**：每条候选片段切片须落可公网拉取的 durable OSS（本地 MinIO 不行，同 lipsync）。
- **成本/延迟**：每首 BGM 1-3 次 audio.understanding 调用（短片段），可接受；带 idempotency 复用。
- **OpenAPI 漂移对环境敏感**：以 CI pinned venv 为准，子代理「本地漂移 BLOCKER」多为假阳性。
- **librosa 切窗启发式质量**：确定性近似，先满足「挑出好用的几段」，后续迭代阈值。
