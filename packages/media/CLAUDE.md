# packages/media

媒体处理领域层：素材库读写、V4 标注流水线（确定性传感器 + VLM 语义）、音频对齐/静音检测、ffmpeg 探测与处理、封面 prompt 与音色 provider 桥接。被 production/planning 节点和 API 服务调用。

## 职责
- 素材库 CRUD 与用量：`SqlAlchemyMediaRepository` 管 portrait/b-roll/BGM 素材、标注、音色（voice）持久化，含 `replace_asset_source_artifact`、`material_usage_ranking`、标注读写 `get_or_create_annotation`/`patch_annotation`/`rerun_annotation`。
- V4 标注：传感器（PySceneDetect/Silero VAD/OpenCV）出客观信号，VLM 只判语义；产出 `AnnotationV4`。
- 视频原语（`video/ffmpeg.py`）：ffmpeg/ffprobe 子进程做 `probe_media`、抽帧（`extract_thumbnails`/`extract_frame_at_time`）、`stabilize_video`、`compress_video_to_budget`、`normalize_for_upload`、`trim_to_valid_segments`、`probe_stream_types`。
- 纯转换：`audio/forced_alignment.py`（MiniMax TTS 字幕→ASR 形状）、`audio/silence.py`（silencedetect→停顿窗）；封面 prompt 组装与音色 preview/clone/design 桥接。

## 关键文件 / 子目录
- `sqlalchemy_repository.py` — 媒体/音色仓储实现（对外导出 `SqlAlchemyMediaRepository`）
- `video/ffmpeg.py` — 所有 ffmpeg/ffprobe 子进程封装，`ffmpeg_bin()`/`ffprobe_bin()`
- `annotation/pipeline.py` — 纯编排 `run_annotation_v4`，依赖经 `V4Deps` 注入
- `annotation/runner.py` — 标注 wiring 层：接 `ProviderGateway` 跑付费 VLM，否则降级
- `annotation/sensors/` — 确定性传感器（shots/faces/cv_quality/frames/motion/voice_activity）
- `annotation/bgm.py` — BGM/音频资产标注：客观特征（librosa 特征 / loudnorm LUFS，`extract_audio_features`/`measure_loudness_lufs`）+ LLM 语义 mood/scene（`annotate_bgm`）
- `annotation/assets/face_detection_yunet_2023mar.onnx` — YuNet 人脸模型权重
- `audio/forced_alignment.py`、`audio/silence.py`、`cover.py`、`voice_provider_bridge.py`、`assets.py`（object-store 落盘助手 `store_file`）

## 约定与要求
- ffmpeg/ffprobe 一律走子进程；二进制路径解析顺序为 settings（env `CUTAGENT_FFMPEG_BIN`/`CUTAGENT_FFPROBE_BIN`）→ `shutil.which` → `~/.local/bin`。
- 传感器一律 fail-open：依赖缺失/解码失败返回空，绝不抛、绝不伪造负证据；无语音/无切镜的空结果是正常路径而非降级。
- 标注 retry-never-degrade：窗口按失败类型重试，耗尽则整资产 `annotation_status=failed`（空 clips），用「返回 FAILED 对象」表达，不向上抛。
- VLM 等付费调用带 `ProviderCall.idempotency_key`（per asset/prompt/frames）；prompt 取自 seeded 模板，`cover.py` 仅组装文本不发图。
- pipeline/runner 全部依赖注入，测试零真实 IO。

## 测试
- `pytest tests/media`（含 `tests/media/annotation/`，传感器与 pipeline 用 mock/fixtures，不触真 ffmpeg/VLM）。

## 注意 / 坑
- `silence.py` 只产停顿窗；语义边界 snap 到窗口的纯匹配器在消费方 `packages/planning/editing/audio_pause.py`，本模块只提供检测原语。
- `forced_alignment.py`/`cover.py` 不加载模型、不发网络/付费调用；真正的 MiniMax 字幕拉取、图像生成在各 provider plugin 内。
