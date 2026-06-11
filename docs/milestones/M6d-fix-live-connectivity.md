# M6d-fix 施工简报：真 Provider 连通修正

负责：Codex（执行）/ Claude（架构 + 真 key 验收）
分支：`feat/m6d-fix-live`
背景：M6d 已合入，MiniMax TTS 真连通验过；真 key live 验收抓到 DashScope ASR 插件调了虚构同步端点，
而 Paraformer 录音文件识别是异步任务 API。本批对照**原版调通实现**逐个修正真 API 形状。

## 唯一可信参照：原版调通代码（只读 `/home/nanzhi/projects/digital-human-Cutagent/backend/app/`）

Codex sandbox 无网络、不能真调 API——**必须严格照搬原版已上线验证过的请求形状**（endpoint、header、
请求体、异步轮询、结果解析），不要自己猜 API。逐个对照：

- **ASR（必修，最高优先）**：`services/asr_service.py` —— DashScope Paraformer 异步录音文件识别：
  提交 transcription task（带 X-DashScope-Async header）→ 轮询 `/api/v1/tasks/{task_id}` 至 SUCCEEDED →
  下载 transcription_url 的结果 JSON 取文字+句级时间戳。新插件 `packages/ai/providers/dashscope.py` 的
  DashScopeASRProvider 当前是虚构同步端点，按原版重写为异步任务流（复用 runninghub.py 已有的 submit+poll
  模式 + context.mark_polling）。
- **VLM 复查**：`services/broll_analysis_runner.py` 与 `video_service.py` 的 Qwen-VL 调用 —— 核对新插件
  DashScopeVLMProvider 的 endpoint/请求体/视频输入格式是否与原版一致（dashscope 多模态对话/兼容模式）。
- **HeyGem 复查**：`ai/adapters/runninghub_heygem.py` + `tests/test_runninghub_heygem_retry.py` —— 核对
  runninghub.py 的 submit 路径、节点字段填充（video_node_id/audio_node_id/field_name）、轮询与结果下载
  与原版一致。
- **LLM 复查**：dashscope chat（OpenAI 兼容）endpoint 与请求体核对。
- **TTS 是正确范例**（已真连通），ASR/VLM 的鉴权与客户端封装比照它。

## 改动清单

- A ASR 改异步任务流（参照原版），产出真 `audio.alignment`（句级时间戳）；错误码按 spec 2.3 映射。
- B VLM/HeyGem/LLM 请求形状对照原版逐项核对修正；不确定处保留原行为并在报告里标出存疑点。
- C mock 单测更新：ASR mock 模拟"提交→轮询→下载结果"三段（不再是单次 POST）；其余按修正后形状更新 mock。
- D live 门控测试（tests/providers/test_live_providers.py）保持，必要时调整 ASR 用例以匹配异步产物结构。

## 边界
- 不碰 sandbox 默认路径、不碰前端、不碰其他 milestone 文件域；只动 packages/ai/providers/ + 对应测试。

## Verification（sandbox 内）
- 全量 pytest 绿（基线含 M6d）；ASR/VLM mock 单测覆盖异步/真实形状；OpenAPI 无变化。

## 验收门（验收官，真 key）
- `CUTAGENT_RUN_LIVE_PROVIDER_TESTS=1` + 真 key + 公开素材：ASR（公开 wav）、VLM（公开图/视频）、
  TTS（已过）、HeyGem（真 portrait，可选）逐个 live 通过，产物真实可解析。
- ASR 产出真句级时间戳 → strict_timestamps=true 真 run 能出对齐字幕成片。
