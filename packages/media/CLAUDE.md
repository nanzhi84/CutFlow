# packages/media

媒体处理领域层：素材库读写、V4 标注流水线（确定性传感器 + VLM 语义）、音频对齐/静音检测、ffmpeg 探测与处理、渲染/成片合成（`rendering/timeline.py`）、封面 prompt 与音色 provider 桥接。被 production/planning 节点和 API 服务调用。

## 职责
- 素材库 CRUD 与用量：`SqlAlchemyMediaRepository` 管 portrait/b-roll/BGM 素材、标注、音色（voice）持久化，含 `replace_asset_source_artifact`、`material_usage_ranking`、标注读写 `get_or_create_annotation`/`patch_annotation`/`rerun_annotation`。
- V4 标注：传感器（PySceneDetect/Silero VAD/OpenCV）出客观信号，VLM 只判语义；产出 `AnnotationV4`。
- 视频原语（`video/ffmpeg.py`）：ffmpeg/ffprobe 子进程做 `probe_media`、抽帧（`extract_thumbnails`/`extract_frame_at_time`）、`stabilize_video`、`compress_video_to_budget`、`normalize_for_upload`（上传可选规范化：旋转/裁剪/1080p/BT.709/H.264 + post-encode 校验）、`trim_to_valid_segments`、`probe_stream_types`。
- 纯转换：`audio/forced_alignment.py`（MiniMax TTS 字幕→ASR 形状）、`audio/silence.py`（silencedetect→停顿窗）；封面 prompt 组装与音色 provider 桥接（`voice_provider_bridge.py`：load / hydrate 上传水合 / persist 音色与预览，`clone_voice` 在仓储 `sqlalchemy_repository.py`）。
- 可恢复上传：`upload_reconciler.py` 驱动 completing → object_completed → verified → ready，流式校验对象并在 ready 后可重入派生缩略图。

## 关键文件 / 子目录
- `sqlalchemy_repository.py` — 媒体/音色仓储实现（对外导出 `SqlAlchemyMediaRepository`）
- `video/ffmpeg.py` — 所有 ffmpeg/ffprobe 子进程封装，`ffmpeg_bin()`/`ffprobe_bin()`；含 HDR→SDR（BT.709）tonemap
- `rendering/timeline.py` — ffmpeg 渲染命令构建层：`render_video_timeline`（主时间线渲染）/`render_broll_montage`（b-roll 蒙太奇）/`transcode_video_segment`/`concat_video_segments`/`fit_video_to_exact_duration`（精确时长拟合）/`validate_rendered_output`，被 production 节点复用
- `annotation/pipeline.py` — 纯编排 `run_annotation_v4`，依赖经 `V4Deps` 注入
- `annotation/runner.py` — 标注 wiring 层：接 `ProviderGateway` 跑付费 VLM，否则降级
- `annotation/sensors/` — 确定性传感器（shots/faces/cv_quality/frames/motion/voice_activity）；`faces.py` 出 `count_faces_in_image`（多脸闸）与 `detect_faces`（暴露 bbox/score/5 关键点，供封面选帧打分）
- `annotation/` 其余：`vlm.py`（窗口 prompt 构建 + VLM 响应解析）、`windows.py`（`plan_windows` 切窗）、`boundary.py`（snap 到切镜 / safety inset）、`report.py`（质量报告）、`errors.py`（V4 错误分类）、`reclip.py`（换源重切）
- `cover_frame.py` — 确定性「最佳人像参考帧」选择：密集抽帧 + YuNet 人脸 + Laplacian 清晰度打分，选最大/居中/清晰/正脸的单脸帧（无 VLM、无付费）；纯函数 `score_portrait_frame` + 编排 `select_best_portrait_frame`（fail-open）。被 `ExportFinishedVideo` 用作 AI 封面参考帧
- `annotation/bgm.py` — BGM/音频资产标注：客观特征（librosa 特征 / loudnorm LUFS，`extract_audio_features`/`measure_loudness_lufs`）+ LLM 语义 mood/scene（`annotate_bgm`）
- `annotation/assets/face_detection_yunet_2023mar.onnx` — YuNet 人脸模型权重
- `audio/forced_alignment.py`、`audio/silence.py`、`audio/sandbox_tts.py`（sandbox 兜底 TTS）、`cover.py`、`voice_provider_bridge.py`、`assets.py`（object-store 落盘助手 `store_file`，S3 后端走 path-based multipart upload/download，避免整文件进内存）

## 约定与要求
- ffmpeg/ffprobe 一律走 `video/ffmpeg.py` 的 `FfmpegRunner`；不得在 media 包内直接调用 `subprocess.run`。runner 负责进程组、取消传播、TERM→KILL 和 reap。二进制路径解析顺序为 settings（env `CUTAGENT_FFMPEG_BIN`/`CUTAGENT_FFPROBE_BIN`）→ `shutil.which` → `~/.local/bin`。
- 最终媒体先写同目录 `*.part.*`，完成媒体校验后用 `promote_staged_media()` 原子晋升；取消或失败时临时目录必须自动清理，未晋升文件不得进入对象存储。
- 传感器一律 fail-open：依赖缺失/解码失败返回空，绝不抛、绝不伪造负证据；无语音/无切镜的空结果是正常路径而非降级。
- 标注 retry-never-degrade：窗口按失败类型重试，耗尽则整资产 `annotation_status=failed`（空 clips），用「返回 FAILED 对象」表达，不向上抛。
- VLM 等付费调用带 `ProviderCall.idempotency_key`（per asset/prompt/frames）；prompt 取自 seeded 模板，`cover.py` 仅组装文本不发图。
- pipeline/runner 全部依赖注入，测试零真实 IO。

## 测试
- `pytest tests/media`；`tests/media/annotation/` 的传感器/pipeline 主要用 mock/fixtures，不触 VLM；`test_ffmpeg_tools.py` / `test_ffmpeg_hdr_normalize.py` 会生成真实媒体并调用 ffmpeg/ffprobe，缺可选 filter 时按 fixture skip。

## 注意 / 坑
- `silence.py` 只产停顿窗；语义边界 snap 到窗口的纯匹配器在消费方 `packages/planning/editing/audio_pause.py`，本模块只提供检测原语。
- `forced_alignment.py`/`cover.py` 不加载模型、不发网络/付费调用；真正的 MiniMax 字幕拉取、图像生成在各 provider plugin 内。
