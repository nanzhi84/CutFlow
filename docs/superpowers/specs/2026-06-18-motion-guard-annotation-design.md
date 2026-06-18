# 设计：motion_guard 动态模糊/抖动标注 + 选片避让

- 日期：2026-06-18
- 分支/工作树：`worktree-motion-blur-annotation`（基于 `origin/main` = `2e03741`）
- 状态：待用户评审

## 1. 背景与目标

用户需求：识别「动态模糊」——主要指**相机抖动 / 收机下坠（摄像头收回时的抖动）**这类运动导致的坏画面，把坏段**标注**出来，并在**成片时不用这些坏段**。

经代码勘查确认的现状关键事实：

- genesis 标注是「确定性传感器 + VLM 语义」两层（`packages/media/annotation/`）。质量检测只有 `cv_quality.py`：黑屏/冻结（ffmpeg，hard/`occlusion`）+ 失焦糊（Laplacian 方差，soft/`blur`）。**完全没有相机运动/抖动检测。**
- 契约与下游**早已为运动检测预留好接口但从未实现**：`QualityEventType` 已定义 `shake`、`camera_drop`（`packages/core/contracts/media.py:467-475`）；`QualityEventV4.source` 注释写明来源可为 `'motion_guard'`（`media.py:572-574`）；`report.py` 的 `_STABILITY_EVENT_TYPES={shake,blur,camera_drop}`、`_SOFT_EVENT_TYPES={shake,blur}`（`report.py:24-26`）已会消费；三个 VLM prompt 都写「镜头晃动/收机下坠优先由 motion_guard 工具检测，VL 不要判」。但 `sensors/` 目录里**没有 motion 传感器**——`motion_guard` 仅存在于 docstring，是「接好线却空着的槽」。
- oldrepo（`oldrepo/main`，DFG0311/digital-human-Cutagent）有一套已验证的 `backend/app/services/broll_analyzer/motion_guard.py`（608 行光流抖动/收机检测），是本设计的算法参考。
- **质量事件目前从不裁剪 clip、下游选片也完全不读它**：clip 边界只由 VLM + 切镜点决定（`_assemble.refine_clip_boundaries`）；`packages/planning`、`packages/production` 对 `quality_events` 零引用（仅读 `quality_report['usable_ratio']` 一个粗标量）。

### 1.1 责任划分（本设计的核心定调）

| 层 | 行为 | 说明 |
|---|---|---|
| **A. 标注层** | motion_guard 传感器**标出**坏段，产出 `shake`/`camera_drop` 质量事件 | **源 clip 几何完全不变**；素材库/编辑器可见坏段标记 |
| **B. 选片层** | 选源窗时**避开**标注坏段、**用干净剩余** | 选片器从「clip 区间减去坏段」后的干净子区间里挑源窗；**不做任何物理裁剪/切割步骤**，渲染照旧只按选片器给出的干净 `[source_start, source_end]` 出片 |

明确不做：

- 不在标注层改 `ClipV4` 几何（不 trim/split/drop clip）。
- 不新增物理切割/ffmpeg 裁剪环节、不改渲染节点。
- 不改契约（复用现有 `shake`/`camera_drop` 枚举与 `QualityEventV4` 形状）→ 无 OpenAPI/`schema.d.ts` 重生成、无 DB 迁移。

## 2. Goals / Non-Goals

**Goals**
- G1：新增确定性 `motion_guard` 传感器，全时间轴、全素材类型检测相机抖动（`shake`）与收机下坠（`camera_drop`），产出 `QualityEventV4` 事件，`source='motion_guard'`。
- G2：坏段在素材库/标注编辑器中可见（事件类型中文化为可选润色）。
- G3：成片选片层（b-roll + 人像主轨）避开标注坏段、用同一 clip 的干净剩余。
- G4：误判有意运镜（推拉摇移跟拍）的概率受控；移植平滑运镜抑制。

**Non-Goals**
- 不检测纯图像「失焦糊」以外的图像层运动拖影（FFT/方向性模糊）——本期只做光流相机运动；失焦糊已由 `cv_quality` 覆盖。
- 不改人类编辑器对事件的增删改能力（已存在）。
- soft 事件（边界抖动、失焦 `blur`）只标注、不参与选片避让（仅 hard 事件避让）。

## 3. Part A — motion_guard 传感器（标注）

### 3.1 新模块 `packages/media/annotation/sensors/motion.py`

公开接口（对齐现有传感器形状 `detect_*(video_path, *, tunables) -> list[dict]`）：

```python
def detect_motion_events(
    video_path: str,
    *,
    sample_fps: float = 10.0,
    width: int = 360,
    window_sec: float = 1.5,
    hop_sec: float = 0.75,
    # ...阈值，默认取自 settings（见 §3.4）
) -> list[dict]:
    """检测相机抖动/收机下坠，返回 QualityEventV4 对齐的 dict（无 event_id）。
    fail-open：缺 cv2/ffmpeg 或视频打不开 → 返回 []，绝不抛、绝不伪造负证据。"""
```

### 3.2 算法（移植 oldrepo 核心 + 全时轴适配）

1. **抽帧**：ffmpeg `fps={sample_fps},scale={width}:-2,format=gray`（复用 `packages/media/video/ffmpeg.py` 的 `ffmpeg_bin()`）；每帧 `GaussianBlur(3,3)`。所有位移单位为「px@360 宽」(px360)，分辨率无关。
2. **每对帧全局运动估计**：`cv2.goodFeaturesToTrack`（maxCorners=700, qualityLevel=0.01, minDistance=6）限定在**边缘掩膜**（左右各 32% 列 + 顶部 18% 行）以忽略前景主体、只测相机/背景运动；`cv2.calcOpticalFlowPyrLK`（winSize=(21,21), maxLevel=3）；`cv2.estimateAffinePartial2D(method=RANSAC, ransacReprojThreshold=2.0)` → `(dx, dy, rot_deg, inlier_ratio)`；仿射失败回退中位光流。
3. **全时轴滑动窗**（替代 oldrepo 仅头尾 2s）：`window_sec` 窗、`hop_sec` 步进覆盖 `[0, duration]`。每窗聚合：`mag_p50/p90/p95/max`、`active_ratio`(mag>active_px)、`hard_ratio`(mag>hard_px)、`max_active_run`、`cum_x/y_range`、`net_x/y`、`straightness_ratio`、`direction_flip_ratio`、`jerk_p90`、`residual_to_p95_ratio`、`rot_p95_deg`。
4. **分类**（移植 `_build_motion_guard_event_from_metrics`）：
   - 门槛：窗时长≥0.8 且 pairs≥8；`sustained`（active_ratio≥0.75 且 max_active_run≥ceil(pairs*0.55)）。
   - **`camera_drop`**：持续竖直下沉（`y_range≥tail_y_range_hard_px(70)` 且 `net_y≥tail_net_y_hard_px(65)` 且 `y_range≥max(25, x_range*1.25)`，或 high_step 变体）；放宽 oldrepo「仅尾部」限制为全轴检测（尾部仍可加权）；子窗细化定位坏区间。
   - **`shake`**：剧烈边缘抖动（`p95≥9.0` 且 `hard_ratio≥0.7` 且 `active_ratio≥0.85` 且 `jitter_like` 且 NOT `smooth_camera_move`）。
   - **平滑运镜抑制**（关键防误判）：`smooth_camera_move`（straightness≥0.88 且 flip_ratio≤0.16）或 `smooth_sweep`（主轴≥80 且 主轴≥次轴*2.3 且 straightness≥0.65）→ 阻断 `shake` 判定。
5. **分级**：清晰/严重 → `risk_tier='hard'`；边界/不确定 → `risk_tier='soft'`。**只有 hard 事件参与选片避让（§4）**；soft 只标注（可见警告）。
6. **输出 dict**：`{event_type, start, end, risk_tier, confidence, severity, source:'motion_guard', description}`。`description` 写中文可读说明 + 关键 metrics（如「镜头剧烈抖动，p95位移9.3px、方向翻转率0.31」），便于编辑器展示与人工复核。相邻同类型事件合并；`end>start` 必须成立（否则被 `assemble_quality_events` 静默丢弃）。

### 3.3 纯函数 / IO 拆分（项目纪律）

- 纯核（可对合成 `(dx,dy)` 序列单测，仿 `merge_blur_segments` / `merge_speech_probabilities`）：`summarize_window(pairs) -> metrics`、`classify_window(metrics, position) -> event|None`、`refine_drop_window(...)`。
- IO 壳：ffmpeg 抽帧 + cv2 光流估计。`cv2`/`ffmpeg` 经 lazy import，缺失即返回 `[]`（fail-open）。

### 3.4 配置（`packages/core/config/settings.py`）

新增 `motion_guard` 配置组（`CUTAGENT_*` env），阈值默认沿用 oldrepo `settings.broll_analysis`（px360 归一化），仅 `sample_fps` 由 12 下调为 10 以平衡全时轴算力：`sample_fps=10`(oldrepo 12), `width=360`, `active_px=1.5`, `hard_px=3.0`, `p95_hard_px=7.0`, `tail_y_range_hard_px=70`, `tail_net_y_hard_px=65`, `smooth_move_straightness=0.88`, `smooth_move_flip_ratio=0.16`, `sweep_axis_ratio=2.3`, `jitter_flip_ratio=0.22`, `jitter_jerk_ratio=0.65`, `refine_min_duration=0.8`, `refine_round_sec=0.1`。不硬编码（阈值需经 §6 真实素材验收调参）。

### 3.5 接线（无契约改动）

- `sensors/__init__.py`：import + `__all__` 加 `detect_motion_events`（及纯核函数，便于测试）。
- `runner.py` `SensorDeps.real()._detect_quality_events`（`runner.py:356-357`）：
  ```python
  def _detect_quality_events(video_path: str) -> list[dict]:
      return (list(sensors.detect_cv_quality_events(video_path) or [])
              + list(sensors.detect_motion_events(video_path) or []))
  ```
  pipeline / `assemble_quality_events` 已自动聚合，**不用改**。dict 显式带 `source='motion_guard'`（`assemble` 仅在缺省时补 `'sensor'`）。
- 适用素材类型：与 `cv_quality` 一致，对所有素材跑（全素材类型）。

### 3.6 可见性

- 编辑器 `apps/web/src/components/annotation/AnnotationEditorModal.tsx` 已把 `quality_events` 叠加到时间轴（`playerEvents` → 362 行）+ 事件列表（`QualityEventRow`）+ 风险徽章（硬/软）+ `description` 文本。**开箱可见。**
- **本期实现**（D3 已定，纯前端、无契约风险）：在 `QualityEventRow` 加 `EVENT_TYPE_LABELS` 中文表（`camera_drop`→「收机下坠」、`shake`→「镜头抖动」、`blur`→「失焦模糊」、`occlusion`→「遮挡/黑屏」、`blooper_laugh`/`look_off_camera`/`exit_frame`/`retake_pause`/`manual_note` 等一并补齐），列表显示中文而非英文原文。

### 3.7 自动获得（已接好的下游，均为软信号、不改 clip）

`report.py` 的 `stability_score`/`soft_quality_count`/（人像）`hard_risk` 降权；`pick_window_sample_times` 把事件中点设为抽帧热点；VLM prompt 已写让位。无需改动。

## 4. Part B — 选片避让（成片用干净剩余）

**定调**：这是**选片器避开标注坏段**，不是物理裁剪。选片点本就是「决定用哪段源」的地方，让它从「clip 减去坏段」的干净子区间里挑源窗即可；渲染节点不变，照旧按选片器给出的 `[source_start, source_end]` 出片。

经勘查确认：**没有统一的「成片前最终源窗」choke point**，人像与 b-roll 是两条独立选源路径，故分别在各自选片点注入。

### 4.1 共享纯函数（新增，可单测；参考 `ffmpeg.py:442 trim_to_valid_segments` 的区间求补形状）

放在 planning 共享工具（如 `packages/planning/material/_motion_avoid.py` 或 `packages/planning/editing/_util.py`）：

```python
def avoid_intervals(annotation, *, types=("shake", "camera_drop", "occlusion"),
                    hard_only=True, severity_min=None) -> list[tuple[float, float]]:
    """从 AnnotationV4.quality_events 提取要避开的区间（默认仅 hard 的 shake/camera_drop/occlusion）。"""

def subtract_bad_spans(start: float, end: float,
                       bad: list[tuple[float, float]], *, min_len: float) -> list[tuple[float, float]]:
    """[start,end] 减去 bad 区间并集 → 干净子区间；短于 min_len 的丢弃。纯函数。"""
```

避让判据（默认，D1 已定）：`event_type ∈ {shake, camera_drop, occlusion}` 且 `risk_tier=='hard'`。soft 事件（含失焦 `blur` 与边界抖动）只标注、不避让。

### 4.2 b-roll 选片点（最窄爆炸半径；事件已到手）

- 源窗起点：`packages/planning/material/broll_pack.py` 的 `_scene_from_clip`（124-125 行）把 `BrollScene.start/end = clip.start/clip.end`，流入 `BrollCandidate.source_start/source_end`（221-222 行匹配流 / 307-308 行通用覆盖流）。
- `rank_broll_candidates` 入参的 `annotation` 已携带 `quality_events`（`broll_planning.py:74-78`、`broll_coverage_planning.py` 已 `ctx.repository.annotation_v4_for_asset`）——**零新增管道**。
- 改法：对每个 clip 用 `subtract_bad_spans(clip.start, clip.end, avoid_intervals(annotation), min_len=_MIN_INSERT_SECONDS)` 得到干净子区间，**每段干净子区间产一个 `BrollCandidate`**（或在候选上携带干净区间，供 `broll_plan` 选窗时约束）。短于 `_MIN_INSERT_SECONDS=1.5s`（`broll_plan.py:18`）/ 覆盖流的 `min_segment_duration` 的丢弃。
- 爆炸半径：`BrollCandidate` 数据类 + 两个 caller；无渲染/契约改动。

### 4.3 人像主轨选片点

- 源窗构造：`packages/production/pipeline/nodes/portrait_planning.py` 的 `_portrait_window_candidates`（304-309 行）构造每 clip 的 `[win_start, win_end]`，boundary planner 再选最终 `source_start_frame/source_end_frame`。**必须在此注入**——因为 `PortraitTrackBuild`（node 11）在 `LipSync`（node 12）前就物理裁定了人像轨，更晚来不及。
- 事件来源：`MaterialPackPlanning`（`material_pack_planning.py:76-80,91-99` 已持 `portrait_annotations`）预先算出每 clip 的运动坏段区间，**通过 `MaterialCandidate.metadata` 透传**给 `PortraitPlanning`（它本就只读 metadata）。
- 改法：对每 clip 用 `subtract_bad_spans` 切出干净子区间，**一个子区间一个候选窗**；复用现有 `≤0.08s` 丢弃下限；注意 boundary planner 的 chunk 最小约束（`chunks.py:60` 约 6s 非开场 / 4s 开场），干净剩余太短的窗会被 beam 拒（等同现有「无可用源」软降级路径）。

### 4.4 诊断与降级

- 加 `broll_motion_excluded` / `portrait_motion_excluded` 计数（仿 `material_pack_planning.py` 的 `broll_person_excluded`，~189-205 行诊断 dict）。
- 避让导致覆盖不足时，报分级 degradation（仿 `broll_skipped_no_material`），**不静默**。

## 5. 契约 / 迁移

**零改动**：复用 `QualityEventType.shake`/`camera_drop` 与现有 `QualityEventV4` 形状；丰富 metrics 写入 `description` 文本。→ 无 OpenAPI 导出、无 `schema.d.ts` 重生成、无 Alembic 迁移。（若以后要在编辑器结构化展示 metrics，再考虑加字段，那才需重生成。）

## 6. 测试

- **纯函数单测**（不触真 ffmpeg/cv2）：`summarize_window`/`classify_window`/`refine_drop_window` 对合成 `(dx,dy)` 序列；`subtract_bad_spans`/`motion_bad_intervals` 对合成区间。覆盖：抖动判正、平滑运镜判负、收机下沉子窗定位、区间求补与最小时长丢弃。
- **集成测**：经 mock `SensorDeps`（`runner.py:338-339` 既有模式）注入合成运动事件，验证事件流过 pipeline 并进入 `quality_events`；b-roll/人像选片点验证「含坏段的 clip → 选出的源窗避开坏段、保留干净剩余」。
- **真实素材验收**（阈值调参，头号风险防线）：真抖动片 vs 真平滑运镜片各一，确认「该标的标、好运镜不误标；成片源窗避开坏段」。
- 改完 `packages/media` / `packages/production` 后**重启 worker**（独立长驻进程）。

## 7. 风险

- **R1 误判有意运镜**（尤其 b-roll 推拉/跟拍本是好镜头）。缓解：忠实移植平滑运镜抑制 + 仅 hard 触发避让 + 保守默认阈值 + 真实素材调参。**因只避让不裁剪、源素材带可见标注，人可在编辑器复核/改事件**——双保险。
- **R2 算力**：全时轴光流比 2fps 失焦抽帧重。缓解：360px 降采样 + 适中 fps(10) + 窗/步进控制；短视频素材（≤60s）可接受。如遇长素材性能问题，可加「先粗扫有无显著全局运动」短路。
- **R3 干净剩余过短**：收机抖动占满短 clip 时，干净剩余可能低于最小时长 → 该 clip 在选片层不被用（等同丢弃）。诊断计数 + 降级通知使其可观测，不静默。

## 8. 交付（3 个增量 PR）

- **PR1（标注）**：`motion.py` 传感器 + settings + `sensors/__init__.py`/`runner.py` 接线 + 纯函数与集成测 + 编辑器事件类型中文标签（D3）。独立可验、低风险；`report.py` 自动消费打分。
- **PR2（b-roll 选片避让）**：共享 `subtract_bad_spans`/`motion_bad_intervals` + `broll_pack` 注入 + 诊断/降级 + 测试。events 已到手，最简。
- **PR3（人像选片避让）**：`MaterialPackPlanning` metadata 透传 + `portrait_planning` 注入 + 测试。稍复杂（透传）。

依赖：PR2/PR3 依赖 PR1（需事件流动）。可先 PR1+PR2 落地见效，PR3 跟进。

## 9. 决策（已定 2026-06-18）

- D1：✅ **都纳入**——选片避让纳入 hard `occlusion`（黑屏/冻结），与 `shake`/`camera_drop` 共用同一机制（见 §4.1）。
- D2：✅ **沿用 oldrepo** 阈值默认，留待 §6 真实素材验收调参。
- D3：✅ **本期做**——编辑器事件类型中文标签（§3.6，纯前端、无契约风险）。

## 10. 执行模式

Codex 实现、Claude Code 负责架构与验收（用户指定）。每个 PR 由 CC 下发精确 brief → Codex 在本工作树写码 → CC 跑测试/审查/去 churn/提交。参考 [[codex-companion-write-mode]]、[[cutagent-worktree-verify-recipe]]。

落地：PR1 `3f4afec`(传感器) · PR2 `fb24551`(b-roll 避让) · PR3 `b9f4a80`(人像避让) · PR4 `7e5474c`(camera_drop 真实素材校准)。

## 11. 真实素材调阈结果（2026-06-19，D2 收口）

数据集：`.data/objectstore-cache/video/` 的 21 片真实便利店手持 b-roll（1080p），含 3 对 原片/stabilized。探针 dump 逐窗 metrics + 事件（脚本见 job tmp `motion_probe.py`）。

结论：
- **阈值默认（px360，沿用 oldrepo）经真实素材验证合理，无需调整。** 问题不在阈值，而在 camera_drop 的两个逻辑缺陷（PR4 已修）。
- **shake + 平滑运镜抑制**：工作良好——A 簇高幅度(p95 30-183)但平滑(str≈1.0)的有意平移全被正确压制（防误杀有意运镜），shake 仅在真抖窗(str≈0.6、flip 高)触发；gentle 手持(p95~3-4)不报。orig 运动指标始终略高于 stab（方向性正确）。
- **camera_drop（收机下坠，用户头号用例）经真实素材暴露并修复**：
  1. 方向 bug——移植丢了 oldrepo 对 net_y 的 `abs()`+方向归一，只认正方向；真实收机 net_y 为负 → 21 片 0 触发（合成/mock 测不出，真实素材一跑即现）。
  2. 平滑过报——方向无关化后平滑有意下摇被误报；补「平滑运镜抑制」门控（careless 收机抖→触发，有意平滑运镜→压制，契合「抖动」语义）。
- 三阶段 camera_drop 事件数：0(原始/bug) → 13(仅符号修复,含平滑误报) → **6(符号+门控；5/6 落在片尾=收机典型位置)**；clip_02 抽帧确认为甩向地面的真收机。
- 生效前提仍同 §6：改动 `packages/media`/`packages/production` 后**重启 worker**。
