# M6a-R 施工简报：前端重设计系列（视觉与易用性对齐原版）

负责：Codex（执行）/ Claude（架构 + 设计规范 + 验收）
背景：M6a-1 功能基线已合 main（链路实测通过），但视觉与易用性被产品负责人打回。
基准：原版前端 `/home/nanzhi/projects/digital-human-Cutagent/frontend/`（dev 分支）——其设计体系
与交互细节是产品打磨过的资产，完整盘点见验收档案（4 路 agent，证据到 file:line）。
**策略：移植而非发明。** 原版的 design tokens、组件原语、交互文案直接搬运进新前端并适度简化；
新前端继续消费新系统 API（生成类型 + 统一错误体 + WS）。

## 设计规范（架构师定版，R1 实现）

### Tokens（基本照搬原版 tailwind.config.js + index.css）

- 色板（纸感浅色）：background `#e9ece3` / secondary `#f4f5ef` / elevated `#ffffff`；
  surface `#fbfbf6` / hover `#f3f4ec`；border `#d9ddd2` / focus `#a7b199`；
  text.primary `#1b1d1a` / secondary `#5f665b` / tertiary `#90988a`。
- 品牌：brand.amber `#d6ff48`（主按钮/激活态/logo 块，深色文字 `#1b1d1a`）；
  accent 橄榄绿 `#5e6d51`（hover `#4c5a42`，light `#edf1e6`）。
- 状态：success `#4c8d62` / warning `#b68f32` / error `#c56a5d` / info `#5d7b6c`。
- 字体：font-sans `'Noto Sans SC','IBM Plex Sans','PingFang SC','Microsoft YaHei',system-ui`；
  font-display `'Playfair Display','Noto Serif SC',serif`（仅品牌名「树影」与一级标题）；
  font-mono `'JetBrains Mono','Fira Code'`（ID/数字，数字一律 tabular-nums）。
- 形状：卡片 rounded-[24px]（移动 18px）半透明白渐变 + glow 阴影；按钮/输入 rounded-2xl；
  badge/pill rounded-full。
- 背景氛围保留但**简化**：body 双层 radial-gradient + 线性底色可保留；网格纹理与 blur 光斑
  最多保留一处，不要三个光斑。
- 移植方式：读取原版 `frontend/tailwind.config.js` 与 `frontend/src/index.css`，
  适配后写入新仓库 `apps/web/tailwind.config.js` 与 `apps/web/src/index.css`
  （类名体系 .card/.btn/.btn-primary/.input/.badge/.nav-item 全保留）。

### 组件原语（src/components/ui/，参照原版同名组件移植）

Modal（portal + size sm~2xl）、Toast/useToast（右上角栈式、4 色、formatToastText 兼容
pydantic/axios 错误对象、默认 5s）、ConfirmDialog（info/warning/danger 三型 + isLoading；
**保留原版「后果说明式」确认文案风格**：写明费用/文件/复用边界）、Skeleton、SearchInput、
DropZone（拖拽高亮 + 扩展名/MIME 双校验 + 内联错误）、FlowStepper、StatusPill（icon+中文
label，processing 类 animate-spin）、InfiniteScrollSentinel、AudioPlayer/VideoPlayer（基础版）。

### 信息架构（对齐原版）

- 侧边栏 188px 纸白渐变：概览 `/`、案例中心 `/studio`、素材库 `/library`、数据统计 `/analytics`、
  账户中心 `/account`、设置 `/settings`；底部用户信息 + 退出。品牌区「树影 · Cutagent」font-display。
- 案例工作台三 tab：创作 `/studio/:caseId`、成片 `/studio/:caseId/outputs`、发布
  `/studio/:caseId/publish`（M6a-1 的 runs/finished-videos 路由保留重定向）。
- 本批占位页（骨架 + 「建设中」）：概览、素材库、数据统计、账户中心、发布 tab。

### 横切规范

- 时间一律 zh-CN：相对时间（刚刚/3 分钟前）+ title 悬浮绝对时间（YYYY-MM-DD HH:mm）。
- 枚举/状态一律走集中 i18n map（status → 中文 label + tone 色），禁止裸英文值上屏。
- 实时性：WS 为主 + react-query 轮询兜底（任务类 10s，document.hidden 时停轮询）；
  WS 无限重连（指数退避 cap 30s，online/visibilitychange 立即 kick）；左下角 ConnectionStatus pill。
- 所有破坏性/计费操作过 ConfirmDialog；所有 mutation 错误 toast（统一错误体 message，
  request_id 放详情）。

## R1 改动清单（本批）

- R1-A Tailwind 3.4 接入（依赖已预装）+ tokens/index.css 移植 + ui 原语组件。
- R1-B 壳层重做：侧边栏（6 入口 + 品牌区 + 用户区）、面包屑、ConnectionStatus、占位页骨架。
- R1-C 既有五页重皮：Cases 列表（卡片网格，含三计数徽标风格）、工作台创作页、Runs/成片
  （任务卡片化：9:16 缩略图位 + 状态 pill + 进度条 + 操作 icon 按钮组 + 中断态「中断中/中断成功」
  特殊呈现）、设置三 tab。
- R1-D 横切规范落地：zh-CN 时间、状态 i18n map、轮询兜底、ConnectionStatus。

## R2 改动清单（本批一并做）

- R2-A 创作 5 步向导：脚本 → 模板 → 成片配置 → 后处理 → 提交（StudioStepper，每步校验器，
  不通过禁用下一步 + toast 具体原因）。
- R2-B 偏好持久化：voice/语速/情绪、lipsync 开关与预设、BGM 配置、字幕样式/字号 全部
  localStorage 跨会话恢复；所选实体被删除时回退默认并提示。
- R2-C 即时反馈：脚本字符计数、必选项未选警示色、选中态卡片边框反馈、提交成功 toast 带
  run id 前 8 位 + 1.5s 跳转成片 tab。
- R2-D Run 详情弹窗（基础版）：节点时间线 + 降级警告（DegradationNotice 中文化）+ 错误
  （统一错误体展示）+ 产物清单（artifact 列表 + 下载）+ 「重试/续跑」带后果说明确认。

## 边界（Out of scope）

- 素材库四件套、标注编辑器、发布中心完整流、数据统计、账户中心实页（R3-R5）；
- 不动后端（如缺只读 API 可加，规范同 M6a-1 D3）；不引入 zustand/antd 等新依赖
  （tailwind/postcss/autoprefixer 已预装）。

## Verification（sandbox 内）

- `cd apps/web && npx tsc --noEmit && npm run build` 全绿。
- 后端不回退：`timeout -k 5 600 /home/nanzhi/projects/cutagent-genesis/.venv/bin/python -m pytest -q`（基线 102 passed）。
- 原版前端目录是只读参考，禁止修改原仓库任何文件。

## 验收门（验收官执行）

1. Playwright 实测 M6a-1 验证过的两条链路在新 UI 下仍通（创作→实时→成片；设置 secret/provider）。
2. 视觉对照原版截图：纸感色板/品牌绿/卡片/状态 pill 一致性；无英文裸枚举、无英文时间格式。
3. 5 步向导每步校验与偏好持久化实测（刷新页面后配置保留）。
4. tsc/build/后端测试三绿。

---

## R1+R2 验收记录（2026-06-11，验收官：Claude）

**判定：通过**（merge 见 git log）。证据：Playwright 实弹——5 步向导逐步校验、提交后成片页实时连接（中文相对时间「刚刚」、任务卡片化 + 原版操作矩阵）、偏好持久化实测（语速改 1.3x 刷新后保留）；视觉与原版纸感体系同源（色板/品牌绿/卡片/侧边栏/状态 pill）；tsc + build + 后端 102 测试三绿。

验收修复 1 处：移除 index.css 对 Google Fonts 的运行时 @import（死代理环境实测 16 个加载错误；离线/受限网络必须可用，字体走系统栈，品牌衬线后续自托管）。

待办（R3-R5）：素材库四件套 + 标注编辑器、发布中心完整流、概览/数据统计/账户中心。

---

## R3 改动清单（素材库四件套 + 标注编辑器）

信息架构对齐原版：`/library` 为 tab 容器（音色/视频模板/字体/BGM 四 tab，默认 /library/voices）。
全部消费新系统 API（media assets / annotations / voices / upload sessions），sandbox 语义下先把
页面与交互做真，媒体处理修真（M6b）后自然增强。

- R3-A 音色库：列表（搜索/类型筛选/分页）、音色卡（试听=调 preview 端点+内嵌播放器、编辑、
  删除带确认）、克隆弹窗（上传引用音频走 UploadSession + DropZone）、设计音色弹窗、生成音频区
  （文本+音色+语速+情绪 → 试听+下载）。
- R3-B 视频模板/B-roll：案例列表 → 案例详情双 tab（人像模板/B-roll，计数徽标）；上传（单个+
  批量文件夹，占位卡片进度混入网格、失败卡保留错误）；卡片（缩略图 hover 播放、时长角标、
  AI 分析/查看标注按钮、分析状态徽标轮询、下载、删除）；筛选（搜索/场景/分析状态）；批量操作
  模式（多选、批量分析/删除/改场景/标签）。
- R3-C 标注编辑器（AnnotationEditView 消费）：标注弹窗只读态（结构化片段/质量事件/有效无效
  时长三卡）+ 手动编辑态（质量状态、无效片段增删、片段字段编辑）→ PATCH 走 etag 乐观锁；
  重新分析两段式（预览对比 → 确认覆盖/放弃）；冲突（409/etag 不一致）给中文提示。
- R3-D 字体库：上传（DropZone 校验扩展名）、@font-face 动态注入实时预览、分类筛选、详情弹窗、
  删除；BGM 库：上传（单/批量带统一风格标签）、在线试听（全局播放条）、风格筛选、标注信息展示。
- R3-E 横切：所有上传走 UploadSession 协议（prepare → PUT → complete）封装成 useUpload hook；
  所有列表查询走生成类型；破坏性操作后果说明式确认。

后端缺口处理：缺的只读/操作 API 按 M6a-1 D3 规范补（契约 + routers/services + OpenAPI 同步），
但不得实现假语义（如假 AI 分析）——没有的能力 UI 上显示「待接入」禁用态并注明依赖 M6b/M6d。

## R3 验收记录（2026-06-12，验收官：Claude）

**判定：通过**（merge 见 git log）。证据：四 tab Playwright 实测零控制台错误；标注编辑器（etag 乐观锁/两段式重分析/409 中文提示）结构与类型齐备；上传统一 UploadSession；「待接入」禁用项如实申报无假语义；后端 104 测试 + tsc/build 绿。

两次验收干预：① 打回 2059 行 LibraryPage 单文件，返工拆为 LibraryLayout + 四 tab + 13 共享组件（最大 375 行）——这正是原版 Templates.tsx 6453 行教训的防复发；② 修复内部 `local://` URI 泄漏进 img/audio src（加 toDisplayUrl 边界净化 + 占位回退）。

待办：R4 发布中心、R5 概览/数据统计/账户中心。

---

## R4 改动清单（发布中心）

信息架构对齐原版：独立 `/publish-center(/:batchId)` + 工作台内嵌 `/studio/:caseId/publish` 双形态，
FlowStepper 三步（选来源 → 编辑 → 发布）。消费新系统 publish API（packages/batches/items/attempts）。

- R4-A 选来源步：从成片创建批次（勾选可发布成片，即时加入批次池）+ 外部视频上传（UploadSession）
  + 批次池汇总（计数/清空/单条移出）→ 创建批次；最近批次侧栏（状态/更新时间/切换/删除带确认）。
- R4-B 编辑步：批次默认设置折叠面板（平台 chips 多选、立即/定时、标签、地区）+「应用默认到选中」；
  逐条草稿编辑：标题（按平台最严字数上限实时计数+超限红色+自动截断）、正文、跳过/恢复、删除、
  重置编辑、保存单条、重试生成；本地 drafts 独立于服务端可回滚。
- R4-C 封面：视频抓帧（选帧秒数+预览帧+用当前画面）、上传封面（UploadSession）；
  AI 生成封面如后端能力未修真则「待接入（依赖 M6c/M6d）」禁用态。
- R4-D 发布步：确认清单（全选/单选、状态 pill、平台 chips）、半自动/全自动发布双按钮、
  失败条目重试；发布结果按 PublishAttempt 状态呈现（sandbox 语义如实标注「沙箱发布」）。
- R4-E 横切：批次详情自适应轮询（活跃态 3s/静态 8s）；小V猫平台状态条「待接入（M6c）」；
  所有计费/不可逆操作后果说明式确认。
