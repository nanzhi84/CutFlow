# M5 施工简报：验收闸门（CI + Golden + Contract Tests）

负责：Codex（执行）/ Claude（架构 + 验收）
分支：`feat/m5-acceptance-gate`
Spec：第 20 章（测试与验收，行 2406-2483）、34.8（Matrix 验收）。
审计依据：tests-golden 审计两条 HIGH（golden 实际仅 5 条 vs spec 要求 ≥12；承载验收断言的 integration
测试被双重环境变量门控、默认全部静默跳过且无 CI 保证执行）。

## Goal

建立防回退闸门：golden cases 扩到 spec 20.2 清单、写接口 contract tests 全覆盖、
CI 流水线（真 Postgres + 真 Temporal）让门控测试必跑、OpenAPI diff 检查。

## 关键设计决定（架构师已定）

- CI 用 GitHub Actions YAML（仓库暂无 remote，YAML 先就位，hosting 由用户后续裁决，记 spec-questions）；
  同时提供 `scripts/ci_gate.sh` 本地等价闸门（验收官/开发者本机跑全量三套）。
- Golden cases 全部跑 LocalRuntimeAdapter + 内存模式 + sandbox providers（秒级、无外部依赖、
  Codex sandbox 内可自验）；DB/Temporal 行为已有专门集成测试，golden 不重复。
- Contract tests 数据驱动：从 OpenAPI schema 遍历写接口，逐个断言 2xx、422 错误体、未登录 401、
  权限不足 403、Idempotency-Key 重放语义。

## 改动清单（逐条核销）

### A. Golden cases（tests/golden/，对照 spec 20.2 编号）

- A1 #1 最小成功视频（已有，确认对齐）
- A2 #2 启用 B-roll 成功 + #3 B-roll 不足 soft degrade（report 含 broll.skipped_no_material）
- A3 #4 BGM 无可用标注 soft degrade（report 含 bgm.skipped_library_unannotated）
- A4 #5 人像素材不足 hard fail（material.insufficient.portrait，run failed）
- A5 #6 lipsync provider 超时后 resume（sandbox provider simulate timeout → run failed → resume 新 run
  复用 TTS 等合法前缀并成功）
- A6 #7 provider quota exceeded（provider.quota_exceeded，错误体 retryable 标注正确）
- A7 #8 timeline 越界被拒绝（render.invalid_timeline hard fail——构造越界 source window 的素材）
- A8 #9 字幕开启成功（subtitle.ass artifact 存在）+ 字幕关闭时无 artifact
- A9 #10 剪映草稿导出 + editor handoff 导出成功（package artifact 形状校验）
- A10 #11 从 FinishedVideo 创建发布批次（已有 publishing ops golden，确认对齐）+ #12 发布失败后
  retry publish（sandbox 平台 adapter simulate 失败 → retry-publish 端点 → 成功）
- A11 #13-15 fresh import 三条（Case+Script+Media 导入可展示；导入 MediaAsset 重新标注后可打开
  AnnotationEditView 并保存 patch；导入 FinishedVideo/PublishRecord/Performance 进入 insights）
- A12 #16 Case 发布 5 条后 reflection 生成 memory proposal（sandbox LLM）
- 注：现有 5 条 golden 保留；如某条因 M6 未实现的真语义无法端到端（如真平台发布），用 sandbox 语义
  实现并在测试 docstring 标注「M6 修真后需加强断言」，不许跳过编号。

### B. Contract tests（tests/contract/）

- B1 `test_api_contract_matrix.py`：数据驱动遍历 OpenAPI 中全部写接口（POST/PATCH/PUT/DELETE）：
  未登录 → 401 统一错误体；viewer 调 operator/admin 接口 → 403；非法 body → 422 统一错误体且
  request_id 存在；带 Idempotency-Key 的重复请求 → 重放原响应（200/原状态码）。
  端点需要的前置实体用工厂函数构造（sandbox/内存模式）。无法泛化的端点显式列入豁免表并写原因。
- B2 错误体统一性扫描：对全部已注册路由发起一次故意失败请求，断言响应体符合 spec 4.1 结构。

### C. CI 与本地闸门

- C1 `.github/workflows/ci.yml`：job1 unit（pytest 全量 + OpenAPI 导出 + `git diff --exit-code`
  确保 openapi.json/schema.d.ts 与代码同步）；job2 integration（services/steps 起 pgvector:pg16 +
  temporalio/auto-setup:1.26，跑 DB 门控 + Temporal 门控测试）；job3 frontend（npm ci + generate:api
  + build）。Python 3.12，pip 装 pyproject 依赖清单。
- C2 `scripts/ci_gate.sh`：本地等价（假定 docker compose 服务已起）：三套测试 + OpenAPI diff +
  前端 build，任一失败非零退出。
- C3 README 更新运行/验收说明（Linux 命令，替换 PowerShell 片段）。

## 边界（Out of scope）

- 不改业务语义（发布/标注/媒体的修真是 M6）；不动 provider 实现；不加新依赖。
- golden 不要求真 DB/Temporal（已有专项集成）；CI YAML 不要求本批真的在远端跑通（无 remote）。

## Verification（sandbox 内）

- `timeout -k 5 600 /home/nanzhi/projects/cutagent-genesis/.venv/bin/python -m pytest -q` 全绿
  （基线 86 passed, 18 skipped；本批会显著增加用例数）。
- 新增 golden/contract 测试全部在你的 sandbox 内可跑（内存模式），必须全绿。
- bash -n 校验 ci_gate.sh 语法。

## 验收门（验收官执行）

1. golden 对照 spec 20.2 编号逐条点名（≥12 条有效覆盖）。
2. contract matrix 实际遍历的写接口数 ≥ OpenAPI 写接口总数的 90%，豁免表合理。
3. `scripts/ci_gate.sh` 在本机真环境全绿走通。
4. 全量 + DB + Temporal 三套全绿。
