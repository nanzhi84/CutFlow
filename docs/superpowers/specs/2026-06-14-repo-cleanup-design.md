# 仓库清理工作流设计（repo-cleanup）

- 日期：2026-06-14
- 分支策略：基于 `main` 的 worktree（`worktree-repo-cleanup`），分批提交，全绿后 ff-merge 回 `main`
- 激进程度：**保守**（高置信 + 测试门控；存疑只标记不删）
- 本 session 范围：**规划 + 安全部分先执行**（文件类立即落地；代码类只产报告、分批待确认）

## 1. 目标与四类清理

| 类别 | 定义 | 引擎 | 本 session 处置 |
|---|---|---|---|
| 冗余文件 | 根目录孤儿截图、错放的文档、临时产物 | 确定性文件操作 | **立即执行 + 提交** |
| AI slop | 废话注释、复述代码的 docstring、注释掉的代码、模板化样板 | agent 阅读 | **产报告**，分批待确认 |
| 过度防御 | 吞异常、永不触发的 guard、重复校验、belt-and-suspenders | agent 阅读（仅在有测试覆盖处标记） | **产报告**，分批待确认 |
| 死函数/未用 | 0 引用且无动态/框架引用的函数/类/导出/依赖 | 分析器播种 + agent 复核 | **产报告**，分批待确认 |

非目标：功能改动、行为改动、无关重构、为清理而清理。YAGNI。

## 2. 方案选型：混合（方案 C）

四类清理性质不同，分两种引擎：

- **机械类**（死函数、未用导入、未用依赖、未用导出）→ 确定性分析器播种候选：`vulture`、`ruff --select F401,F811,F841`、`knip`（前端）、`depcheck`。
- **语义类**（AI slop、过度防御）→ agent 阅读代码发现，分析器对此完全失明。
- 两条线汇入同一个**对抗式复核 + 测试门控落地**阶段。

否决的备选：纯分析器（对语义类失明）、纯 agent 扫描（死代码部分噪声大、不可复现、贵）。

## 3. 分阶段流程

### Phase 0 — 盘点（确定性，工作流外先跑，已完成）

在主 checkout（有 `.venv` 和 `node_modules`）跑只读分析器，输出存 job tmp 播种候选：

- `vulture packages apps --min-confidence 60` → 241 条，其中 function/method/class 仅 31 条（210 条 "unused variable" 多为 Pydantic 字段假阳性，忽略）。
- `ruff --select F401,F811,F841` → 2 个测试未用导入（高置信，安全）。
- `knip`（apps/web）→ 8 个 unused files、8 个 unused exports、7 个 unused types；抓到重复组件 `Toast.tsx` vs `ui/Toast.tsx`。
- `depcheck` → 未用 npm 依赖（待跑）。

**关键认知**：分析器只播种，**判定权在复核阶段**。已知假阳性类：Pydantic 模型字段、Enum 成员、FastAPI 路由 handler、Temporal `@activity/@workflow`、Alembic `upgrade/downgrade`、SQLAlchemy 钩子（`get_col_spec`）、`.typecheck.ts` 类型断言文件、`__all__` 再导出、pytest fixture/conftest。

### Phase 1 — 文件清理（本 session 立即执行 + 提交）

worktree 内 git-tracked 操作：

- `git rm` 16 个 0 引用的 tracked 根 PNG（`m6a-*`、`m6ar-*`、`m6e-01`、`r3-*`、`r4-*`、`r5-*`、`r6-*`）。
- `m6b-final-frame.png`（被 M6b 文档引用）→ 移到 `docs/assets/`，修 `docs/milestones/M6b-real-media.md` 的文字引用。
- 根目录 `树影_Cutagent_CleanSlate重写Spec_v3_2026-06-11.md` → 移到 `docs/`，修 README / M1 / M2（及 audit json，若有）里的路径与"仓库根"措辞。**绝不删**（是 source-of-truth 规格）。
- 目录策略：今后文档统一归 `docs/`。

磁盘 housekeeping（gitignored 临时产物，仅主 checkout 有，不进任何提交）：`cutagent_cleanslate.egg-info/`、`.playwright-mcp/`、`.pytest_cache/`，以及未被跟踪的 `m6j-*.png`（其中 `m6j-prompts.png` 被 M6j 文档以文字引用，删前在报告标注）。

提交：`chore(cleanup): 清空根目录孤儿截图、归集文档到 docs/`。

### Phase 2 — Find（工作流，按类别并行扇出）

- **死代码线**：把 vulture function/method/class + ruff + knip 输出按 package/area 切片，agent 逐条映射成 `file:line + 证据 + 初判置信`。
- **slop 线**：agent 按模块区读代码，找废话注释 / 复述式 docstring / 注释掉的代码 / 模板样板。
- **过度防御线**：agent 找吞异常 / 永不触发 guard / 重复校验；**仅在该处有测试覆盖时**才标记（保守约束）。

每条 finding 结构化：`{类别, 文件, 起止行, 类型, 证据, 置信, 是否测试覆盖, 建议改法}`。

### Phase 3 — 对抗式复核（工作流，每条 finding 独立 skeptic）

skeptic 专门尝试**反驳"可删/可简化"**，检查：

- 动态引用：`getattr` / `globals()` / 字符串派发 / `importlib` / `__all__` 再导出。
- 框架注册：FastAPI 路由、Temporal `@activity/@workflow`、Alembic 版本、Pydantic / SQLAlchemy 事件钩子、pytest fixture/conftest、`entry_points`、setuptools。
- 对外面：HTTP route、CLI 入口、被测试引用、被前端调用的 API 契约。
- 过度防御项额外查：去掉 guard 后是否真有测试覆盖该路径、异常是否真不可能发生。

**多数 skeptic 反驳 → 降级为"仅标记不删"**。这是保守模式的精度闸。skeptic 与所有 subagent 一律 Opus，不降级。

### Phase 4 — 报告（本 session）/ 落地（确认后）

- 确认的 finding 按"类别 × 风险"分批，带 diff，写入 `docs/cleanup/2026-06-14-report.md`。
- 本 session **只出报告，不自动改代码**。
- 可复用脚本含**落地阶段**：批准某批后重跑 → 应用 edit → `.venv/bin/pytest -q`（动前端则加 `cd apps/web && npx tsc --noEmit`）→ 全绿提交，红则回滚并记录。

## 4. 测试门控

- 后端：`.venv/bin/pytest -q`（testpaths=tests，238 测试基线）。
- 前端：`cd apps/web && npx tsc --noEmit`（类型）；如有构建脚本再 `npm run build`。
- 规则：每批落地后必须门控全绿才提交；任一红灯 → 回滚该批 + 在报告记录原因。

## 5. 交付物（本 session）

1. 本设计文档（已提交）。
2. 可复用工作流脚本 `.claude/workflows/repo-cleanup.js`（Find / Verify / Apply 三阶段）。
3. Phase 1 文件清理已执行并提交。
4. Phase 2–3 产出的代码类清理报告 `docs/cleanup/2026-06-14-report.md`，分批待确认。

## 6. 落地与回滚

- 全程在 `worktree-repo-cleanup`，每批一个语义化 commit。
- 全部门控通过后 `git switch main && git merge --ff-only worktree-repo-cleanup`。
- 任何阶段可丢弃整个 worktree 而不影响 main。
