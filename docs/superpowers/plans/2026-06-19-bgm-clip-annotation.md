# BGM 按 clip 标注（librosa 掐秒 + Qwen-Omni 真听）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 BGM 素材像视频素材一样产出「带精确秒数的 1-3 条推荐使用片段 + 卡点网格」，由 librosa 确定性掐时间、Qwen-Omni 真听回填语义，并在标注编辑器里展示+可编辑。

**Architecture:** 与视频 V4 路径严格同构——确定性传感器（librosa）切 clip 并拥有全部时间戳；gated 多模态模型（DashScope `qwen3.5-omni-plus`，新 capability `audio.understanding`）逐片段听音频、只回语义不报秒。新增类型化契约 `BgmUsageWindowV4` 挂到 `AnnotationV4.bgm_usage_windows`，卡点网格入 `quality_report["bgm"]`。真实 IO（ffmpeg 切片 / presign / 网关调用）经依赖注入隔离，`annotate_bgm` 保持零真实 IO 可测。

**Tech Stack:** Python 3 / Pydantic v2 (contracts)、librosa（可选依赖，信号特征）、ffmpeg 子进程（切片）、httpx（DashScope OpenAI 兼容流式）、FastAPI（service 层）、React 18 + Vite + TS（前端）。

## Global Constraints

- **Contract-first**：改任何 API 形状后必须重生成 `apps/web/src/api/openapi.json`（`python scripts/export_openapi.py`）+ `apps/web/src/api/schema.d.ts`（`cd apps/web && npm run generate:api`）；`schema.d.ts` 是生成物禁手改。OpenAPI 漂移对环境敏感，以 CI pinned venv 为准。
- **领域类型唯一来源** `packages/core/contracts`（Pydantic v2，`ContractModel` 设 `extra="forbid"`）；新增契约必须在 `contracts/__init__.py` 同步 import + `__all__`。
- **降级显式、不静默、不伪造**：依赖缺失/未配 provider 时 fail-open 返回空或降级对象，绝不抛、绝不编造语义（retry-never-fabricate）。
- **真实 provider 调用**经 `ProviderGateway` 按 `capability_id` 分发，带 `idempotency_key`；无真实 profile+active secret 则降级（不静默回退 sandbox）。
- **librosa 是可选依赖**：缺失时 BGM 段落/卡点降级，模块仍可 import、标注仍完成。
- **lint**：ruff line-length 100（`pyproject.toml`）。
- **worker 是独立进程**：改 `packages/production`/节点后需重启；本特性主要走单测，无需。
- **测试零真实 IO**：`tests/media/annotation` 用注入的 extractor / mock gateway，不触真 ffmpeg/librosa/网络。
- **DashScope omni 强制流式**：`stream=True` 必填，否则 API 报错；插件必须累积流式 delta 再解析。
- 模型固定 `qwen3.5-omni-plus`（profile `model_id` 可热切），不用 `qwen-omni-turbo`(已停用) / `qwen3-omni-30b-a3b-captioner`(只出英文 caption)。

参考 spec：`docs/superpowers/specs/2026-06-19-bgm-clip-annotation-design.md`

---

### Task 1: 契约 `BgmSegmentRole` + `BgmUsageWindowV4` + `AnnotationV4.bgm_usage_windows`

**Files:**
- Modify: `packages/core/contracts/media.py`（新增枚举/模型，约在 `ClipV4`(544) 与 `AnnotationV4`(638) 之间；在 `AnnotationV4`(638) 增字段 + 扩 `_validate_time_bounds`(655)）
- Modify: `packages/core/contracts/__init__.py`（import + `__all__` 增 `BgmSegmentRole`、`BgmUsageWindowV4`）
- Test: `tests/contract/test_bgm_usage_window.py`

**Interfaces:**
- Produces:
  - `class BgmSegmentRole(str, Enum)`: `hook|climax|outro|general`
  - `class BgmUsageWindowV4(ContractModel)`: 字段 `segment_id:str, start:float, end:float, duration:float, role:BgmSegmentRole=general, drop_anchor_sec:float|None=None, energy:float=0.0, mood:str="", scene_fit:list[str]=[], avoid_scene:list[str]=[], reason:str="", confidence:float=0.8, source:str="sensor"`
  - `AnnotationV4.bgm_usage_windows: list[BgmUsageWindowV4]`

- [ ] **Step 1: 写失败测试**

```python
# tests/contract/test_bgm_usage_window.py
import pytest
from packages.core import contracts as c


def test_bgm_usage_window_valid():
    w = c.BgmUsageWindowV4(
        segment_id="w1", start=45.0, end=75.0, duration=30.0,
        role=c.BgmSegmentRole.climax, drop_anchor_sec=58.0, energy=0.8,
        mood="燃", scene_fit=["产品高光", "结尾收束"], reason="副歌高潮",
        confidence=0.9, source="sensor+audio",
    )
    assert w.role == c.BgmSegmentRole.climax
    assert w.drop_anchor_sec == 58.0


def test_bgm_usage_window_end_must_exceed_start():
    with pytest.raises(Exception):
        c.BgmUsageWindowV4(segment_id="w", start=10.0, end=10.0, duration=0.0)


def test_bgm_usage_window_drop_anchor_must_be_inside():
    with pytest.raises(Exception):
        c.BgmUsageWindowV4(segment_id="w", start=10.0, end=20.0, duration=10.0, drop_anchor_sec=25.0)


def test_annotation_v4_bgm_windows_bounds_enforced():
    meta = c.AnnotationMetaV4(asset_id="a", case_id="c", material_type="bgm", duration=60.0)
    # window end beyond duration -> raises (time-bounds safety net)
    with pytest.raises(Exception):
        c.AnnotationV4(
            meta=meta,
            bgm_usage_windows=[c.BgmUsageWindowV4(segment_id="w", start=50.0, end=90.0, duration=40.0)],
        )


def test_annotation_v4_bgm_windows_ok_within_bounds():
    meta = c.AnnotationMetaV4(asset_id="a", case_id="c", material_type="bgm", duration=60.0)
    ann = c.AnnotationV4(
        meta=meta,
        bgm_usage_windows=[c.BgmUsageWindowV4(segment_id="w", start=10.0, end=40.0, duration=30.0)],
    )
    assert len(ann.bgm_usage_windows) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/contract/test_bgm_usage_window.py -q`
Expected: FAIL（`AttributeError: module ... has no attribute 'BgmUsageWindowV4'`）

- [ ] **Step 3: 在 `packages/core/contracts/media.py` 加枚举与模型（紧接 `ClipV4` 之后，`QualityEventV4` 之前或同区）**

```python
class BgmSegmentRole(str, Enum):
    """BGM 推荐使用片段的用途。"""

    hook = "hook"        # 开场钩子
    climax = "climax"    # 高潮/副歌
    outro = "outro"      # 收尾
    general = "general"  # 通用铺底


class BgmUsageWindowV4(ContractModel):
    """BGM 推荐使用片段：整轨里值得用的一小段（非铺满整轨）。

    时间由 librosa 确定性产出（精确到秒、snap 到节拍）；语义由 Qwen-Omni 听音频回填。
    """

    segment_id: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    duration: float = Field(ge=0)
    role: BgmSegmentRole = BgmSegmentRole.general
    drop_anchor_sec: float | None = None
    energy: float = Field(0.0, ge=0, le=1)
    mood: str = ""
    scene_fit: list[str] = Field(default_factory=list)
    avoid_scene: list[str] = Field(default_factory=list)
    reason: str = ""
    confidence: float = Field(0.8, ge=0, le=1)
    source: str = "sensor"

    @model_validator(mode="after")
    def _validate(self) -> "BgmUsageWindowV4":
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be greater than start ({self.start})")
        derived = round(self.end - self.start, 3)
        if abs(derived - self.duration) > 0.12:
            self.duration = derived
        if self.drop_anchor_sec is not None and not (
            self.start - 1e-6 <= self.drop_anchor_sec <= self.end + 1e-6
        ):
            raise ValueError(
                f"drop_anchor_sec ({self.drop_anchor_sec}) must fall inside [{self.start}, {self.end}]"
            )
        return self
```

注意：`Enum` 已在文件顶部 import（确认 `from enum import Enum`；若无则补）。

- [ ] **Step 4: 在 `AnnotationV4` 增字段并扩时间边界校验**

在 `AnnotationV4`（约 648 行 `clips:` 附近）加：

```python
    bgm_usage_windows: list[BgmUsageWindowV4] = Field(default_factory=list)
```

在 `_validate_time_bounds`（约 655）已有 `for clip in self.clips: ...` 的循环后，补一段（同样 upper = duration + 1e-6）：

```python
            for win in self.bgm_usage_windows:
                if win.start < 0 or win.end > upper:
                    raise ValueError(
                        f"bgm_usage_window {win.segment_id} time [{win.start}, {win.end}] "
                        f"out of bounds [0, {duration}]"
                    )
```

- [ ] **Step 5: 在 `contracts/__init__.py` 同步导出**

在合适的 import 行（与 `ClipV4`/`AnnotationV4` 同组）加 `BgmSegmentRole, BgmUsageWindowV4`，并在 `__all__` 同步追加两项。

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/contract/test_bgm_usage_window.py -q`
Expected: PASS（5 passed）

- [ ] **Step 7: Commit**

```bash
git add packages/core/contracts/media.py packages/core/contracts/__init__.py tests/contract/test_bgm_usage_window.py
git commit -m "feat(contracts): add BgmUsageWindowV4 + AnnotationV4.bgm_usage_windows"
```

---

### Task 2: librosa 传感器扩展 — 纯函数 beats/drops/候选窗 + 切片

**Files:**
- Modify: `packages/media/annotation/bgm.py`（新增纯 helper + 在 `extract_audio_features`(102) / `_extract_librosa_features`(123) 接线）
- Test: `tests/media/annotation/test_bgm_sensor.py`

**Interfaces:**
- Produces（纯函数，不依赖 librosa，吃数组/标量，便于单测）:
  - `_tempo_bucket(bpm: float) -> str`（已存在，复用）
  - `detect_drops(energy: list[float], times: list[float], *, z: float = 1.2) -> list[float]`：能量显著正跃迁的时间点（秒）
  - `candidate_windows(duration: float, energy: list[float], times: list[float], beats: list[float], drops: list[float], *, max_windows: int = 3, target_len: float = 20.0) -> list[dict]`：返回 `[{start,end,energy,drop_anchor,role_hint}]`，start/end 已 snap 到最近 beat
  - `snap_to_beats(value: float, beats: list[float]) -> float`
  - `extract_audio_features` 新增返回键：`beats: list[float]`, `drops: list[float]`, `candidate_windows: list[dict]`

- [ ] **Step 1: 写失败测试（纯函数，零 IO）**

```python
# tests/media/annotation/test_bgm_sensor.py
from packages.media.annotation import bgm


def test_snap_to_beats_picks_nearest():
    assert bgm.snap_to_beats(10.4, [0.0, 5.0, 10.0, 15.0]) == 10.0
    assert bgm.snap_to_beats(12.6, [0.0, 5.0, 10.0, 15.0]) == 15.0
    assert bgm.snap_to_beats(7.0, []) == 7.0  # no beats -> unchanged


def test_detect_drops_finds_energy_jump():
    times = [float(i) for i in range(10)]
    energy = [0.1] * 5 + [0.9] * 5  # big jump at t=5
    drops = bgm.detect_drops(energy, times)
    assert any(abs(d - 5.0) < 1.0 for d in drops)


def test_detect_drops_flat_signal_none():
    times = [float(i) for i in range(10)]
    energy = [0.5] * 10
    assert bgm.detect_drops(energy, times) == []


def test_candidate_windows_capped_and_snapped_and_bounded():
    duration = 60.0
    times = [float(i) for i in range(61)]
    energy = [0.2] * 20 + [0.9] * 10 + [0.3] * 31  # high-energy region 20-30
    beats = [float(i) for i in range(0, 61, 2)]
    drops = bgm.detect_drops(energy, times)
    wins = bgm.candidate_windows(duration, energy, times, beats, drops, max_windows=3)
    assert 1 <= len(wins) <= 3
    for w in wins:
        assert 0 <= w["start"] < w["end"] <= duration
        assert w["start"] in beats and w["end"] in beats  # snapped
        assert 0.0 <= w["energy"] <= 1.0


def test_candidate_windows_short_track_single_window():
    duration = 18.0
    times = [float(i) for i in range(19)]
    energy = [0.5] * 19
    wins = bgm.candidate_windows(duration, energy, times, [], [], max_windows=3, target_len=20.0)
    assert len(wins) == 1
    assert wins[0]["start"] == 0.0
    assert abs(wins[0]["end"] - duration) < 1e-6
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/media/annotation/test_bgm_sensor.py -q`
Expected: FAIL（`AttributeError: ... has no attribute 'snap_to_beats'`）

- [ ] **Step 3: 实现纯 helper（加到 `bgm.py`，在 `_tempo_bucket` 附近）**

```python
def snap_to_beats(value: float, beats: list[float]) -> float:
    """Snap a timestamp to the nearest beat; unchanged when no beats."""
    if not beats:
        return value
    return min(beats, key=lambda b: abs(b - value))


def detect_drops(energy: list[float], times: list[float], *, z: float = 1.2) -> list[float]:
    """Time points (sec) of significant positive energy jumps (drop candidates)."""
    n = min(len(energy), len(times))
    if n < 3:
        return []
    deltas = [energy[i] - energy[i - 1] for i in range(1, n)]
    mean = sum(deltas) / len(deltas)
    var = sum((d - mean) ** 2 for d in deltas) / len(deltas)
    std = var ** 0.5
    if std <= 1e-9:
        return []
    drops: list[float] = []
    for i, d in enumerate(deltas, start=1):
        if (d - mean) / std >= z:
            drops.append(times[i])
    return drops


def candidate_windows(
    duration: float,
    energy: list[float],
    times: list[float],
    beats: list[float],
    drops: list[float],
    *,
    max_windows: int = 3,
    target_len: float = 20.0,
) -> list[dict]:
    """Pick 1-3 usable excerpt windows: drop-neighborhoods + highest-energy region.

    Deterministic. start/end snapped to nearest beats and clamped to [0, duration].
    Short tracks (<= target_len) collapse to a single whole-track window.
    """
    if duration <= 0:
        return []
    if duration <= target_len + 1e-6:
        e = _mean(energy)
        return [{"start": 0.0, "end": round(duration, 3), "energy": e,
                 "drop_anchor": (drops[0] if drops else None),
                 "role_hint": "climax" if drops else "general"}]

    raw: list[tuple[float, float, float | None, str]] = []  # (start, end, drop_anchor, role_hint)
    half = target_len / 2.0
    for d in drops:
        start = max(0.0, d - half * 0.4)        # a bit of lead-in before the drop
        end = min(duration, start + target_len)
        raw.append((start, end, d, "climax"))
    # highest-energy contiguous region center
    peak_t = _peak_time(energy, times)
    if peak_t is not None:
        start = max(0.0, peak_t - half)
        end = min(duration, start + target_len)
        raw.append((start, end, None, "general"))
    if not raw:  # no drops, no energy info -> opening window
        raw.append((0.0, min(duration, target_len), None, "hook"))

    # snap + dedupe overlapping (keep higher mean energy), cap to max_windows
    snapped: list[dict] = []
    for start, end, anchor, hint in raw:
        s = min(snap_to_beats(start, beats), duration)
        e = min(snap_to_beats(end, beats), duration)
        if e <= s:
            e = min(duration, s + target_len)
        win = {
            "start": round(s, 3), "end": round(e, 3),
            "energy": _mean_between(energy, times, s, e),
            "drop_anchor": (round(snap_to_beats(anchor, beats), 3) if anchor is not None else None),
            "role_hint": hint,
        }
        if not any(_overlaps(win, kept) for kept in snapped):
            snapped.append(win)
    snapped.sort(key=lambda w: w["energy"], reverse=True)
    return snapped[:max_windows]


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _mean_between(energy: list[float], times: list[float], start: float, end: float) -> float:
    vals = [energy[i] for i in range(min(len(energy), len(times))) if start <= times[i] <= end]
    return _mean(vals) if vals else _mean(energy)


def _peak_time(energy: list[float], times: list[float]) -> float | None:
    n = min(len(energy), len(times))
    if n == 0:
        return None
    idx = max(range(n), key=lambda i: energy[i])
    return times[idx]


def _overlaps(a: dict, b: dict) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]
```

- [ ] **Step 4: 在 `_extract_librosa_features` 接线（保留 beats，加 drops/windows）**

修改 `_extract_librosa_features`（123）：`beat_track` 现在丢弃 `_beats`，改为保留并转秒；用 RMS 帧能量曲线 + 帧时间算 drops/candidate_windows。把这些塞进返回 dict。在 `extract_audio_features`(102) 把它们合并进 features（已有 `features.update(librosa_features)`，无需改）。关键片段：

```python
    samples, sample_rate = librosa.load(str(path), sr=None, mono=True)
    if samples is None or len(samples) == 0:
        return None
    tempo, beat_frames = librosa.beat.beat_track(y=samples, sr=sample_rate)
    bpm = float(np.atleast_1d(tempo)[0])
    if not math.isfinite(bpm) or bpm <= 0:
        return None
    beats = [round(float(t), 3) for t in librosa.frames_to_time(beat_frames, sr=sample_rate)]
    rms_frames = librosa.feature.rms(y=samples)[0]
    energy = max(0.0, min(1.0, float(np.mean(rms_frames))))
    frame_times = [round(float(t), 3) for t in
                   librosa.frames_to_time(range(len(rms_frames)), sr=sample_rate)]
    energy_curve = [max(0.0, min(1.0, float(v))) for v in rms_frames]
    duration = float(len(samples) / sample_rate)
    drops = detect_drops(energy_curve, frame_times)
    windows = candidate_windows(duration, energy_curve, frame_times, beats, drops)
    return {
        "bpm": round(bpm, 2),
        "energy": round(energy, 4),
        "tempo_bucket": _tempo_bucket(bpm),
        "beats": beats,
        "drops": [round(d, 3) for d in drops],
        "candidate_windows": windows,
    }
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/media/annotation/test_bgm_sensor.py -q`
Expected: PASS（5 passed）

- [ ] **Step 6: Commit**

```bash
git add packages/media/annotation/bgm.py tests/media/annotation/test_bgm_sensor.py
git commit -m "feat(media): librosa beats/drops/candidate-windows for BGM segmentation"
```

---

### Task 3: ffmpeg 按窗切音频 helper

**Files:**
- Modify: `packages/media/video/ffmpeg.py`（新增 `extract_audio_segment`，紧邻 `trim_to_valid_segments`(442)）
- Test: `tests/media/test_extract_audio_segment.py`

**Interfaces:**
- Produces: `extract_audio_segment(source: str|Path, start: float, end: float, output: str|Path) -> Path`（用 `-ss/-t -vn -acodec libmp3lame` 切出片段音频；返回 output 路径）

- [ ] **Step 1: 写失败测试（monkeypatch runner，不跑真 ffmpeg）**

```python
# tests/media/test_extract_audio_segment.py
from pathlib import Path
from packages.media.video import ffmpeg


def test_extract_audio_segment_builds_expected_args(monkeypatch, tmp_path):
    captured = {}

    class FakeRunner:
        def run(self, args, *, timeout_sec=None):
            captured["args"] = list(args)
            Path(args[-1]).write_bytes(b"fake")  # output path is last arg
            class R: returncode = 0
            return R()

    monkeypatch.setattr(ffmpeg, "FfmpegRunner", lambda *a, **k: FakeRunner())
    out = ffmpeg.extract_audio_segment(tmp_path / "in.mp3", 45.0, 75.0, tmp_path / "out.mp3")
    assert out == tmp_path / "out.mp3"
    args = captured["args"]
    assert "-ss" in args and "45.000" in args
    assert "-t" in args and "30.000" in args
    assert "-vn" in args
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/media/test_extract_audio_segment.py -q`
Expected: FAIL（`AttributeError: ... 'extract_audio_segment'`）

- [ ] **Step 3: 实现（镜像 `trim_to_valid_segments` 的子进程风格）**

```python
def extract_audio_segment(source: str | Path, start: float, end: float, output: str | Path) -> Path:
    """Cut [start, end) of ``source`` to an mp3 at ``output`` (audio only)."""
    out = Path(output)
    duration = max(0.0, float(end) - float(start))
    runner = FfmpegRunner()
    runner.run([
        ffmpeg_bin(), *FFMPEG_QUIET_ARGS,
        "-ss", f"{float(start):.3f}", "-t", f"{duration:.3f}",
        "-i", str(source), "-vn", "-acodec", "libmp3lame", "-y", str(out),
    ])
    return out
```

（确认 `FfmpegRunner`、`FFMPEG_QUIET_ARGS`、`ffmpeg_bin` 均已在本文件定义/导入——见 105/126 行。）

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/media/test_extract_audio_segment.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add packages/media/video/ffmpeg.py tests/media/test_extract_audio_segment.py
git commit -m "feat(media): extract_audio_segment ffmpeg helper"
```

---

### Task 4: DashScope `audio.understanding` provider（流式 + 音频 URL）

**Files:**
- Modify: `packages/ai/providers/dashscope.py`（新增 `DashScopeOmniProvider` + 流式 helper `_chat_completion_stream`）
- Modify: `packages/ai/providers/__init__.py`（在 `register_real_provider_plugins` 注册 `dashscope.omni`）
- Test: `tests/providers/test_dashscope_omni.py`

**Interfaces:**
- Consumes: `ProviderCall(capability_id="audio.understanding", input={"prompt": str, "audio_uri": str, ...})`
- Produces: `DashScopeOmniProvider(provider_id="dashscope.omni").invoke_with_context(call, context) -> ProviderResult`，`output={"content": str, "intent": dict}`（`intent` = 解析出的 JSON）

- [ ] **Step 1: 写失败测试（mock 流式 SSE 响应）**

```python
# tests/providers/test_dashscope_omni.py
import json
import httpx
from packages.ai.providers.dashscope import DashScopeOmniProvider
from packages.ai.gateway.provider_gateway import ProviderCall


def _sse(lines):  # build an OpenAI-style streamed body
    chunks = []
    for piece in lines:
        chunks.append(f"data: {json.dumps(piece)}\n\n")
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode()


def test_omni_streams_and_parses_json(monkeypatch, dashscope_context_factory):
    body = _sse([
        {"choices": [{"delta": {"content": "{\"mood\":"}}]},
        {"choices": [{"delta": {"content": " \"燃\", \"scene_fit\": [\"高光\"]}"}}]},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["model"] == "qwen3.5-omni-plus"
        content = payload["messages"][0]["content"]
        assert any(p.get("type") == "input_audio" for p in content)
        return httpx.Response(200, content=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = DashScopeOmniProvider(client)
    ctx = dashscope_context_factory(model_id="qwen3.5-omni-plus")  # see conftest helper
    call = ProviderCall(
        case_id="c", provider_profile_id=ctx.profile.id, capability_id="audio.understanding",
        input={"prompt": "标注这段BGM", "audio_uri": "https://x/clip.mp3"},
        idempotency_key="k",
    )
    result = provider.invoke_with_context(call, ctx)
    assert result.output["intent"]["mood"] == "燃"
    assert result.output["intent"]["scene_fit"] == ["高光"]


def test_omni_rejects_wrong_capability(dashscope_context_factory):
    provider = DashScopeOmniProvider(httpx.Client())
    ctx = dashscope_context_factory(model_id="qwen3.5-omni-plus")
    call = ProviderCall(case_id="c", provider_profile_id=ctx.profile.id,
                        capability_id="llm.chat", input={}, idempotency_key="k")
    import pytest
    with pytest.raises(Exception):
        provider.invoke_with_context(call, ctx)
```

> 注：若 `tests/providers/conftest.py` 没有 `dashscope_context_factory`，本 Task 增一个最小 fixture（构造一个带 `model_id`、`default_options={}`、`timeout_sec` 的 `ProviderInvocationContext`，并让 `require_secret` 返回 "test-key"；参考现有 `tests/providers` 里对 vlm/llm 的构造方式 grep `ProviderInvocationContext(`）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/providers/test_dashscope_omni.py -q`
Expected: FAIL（import error / 未实现）

- [ ] **Step 3: 实现 provider + 流式 helper（加到 `dashscope.py`）**

```python
class DashScopeOmniProvider:
    provider_id = "dashscope.omni"

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def invoke_with_context(self, call, context):
        if call.capability_id != "audio.understanding":
            raise ProviderRuntimeError(
                ErrorCode.provider_unsupported_option,
                "DashScope Omni requires audio.understanding.",
            )
        messages = call.input.get("messages")
        if not isinstance(messages, list):
            prompt = str(call.input.get("prompt") or "")
            audio_uri = str(call.input.get("audio_uri") or "")
            if not audio_uri:
                raise ProviderRuntimeError(ErrorCode.provider_unsupported_option, "audio_uri is required.")
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "input_audio", "input_audio": {"data": audio_uri}},
                ],
            }]
        content = _chat_completion_stream(self.client, call, context, messages)
        intent = _parse_json_object(content)
        return ProviderResult(
            output={"content": content, "intent": intent or {"text": content}},
            audio_seconds=float(call.input.get("audio_seconds") or 0.0),
            raw_usage={"provider_response": {"streamed": True}},
        )


def _chat_completion_stream(client, call, context, messages) -> str:
    api_key = require_secret(context)
    payload = {
        "model": context.profile.model_id,
        "messages": messages,
        "modalities": ["text"],
        "stream": True,
    }
    parts: list[str] = []
    with client.stream(
        "POST",
        _chat_url(context.profile.default_options),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=float(context.profile.timeout_sec),
    ) as response:
        if response.status_code >= 400:
            response.read()
            raise ProviderRuntimeError(ErrorCode.provider_remote_failed,
                                       f"DashScope Omni HTTP {response.status_code}.")
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data in ("", "[DONE]"):
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str):
                    parts.append(piece)
    return "".join(parts)
```

- [ ] **Step 4: 注册插件**

在 `packages/ai/providers/__init__.py` 的 `register_real_provider_plugins` 里，挨着 dashscope.vlm/llm 注册：

```python
    gateway.register_plugin(DashScopeOmniProvider(dashscope_client))
```

（`dashscope_client` 复用现有 dashscope 共享 httpx client；import `DashScopeOmniProvider`。grep 现有 `DashScopeLLMProvider(` 注册行照搬。）

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/providers/test_dashscope_omni.py -q`
Expected: PASS（2 passed）

- [ ] **Step 6: Commit**

```bash
git add packages/ai/providers/dashscope.py packages/ai/providers/__init__.py tests/providers/test_dashscope_omni.py
git commit -m "feat(providers): DashScope Omni audio.understanding (streaming + audio URL)"
```

---

### Task 5: Seed — provider profile + capability + 价目

**Files:**
- Modify: `packages/core/storage/provider_seed.py`（profiles 列表加 `dashscope.omni.prod`；`_seed_price_catalogs` 加 catalog + items；若有 ProviderCapability 列表则加 audio.understanding）
- Modify: `packages/core/storage/repository.py`（内存仓若也 seed profiles，同步加；grep `dashscope.llm` 定位）
- Test: `tests/contract/test_bgm_omni_seed.py`

**Interfaces:**
- Produces: 一个 `capability="audio.understanding"`、`model_id="qwen3.5-omni-plus"`、`provider_id="dashscope.omni"` 的 enabled ProviderProfile + 对应 price items。

- [ ] **Step 1: 写失败测试**

```python
# tests/contract/test_bgm_omni_seed.py
from packages.core.storage.repository import Repository
from packages.core.storage import provider_seed


def test_omni_profile_seeded():
    repo = Repository()
    provider_seed.seed_providers(repo)  # use the real seed entrypoint name (grep)
    profiles = [p for p in repo.provider_profiles.values() if p.capability == "audio.understanding"]
    assert profiles, "expected an audio.understanding profile"
    p = profiles[0]
    assert p.provider_id == "dashscope.omni"
    assert p.model_id == "qwen3.5-omni-plus"
    assert p.enabled
```

> 把 `seed_providers` 换成该文件实际的 seed 函数名（grep `^def .*seed` packages/core/storage/provider_seed.py）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/contract/test_bgm_omni_seed.py -q`
Expected: FAIL（无 audio.understanding profile）

- [ ] **Step 3: 加 profile（镜像 `dashscope.llm.prod`，68-79 行）**

```python
        ProviderProfile(
            id="dashscope.omni.prod",
            provider_id="dashscope.omni",
            model_id="qwen3.5-omni-plus",
            capability="audio.understanding",
            display_name="DashScope Qwen-Omni Audio Production",
            environment="prod",
            enabled=True,
            secret_ref="dashscope_prod.secret",
            concurrency_key="dashscope:audio.understanding",
            timeout_sec=120,
            options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.audio.options"),
        ),
```

（确认 `enabled` 字段名/默认值与其他 profile 一致；若其他 profile 没显式写 enabled 则去掉此行，默认即 enabled。）

- [ ] **Step 4: 加价目（镜像 dashscope.llm input/output，232-249 行）**

`_seed_price_catalogs` 的 catalogs 加：
```python
        ProviderPriceCatalog(id="price_dashscope_omni_prod", provider_id="dashscope.omni", status="published"),
```
price_items 加 input/output 两条（`model_id="qwen3.5-omni-plus"`, `capability_id="audio.understanding"`, 单价用占位 `Decimal("0.000002")`/`Decimal("0.000008")`，注释「实现时按 DashScope 实际价目核定」）。

- [ ] **Step 5: ProviderCapability（如存在列表）**

`grep -n "ProviderCapability(" packages/core/storage/*.py`。若 seed 里有 capability 列表，加一条 `provider_id="dashscope.omni", capability="audio.understanding", model_id="qwen3.5-omni-plus"` 镜像 llm 那条；没有则跳过（gateway 按 profile.capability + plugins 分发即可）。

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/contract/test_bgm_omni_seed.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add packages/core/storage/provider_seed.py packages/core/storage/repository.py tests/contract/test_bgm_omni_seed.py
git commit -m "feat(seed): dashscope.omni audio.understanding profile + pricing"
```

---

### Task 6: `annotate_bgm` 重写 — 切窗→每段真听→组装 bgm_usage_windows + 降级

**Files:**
- Modify: `packages/media/annotation/bgm.py`（`annotate_bgm`(216) 重写；加 `resolve_audio_profile`；改 `_build_semantic_prompt`/`_normalize_semantics` 为按窗；assembly 产 `bgm_usage_windows` + `quality_report.bgm.beats/drops`）
- Test: `tests/media/annotation/test_bgm_annotate.py`（扩现有或新增）

**Interfaces:**
- Consumes: Task1 契约、Task2 features（beats/drops/candidate_windows）
- Produces:
  - `resolve_audio_profile(gateway, *, candidate_profiles, explicit_profile=None) -> ProviderProfile | None`（镜像 `resolve_llm_profile`，capability `audio.understanding`）
  - `annotate_bgm(..., audio_profile: ProviderProfile|None, audio_url_for_window: Callable[[float,float], str|None]|None = None, feature_extractor=None) -> BgmAnnotationResult`（返回的 `annotation.bgm_usage_windows` 类型化；`quality_report["bgm"]` 含 beats/drops）

- [ ] **Step 1: 写失败测试（注入 extractor + mock gateway，零 IO）**

```python
# tests/media/annotation/test_bgm_annotate.py
from packages.media.annotation.bgm import annotate_bgm
from packages.core import contracts as c
# 复用现有 BGM 测试里的 mock gateway 模式（grep tests/media/annotation 里现有 bgm 测试的 fixtures）

FEATURES = {
    "librosa_available": True, "bpm": 128.0, "energy": 0.6, "tempo_bucket": "fast",
    "loudness_lufs": -14.0, "beats": [0.0, 0.5, 45.0, 58.0, 75.0],
    "drops": [58.0],
    "candidate_windows": [
        {"start": 45.0, "end": 75.0, "energy": 0.8, "drop_anchor": 58.0, "role_hint": "climax"},
    ],
}


def test_annotate_bgm_full_listen_produces_typed_windows(omni_gateway_ok):
    # omni_gateway_ok: a gateway whose audio.understanding returns
    # {"mood":"燃","scene_fit":["高光"],"avoid_scene":[],"role":"climax","reason":"副歌"}
    result = annotate_bgm(
        asset_id="a", case_id="c", audio_path="x.mp3", duration=80.0, asset_title="一马当先",
        gateway=omni_gateway_ok,
        audio_profile=omni_gateway_ok.audio_profile,
        audio_url_for_window=lambda s, e: f"https://x/{s}-{e}.mp3",
        feature_extractor=lambda _p: dict(FEATURES),
    )
    ann = result.annotation
    assert ann.meta.material_type == "bgm"
    assert len(ann.bgm_usage_windows) == 1
    w = ann.bgm_usage_windows[0]
    assert w.start == 45.0 and w.end == 75.0 and w.drop_anchor_sec == 58.0
    assert w.role == c.BgmSegmentRole.climax
    assert w.mood == "燃" and w.scene_fit == ["高光"]
    assert w.source == "sensor+audio"
    assert ann.quality_report["bgm"]["beats"] == FEATURES["beats"]
    assert ann.quality_report["bgm"]["drops"] == FEATURES["drops"]


def test_annotate_bgm_no_audio_profile_degrades_to_sensor(sandbox_gateway):
    result = annotate_bgm(
        asset_id="a", case_id="c", audio_path="x.mp3", duration=80.0, asset_title="t",
        gateway=sandbox_gateway, audio_profile=None,
        audio_url_for_window=lambda s, e: None,
        feature_extractor=lambda _p: dict(FEATURES),
    )
    ann = result.annotation
    assert result.llm_configured is False
    assert len(ann.bgm_usage_windows) == 1          # objective windows still produced
    w = ann.bgm_usage_windows[0]
    assert w.source == "sensor"
    assert w.mood == ""                              # no fabricated semantics
    assert w.role == c.BgmSegmentRole.climax         # heuristic from drop
    assert ann.quality_report["bgm"]["beats"] == FEATURES["beats"]


def test_annotate_bgm_no_librosa_degrades_whole_track(sandbox_gateway):
    result = annotate_bgm(
        asset_id="a", case_id="c", audio_path="x.mp3", duration=0.0, asset_title="t",
        gateway=sandbox_gateway, audio_profile=None,
        feature_extractor=lambda _p: {"librosa_available": False, "loudness_lufs": -14.0},
    )
    ann = result.annotation
    assert ann.bgm_usage_windows == []
    assert ann.meta.annotation_status == c.AnnotationStatus.failed
```

> `omni_gateway_ok` / `sandbox_gateway` fixtures：复用现有 bgm 测试里 mock gateway 的写法（现有 `annotate_bgm` 测试已用 mock gateway，照搬并把 capability 从 `llm.chat` 改 `audio.understanding`，给 mock 加一个 `.audio_profile` 属性指向一个 real-looking ProviderProfile）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/media/annotation/test_bgm_annotate.py -q`
Expected: FAIL

- [ ] **Step 3: 加 `resolve_audio_profile`（镜像 `resolve_llm_profile`(71)）**

```python
def resolve_audio_profile(gateway, *, candidate_profiles, explicit_profile=None):
    ordered = [p for p in (explicit_profile, *candidate_profiles) if p is not None]
    seen: set[str] = set()
    for profile in ordered:
        if profile.id in seen:
            continue
        seen.add(profile.id)
        if _is_real_audio_profile(gateway, profile):
            return profile
    return None


def _is_real_audio_profile(gateway, profile) -> bool:
    if profile.capability != "audio.understanding" or not profile.enabled:
        return False
    if profile.provider_id == "sandbox":
        return False
    if profile.provider_id not in gateway.plugins:
        return False
    if profile.secret_ref and not gateway._secret_is_active(profile.secret_ref):
        return False
    return True
```

- [ ] **Step 4: 重写 `annotate_bgm` 主体 + 每窗听 + 组装**

要点（在现有结构上改）：
1. 签名换 `llm_profile` → `audio_profile`，加 `audio_url_for_window: Callable[[float,float], str|None]|None=None`。
2. 抽 features 后取 `candidate_windows = features.get("candidate_windows") or []`。
3. 对每个候选窗构造 `BgmUsageWindowV4`：客观字段直接来自窗（start/end/energy/drop_anchor/role_hint→role）。`role` 由 `role_hint` 映射（climax/hook/general），无 audio 时即为 sensor。
4. 若 `audio_profile` 且有 `audio_url_for_window`：对每窗取 URL→`gateway.invoke(ProviderCall(capability_id="audio.understanding", input={"prompt": _build_window_prompt(...), "audio_uri": url}, idempotency_key=f"bgm-omni-{asset_id}-{i}"))`→解析 `intent`→回填 mood/scene_fit/avoid_scene/role(clamp)/reason，`source="sensor+audio"`，收集 invocation_ids。单窗失败：该窗保持 sensor-only（不抛、不伪造）。
5. `quality_report["bgm"]` = track 摘要(bpm/energy/tempo_bucket/loudness/genre/mood整体/librosa_available/status/source) + `"beats"`/`"drops"`（来自 features）。
6. 无 librosa（`candidate_windows` 空）→ 沿用现有降级（无 windows，status failed/`features_unavailable`）。
7. `BgmAnnotationResult(annotation, llm_configured=<bool: audio_profile is not None>, provider_invocation_ids=...)`（保留字段名 `llm_configured` 以兼容 `asset_annotation` 现有读取，或一并改名——见 Task 7）。

新增 prompt 构造（替换 `_build_semantic_prompt`，按单窗、要中文 JSON）：

```python
def _build_window_prompt(*, asset_title, window, features) -> str:
    payload = {
        "bgm_name": asset_title,
        "window": {"start": window["start"], "end": window["end"],
                   "energy": window.get("energy"), "has_drop": window.get("drop_anchor") is not None},
        "required_schema": {
            "mood": "一个简短情绪词", "role": "hook|climax|outro|general",
            "scene_fit": ["2-6 个该片段适配的中文短视频场景"],
            "avoid_scene": ["0-4 个应避免的中文场景"],
            "reason": "一句中文推荐理由",
        },
    }
    return (
        "你在听一段BGM片段。结合你听到的音乐与给定信息，推断情绪/用途/适配场景，"
        "只返回一个合法 JSON 对象，不要 markdown 或多余文字。\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
```

`_normalize_window_semantics(intent, *, role_hint)`：缺失字段不伪造（mood/scene 留空），`role` clamp 到 `BgmSegmentRole`（非法→role_hint→general）。

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/media/annotation/test_bgm_annotate.py tests/media/annotation/test_bgm_sensor.py -q`
Expected: PASS（同时旧 bgm 测试若断言已变的形状需同步更新）

- [ ] **Step 6: 跑整组 media 标注测试防回归**

Run: `python -m pytest tests/media/annotation -q`
Expected: PASS（修正任何因签名变更而失败的旧用例）

- [ ] **Step 7: Commit**

```bash
git add packages/media/annotation/bgm.py tests/media/annotation/
git commit -m "feat(media): annotate_bgm produces typed bgm_usage_windows via per-window Qwen-Omni listen"
```

---

### Task 7: API service 接线 — gating + 切片 presign + 持久化 windows

**Files:**
- Modify: `apps/api/services/asset_annotation.py`（`_run_bgm_annotation`(107) 与 `_run_sqlalchemy_bgm_annotation`(311)：candidates capability 改 `audio.understanding`、用 `resolve_audio_profile`、构造 `audio_url_for_window`、editable_paths 加 bgm 路径）
- Test: `tests/api/test_bgm_annotation_run.py`（扩现有或新增）

**Interfaces:**
- Consumes: Task6 `annotate_bgm` / `resolve_audio_profile`；Task3 `extract_audio_segment`；`object_store(request).signed_url(uri)`；`store_file`。
- Produces: 运行后 `AnnotationEditorVm.projection` 含 `bgm_usage_windows` 与 `bgm`(含 beats/drops)。

- [ ] **Step 1: 写失败测试**

```python
# tests/api/test_bgm_annotation_run.py
# 用现有 api conftest（CUTAGENT_ALLOW_SANDBOX_FALLBACK=1）创建一个 bgm 资产，
# rerun 标注（无真实 omni profile -> 降级），断言 projection 暴露结构：
def test_bgm_rerun_exposes_windows_projection(client_with_bgm_asset):
    app_client, asset_id, case_id = client_with_bgm_asset
    resp = app_client.post(f"/cases/{case_id}/media/{asset_id}/annotation:rerun", json={"force": True})
    assert resp.status_code == 200
    editor = app_client.get(f"/media/{asset_id}/annotation").json()  # 用真实路由 grep 确认
    assert "bgm" in editor["projection"]
    assert "bgm_usage_windows" in editor["projection"]
    assert "beats" in editor["projection"]["bgm"]
```

> 路由/构造资产细节按现有 api 测试模式（grep `annotation:rerun` 或 `annotations.rerun` 在 tests/api 与 routers/media.py），把断言聚焦在「projection 暴露 bgm_usage_windows + bgm.beats」。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/api/test_bgm_annotation_run.py -q`
Expected: FAIL（projection 无 bgm_usage_windows）

- [ ] **Step 3: 改 `_run_bgm_annotation` / `_run_sqlalchemy_bgm_annotation`**

- candidates：`capability == "audio.understanding"`；`resolve_audio_profile(...)`。
- `audio_url_for_window`：构造一个闭包，对 `(start,end)`：用 `extract_audio_segment` 切到临时文件→`store_file(object_store(request), tmp, purpose="bgm-clip")`→`object_store(request).signed_url(stored.ref.uri).url`→返回。失败返回 None（降级）。整轨本地路径用现有 `_local_audio_path` / `_sqlalchemy_local_audio_path`。无本地音频则 `audio_url_for_window=None`（纯客观）。
- 调 `annotate_bgm(..., audio_profile=profile, audio_url_for_window=urlizer, ...)`。
- 持久化：`projection = build_projection(annotation, asset, ...)` 现已含 `bgm_usage_windows`（Task 8）；再 `projection["bgm"] = bgm_report`（含 beats/drops，来自 `canonical["quality_report"]["bgm"]`）。`editable_paths` 加 `"/canonical/bgm_usage_windows"`。
- `usable`：`(not failed) and audio_profile is not None and bool(bgm_report.get("..."))` 或「至少 1 条 window」。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/api/test_bgm_annotation_run.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/services/asset_annotation.py tests/api/test_bgm_annotation_run.py
git commit -m "feat(api): BGM annotation wires audio.understanding gating + clip presign + windows projection"
```

---

### Task 8: Patch 支持编辑 `bgm_usage_windows` + projection 暴露

**Files:**
- Modify: `apps/api/services/annotation_patch.py`（`_BGM_WINDOW_PATHS` + `_validate_bgm_windows` + `apply_patch` 合并分支 + `build_projection` 加 `bgm_usage_windows`）
- Test: `tests/api/test_annotation_patch_bgm.py`

**Interfaces:**
- Consumes: Task1 `BgmUsageWindowV4`
- Produces: patch path `/canonical/bgm_usage_windows` 校验+并入 canonical；projection 含 `bgm_usage_windows`

- [ ] **Step 1: 写失败测试**

```python
# tests/api/test_annotation_patch_bgm.py
from apps.api.services.annotation_patch import apply_patch, build_projection
from packages.core import contracts as c


def _bgm_asset():
    return c.MediaAssetRecord(id="a", case_id="c", kind="bgm", title="t",
                              annotation_status="annotated", usable=True)  # 补齐必填字段


def test_build_projection_includes_bgm_windows():
    meta = c.AnnotationMetaV4(asset_id="a", case_id="c", material_type="bgm", duration=80.0)
    ann = c.AnnotationV4(meta=meta, bgm_usage_windows=[
        c.BgmUsageWindowV4(segment_id="w", start=45.0, end=75.0, duration=30.0)])
    proj = build_projection(ann, _bgm_asset())
    assert proj["bgm_usage_windows"][0]["segment_id"] == "w"


def test_patch_bgm_windows_valid_round_trip():
    canonical = c.AnnotationV4(
        meta=c.AnnotationMetaV4(asset_id="a", case_id="c", material_type="bgm", duration=80.0)
    ).model_dump(mode="json")
    new_canonical, new_proj = apply_patch(
        canonical=canonical, projection={}, asset=_bgm_asset(),
        operations=[{"op": "replace", "path": "/canonical/bgm_usage_windows",
                     "value": [{"segment_id": "w", "start": 45.0, "end": 75.0,
                                "duration": 30.0, "role": "climax", "mood": "燃"}]}],
    )
    assert new_canonical["bgm_usage_windows"][0]["role"] == "climax"
    assert new_proj["bgm_usage_windows"][0]["mood"] == "燃"


def test_patch_bgm_windows_out_of_bounds_rejected():
    import pytest
    canonical = c.AnnotationV4(
        meta=c.AnnotationMetaV4(asset_id="a", case_id="c", material_type="bgm", duration=60.0)
    ).model_dump(mode="json")
    with pytest.raises(Exception):
        apply_patch(canonical=canonical, projection={}, asset=_bgm_asset(),
                    operations=[{"op": "replace", "path": "/canonical/bgm_usage_windows",
                                 "value": [{"segment_id": "w", "start": 50.0, "end": 90.0, "duration": 40.0}]}])
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/api/test_annotation_patch_bgm.py -q`
Expected: FAIL

- [ ] **Step 3: 实现 patch 分支 + projection**

`annotation_patch.py`：
```python
_BGM_WINDOW_PATHS = {"/canonical/bgm_usage_windows", "/projection/bgm_usage_windows"}


def _validate_bgm_windows(raw: Any, duration: float) -> list[c.BgmUsageWindowV4]:
    if not isinstance(raw, list):
        raise _schema_mismatch("bgm_usage_windows 必须是数组。")
    out: list[c.BgmUsageWindowV4] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _schema_mismatch(f"bgm_usage_window[{index}] 必须是对象。")
        data = dict(item)
        data.setdefault("segment_id", f"bgm_edit_{index}")
        if "duration" not in data:
            try:
                data["duration"] = round(float(data.get("end", 0)) - float(data.get("start", 0)), 3)
            except (TypeError, ValueError):
                data["duration"] = 0.0
        try:
            win = c.BgmUsageWindowV4.model_validate(data)
        except Exception as exc:
            raise _schema_mismatch(f"bgm_usage_window[{index}] 不符合 schema: {exc}") from exc
        if duration and duration > 0 and (win.start < 0 or win.end > duration + 1e-6):
            raise NodeExecutionError(
                c.ErrorCode.render_invalid_timeline,
                f"bgm_usage_window[{index}] 时间 [{win.start}, {win.end}] 越界 [0, {duration}]。",
            )
        out.append(win)
    return out
```

`apply_patch`：在循环里加 `elif path in _BGM_WINDOW_PATHS: structural_bgm = _validate_bgm_windows(value, duration)`（声明 `structural_bgm: list[...] | None = None`）；在合并块把 `structural_bgm` 并入 `annotation.model_copy(update={..., "bgm_usage_windows": structural_bgm if structural_bgm is not None else annotation.bgm_usage_windows})`，并把合并触发条件加上 `or structural_bgm is not None`。

`build_projection`：在 projection dict 加：
```python
        "bgm_usage_windows": [w.model_dump(mode="json") for w in annotation.bgm_usage_windows],
```
并把 `bgm_usage_windows` 加进 `apply_patch` 末尾的 `_BUILT` 集合。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/api/test_annotation_patch_bgm.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/api/services/annotation_patch.py tests/api/test_annotation_patch_bgm.py
git commit -m "feat(api): patch + projection support for bgm_usage_windows"
```

---

### Task 9: 重生成 OpenAPI + schema.d.ts

**Files:**
- Modify: `apps/web/src/api/openapi.json`、`apps/web/src/api/schema.d.ts`（生成物）

- [ ] **Step 1: 重生成**

```bash
python scripts/export_openapi.py
(cd apps/web && npm run generate:api)
```

- [ ] **Step 2: 确认 `BgmUsageWindowV4` 进入 schema**

Run: `grep -c "bgm_usage_windows\|BgmUsageWindowV4" apps/web/src/api/openapi.json`
Expected: > 0

- [ ] **Step 3: Commit**

```bash
git add apps/web/src/api/openapi.json apps/web/src/api/schema.d.ts
git commit -m "chore(web): regenerate OpenAPI + schema for bgm_usage_windows"
```

---

### Task 10: 前端适配器 `annotationV4.ts`

**Files:**
- Modify: `apps/web/src/utils/annotationV4.ts`（加类型 `BgmUsageWindow` + `canonicalToBgmWindows` / `bgmWindowsToCanonical`）
- Test: 无单测，靠 `npm run build`（见 Task 11）

**Interfaces:**
- Produces:
  - `interface BgmUsageWindow { segment_id; start; end; duration; role; drop_anchor_sec?; energy; mood; scene_fit; avoid_scene; reason; confidence; source }`
  - `canonicalToBgmWindows(canonical?: unknown): BgmUsageWindow[]`（防御式读 `canonical.bgm_usage_windows`）
  - `bgmWindowsToCanonical(windows: BgmUsageWindow[]): AnnotationClip[]`-风格的纯对象数组（用于 patch `/canonical/bgm_usage_windows`）

- [ ] **Step 1: 加类型与适配器（镜像 `clipsToSegments`/`canonicalToSegments` 的防御式写法）**

```ts
export type BgmSegmentRole = "hook" | "climax" | "outro" | "general";
const BGM_ROLES: BgmSegmentRole[] = ["hook", "climax", "outro", "general"];

export interface BgmUsageWindow {
  segment_id: string;
  start: number; end: number; duration: number;
  role: BgmSegmentRole;
  drop_anchor_sec: number | null;
  energy: number;
  mood: string;
  scene_fit: string[];
  avoid_scene: string[];
  reason: string;
  confidence?: number;
  source: string;
}

function asBgmRole(v: unknown): BgmSegmentRole {
  const t = cleanString(v).trim().toLowerCase();
  return (BGM_ROLES as string[]).includes(t) ? (t as BgmSegmentRole) : "general";
}

export function canonicalToBgmWindows(canonical?: unknown): BgmUsageWindow[] {
  return asArray(asRecord(canonical).bgm_usage_windows).map((raw) => {
    const w = asRecord(raw);
    return {
      segment_id: cleanString(w.segment_id),
      start: asNumber(w.start), end: asNumber(w.end), duration: asNumber(w.duration),
      role: asBgmRole(w.role),
      drop_anchor_sec: w.drop_anchor_sec == null ? null : asOptionalNumber(w.drop_anchor_sec) ?? null,
      energy: asNumber(w.energy),
      mood: cleanString(w.mood),
      scene_fit: asStringList(w.scene_fit),
      avoid_scene: asStringList(w.avoid_scene),
      reason: cleanString(w.reason),
      confidence: asOptionalNumber(w.confidence),
      source: cleanString(w.source) || "sensor",
    };
  });
}

export function bgmWindowsToCanonical(windows: BgmUsageWindow[]): Record<string, unknown>[] {
  return windows.map((w, i) => {
    const start = asNumber(w.start);
    const end = asNumber(w.end, start) > start ? asNumber(w.end, start) : start + 0.1;
    return {
      segment_id: cleanString(w.segment_id) || `bgm_${i + 1}`,
      start, end, duration: Math.max(0, Number((end - start).toFixed(3))),
      role: asBgmRole(w.role),
      drop_anchor_sec: w.drop_anchor_sec ?? null,
      energy: Math.max(0, Math.min(1, asNumber(w.energy))),
      mood: cleanString(w.mood),
      scene_fit: [...(w.scene_fit ?? [])],
      avoid_scene: [...(w.avoid_scene ?? [])],
      reason: cleanString(w.reason),
      confidence: w.confidence ?? 0.8,
      source: cleanString(w.source) || "sensor",
    };
  });
}
```

- [ ] **Step 2: 类型检查**

Run: `cd apps/web && npx tsc -b --noEmit`（或留待 Task 11 的 build）
Expected: 无新错误

- [ ] **Step 3: Commit**

```bash
git add apps/web/src/utils/annotationV4.ts
git commit -m "feat(web): annotationV4 adapters for bgm_usage_windows"
```

---

### Task 11: 前端编辑器 `isBgm` 分支（展示 + 编辑）

**Files:**
- Modify: `apps/web/src/components/annotation/AnnotationEditorModal.tsx`（加 `isBgm` 判定 + BGM 摘要/片段卡片/编辑表单 + beat 时间轴条）
- Test: 靠 `cd apps/web && npm run build`

**Interfaces:**
- Consumes: Task10 `canonicalToBgmWindows`/`bgmWindowsToCanonical`、`projection.bgm`(beats/drops/track摘要)

- [ ] **Step 1: 加 `isBgm` 判定 + 数据读取**

在 `AnnotationEditorModal`：
```ts
const isBgm = editor?.asset.kind === "bgm";
const bgmWindows = useMemo(() => (canonical ? canonicalToBgmWindows(canonical) : []), [canonical]);
const bgmReport = (projection.bgm ?? {}) as Record<string, unknown>;
```

- [ ] **Step 2: 渲染只读 BGM 面板（当 `isBgm`，替代 b-roll/portrait 的 `ReadonlyStructurePanel`）**

新增 `BgmStructurePanel`：渲染
- track 摘要 chips：从 `bgmReport` 读 `bpm/tempo_bucket/genre/loudness_lufs/mood`；
- beat 时间轴条：`bgmReport.beats`(number[]) 画刻度、`bgmReport.drops` 高亮、各 window 按 role 上色为区间（用百分比定位 `start/end / totalDuration`）；
- window 卡片列表：`起止秒(formatWindow) · 时长 · role(中文映射) · 能量 · drop锚点 · scene_fit 标签 · reason`。
- 在主 JSX 里：`{isBgm ? <BgmStructurePanel .../> : editing ? <StructuredAnnotationForm.../> : <ReadonlyStructurePanel.../>}`（BGM 的编辑见 Step 3；若时间紧可先只读，编辑表单作为同 Task 后续 step）。

加 role 中文映射常量：
```ts
const BGM_ROLE_LABELS: Record<string, string> = {
  hook: "开场钩子", climax: "高潮", outro: "收尾", general: "通用铺底",
};
```

- [ ] **Step 3: 加 BGM 编辑表单 `BgmAnnotationForm`（增删窗 / start-end / role / drop / mood / scene）**

镜像 `StructuredAnnotationForm` 的字段控件（`NumberField`/`TextField`/`SelectField`/`TextareaField` 已存在可复用）。保存走 patch：
```ts
return api.annotations.patch(assetId, {
  etag: editor.etag,
  patch: { operations: [
    { op: "replace", path: "/canonical/bgm_usage_windows", value: bgmWindowsToCanonical(bgmForm) },
    { op: "replace", path: "/projection/usable", value: form.usable },
  ]},
});
```
编辑/只读切换复用现有 `editing` state；BGM 模式下「手动编辑」按钮切到 `BgmAnnotationForm`。

- [ ] **Step 4: 构建验证**

```bash
cd apps/web && npm run build
```
Expected: 构建成功（`tsc -b && vite build` 通过，含 `contracts/*.typecheck.ts`）

- [ ] **Step 5: Commit**

```bash
git add apps/web/src/components/annotation/AnnotationEditorModal.tsx
git commit -m "feat(web): BGM annotation editor (isBgm branch: summary + windows + beat timeline + edit)"
```

---

### Task 12: 全量门禁 + 收尾

**Files:** 无新增

- [ ] **Step 1: 后端相关域测试**

Run: `python -m pytest tests/contract tests/media/annotation tests/providers/test_dashscope_omni.py tests/api/test_bgm_annotation_run.py tests/api/test_annotation_patch_bgm.py -q`
Expected: 全 PASS

- [ ] **Step 2: 前端构建**

Run: `cd apps/web && npm run build`
Expected: 成功

- [ ] **Step 3: CI 门禁（含契约漂移校验）**

Run: `scripts/ci_gate.sh`
Expected: PASS（若本地报 openapi 漂移，按记忆 cutagent-openapi-drift-env-sensitive 以 CI pinned venv 为准，不本地强修）

- [ ] **Step 4: 最终 commit（如有收尾）**

```bash
git add -A
git commit -m "test: BGM clip annotation full gate green"
```

---

## Self-Review

**Spec coverage（逐条对照 spec）：**
- §3 契约 BgmUsageWindowV4 + bgm_usage_windows + 边界校验 → Task 1 ✅
- §3 beats/drops 入 quality_report → Task 6（assembly）✅
- §4 新增 audio.understanding（插件/注册/seed/pricing/gating）→ Task 4/5/6 ✅
- §4 model `qwen3.5-omni-plus`、流式、input_audio URL → Task 4/5 ✅
- §5 librosa 切 clip（拥有时间戳）+ 每段真听 → Task 2/3/6 ✅
- §6 降级三/四路径 → Task 6 测试覆盖（无 librosa / 无 audio profile / 单窗失败）✅
- §7 前端 isBgm（摘要/时间轴/卡片/编辑）→ Task 10/11 ✅
- §8 editable_paths + patch 校验 → Task 7/8 ✅
- §9 测试 + ci_gate → Task 12 ✅；OpenAPI 重生成 → Task 9 ✅

**Placeholder 扫描：** 价目单价标注「实现时核定」属真实占位但有默认值可跑；fixtures 处给了 grep 指引而非凭空命名（因现有 bgm/provider 测试 fixtures 命名需到位核对）。无 TODO/TBD 裸占位。

**Type 一致性：** `BgmUsageWindowV4` 字段在 Task1 定义，Task6/8/10 引用一致（segment_id/start/end/duration/role/drop_anchor_sec/energy/mood/scene_fit/avoid_scene/reason/confidence/source）；`resolve_audio_profile`/`audio_url_for_window` 签名 Task6 定义、Task7 调用一致；patch path `/canonical/bgm_usage_windows` 在 Task7(editable_paths)/Task8(校验)/Task11(前端保存) 一致。

**待执行者核对（非阻断）：** ① 现有 bgm 测试与 provider 测试的 fixture/mock-gateway 命名（grep）；② provider profile `enabled` 字段写法；③ ProviderCapability 是否有独立 seed 列表；④ media 标注 rerun 的真实路由路径。
