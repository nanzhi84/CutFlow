# BGM Full Segmentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace BGM "recommended windows" with full-track BGM segment annotation, surface those segments in the editor/player, and make production BGM selection/mixing use the selected segment.

**Architecture:** This is a breaking contract migration: canonical `AnnotationV4` uses `bgm_segments` only. The old `bgm_usage_windows` field is removed from contracts, adapters, projections, editable paths, tests, and generated API schema; old rows are intentionally refreshed by rerunning annotation instead of read-time compatibility. Librosa owns every timestamp by producing contiguous full-track segments; Qwen-Omni only enriches each segment with semantic labels. Production planning carries `segment_id/source_start/source_end` from material candidates into `BgmPlan`, and the final ffmpeg mix starts from that source offset.

**Tech Stack:** Python/Pydantic contracts, librosa, FastAPI service layer, pytest, React/TypeScript/Vite frontend, ffmpeg filter graph, OpenAPI schema generation.

---

## Non-Negotiable Requirements

- No compatibility reader/writer for `bgm_usage_windows`; grep must show no live code references after migration, except historical docs.
- `AnnotationV4` canonical field is `bgm_segments: list[BgmSegmentV4]`.
- Librosa segmentation covers the whole non-empty BGM track with contiguous, non-overlapping segments.
- Qwen-Omni listens to every generated BGM segment when an active `audio.understanding` profile and segment URL are available.
- BGM editor shows BGM segments on the same player progress rail used by visual clips; the standalone dense beat timeline is not the primary UI.
- Production BGM candidate selection is segment-aware and carries `segment_id`, `source_start`, `source_end`, `duration`, and semantic tags.
- `BgmPlan` carries the chosen segment, and `SubtitleAndBgmMix` trims BGM from that segment start before mixing.
- Local Windows/WSL browser/video verification must pass before touching Mac mini.
- Only after local verification: push to `origin/main`, wait for CI green, then fast-forward Mac mini and reannotate BGM there.

## File Structure

- Modify `packages/core/contracts/media.py`: replace `BgmUsageWindowV4` with `BgmSegmentV4`; replace `AnnotationV4.bgm_usage_windows` with `bgm_segments`.
- Modify `packages/core/contracts/__init__.py`: export `BgmSegmentV4` and stop exporting `BgmUsageWindowV4`.
- Modify `packages/core/contracts/artifacts.py`: extend `BgmPlan` with `segment_id`, `source_start`, `source_end`, `duration`, `mood`, `scene_fit`, `reason`.
- Modify `packages/media/annotation/bgm.py`: replace candidate-window extraction with full-track segment extraction and per-segment listening.
- Modify `apps/api/services/annotation_patch.py`: patch/build projection for `/canonical/bgm_segments` only.
- Modify `apps/api/services/asset_annotation.py`: persist BGM annotations using `bgm_segments` and editable path `/canonical/bgm_segments`.
- Modify `packages/production/pipeline/nodes/material_pack_planning.py`: build BGM candidates from `AnnotationV4.bgm_segments`.
- Modify `packages/production/pipeline/nodes/style_planning.py`: choose a BGM segment and write it into `BgmPlan`.
- Modify `packages/production/pipeline/_selection.py`: record BGM `clip_id=segment_id`.
- Modify `packages/production/pipeline/_ffmpeg.py`: support `bgm_source_start` in BGM filter graph.
- Modify `packages/production/pipeline/nodes/subtitle_and_bgm_mix.py`: pass selected BGM segment offset to ffmpeg.
- Modify `apps/web/src/utils/annotationV4.ts`: rename adapters to `canonicalToBgmSegments` / `bgmSegmentsToCanonical`; no old-field fallback.
- Modify `apps/web/src/components/annotation/AnnotationEditorModal.tsx`: BGM-specific editor, segment rail, cards, edit form, and metrics.
- Regenerate `apps/web/src/api/openapi.json` and `apps/web/src/api/schema.d.ts`.
- Update tests in `tests/contract`, `tests/media/annotation`, `tests/api`, `tests/frontend`, `tests/production`, and `tests/providers`.

## Task 1: Contract Break To `bgm_segments`

**Files:**
- Modify: `packages/core/contracts/media.py`
- Modify: `packages/core/contracts/__init__.py`
- Modify: `packages/core/contracts/artifacts.py`
- Replace test: `tests/contract/test_bgm_usage_window.py` -> `tests/contract/test_bgm_segment.py`

- [ ] **Step 1: Write the failing contract tests**

Create/replace `tests/contract/test_bgm_segment.py` with tests asserting:

```python
from packages.core import contracts as c


def test_bgm_segment_valid_and_derives_duration():
    segment = c.BgmSegmentV4(
        segment_id="seg_1",
        start=10.0,
        end=70.0,
        duration=0.0,
        role="climax",
        drop_anchor_sec=38.0,
        energy=0.72,
        mood="燃",
        scene_fit=["高光混剪"],
        source="sensor+audio",
    )
    assert segment.duration == 60.0
    assert segment.role == c.BgmSegmentRole.climax


def test_annotation_v4_bgm_segments_bounds_enforced():
    meta = c.AnnotationMetaV4(asset_id="bgm", case_id="case", material_type="bgm", duration=90.0)
    with pytest.raises(ValueError, match="bgm_segment"):
        c.AnnotationV4(
            meta=meta,
            bgm_segments=[
                c.BgmSegmentV4(segment_id="bad", start=80.0, end=95.0, duration=15.0)
            ],
        )


def test_annotation_v4_has_no_bgm_usage_windows_field():
    fields = c.AnnotationV4.model_fields
    assert "bgm_segments" in fields
    assert "bgm_usage_windows" not in fields
    assert not hasattr(c, "BgmUsageWindowV4")
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest tests/contract/test_bgm_segment.py -q`

Expected: FAIL because `BgmSegmentV4` / `bgm_segments` do not exist and old `BgmUsageWindowV4` still exists.

- [ ] **Step 3: Implement the contract**

Change `media.py`:
- keep `BgmSegmentRole`, but update docstring to "BGM full-track segment role";
- delete `BgmUsageWindowV4`;
- add `BgmSegmentV4` with the same validated time fields plus semantic fields;
- add `bgm_segments: list[BgmSegmentV4]` to `AnnotationV4`;
- validate `bgm_segments` bounds with error prefix `bgm_segment`.

Change `__init__.py` imports/`__all__`.

Change `artifacts.py`:

```python
class BgmPlan(ContractModel):
    enabled: bool = True
    asset_id: str | None = None
    segment_id: str | None = None
    source_start: float | None = None
    source_end: float | None = None
    duration: float | None = None
    mood: str = ""
    scene_fit: list[str] = Field(default_factory=list)
    reason: str = ""
    volume: float = 0.25
    auto_mix: bool = True
```

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/contract/test_bgm_segment.py -q`

Expected: PASS.

## Task 2: Librosa Full-Track Segmentation

**Files:**
- Modify: `packages/media/annotation/bgm.py`
- Modify: `tests/media/annotation/test_bgm_sensor.py`

- [ ] **Step 1: Write failing sensor tests**

Replace candidate-window tests with:

```python
def test_segment_audio_track_covers_full_track_with_contiguous_segments():
    duration = 230.0
    times = [float(i) for i in range(231)]
    energy = [0.2] * 60 + [0.55] * 60 + [0.9] * 70 + [0.35] * 41
    beats = [round(i * 0.5, 3) for i in range(1, 460)]
    drops = [62.0, 128.0]

    segments = bgm.segment_audio_track(duration, energy, times, beats, drops)

    assert segments[0]["start"] == 0.0
    assert abs(segments[-1]["end"] - duration) < 1e-6
    assert len(segments) >= 4
    for prev, cur in zip(segments, segments[1:]):
        assert abs(prev["end"] - cur["start"]) <= 1e-6
    assert all(s["duration"] >= 24.0 for s in segments[:-1])
    assert any(s["duration"] >= 55.0 for s in segments)
    assert any(s["role_hint"] == "climax" for s in segments)


def test_segment_audio_track_short_track_is_single_segment():
    segments = bgm.segment_audio_track(
        42.0,
        [0.5] * 43,
        [float(i) for i in range(43)],
        [float(i) for i in range(43)],
        [],
    )
    assert segments == [{
        "start": 0.0,
        "end": 42.0,
        "duration": 42.0,
        "energy": 0.5,
        "drop_anchor": None,
        "role_hint": "hook",
    }]
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest tests/media/annotation/test_bgm_sensor.py -q`

Expected: FAIL because `segment_audio_track` does not exist and old `candidate_windows` tests still expect capped windows.

- [ ] **Step 3: Implement `segment_audio_track`**

Implementation rules:
- if `duration <= 0`, return `[]`;
- if `duration <= 70`, return one full segment;
- derive beat-grid boundaries when beats exist; target about 60 seconds, snap to existing beat times, round to 3 decimals;
- fallback to fixed 60-second boundaries when beats are unavailable;
- merge tails shorter than 24 seconds into the previous segment;
- split never creates overlaps or gaps;
- calculate mean energy per segment with `_mean_between`;
- `role_hint` is `hook` for the first segment, `outro` for the last low-energy segment, `climax` when a drop falls inside or segment energy is top-quartile, otherwise `general`;
- `drop_anchor` is the first drop inside the segment, snapped to beats.

Replace `_extract_librosa_features(... "candidate_windows": windows)` with `"segments": segments`.

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/media/annotation/test_bgm_sensor.py -q`

Expected: PASS.

## Task 3: Annotate Every BGM Segment

**Files:**
- Modify: `packages/media/annotation/bgm.py`
- Modify: `tests/media/annotation/test_bgm.py`
- Modify: `tests/media/annotation/test_bgm_annotate.py`
- Modify: `tests/providers/test_provider_pipeline_integration.py`

- [ ] **Step 1: Write failing annotation tests**

Update test features to use:

```python
FEATURES = {
    "librosa_available": True,
    "bpm": 128.0,
    "energy": 0.6,
    "tempo_bucket": "fast",
    "loudness_lufs": -14.0,
    "beats": [0.0, 0.5, 45.0, 58.0, 75.0, 120.0],
    "drops": [58.0],
    "segments": [
        {"start": 0.0, "end": 60.0, "duration": 60.0, "energy": 0.45, "drop_anchor": None, "role_hint": "hook"},
        {"start": 60.0, "end": 120.0, "duration": 60.0, "energy": 0.8, "drop_anchor": 75.0, "role_hint": "climax"},
    ],
}
```

Assertions:
- `len(ann.bgm_segments) == 2`;
- plugin call count equals number of segments when audio profile is configured;
- every plugin input uses `segment_id` and `audio_seconds`;
- no assertion reads `bgm_usage_windows`;
- no-librosa degrade produces `ann.bgm_segments == []`.

- [ ] **Step 2: Run and verify RED**

Run:

```bash
pytest tests/media/annotation/test_bgm.py tests/media/annotation/test_bgm_annotate.py -q
```

Expected: FAIL because production still reads `candidate_windows` and writes `bgm_usage_windows`.

- [ ] **Step 3: Implement segment annotation**

In `bgm.py`:
- rename `_sensor_windows` to `_sensor_segments`;
- read `raw_segments = features.get("segments") or []`;
- create `BgmSegmentV4(segment_id=f"bgm_segment_{index+1}", ...)`;
- rename `_listen_to_window` to `_listen_to_segment`;
- prompt text says "BGM段落";
- `_bgm_quality_report` stores `segment_count`, `annotated_coverage_sec`, `annotated_coverage_ratio`, and `recommended_segment_ids`, not `candidate_window_count`;
- `_annotation_with_segments` writes `bgm_segments=segments`.

- [ ] **Step 4: Verify GREEN**

Run the same pytest command.

Expected: PASS.

## Task 4: API Projection And Patch Migration

**Files:**
- Modify: `apps/api/services/annotation_patch.py`
- Modify: `apps/api/services/asset_annotation.py`
- Modify: `tests/api/test_annotation_patch_bgm.py`
- Modify: `tests/api/test_bgm_annotation_run.py`
- Modify: `tests/api/test_sqlalchemy_bgm_annotation_routing.py`

- [ ] **Step 1: Write failing API tests**

Update tests to assert:
- projection key is `bgm_segments`;
- editable path contains `/canonical/bgm_segments`;
- patch path `/canonical/bgm_segments` updates canonical/projection;
- patch path `/canonical/bgm_usage_windows` is ignored as a non-structural note, or rejected if routed to structural validation is preferred; it must not update canonical BGM segments.

Preferred assertion:

```python
assert "bgm_usage_windows" not in projection
assert projection["bgm_segments"][0]["segment_id"] == "bgm_10.0_70.0"
assert "/canonical/bgm_segments" in editor.editable_paths
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
pytest tests/api/test_annotation_patch_bgm.py tests/api/test_bgm_annotation_run.py tests/api/test_sqlalchemy_bgm_annotation_routing.py -q
```

Expected: FAIL because projection/editable paths still use `bgm_usage_windows`.

- [ ] **Step 3: Implement API changes**

In `annotation_patch.py`:
- rename `_BGM_WINDOW_PATHS` to `_BGM_SEGMENT_PATHS = {"/canonical/bgm_segments", "/projection/bgm_segments"}`;
- validate via `c.BgmSegmentV4`;
- `build_projection` writes `"bgm_segments": [...]`;
- `_BUILT` includes `bgm_segments` and not `bgm_usage_windows`;
- `annotation.model_copy(update={"bgm_segments": structural_bgm, ...})`.

In `asset_annotation.py`:
- usability uses `bool(annotation.bgm_segments)`;
- editable paths use `/canonical/bgm_segments`;
- comments say segment, not window.

- [ ] **Step 4: Verify GREEN**

Run same pytest command.

Expected: PASS.

## Task 5: Frontend Model And Editor UI

**Files:**
- Modify: `apps/web/src/utils/annotationV4.ts`
- Modify: `apps/web/src/components/annotation/AnnotationEditorModal.tsx`
- Modify: `apps/web/src/components/ui/VideoPlayer.tsx` only if BGM roles need colors there
- Modify: `tests/frontend/test_annotation_v4_model.py`

- [ ] **Step 1: Write failing frontend model test**

Update `tests/frontend/test_annotation_v4_model.py` inline TypeScript to assert:
- `canonicalToBgmSegments` reads `bgm_segments`;
- `bgmSegmentsToCanonical` writes `bgm_segments` payload values;
- no adapter reads `bgm_usage_windows`.

Required TypeScript fragment:

```ts
import { bgmSegmentsToCanonical, canonicalToBgmSegments } from "./src/utils/annotationV4";

const canonical = {
  bgm_segments: [
    { segment_id: "seg1", start: 0, end: 60, duration: 60, role: "hook", mood: "hope" },
  ],
};

const segments = canonicalToBgmSegments(canonical);
assert.equal(segments.length, 1);
assert.equal(segments[0].segment_id, "seg1");
assert.deepEqual(bgmSegmentsToCanonical(segments)[0].duration, 60);
assert.equal(canonicalToBgmSegments({ bgm_usage_windows: canonical.bgm_segments }).length, 0);
```

- [ ] **Step 2: Run and verify RED**

Run: `pytest tests/frontend/test_annotation_v4_model.py -q`

Expected: FAIL because functions are still named windows and read old field.

- [ ] **Step 3: Implement frontend model and UI**

In `annotationV4.ts`:
- define `BgmSegment`;
- rename constants/functions to segment naming;
- `canonicalToBgmSegments(canonical)` reads only `canonical["bgm_segments"]`;
- `bgmSegmentsToCanonical(segments)` returns array values for `/canonical/bgm_segments`.

In `AnnotationEditorModal.tsx`:
- header badge shows `BGM` for BGM assets;
- hide the "裁剪无效" action for BGM assets;
- for BGM, compute `playerSegments` from `bgmSegments` and pass them to `VideoPlayer`;
- use `activeBgmSegmentId` through the existing `activeSegmentId` state;
- replace `BgmStructurePanel` with a segment-focused panel:
  - summary chips;
  - coverage/segment count;
  - segment cards under title `音乐段落`;
  - no dense standalone beat timeline as the main UI;
  - optional drop markers as chips inside cards only.
- edit form saves `/canonical/bgm_segments`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
pytest tests/frontend/test_annotation_v4_model.py -q
cd apps/web && npm run build -- --configLoader runner
```

Expected: PASS/build succeeds.

## Task 6: BGM Segment-Aware Production Selection

**Files:**
- Modify: `packages/production/pipeline/nodes/material_pack_planning.py`
- Modify: `packages/production/pipeline/nodes/style_planning.py`
- Modify: `packages/production/pipeline/_selection.py`
- Modify: `tests/production/test_material_planning.py` or create `tests/production/test_bgm_segment_selection.py`
- Modify: `tests/production/test_selection_ledger.py`

- [ ] **Step 1: Write failing production tests**

Create `tests/production/test_bgm_segment_selection.py` with:

```python
def test_material_pack_exposes_bgm_segments_as_candidates():
    # repository has one bgm asset with AnnotationV4.bgm_segments containing seg_intro and seg_climax
    # run MaterialPackPlanning
    # assert candidates include metadata clip_id/source_start/source_end/duration/mood/scene_fit
```

Create a style-planning test:

```python
def test_style_planning_carries_selected_bgm_segment():
    material = {
        "bgm_candidates": [{
            "asset_id": "bgm1",
            "score": 2.0,
            "metadata": {
                "clip_id": "bgm_segment_2",
                "source_start": 60.0,
                "source_end": 120.0,
                "duration": 60.0,
                "mood": "燃",
                "scene_fit": ["高光"],
                "reason": "60秒高潮段",
            },
        }]
    }
    # run StylePlanning
    # assert payload["bgm"]["segment_id"] == "bgm_segment_2"
    # assert payload["bgm"]["source_start"] == 60.0
```

Update ledger test:

```python
assert entry.medium == "bgm"
assert entry.clip_id == "bgm_segment_2"
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
pytest tests/production/test_bgm_segment_selection.py tests/production/test_selection_ledger.py -q
```

Expected: FAIL because BGM is asset-grain and ledger writes `clip_id=None`.

- [ ] **Step 3: Implement segment-aware selection**

In `material_pack_planning.py`:
- collect `bgm_annotations` via `repo.annotation_v4_for_asset(asset.id)`;
- build one candidate per `annotation.bgm_segments`;
- skip assets with no segments;
- score by availability, segment duration, semantic completeness, and recency;
- carry metadata:

```python
{
    "clip_id": segment.segment_id,
    "source_start": segment.start,
    "source_end": segment.end,
    "duration": segment.duration,
    "role": segment.role.value,
    "mood": segment.mood,
    "scene_fit": list(segment.scene_fit),
    "energy": segment.energy,
    "reason": segment.reason,
}
```

In `style_planning.py`:
- choose requested `bgm_id` by matching candidate asset first, otherwise top candidate;
- write `BgmPlan` fields from selected candidate metadata.

In `_selection.py`:
- for BGM, pass `bgm.get("segment_id")` as `clip_id`.

- [ ] **Step 4: Verify GREEN**

Run same pytest command.

Expected: PASS.

## Task 7: Mix The Selected BGM Segment

**Files:**
- Modify: `packages/production/pipeline/_ffmpeg.py`
- Modify: `packages/production/pipeline/nodes/subtitle_and_bgm_mix.py`
- Modify: `tests/production/test_subtitle_font_and_bgm_mix.py`

- [ ] **Step 1: Write failing ffmpeg tests**

Add:

```python
def test_build_bgm_audio_filters_starts_from_selected_source_offset():
    filters = _ffmpeg._build_bgm_audio_filters(
        bgm_volume=0.2,
        duration=5.0,
        auto_mix=False,
        fade_in=0.0,
        fade_out=0.0,
        bgm_source_start=60.0,
    )
    assert "atrim=60.000:65.000" in filters
    assert "asetpts=PTS-STARTPTS" in filters
```

Add a node-level test that `SubtitleAndBgmMix` reads `style["bgm"]["source_start"]` and passes it to `render_final_media`.

- [ ] **Step 2: Run and verify RED**

Run: `pytest tests/production/test_subtitle_font_and_bgm_mix.py -q`

Expected: FAIL because filters always trim `0:duration`.

- [ ] **Step 3: Implement segment trim**

In `_ffmpeg.py`:
- add parameter `bgm_source_start: float = 0.0` to `_build_bgm_audio_filters` and `render_final_media`;
- compute `bgm_trim_start = max(0.0, bgm_source_start)`;
- compute `bgm_trim_end = bgm_trim_start + duration`;
- use `atrim={bgm_trim_start:.3f}:{bgm_trim_end:.3f}`;
- keep `-stream_loop -1` so a segment near the end can still cover the output if the source loops.

In `subtitle_and_bgm_mix.py`:
- `bgm_source_start=float((bgm_plan or {}).get("source_start") or 0.0)`.

- [ ] **Step 4: Verify GREEN**

Run same pytest command.

Expected: PASS.

## Task 8: Generated API Artifacts And Repo-Wide Cleanup

**Files:**
- Modify generated: `apps/web/src/api/openapi.json`
- Modify generated: `apps/web/src/api/schema.d.ts`
- Update all tests that still reference old field.

- [ ] **Step 1: Verify old field references remain**

Run:

```bash
rg -n "bgm_usage_windows|BgmUsageWindow|candidate_windows|BGM 使用窗口" packages apps tests --glob '!docs/**'
```

Expected: references exist before cleanup.

- [ ] **Step 2: Generate API schema**

Run:

```bash
python3 scripts/export_openapi.py
cd apps/web && npm run generate:api
```

- [ ] **Step 3: Remove/update remaining old-field references**

All live code/tests must use `bgm_segments`, `BgmSegmentV4`, and `音乐段落`.

- [ ] **Step 4: Verify old live references are gone**

Run:

```bash
rg -n "bgm_usage_windows|BgmUsageWindow|candidate_windows|BGM 使用窗口" packages apps tests --glob '!docs/**'
```

Expected: no matches.

## Task 9: Focused Verification

**Files:** no production edits unless tests expose bugs.

- [ ] **Step 1: Run backend focused suite**

Run:

```bash
pytest \
  tests/contract/test_bgm_segment.py \
  tests/media/annotation/test_bgm_sensor.py \
  tests/media/annotation/test_bgm.py \
  tests/media/annotation/test_bgm_annotate.py \
  tests/api/test_annotation_patch_bgm.py \
  tests/api/test_bgm_annotation_run.py \
  tests/api/test_sqlalchemy_bgm_annotation_routing.py \
  tests/providers/test_provider_pipeline_integration.py \
  tests/production/test_bgm_segment_selection.py \
  tests/production/test_subtitle_font_and_bgm_mix.py \
  tests/production/test_selection_ledger.py \
  -q
```

- [ ] **Step 2: Run frontend build**

Run:

```bash
cd apps/web && npm run build -- --configLoader runner
```

- [ ] **Step 3: Run repo quality checks**

Run:

```bash
git diff --check
bash -n scripts/dev_up.sh
```

## Task 10: Subagent Adversarial Review

**Files:** review only unless fixing accepted issues.

- [ ] **Step 1: Dispatch backend reviewer**

Ask one subagent to review contracts, annotation algorithm, API patch, production BGM planning, selection ledger, and ffmpeg segment trim. It must look for old-field compatibility, partial BGM asset-grain remnants, timeline bugs, and test gaps.

- [ ] **Step 2: Dispatch frontend reviewer**

Ask one subagent to review BGM editor behavior, player segment rail, edit/save paths, generated schema, and mobile layout risks.

- [ ] **Step 3: Fix Critical/Important findings**

Fix reviewer findings with new failing tests first when behavior changes.

## Task 11: Local Windows/WSL Manual Verification Before Mac Mini

**Files:** no code edits unless verification exposes bugs.

- [ ] **Step 1: Start local stack**

Run local API/web stack in this Windows/WSL workspace. Do not deploy or sync Mac mini.

- [ ] **Step 2: Self-run one BGM annotation**

Use a real BGM asset in the local environment and rerun annotation. Verify canonical contains `bgm_segments`, not `bgm_usage_windows`.

- [ ] **Step 3: Browser check**

Open the BGM annotation editor in a browser. Verify:
- header says BGM;
- the player progress bar shows segment blocks;
- right panel says `音乐段落`;
- editing saves `/canonical/bgm_segments`;
- no standalone dense beat wall dominates the UI.

- [ ] **Step 4: Generate one local video**

Create or rerun a local production video with BGM enabled. Verify:
- `plan.style` artifact has `bgm.segment_id/source_start/source_end`;
- selection ledger records BGM `clip_id`;
- final mp4 has audio;
- selected BGM begins at the chosen source offset, not always 0 seconds.

## Task 12: Push, CI, Mac Mini Sync, Reannotation

**Files:** no code edits unless CI exposes bugs.

- [ ] **Step 1: Commit and push**

Commit the implementation and push to `origin/main` only after local Windows/WSL verification passes.

- [ ] **Step 2: Wait for CI**

Use GitHub/CI status. Do not proceed to Mac mini until required checks are green.

- [ ] **Step 3: Fast-forward Mac mini**

On Mac mini `/Users/wzm/cutagent-genesis`, fast-forward to `origin/main`. Use Clash Verge proxy `127.0.0.1:7897` for git/network if needed; do not change Tailscale/TUN state.

- [ ] **Step 4: Reannotate BGM on Mac mini**

Rerun BGM annotation so stored canonical rows are rewritten with `bgm_segments`.

- [ ] **Step 5: Verify Cloudflare Tunnel entry**

If frontend changed, do not use the old `shuying` SSH/static deploy path. After Mac fast-forward and `scripts/dev_up.sh restart`, verify `https://dev.shuying.cyou/` and `https://dev.shuying.cyou/api/health` through the Mac mini Cloudflare Tunnel.

## Self-Review

- Spec coverage: all user requirements are covered: no old-field compatibility, full方案, subagent adversarial review, local annotation/browser check, local video verification, push+CI, Mac fast-forward+reannotation, and no Mac deployment before local success.
- Placeholder scan: no task uses TBD/TODO/fill later. Browser/manual steps specify concrete evidence.
- Type consistency: canonical field is consistently `bgm_segments`; contract type is `BgmSegmentV4`; plan field is `BgmPlan.segment_id`.
