# M6e 施工简报：保真度补缺（坦白①纯软件项 + ②可独立做的部分）

负责：Codex（执行）/ Claude（架构 + 验收）
分支：`feat/m6e-parity-gaps`
依据：`docs/audit/parity-check-2026-06-12.json`（113 条对照）。本批做**不依赖外部 key、不依赖真媒体**的缺口，
与 M6d（provider）并行——尽量只动 `apps/web/` 与少量后端端点，避开 packages/ai/providers。

## 改动清单

### A. Case 删除（spec 2.1「必须」，当前完全缺失）

- A1 后端 `DELETE /api/cases/{case_id}`（cases.py + service）：软删除（status=archived，PatchCaseRequest/
  CaseRecord 补 status 字段）或硬删除前校验无活跃 run/成片引用——按 spec 删除前引用检查原则，有引用则
  归档、无引用可硬删；契约补响应；contract test 覆盖（含权限、引用冲突 409）。
- A2 前端 CaseListPage 加删除入口 + 后果说明式 ConfirmDialog（说明成片/历史的去向）。

### B. Prompt 管理 UI（后端 Registry 已就位，前端零入口——高频能力丢失）

- B1 新页面 `/ops/prompts` 或设置页加「提示词」tab：列出 PromptTemplate + 版本，支持新建版本、
  查看 diff、approve/publish/rollback（调既有 prompts API）、binding 管理（按 node/capability/case 绑定）。
- B2 prompt 编辑器：变量插值提示（variables_schema 渲染成可点击插入的变量 chip）、输出 schema 展示、
  保存草稿→审批→发布流转可视化；prod 只读已发布版本。
- B3 把原版"视频分析/脚本生成/封面"等 prompt 作为 seed 录入（与 M6d 协调，避免重复——本批只做 UI，
  seed 若 M6d 已加则复用）。

### C. 成本预估（出片前预估，价格表已就位）

- C1 后端 `POST /api/jobs/digital-human-video/estimate-cost`（或 /api/cost/estimate）：按脚本长度估 TTS
  字符、按预估时长估 lipsync/视频秒数，查 price catalog 算 TTS/视频/总价三项；无价标 unpriced。
- C2 前端创作页「预估成本」按钮 → 弹窗显示 TTS/视频/总成本三行（对齐原版）。

### D. 任务操作与列表补缺（坦白①交互项）

- D1 强制终止：RunsPage 操作矩阵暴露 force-cancel（cancel 已支持 force 参数，前端传 true + 独立确认文案）。
- D2 任务记录删除：DELETE run/job 记录入口（不删成片文件，文案说明），处理中禁删。
- D3 无限滚动接线：InfiniteScrollSentinel 组件已存在但全仓零引用——接到 Runs/成片/素材/音色/BGM 列表
  （游标分页，后端列表接口已支持 cursor）。
- D4 登录支持用户名或邮箱（identifier）：后端 login 接受 email 或 display_name 查找；前端文案改「邮箱/用户名」。
- D5 注册码生成补用途备注 + 自定义码字段（后端 CreateRegistrationCodeRequest 补可选字段）。

### E. 概览/统计小补（坦白①）

- E1 全局任务完成/失败 toast：登录后挂一个轻量全局通道（可复用 useRunEvents 或新增全局 SSE/WS），
  run 终态弹 toast。若成本高可降级为：概览页轮询时对比上次快照弹 toast。
- E2 平台指标未回流→insights 页显式「数据等待中」空态文案（小改）。

### F. 收尾契约修正（坦白里的拼写/一致性 bug）

- F1 WarningCode 拼写对齐 spec：`font_default_used`→`font.default_used`、其余 warning code 点分命名核对
  （spec 27.1），pipeline 发出点同步；不改语义只改字面量 + 相关断言。

## 边界（Out of scope）

- 真媒体类（防抖/裁剪/自动匹配替换/剪映真包/MinIO）→ M6f；真 provider 类 → M6d；M6c 冻结。
- Broll 插入点预览时间轴、素材使用排行榜：依赖 planning/selection 聚合，**本批做只读 API + 基础 UI**
  即可（数据来自已有 material pack / timeline plan / selection ledger），复杂可视化可降级为列表。

## Verification（sandbox 内）

- 全量 pytest（基线 116）+ 新端点 contract test 全绿；`cd apps/web && npx tsc --noEmit && npm run build` 绿；
  OpenAPI 导出 + schema.d.ts 同步提交；文件纪律 ≤400 行。

## 验收门（验收官执行）

1. Playwright：建 Case→删 Case（有引用走归档）；Prompt 管理页新建版本→发布→回滚；创作页预估成本弹窗；
   强制终止/任务删除/无限滚动；用户名登录。
2. 全量 + DB + Temporal 三套绿；OpenAPI 无意外删改。

---

## 验收记录（2026-06-12，验收官：Claude）

**判定：通过**（merge 见 git log）。证据：126 单测 + 23 DB 集成（重建 schema 含 registration_codes.purpose）+ tsc/build 三绿；用户名登录、Prompt 管理页（/ops/prompts，模板/版本/变量chip/binding）Playwright 实测；estimate-cost（TTS+视频三段，sandbox unpriced）与 DELETE case（删后 404）curl 实测。A-F 全核销。

DB 集成首跑 22 失败=表结构新增 registration_codes.purpose 需重建 schema（M2 起的已知模式：契约改表后验收先 DROP+bootstrap）。
