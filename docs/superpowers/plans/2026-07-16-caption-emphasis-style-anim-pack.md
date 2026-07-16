# 花字样式模板库 + 动画注册表 + 音效链路激活（预制资产包）

> 日期：2026-07-16 · 状态：方案 · 关联：#209（字幕 Clean-Slate）、#212（实现 PR）
>
> 本文是可直接交给 AI 执行的完备规格：包含预制动画目录、花字样式模板目录、
> 字体/音效资产下载清单（来源 + 许可证均已人工验证可用）。

## 1. 背景与目标

对标竞品口播视频（全屋定制类，49s）逐帧拆解，其字幕层由四类元素 + 一层音效构成：

| 元素 | 表现 |
| --- | --- |
| 基础字幕 | 白色衬线体+黑描边；入场三种：直接切换、**逐字拖尾划入**（~0.5s）、下落入位 |
| 行内关键词花字 | 句中关键词**换字体**（圆胖 POP 体、蓝填充+白描边）、字号更大、**爆炸星形底衬**、缩放过冲弹入（~250ms）、驻留期间轻微摆动 |
| 品牌花字 | 第三种字体（浅蓝填充+深蓝描边）、独占字幕位弹入（对应我们的 `display_mode=whole_cue`） |
| CTA/装饰贴纸 | 红色霓虹箭头呼吸脉动、漂浮绿叶（本方案不做，见 §9） |
| 音效层 | 49s 内 60+ 响度爆点：花字弹入配 pop、划入配 whoosh、转场配 whoosh |

**结论：竞品的"灵动"不是更智能，而是模板库更大** ——
字体 × 配色 × 底衬 × 入场动画 × 音效 五个维度各有几个选项互相组合。
这与 #209 的确定性红线完全兼容：我们要做的是把每个维度从"单值"扩成"注册表"。

### 现状根因（已逐条与代码核对）

1. **音效链路整体休眠（真 bug）**：`scripts/import_sfx_assets.py` 给资产打的 tag 是
   `{sfx, caption_liveliness_v3, license:CC0-1.0, sfx_class:*}`，而选择器
   `_CAPTION_EMPHASIS_SFX_TAGS = {caption_emphasis, caption-emphasis, light_pop, light-pop}`
   （`packages/production/pipeline/nodes/subtitle_and_bgm_mix.py:38`）**两个集合不相交**，
   成片实际零音效。混音层（N 路 amix + adelay + alimiter）本身是通的。
2. **动画只有 2 种且五处写死**：`soft_in=\fad(120,0)`、`pop=\fscx 85→105→100`；
   `CaptionRun.effect_id` 封闭 `Literal["none","soft_in","pop"]` + role→effect
   白名单 validator（`packages/core/contracts/artifacts.py`，CaptionRun.validate_run）；
   指派是一行三元式（`packages/production/pipeline/_caption_composition.py:679`）；
   ASS 翻译是 if/elif（`packages/production/pipeline/_subtitles.py:102-112`）。
   无出场动画、无逐字、无位移。libass 本身支持 `\move`/`\t` 链/逐字/`\frz`/`\blur`/`\p` 绘图，
   **渲染层是最不卡的一层**。
3. **花字只有一种视觉形态**：全片仅 normal/emphasis 两个字体槽、两条 ASS Style；
   配色 5 套 preset 写死（`_materialize.py` `_SUBTITLE_COLOR_DEFAULTS`），用户只能覆盖一个
   `emphasis_primary_color`；底衬/双描边/渐变在契约里没有字段。
4. **装饰字体进不了门**：`font_text_safety_issue`（`packages/production/pipeline/_font_metrics.py`）
   的缺字检查 + 墨迹越界检查对手写体/POP 体几乎是死刑；TTC 直接拒绝。
5. **逐字入场缺数据**：`CaptionRun` 只存 `token_ids` 不存 token 时间与逐字宽度。

### 设计原则（不可违背）

- 遵守 #209 红线：**LLM 只输出语义**（短语/优先级/强度），字体/动画/音效/坐标由
  本地注册表**确定性映射**；同输入必得同输出（组内轮换用 hint 序号取模，不用随机）。
- 一切表现力经 `CaptionCompositionPlanArtifact` 单一事实源下传；禁止引入独立
  overlay 通道、禁止节点读像素。
- fail-closed 检查不得绕过，只能改成**显式降级**（记 degradation，不静默）。

---

## 2. Phase 0 — 激活音效链路（半天，先行合并）

1. **tag 对齐**（修 bug）：`scripts/import_sfx_assets.py` 的 `_upsert` tags 集合追加
   `"caption_emphasis"`；dev/prod 重跑脚本（upsert 幂等，会更新既有资产 tags）。
   选择器端不改，保持"显式打 tag 才发声"的语义。
2. **扩池**：`SFX_PACK` 从 4 条扩到 ~10 条，全部来自**既有两个 kenney CC0 zip**（无新 URL）。
   候选成员（执行前用 `unzip -l` 核实存在、导入时 <1.5s 硬闸自动把关，超长换同系列邻号）：

| asset_id | sfx_class | archive | member（候选） | 用途 |
| --- | --- | --- | --- | --- |
| asset_sfx_click | click | interface | Audio/click_001.ogg | （已有） |
| asset_sfx_ding | ding | interface | Audio/confirmation_001.ogg | （已有） |
| asset_sfx_whoosh | whoosh | interface | Audio/scroll_001.ogg | （已有） |
| asset_sfx_impact | impact | impact | Audio/impactPunch_heavy_002.ogg | （已有） |
| asset_sfx_pop_soft | pop | interface | Audio/select_001.ogg | 轻弹出 |
| asset_sfx_pop_bright | pop | interface | Audio/click_002.ogg | 亮弹出 |
| asset_sfx_ding_soft | ding | interface | Audio/confirmation_002.ogg | 柔提示 |
| asset_sfx_whoosh_fast | whoosh | interface | Audio/scroll_003.ogg | 快划过 |
| asset_sfx_rise | rise | interface | Audio/maximize_003.ogg | 上升感 |
| asset_sfx_sparkle | sparkle | impact | Audio/impactGlass_light_002.ogg | 星闪/玻璃 |

3. 验收：dev 起一条含 emphasis 的 run，ffprobe 成片音轨确认 SFX 混入；
   `sfx_asset_missing` 告警不再出现。

## 3. Phase 1 — 字体包 + 字体安全门放宽（花字字体的前置）

### 3.1 脚本改造（先做，否则新字体进不来）

- `FontSpec` 增加 `license: str` 字段：现在 `_tags()` **写死 `license:OFL-1.1`**
  （`scripts/import_font_assets.py:169`），加非 OFL 免费商用字体会打错标。
- `FontSpec` 增加可选 `archive_url/archive_member`（照抄 `SfxSpec` 的 zipfile 先例）：
  得意黑等只发 release zip 的字体需要。
- 沿用现有约定：URL **锚定 commit SHA**（不是 main）、下载后 `sha256sum` 回填 spec。

### 3.2 字体安全门放宽（`_font_metrics.py` + `caption_composition_planning.py`）

- **墨迹越界**（horizontal_ink_overhang）：从 fail-closed 改为"记录每字体最大 overhang 值，
  排版时把该值计入行宽余量"；仍然超带才 fail。
- **缺字**（missing_glyph）：从整体 fail 改为 **per-hint 显式降级**——该 hint 的花字字体
  回退到默认 emphasis 字体，记 `font_glyph_fallback` degradation（新增 Warning/Degradation
  枚举成员 → **触发 openapi 重生成**）。这是手写体（覆盖 ~2500-7000 字）可用的唯一途径。
- TTC 拒绝保持不变。

### 3.3 字体清单（来源与许可证已验证，2026-07-16）

全部免费可商用。`style:` 与 `usage:` 进 tags（`usage:huazi` 本次正式接线为花字候选池）。

| asset_id | 字体 | 风格 | weight | 来源（锚定 commit/release 后填 sha256） | 许可证 |
| --- | --- | --- | --- | --- | --- |
| asset_font_zcool_kuaile | 站酷快乐体 | POP 圆手写 | 400 | `google/fonts` 仓库 `ofl/zcoolkuaile/ZCOOLKuaiLe-Regular.ttf`（raw 直链已验证 200） | OFL-1.1 |
| asset_font_zcool_qingke_huangyou | 站酷庆科黄油体 | 圆胖 POP（最接近参考「套模板」） | 400 | `google/fonts` 仓库 `ofl/zcoolqingkehuangyou/ZCOOLQingKeHuangYou-Regular.ttf`（200） | OFL-1.1 |
| asset_font_mashanzheng | 马善政毛笔楷 | 手写毛笔 | 400 | `google/fonts` 仓库 `ofl/mashanzheng/MaShanZheng-Regular.ttf`（200） | OFL-1.1 |
| asset_font_zhimangxing | 志莽行书 | 手写行书 | 400 | `google/fonts` 仓库 `ofl/zhimangxing/ZhiMangXing-Regular.ttf`（200） | OFL-1.1 |
| asset_font_longcang | 龙藏体 | 手写钢笔 | 400 | `google/fonts` 仓库 `ofl/longcang/LongCang-Regular.ttf`（200） | OFL-1.1 |
| asset_font_smiley_sans | 得意黑 | 窄斜现代黑 | 400 | `atelier-anchor/smiley-sans` release **v2.0.1** zip `smiley-sans-v2.0.1.zip`（需 archive 支持；取 TTF 版 SmileySans-Oblique.ttf） | OFL-1.1 |
| asset_font_lxgw_marker | 霞鹜漫黑 | 马克笔圆黑 | 400 | `lxgw/LxgwMarkerGothic` 仓库 `fonts/ttf/LXGWMarkerGothic-Regular.ttf`（仓库内直链已确认存在） | OFL-1.1 |

执行注意：
- 手写系（马善政/志莽/龙藏）字符覆盖有限，**必须在 §3.2 缺字降级落地后再启用**
  （顺序：脚本改造 → 安全门放宽 → 导入字体）。
- 得意黑是斜体设计，是墨迹 overhang 放宽的天然测试用例。
- 每款导入后跑一次真实脚本文本的 `font_text_safety_issue` 冒烟，记录 overhang 值。

## 4. Phase 2 — 动画注册表

### 4.1 结构

新建 `packages/production/pipeline/_caption_effects.py`：

```python
@dataclass(frozen=True)
class CaptionEffectSpec:
    effect_id: str
    roles: frozenset[str]            # 允许的 run role
    enter_ms: int                    # 入场时长（排版/SFX 对齐用）
    headroom_px_ratio: float         # 排版需预留的额外余量（相对字号）
    needs_char_timing: bool          # 是否需要逐字帧/逐字宽度
    sfx_class: str | None            # 入场音效类别（Phase 0 的 sfx_class tag）
    render(x, y, font_size, run) -> list[str]   # 产出 ASS override tags
```

- `_subtitles.py` 的 if/elif 替换为查注册表；`_sfx_events.py` 的
  `effect_id == "pop"` 过滤替换为 `spec.sfx_class is not None`，音效资产按
  `sfx_class:<class>` tag 查找（每类取 id 字典序第一，保确定性）。
- role→effect 白名单收敛为**一处**：contracts 层新增
  `caption_effects.json`（照 `caption_policy.json` 跨端共享 JSON 的先例），
  契约 validator 与 production 注册表都读它，消灭双写。

### 4.2 预制动画目录（v1 共 9 条）

坐标约定：`(x, y)` 为该 run 的 `\pos` 目标位；`\move` 与 `\pos` 互斥，模板整体产出 tag 串。

| effect_id | 角色 | 类型 | ASS 模板（关键 tag） | 时长 | SFX | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| soft_in（已有） | normal | 入 | `\fad(120,0)` | 120ms | — | 保持不变 |
| fade_through | normal | 入+出 | `\fad(120,100)` | — | — | 首个出场动画 |
| wipe_reveal | normal | 入（逐字） | 逐字 Dialogue：`\fad(60,0)\move(cx-14,cy,cx,cy,0,90)`，字间 stagger=45ms | 90ms/字 | whoosh（行首一次） | 复刻竞品逐字拖尾；需 §4.3 数据 |
| slide_up_in | normal/emphasis | 入 | `\move(x,y+22,x,y,0,160)\fad(90,0)` | 160ms | whoosh | 22px 需计入 headroom |
| pop（已有） | emphasis | 入 | `\fscx85\fscy85\t(0,120,\fscx105\fscy105)\t(120,240,\fscx100\fscy100)` | 240ms | pop | 保持不变 |
| pop_rotate | emphasis | 入 | `\frz-6\fscx80\fscy80\t(0,140,\frz0\fscx108\fscy108)\t(140,260,\fscx100\fscy100)` | 260ms | impact | 复刻参考「套模板」过冲+回正 |
| jelly_pop | emphasis | 入+驻留 | pop 后接 `\t(240,420,\fscx97\fscy103)\t(420,600,\fscx101\fscy99)\t(600,780,\fscx100\fscy100)` | 780ms | pop | 果冻余摆（竞品驻留摆动） |
| drop_in | emphasis | 入 | `\move(x,y-26,x,y,0,140)\fad(60,0)\t(140,200,\fscy94)\t(200,260,\fscy100)` | 260ms | impact | 下落+落地压扁 |
| zoom_settle | emphasis(whole_cue) | 入 | `\fscx130\fscy130\fad(80,0)\t(0,200,\fscx100\fscy100)` | 200ms | ding | 品牌花字（「三只喜鹊」样式） |

排版联动：`pop` 已放大到 105% 吃掉余量的问题一并修——规划层按
`headroom_px_ratio`（pop_rotate=0.12、jelly=0.06、slide=22px 等）在 max_width/带高
校验时预留，防止出带（`CaptionCue` 行宽校验容差与 `band.max_width_ratio=0.85` 处）。

### 4.3 契约变更（全部 additive、带默认值，防老 run resume 回放炸 `extra=forbid`）

| 字段 | 位置 | 说明 | openapi regen |
| --- | --- | --- | --- |
| `effect_id` Literal 扩展至 9 值 | `CaptionRun`（artifacts.py） | validator 白名单改读共享 JSON | 否（artifact 层走 `artifact_payloads` 无类型透传；但仍跑 CI drift 校验确认） |
| `style_id: str \| None = None` | `CaptionRun` | Phase 3 花字样式 | 否（同上） |
| `char_enter_frames: list[int] \| None = None` | `CaptionRun` | wipe_reveal 逐字帧；规划层从 SpeechTokenTiming 填充 | 否 |
| `char_advances_px: list[float] \| None = None` | `CaptionRun` | 逐字 x 定位；规划层已有逐字 hmtx 度量，顺手下传 | 否 |
| `font_glyph_fallback` | WarningCode + DegradationCode | §3.2 缺字降级 | **是** |
| `emphasis_style_id: str \| None = None` | `SubtitleOptions`（jobs.py） | 用户强制指定样式（可选） | **是** |

`policy_version` 保持 `caption_composition_v1` 不 bump（纯 additive）；
reuse 闸门不受影响（真闸是 `_TIMELINE_REUSE_BREAK_NODES`，本改动不增删节点）。

## 5. Phase 3 — 花字样式模板库

### 5.1 结构

`packages/production/pipeline/_emphasis_styles.py`：

```python
@dataclass(frozen=True)
class EmphasisStyleSpec:
    style_id: str
    font_asset_id: str            # 缺字时 per-hint 回退默认 emphasis 字体
    fill: str                     # #RRGGBB
    outline: str                  # #RRGGBB
    outline_width: float
    size_ratio: float             # 相对 normal 字号
    backing: str | None           # None | burst_star | underline_swipe | highlight_rect
    backing_color: str | None
    effect_id: str                # 绑定 Phase 2 动画
    sfx_class: str | None
    sfx_volume: float = 0.48      # 0.40-0.62
```

渲染侧：每个用到的 style 动态生成一条 ASS Style 行（打破"只有 Normal/Emphasis
两桶"的限制，Style 名 = `Emph_<style_id>`）；底衬用 **ASS `\p1` 矢量绘图**
（libass 单路径内可实现，不违红线）：
- `burst_star`：固定 12 角星多边形 path，按 run `advance_px` 缩放，layer 0 置于文字下；
- `underline_swipe`：文字基线下矩形，`\t(\clip)` 动画从左扫入（libass 支持矩形 clip 插值）；
- `highlight_rect`：文字底色块（含 padding），随文字同时入场。
- 文字 Dialogue 的 layer 从 0 提升为 1（底衬 layer 0），存量行为不变。

### 5.2 预制样式目录（v1 共 8 款）

| style_id | 名称 | 字体 | 填充 | 描边(宽) | 倍率 | 底衬 | 动画 | SFX |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| classic_yellow | 经典抖音黄 | noto_sans_cjk_sc_bold | #FFE14D | #000000 (4) | 1.40 | — | pop | pop |
| blue_burst | 蓝色爆炸贴 | zcool_qingke_huangyou | #FFFFFF | #1E6FD9 (5) | 1.50 | burst_star #3FA7F5 | pop_rotate | impact |
| red_alert | 警示红 | noto_sans_cjk_sc_bold | #FF4D4F | #FFFFFF (4) | 1.45 | — | drop_in | impact |
| brand_stamp | 品牌章 | smiley_sans | #A8D8F0 | #1B4F8A (5) | 1.50 | — | zoom_settle | ding |
| marker_orange | 马克笔橙 | lxgw_marker | #FF8A00 | #FFFFFF (3.5) | 1.40 | underline_swipe #FFD34D | slide_up_in | whoosh |
| ink_hand | 手写墨 | mashanzheng | #FFFFFF | #000000 (3) | 1.55 | — | soft_in | — |
| gold_serif | 高级金衬线 | noto_serif_cjk_sc_bold | #E8C97A | #4A3418 (3) | 1.35 | — | soft_in | — |
| highlight_box | 荧光底块 | noto_sans_cjk_sc_bold | #111111 | 无 (0) | 1.30 | highlight_rect #FFE14D | soft_in | click |

（参考视频复刻对照：「套模板」≈ blue_burst；「三只喜鹊」≈ brand_stamp；黄字强调 ≈ classic_yellow。）

### 5.3 LLM 语义接入（与 bgm_mood 模式同构，不违红线）

- `EmphasisHint` 加 `intensity: Literal["normal","strong","hero"] = "normal"`。
- prompt 迁移：**0064**（当前 head 0063）内联冻结新 `prompt_creative_intent` 正文
  （新增 intensity 字段说明：hero≤1 条/视频、strong≤3 条），三处同步——
  迁移内联正文 + `repository.py` 内存 seed + `seed.py` marker
  （`_CREATIVE_INTENT_RUNS_MARKER` 换成 `"intensity"`）。
- `resolve_creative_intent.py` 白名单提升逻辑加 intensity 解析（非法值静默降 normal）。

### 5.4 确定性映射规则（纯函数，同输入同输出）

```
tone/bgm_mood → 风格组：
  俏皮|轻快   → [blue_burst, marker_orange, classic_yellow]
  高级|沉稳   → [gold_serif, ink_hand, brand_stamp]
  高能|紧张   → [red_alert, classic_yellow, highlight_box]
  温暖|励志   → [classic_yellow, marker_orange, gold_serif]
  （未识别    → [classic_yellow, blue_burst, gold_serif]）
选择：组内按该 hint 在 emphasis 列表中的序号取模轮换；
hero intensity → 优先组内带底衬的样式；display_mode=whole_cue → 强制 brand_stamp 系。
请求带 emphasis_style_id → 全片强制该样式（用户显式覆盖优先）。
现有 emphasis_primary_color 请求字段继续生效：覆盖所选样式的 fill。
```

## 6. 工程注意事项（执行 AI 必读）

1. **openapi**：动 `SubtitleOptions`/Warning/Degradation 枚举后必须
   `uv run --extra dev python scripts/export_openapi.py && (cd apps/web && npm run generate:api)`；
   本地 drift 报警是环境敏感假阳性，以 CI unit 为准，勿手改 `schema.d.ts`。
2. **迁移**：只在 `packages/core/storage/alembic/versions/` 新增 `0064_*`，prompt 正文
   内联冻结，不得读可变 seed JSON；保持单一 head。
3. **worker 是独立进程**：改 `packages/production` 后重启 worker 才生效。
4. **与在途改动的关系**：工作区正在做"字体家族+字重"（`_fonts.py` weight_class 等），
   本方案假设其已合并；`distinct_font_assets_have_ambiguous_ass_style` 守卫对
   多样式多字体的新形态：不同 family 之间无约束，无需改动。
5. **测试**：`test_caption_composition.py` 现有 golden 断言（`\fscx105` 等）逐 effect 扩展；
   每个新动画/新样式各加一条 ASS golden 测试；libass 实机冒烟防空帧
   （历史坑：`\fad` t=0 空帧、BorderStyle=4 整块底衬）；覆盖率地板 ~90%，新代码必须带测试。
6. **资产完整性**：每款字体下载后 `sha256sum` 回填 FontSpec；URL 锚定 commit/release tag；
   许可证逐款复核（本清单全部 OFL-1.1，kenney 全部 CC0-1.0）。
7. **顺序**：Phase 0 → 1 → 2 → 3 严格串行（3 依赖 1 的字体与 2 的动画）；各 Phase 独立 PR。

## 7. 验收标准

- [ ] Phase 0：dev 实测成片含音效（ffprobe 断言 + 人耳抽检）；10 个 SFX 资产入库且 <1.5s。
- [ ] Phase 1：7 款字体入库；手写体缺字触发 `font_glyph_fallback` 显式降级而非 fail；
      得意黑（斜体）通过 overhang 余量方案完成排版不越带。
- [ ] Phase 2：9 种动画各有 golden ASS 测试；wipe_reveal 逐字帧与人声 token 对齐误差 ≤1 帧；
      pop_rotate/jelly 在 max_width 边界 cue 上不出带（新增边界测试）。
- [ ] Phase 3：8 款样式渲染冒烟全绿；tone→样式映射有确定性单测（同输入两次输出一致）；
      hero/strong/normal 在真实 prompt 下分布合理（人工抽 3 条 run 验收）。
- [ ] 全程：`python -m pytest -q` 绿；CI 五 workflow 绿；openapi 无漂移；老 run resume 回放不炸。

## 8. 明确不做（本 issue 范围外）

- PNG/APNG 贴纸轨（霓虹箭头、漂浮装饰）：需要第二渲染路径，违背"单一 libass 路径"，
  另行开 issue 讨论是否修订 #209 约定。
- 转场视觉特效（故障风等）与逐段落字幕换色：观感贡献小，优先级低。
- 前端样式选择 UI：本方案只开 `emphasis_style_id` 请求字段，创建页 UI 另行迭代。

## 9. 参考

- 竞品参考视频：`微信视频2026-07-15_002436_344.mp4`（本地），逐帧拆解见 §1。
- 现状架构扫描：8 个子系统并行审读结论（规划节点/注册表/字体/渲染/SFX/prompt/契约/在途 diff），
  关键 file:line 已在正文引用处逐条人工核对。
