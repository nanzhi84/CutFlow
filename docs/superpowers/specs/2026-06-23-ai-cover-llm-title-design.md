# 默认 AI 封面 + LLM 真标题全链路 设计

日期：2026-06-23
分支：`worktree-ai-cover-title`
基准：origin/main @ `0766648`（#51）

## 背景与问题

当前每条成片的标题与封面都"看起来没接 AI"：

- **标题**：系统里没有任何"内容标题"。所有 `title` 要么是案例代理产出的 **人设·模式标签**（如 `硬广 · 全新创作脚本`，存进 `script_versions.title` / `finished_videos.title` / `jobs.request.title`），要么 `request.title` 为空 → 运行卡片回退 `script[:28]`（`jobs_runs.py:200` / `sqlalchemy_repository.py:209`）。唯一被设计来产出真标题（title / publish_content / cover_title / cover_subtitle）的 `packages/publishing/copy_node.py`，在 API 路径被 `apps/api/services/publishing_nodes.py:78` 写死 `llm_chat=None`，只能走"脚本首句"确定性兜底。
- **封面**：`packages/core/contracts/jobs.py:94` `CoverOptions.mode` 默认 `"frame"`，生产请求从不进 AI 分支。即便进了，AI 封面 prompt 用的 `title` 是 `request.title or ""`（空或 persona 标签）。

已核实（本地 DB / 密钥）：

- `openai.image.prod`（gpt-image-2-all，neuromash 镜像）enabled ✓，secret `sec_f2b633a1309f.secret` **active** ✓，插件已注册 → 图像生成已 arm。
- `dashscope.llm.prod` enabled ✓，secret `sec_250fdf310869.secret` **active** ✓ → `llm.chat` 已 arm。
- `PublishingCopy` 提示词（`prompt_publishing_copy_v1`）+ 绑定（node_id `PublishingCopy`）+ 输出 schema（`publish_copy.output`，由 `validate_publish_copy_output` 校验）全部已种子化；变量 `{case_name}`/`{description}`/`{script}`。

## 目标

1. 生产请求**默认生成 AI 封面**，帧封面仅作兜底（用户明确要求）。
2. 标题**全链路打通**：生产时一次性生成真 headline → 写进成片标题、喂进 AI 封面、复用到发布包；发布中心"生成/预览文案"也接真 LLM；运行卡片标题改用成片真标题。
3. 不静默降级；无真 LLM/无真图像 provider 时优雅退回确定性兜底/帧封面并显式上报。

## 方案

### A. 封面默认 AI（降级契约不变）

- `jobs.py:94` `CoverOptions.mode` 默认 `"frame"` → `"ai"`。
- 重生成 `apps/web/src/api/openapi.json` + `schema.d.ts`（CI 校验漂移）。
- `export_finished_video.py::_build_cover` 降级语义**收窄**（默认 ai 的必要配套）：
  - 解析到真 image profile（能力可用）但 AI 调用失败 → 回退帧 + 显式 `cover_frame_fallback`（degraded）——这是有信号、可行动的降级。
  - **完全没配 image provider**（部署无 AI 封面能力）→ 帧封面是诚实**基线**，不发降级。否则默认 ai 会让每条 CI/sandbox/未配密钥的 run 都凭空多一条 `cover_frame_fallback`，污染降级列表（run 仍 succeeded，但噪音 + 打挂断言精确降级列表的用例）。
  - 这不是"静默降级"：无能力时帧本就是唯一可产出的封面（基线），不是从可用更优路径的退化。
  - 影响 `tests/production/test_ai_cover_path.py`：原 `test_ai_cover_requested_but_unconfigured_falls_back_to_frame_cover`（无 profile）改为断言"帧 + 无降级 + succeeded"；新增"profile 在但调用失败 → 帧 + cover_frame_fallback"。

### B. 共享 LLM 文案接线 `packages/publishing/copy_llm.py`（新增）

```
build_copy_llm_chat(*, gateway, repository, prompt_registry=None,
                    case_id=None, run_id=None, node_run_id=None) -> LlmChatPort | None
```

- 解析 `llm.chat` 真 profile（enabled + 插件已注册 + 活密钥；排除 sandbox），无则返回 `None`。
- 返回的 `LlmChatPort`：用 PromptRegistry 解析 `PublishingCopy` 绑定的已发布版本，按 `{case_name}/{description}/{script}` 渲染，经 gateway `capability_id="llm.chat"` 调用，解析 JSON → dict（`copy_node.validate_publish_copy_output` 校验），返回 `(output_dict, prompt_invocation_id)`。
- `copy_node.py` 保持 provider 无关（仅依赖注入的 `LlmChatPort`）。
- 分层：`packages/publishing` 依赖 `packages/ai`（gateway/prompts），不构成环。

### C. 生产节点一次性生成文案 `export_finished_video.py`

- 节点早段调用 `generate_publish_copy(context, llm_chat=build_copy_llm_chat(...))`，得到 `copy`（title/publish_content/cover_title/cover_subtitle）+ source + invocation_id。
- `finished.title = copy.title or state.request.title or script.title or "未命名成片"`。
- `_build_cover` / `_generate_ai_cover` 接收 `copy`，AI 封面 `CoverPromptInputs.title = copy.cover_title`、`subtitle = copy.cover_subtitle`、`description = copy.publish_content or request.publish_content`。
- 发布包：`title = copy.title`，`description = copy.publish_content or request.publish_content`。
- 文本 LLM 调用幂等键 `copy-text-{run_id}`；节点已声明 `side_effects=["provider_call"]` + idempotency，reuse/replay 安全。
- 无真 LLM（`build_copy_llm_chat` 返回 None）→ `generate_publish_copy` 走确定性兜底（现行为），不硬失败。

### D. 发布中心接 LLM `publishing_nodes.py`

- `run_copy_node` 的 `generate_publish_copy(context, llm_chat=None)` → 用 `build_copy_llm_chat(gateway=request.app.state.provider_gateway, repository=repo, ...)`，手动"生成/预览文案"走真 LLM；无真 LLM 时仍兜底。

### E. 运行卡片标题 `_run_title`

- `jobs_runs.py` + `sqlalchemy_repository.py` 的 `_run_title`：优先该 run 的成片标题（按 run_id 查 FinishedVideo），有则显示真 headline；无（在跑/失败）才回退 `request.title or script[:28] or job.id`。

### F. 测试 + 契约

- 更新受默认改动波及的全链路/集成测试：不测封面的显式设 `cover={"mode": "frame"}`。
- 新增：默认即 ai、生产 LLM 标题接线（title/cover_title 进封面 prompt + 进 finished.title）、发布中心 LLM 文案、运行卡片用成片标题、`build_copy_llm_chat` 无 profile 返回 None 走兜底。
- `scripts/export_openapi.py` + `npm run generate:api` 重生成契约；`scripts/ci_gate.sh` 门禁。

## 成本

每条成片新增：1 次 `image.generate`（gpt-image，已确认）+ 1 次 `llm.chat`（文本，便宜）。发布中心手动生成文案各 1 次 `llm.chat`。

## 非目标（YAGNI）

- 不新增节点、不动 16 节点序列（仅在既有 ExportFinishedVideo 内扩展）。
- 不改 prompt 内容/绑定（已种子化可用）。
- Seedance 链路封面维持 `_safe_frame_cover`（无数字人/无 image2 主体，另行评估）。
- 不做封面/标题的人工编辑 UI（已有发布中心改 cover_artifact 能力）。
