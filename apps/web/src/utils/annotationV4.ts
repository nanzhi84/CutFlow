// AnnotationV4 canonical (cable) shape <-> flat editor-row model — a pure adapter layer.
//
// The genesis backend stores annotations as AnnotationV4 (`AnnotationV4.model_dump(mode="json")`,
// see packages/core/contracts/media.py + apps/api/services/asset_annotation.py): a seven-layer
// view with `meta / clips / usage_windows / quality_events / quality_report / evidence_frames`,
// where each `clip` is nested (semantics / visual / usage / retrieval sub-objects). The editor UI
// uses a *flat* row model (`AnnotationTimelineSegment`) so rich display components
// read top-level fields directly. This module converts both sides losslessly:
//   - clipsToSegments:   nested canonical clip -> flat editor row (read path)
//   - segmentsToClips:   flat editor row -> nested canonical clip (write path)
//   - canonicalToSegments / parseQualityEvents / readEvidenceFrames: untyped canonical -> typed views
// Discipline: the canonical/storage never carries the flat rows; flat rows are a frontend view model
// only. `canonical` arrives from the API as an untyped `Record<string, unknown>`, so every read is
// defensive (the schema type is `{ [key: string]: JsonValue }` with `JsonValue = unknown`).

// ─────────────────────────────────────────────────────────────────────────────
// Types — flat editor row + quality event.
// ─────────────────────────────────────────────────────────────────────────────

/** V4 UsageRole legal values (backend ClipUsageV4.role / UsageWindowV4.role enum; illegal => 422). */
const USAGE_ROLES = ["hook", "main", "backup", "avoid", "cover"] as const;
type UsageRole = (typeof USAGE_ROLES)[number];

/** Usability flags flattened from clip.usage (display-only mirror of the cable usage layer). */
export interface AnnotationSegmentQuality {
  lip_sync_safe?: boolean;
  voiceover_cover_ok?: boolean;
  voiceover_only?: boolean;
}

/** Flat editor row. Rich display components read these top-level fields directly. Not a cable shape. */
export interface AnnotationTimelineSegment {
  segment_id: string;
  level: string;
  start: number;
  end: number;
  duration: number;
  confidence?: number;
  // retrieval layer
  summary?: string;
  retrieval_sentence?: string;
  keywords?: string[];
  // shared semantics
  subject_type?: string;
  scene_type?: string;
  // visual layer
  shot_scale?: string;
  camera_motion?: string;
  composition?: string;
  // usage layer (flattened)
  usable_roles?: string[];
  quality?: AnnotationSegmentQuality;
  // portrait (talking-head) semantics
  gaze_to_camera?: boolean | null;
  mouth_visible?: boolean | null;
  mouth_moving?: boolean | null;
  gesture_type?: string;
  body_orientation?: string;
  emotion_state?: string;
  speaker_intent?: string;
  speech_action_alignment?: string;
  retake_cue?: string;
  // b-roll (scenery / product) semantics
  action?: string;
  narrative_role?: string;
  contains_face?: boolean | null;
  face_count_max?: number | null;
  process_stage?: string;
}

/** Flat quality event (the single authoritative risk source; canonical.quality_events). */
export interface AnnotationQualityEvent {
  event_id: string;
  event_type: string;
  start: number;
  end: number;
  description?: string;
  risk_tier?: string;
  confidence?: number;
  severity?: number;
  source?: string | null;
  segment_id?: string | null;
}

/**
 * Evidence frame marker: a timestamp (seconds) optionally paired with a sampled
 * thumbnail URL. `time` comes from canonical.evidence_frames (flat number[]); the
 * optional `image_url` is matched from canonical.evidence_frame_images.
 */
export interface AnnotationEvidenceFrame {
  time: number;
  image_url?: string;
}

/** Meta layer (canonical.meta). */
export interface AnnotationMeta {
  annotation_version?: string;
  asset_id?: string;
  case_id?: string;
  material_type?: string;
  duration?: number;
  generated_at?: string | null;
  annotation_status?: string;
}

// ─────────────────────────────────────────────────────────────────────────────
// Cable (canonical) clip shapes — the nested structure exactly as persisted, so a
// write path produces a JSON-Patch-ready `clips` array the backend can re-validate.
// ─────────────────────────────────────────────────────────────────────────────

interface AnnotationClipSemantics {
  subject_type?: string;
  scene_type?: string;
  gaze_to_camera?: boolean | null;
  mouth_visible?: boolean | null;
  mouth_moving?: boolean | null;
  gesture_type?: string;
  body_orientation?: string;
  emotion_state?: string;
  speaker_intent?: string;
  speech_action_alignment?: string;
  retake_cue?: string;
  action?: string;
  narrative_role?: string;
  contains_face?: boolean | null;
  face_count_max?: number | null;
  process_stage?: string;
}

interface AnnotationClipVisual {
  shot_scale?: string;
  camera_motion?: string;
  composition?: string;
}

interface AnnotationClipUsage {
  recommended_for_lip_sync?: boolean;
  recommended_for_voiceover?: boolean;
  voiceover_only?: boolean;
  role: string;
}

interface AnnotationClipRetrieval {
  summary?: string;
  keywords?: string[];
  retrieval_sentence?: string;
}

export interface AnnotationClip {
  segment_id: string;
  start: number;
  end: number;
  duration: number;
  semantics?: AnnotationClipSemantics;
  visual?: AnnotationClipVisual;
  usage?: AnnotationClipUsage;
  retrieval?: AnnotationClipRetrieval;
  confidence?: number;
}

// ─────────────────────────────────────────────────────────────────────────────
// Small defensive readers (canonical fields are `unknown`).
// ─────────────────────────────────────────────────────────────────────────────

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown): UnknownRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as UnknownRecord) : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function cleanString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asNumber(value: unknown, fallback = 0): number {
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function asOptionalNumber(value: unknown): number | undefined {
  if (value === null || value === undefined || value === "") return undefined;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : undefined;
}

function asTriBool(value: unknown): boolean | null {
  if (value === true || value === false) return value;
  return null;
}

function asStringList(value: unknown): string[] {
  return asArray(value)
    .map((item) => cleanString(item))
    .filter((item) => item.length > 0);
}

/** Collapse an editor role token (possibly 'cover_broll' / 'detail' / etc.) to a legal V4 UsageRole. */
function normalizeUsageRole(raw: unknown, fallback: UsageRole = "main"): UsageRole {
  const token = cleanString(raw).trim().toLowerCase();
  if ((USAGE_ROLES as readonly string[]).includes(token)) return token as UsageRole;
  if (token.startsWith("cover")) return "cover";
  return fallback;
}

// ─────────────────────────────────────────────────────────────────────────────
// Read path: cable clip -> flat editor row.
// ─────────────────────────────────────────────────────────────────────────────

/** Nested cable clip -> flat editor row. Every top-level field a display component reads is laid out here. */
function clipToSegment(rawClip: unknown): AnnotationTimelineSegment {
  const clip = asRecord(rawClip);
  const semantics = asRecord(clip.semantics);
  const visual = asRecord(clip.visual);
  const usage = asRecord(clip.usage);
  const retrieval = asRecord(clip.retrieval);
  const role = cleanString(usage.role);
  return {
    segment_id: cleanString(clip.segment_id),
    level: "editable_clip",
    start: asNumber(clip.start),
    end: asNumber(clip.end),
    duration: asNumber(clip.duration),
    confidence: asOptionalNumber(clip.confidence),
    summary: cleanString(retrieval.summary),
    retrieval_sentence: cleanString(retrieval.retrieval_sentence),
    keywords: asStringList(retrieval.keywords),
    subject_type: cleanString(semantics.subject_type),
    scene_type: cleanString(semantics.scene_type),
    shot_scale: cleanString(visual.shot_scale),
    camera_motion: cleanString(visual.camera_motion),
    composition: cleanString(visual.composition),
    usable_roles: role ? [role] : [],
    // V4 clips no longer store per-clip risk; risk lives in the global quality_events.
    quality: {
      lip_sync_safe: usage.recommended_for_lip_sync === true,
      voiceover_cover_ok: usage.recommended_for_voiceover === true,
      voiceover_only: usage.voiceover_only === true,
    },
    gaze_to_camera: asTriBool(semantics.gaze_to_camera),
    mouth_visible: asTriBool(semantics.mouth_visible),
    mouth_moving: asTriBool(semantics.mouth_moving),
    gesture_type: cleanString(semantics.gesture_type),
    body_orientation: cleanString(semantics.body_orientation),
    emotion_state: cleanString(semantics.emotion_state),
    speaker_intent: cleanString(semantics.speaker_intent),
    speech_action_alignment: cleanString(semantics.speech_action_alignment),
    retake_cue: cleanString(semantics.retake_cue),
    action: cleanString(semantics.action),
    narrative_role: cleanString(semantics.narrative_role),
    contains_face: asTriBool(semantics.contains_face),
    face_count_max: asOptionalNumber(semantics.face_count_max) ?? null,
    process_stage: cleanString(semantics.process_stage),
  };
}

function clipsToSegments(clips?: unknown): AnnotationTimelineSegment[] {
  return asArray(clips).map(clipToSegment);
}

/** Read flat rows straight from an untyped canonical record (`canonical.clips`). */
export function canonicalToSegments(canonical?: unknown): AnnotationTimelineSegment[] {
  return clipsToSegments(asRecord(canonical).clips);
}

// ─────────────────────────────────────────────────────────────────────────────
// Write path: flat editor row -> cable clip.
// ─────────────────────────────────────────────────────────────────────────────

function buildSemantics(segment: AnnotationTimelineSegment): AnnotationClipSemantics {
  return {
    subject_type: cleanString(segment.subject_type),
    scene_type: cleanString(segment.scene_type),
    gaze_to_camera: segment.gaze_to_camera ?? null,
    mouth_visible: segment.mouth_visible ?? null,
    mouth_moving: segment.mouth_moving ?? null,
    gesture_type: cleanString(segment.gesture_type),
    body_orientation: cleanString(segment.body_orientation),
    emotion_state: cleanString(segment.emotion_state),
    speaker_intent: cleanString(segment.speaker_intent),
    speech_action_alignment: cleanString(segment.speech_action_alignment),
    retake_cue: cleanString(segment.retake_cue),
    action: cleanString(segment.action),
    narrative_role: cleanString(segment.narrative_role),
    contains_face: segment.contains_face ?? null,
    face_count_max: segment.face_count_max ?? null,
    process_stage: cleanString(segment.process_stage),
  };
}

function buildVisual(segment: AnnotationTimelineSegment): AnnotationClipVisual {
  return {
    shot_scale: cleanString(segment.shot_scale),
    camera_motion: cleanString(segment.camera_motion),
    composition: cleanString(segment.composition),
  };
}

function buildUsage(segment: AnnotationTimelineSegment, fallbackRole: UsageRole): AnnotationClipUsage {
  const lipSyncSafe = Boolean(segment.quality?.lip_sync_safe);
  const voiceoverOk = Boolean(segment.quality?.voiceover_cover_ok);
  return {
    recommended_for_lip_sync: lipSyncSafe,
    recommended_for_voiceover: voiceoverOk,
    voiceover_only: segment.quality?.voiceover_only ?? (voiceoverOk && !lipSyncSafe),
    role: normalizeUsageRole(segment.usable_roles?.[0], fallbackRole),
  };
}

/** Flat editor row -> nested cable clip. ``fallbackRole`` distinguishes portrait (main) / b-roll (cover). */
function segmentToClip(segment: AnnotationTimelineSegment, index: number, fallbackRole: UsageRole): AnnotationClip {
  const start = asNumber(segment.start);
  const rawEnd = asNumber(segment.end, start);
  const end = rawEnd > start ? rawEnd : start;
  return {
    segment_id: cleanString(segment.segment_id) || `clip_${index + 1}`,
    start,
    end,
    duration: Math.max(0, Number((end - start).toFixed(3))),
    confidence: asOptionalNumber(segment.confidence),
    semantics: buildSemantics(segment),
    visual: buildVisual(segment),
    usage: buildUsage(segment, fallbackRole),
    retrieval: {
      summary: cleanString(segment.summary),
      retrieval_sentence: cleanString(segment.retrieval_sentence),
      keywords: [...(segment.keywords ?? [])],
    },
  };
}

export function segmentsToClips(segments: AnnotationTimelineSegment[], isMainTrackMode: boolean): AnnotationClip[] {
  const fallbackRole: UsageRole = isMainTrackMode ? "main" : "cover";
  return segments.map((segment, index) => segmentToClip(segment, index, fallbackRole));
}

// ─────────────────────────────────────────────────────────────────────────────
// Other canonical layers — typed views read defensively from the untyped canonical.
// ─────────────────────────────────────────────────────────────────────────────

function qualityEventToView(raw: unknown): AnnotationQualityEvent {
  const event = asRecord(raw);
  return {
    event_id: cleanString(event.event_id),
    event_type: cleanString(event.event_type),
    start: asNumber(event.start),
    end: asNumber(event.end),
    description: cleanString(event.description),
    risk_tier: cleanString(event.risk_tier) || undefined,
    confidence: asOptionalNumber(event.confidence),
    severity: asOptionalNumber(event.severity),
    source: typeof event.source === "string" ? event.source : null,
    segment_id: typeof event.segment_id === "string" ? event.segment_id : null,
  };
}

export function parseQualityEvents(events?: unknown): AnnotationQualityEvent[] {
  return asArray(events).map(qualityEventToView);
}

export function canonicalToQualityEvents(canonical?: unknown): AnnotationQualityEvent[] {
  return parseQualityEvents(asRecord(canonical).quality_events);
}

/** Evidence frame timestamps (seconds). canonical.evidence_frames is a flat number[]. */
function readEvidenceFrames(canonical?: unknown): number[] {
  return asArray(asRecord(canonical).evidence_frames)
    .map((item) => asOptionalNumber(item))
    .filter((item): item is number => item !== undefined);
}

/**
 * Evidence frame thumbnails (canonical.evidence_frame_images — added by the backend
 * Regen). Each entry may be a plain URL string (positional, matched to the i-th
 * evidence_frames timestamp) or an object carrying its own `{ time, image_url }`.
 * Returns a list of `{ time?, image_url }` where `time` is only present for the
 * object form; the positional fallback is resolved by the caller.
 */
function readEvidenceFrameImages(canonical?: unknown): Array<{ time?: number; image_url: string }> {
  return asArray(asRecord(canonical).evidence_frame_images)
    .map((item) => {
      if (typeof item === "string") {
        const url = item.trim();
        return url ? { image_url: url } : null;
      }
      const record = asRecord(item);
      const url = cleanString(record.image_url ?? record.url ?? record.thumbnail_url).trim();
      if (!url) return null;
      return { time: asOptionalNumber(record.time ?? record.start ?? record.timestamp), image_url: url };
    })
    .filter((item): item is { time?: number; image_url: string } => item !== null);
}

/**
 * Evidence-frame markers for the timeline: pairs each canonical.evidence_frames
 * timestamp with its thumbnail (object-form match by nearest `time`, else positional
 * fallback). Frames without a thumbnail render as plain ticks (`image_url` undefined).
 */
export function canonicalToEvidenceFrames(canonical?: unknown): AnnotationEvidenceFrame[] {
  const times = readEvidenceFrames(canonical);
  const images = readEvidenceFrameImages(canonical);
  if (times.length === 0) {
    // No timestamps: surface any timestamped thumbnails on their own.
    return images
      .filter((img) => img.time !== undefined)
      .map((img) => ({ time: img.time as number, image_url: img.image_url }));
  }
  const positional = images.filter((img) => img.time === undefined).map((img) => img.image_url);
  const timed = images.filter((img): img is { time: number; image_url: string } => img.time !== undefined);
  return times.map((time, index) => {
    const matched = timed.find((img) => Math.abs(img.time - time) < 0.05);
    const image_url = matched?.image_url ?? positional[index];
    return image_url ? { time, image_url } : { time };
  });
}

export function readMeta(canonical?: unknown): AnnotationMeta {
  const meta = asRecord(asRecord(canonical).meta);
  return {
    annotation_version: cleanString(meta.annotation_version) || undefined,
    asset_id: cleanString(meta.asset_id) || undefined,
    case_id: cleanString(meta.case_id) || undefined,
    material_type: cleanString(meta.material_type) || undefined,
    duration: asOptionalNumber(meta.duration),
    generated_at: typeof meta.generated_at === "string" ? meta.generated_at : null,
    annotation_status: cleanString(meta.annotation_status) || undefined,
  };
}

/** Total media duration: prefer meta.duration, else fall back to the max clip/window/event end. */
export function readDuration(canonical?: unknown): number {
  const metaDuration = readMeta(canonical).duration;
  if (metaDuration && metaDuration > 0) return metaDuration;
  const record = asRecord(canonical);
  const ends: number[] = [];
  for (const clip of asArray(record.clips)) ends.push(asNumber(asRecord(clip).end));
  for (const win of asArray(record.usage_windows)) ends.push(asNumber(asRecord(win).end));
  for (const ev of asArray(record.quality_events)) ends.push(asNumber(asRecord(ev).end));
  return ends.length > 0 ? Math.max(...ends) : 0;
}

export type BgmSegmentRole = "hook" | "climax" | "outro" | "general";
export type BgmSectionType = "intro" | "stable_bed" | "verse" | "chorus" | "drop" | "bridge" | "outro" | "loop" | "build" | "general";
export type BgmEnergyProfile = "stable" | "rising" | "falling" | "drop" | "peak";

export const BGM_ROLES: BgmSegmentRole[] = ["hook", "climax", "outro", "general"];
export const BGM_SECTION_TYPES: BgmSectionType[] = ["intro", "stable_bed", "verse", "chorus", "drop", "bridge", "outro", "loop", "build", "general"];
export const BGM_ENERGY_PROFILES: BgmEnergyProfile[] = ["stable", "rising", "falling", "drop", "peak"];

export interface BgmSegment {
  segment_id?: string;
  start: number;          // seconds
  end: number;            // seconds
  duration?: number;
  role: BgmSegmentRole;
  section_type?: BgmSectionType;
  section_label?: string;
  repeat_group?: string;
  loopable?: boolean;
  energy_profile?: BgmEnergyProfile;
  energy?: number;        // 0-1
  drop_anchor_sec?: number;
  mood?: string;
  script_fit?: string[];
  avoid_script?: string[];
  scene_fit?: string[];   // tags
  avoid_scene?: string[];
  reason?: string;
  confidence?: number;
  source?: string;
}

export function asBgmRole(v: unknown): BgmSegmentRole {
  if (typeof v === "string" && (BGM_ROLES as string[]).includes(v)) {
    return v as BgmSegmentRole;
  }
  return "general";
}

export function asBgmSectionType(v: unknown): BgmSectionType {
  if (typeof v === "string" && (BGM_SECTION_TYPES as string[]).includes(v)) {
    return v as BgmSectionType;
  }
  return "general";
}

export function asBgmEnergyProfile(v: unknown): BgmEnergyProfile {
  if (typeof v === "string" && (BGM_ENERGY_PROFILES as string[]).includes(v)) {
    return v as BgmEnergyProfile;
  }
  return "stable";
}

export function canonicalToBgmSegments(canonical: Record<string, unknown>): BgmSegment[] {
  const arr = asArray(canonical["bgm_segments"]);
  return arr.map((item) => {
    const r = asRecord(item);
    return {
      segment_id: cleanString(r["segment_id"]) || undefined,
      start: asNumber(r["start"], 0),
      end: asNumber(r["end"], 0),
      duration: asOptionalNumber(r["duration"]),
      role: asBgmRole(r["role"]),
      section_type: asBgmSectionType(r["section_type"]),
      section_label: cleanString(r["section_label"]) || undefined,
      repeat_group: cleanString(r["repeat_group"]) || undefined,
      loopable: typeof r["loopable"] === "boolean" ? r["loopable"] : undefined,
      energy_profile: asBgmEnergyProfile(r["energy_profile"]),
      energy: asOptionalNumber(r["energy"]),
      drop_anchor_sec: asOptionalNumber(r["drop_anchor_sec"]),
      mood: cleanString(r["mood"]),
      script_fit: asStringList(r["script_fit"]),
      avoid_script: asStringList(r["avoid_script"]),
      scene_fit: asStringList(r["scene_fit"]),
      avoid_scene: asStringList(r["avoid_scene"]),
      reason: cleanString(r["reason"]),
      confidence: asOptionalNumber(r["confidence"]),
      source: cleanString(r["source"]) || undefined,
    };
  });
}

export function bgmSegmentsToCanonical(segments: BgmSegment[]): Record<string, unknown>[] {
  return segments.map((segment) => ({
    ...(segment.segment_id ? { segment_id: segment.segment_id } : {}),
    start: segment.start,
    end: segment.end,
    duration: Math.max(0, Number((segment.end - segment.start).toFixed(3))),
    role: segment.role,
    ...(segment.section_type ? { section_type: segment.section_type } : {}),
    ...(segment.section_label ? { section_label: segment.section_label } : {}),
    ...(segment.repeat_group ? { repeat_group: segment.repeat_group } : {}),
    ...(segment.loopable !== undefined ? { loopable: segment.loopable } : {}),
    ...(segment.energy_profile ? { energy_profile: segment.energy_profile } : {}),
    ...(segment.energy !== undefined ? { energy: segment.energy } : {}),
    ...(segment.drop_anchor_sec !== undefined ? { drop_anchor_sec: segment.drop_anchor_sec } : {}),
    ...(segment.mood ? { mood: segment.mood } : {}),
    ...(segment.script_fit && segment.script_fit.length > 0 ? { script_fit: segment.script_fit } : {}),
    ...(segment.avoid_script && segment.avoid_script.length > 0 ? { avoid_script: segment.avoid_script } : {}),
    ...(segment.scene_fit && segment.scene_fit.length > 0 ? { scene_fit: segment.scene_fit } : {}),
    ...(segment.avoid_scene && segment.avoid_scene.length > 0 ? { avoid_scene: segment.avoid_scene } : {}),
    ...(segment.reason ? { reason: segment.reason } : {}),
    ...(segment.confidence !== undefined ? { confidence: segment.confidence } : {}),
    ...(segment.source ? { source: segment.source } : {}),
  }));
}
