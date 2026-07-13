import type { components } from "../../api/schema";

export type UserGenerationDefaults = components["schemas"]["UserGenerationDefaults"];

export type StudioStep = 0 | 1 | 2 | 3 | 4;

type ContentMode = "deterministic" | "editing_agent" | "seedance";
type VisualMode = "digital_human" | "broll_full_coverage";
type LegacySubtitleStyle =
  | "douyin"
  | "clean"
  | "variety"
  | "news"
  | "movie"
  | "youshe_title_black";

export type FormState = {
  title: string;
  script: string;
  // Adopted script version id (E-UI): set when a script is adopted from the case agent
  // or a generated script version, so the digital-human job carries the canonical
  // script_version_id instead of only the raw text. Cleared on manual script edits.
  scriptVersionId: string | null;
  contentMode: ContentMode;
  visualMode: VisualMode;
  // Seedance (文生/图生视频) reference-image media-asset ids. Only used when
  // contentMode === "seedance"; submitted as DigitalHumanVideoRequest.reference_asset_ids.
  seedanceReferenceAssetIds: string[];
  voiceId: string;
  speed: number;
  emotion: string;
  brollEnabled: boolean;
  maxInserts: number;
  subtitleEnabled: boolean;
  normalSubtitleEnabled: boolean;
  huaziEnabled: boolean;
  subtitleStyle: LegacySubtitleStyle;
  subtitleFontId: string;
  huaziFontId: string;
  subtitleSize: number;
  huaziSize: number;
  huaziColor: string;
  subtitlePositionY: number;
  bgmEnabled: boolean;
  bgmVolume: number;
  bgmAutoMix: boolean;
  coverMode: "none" | "frame" | "ai";
  lipsyncEnabled: boolean;
  lipsyncTimeoutMinutes: number;
  // Per-video extra editing instruction for the LLM editing-agent template
  // (contentMode === "editing_agent" -> digital_human_editing_agent_v2). Free text,
  // optional; submitted as DigitalHumanVideoRequest.edit.instruction. It is per-video
  // content, not a saved preference, so it stays out of UserGenerationDefaults.
  editInstruction: string;
};

export const STORAGE_KEY = "m6ar_studio_create_preferences_v1";

const defaultForm: FormState = {
  title: "",
  script: "",
  scriptVersionId: null,
  contentMode: "deterministic",
  visualMode: "digital_human",
  seedanceReferenceAssetIds: [],
  voiceId: "",
  speed: 1,
  emotion: "neutral",
  brollEnabled: true,
  maxInserts: 4,
  subtitleEnabled: true,
  normalSubtitleEnabled: true,
  huaziEnabled: true,
  subtitleStyle: "douyin",
  subtitleFontId: "",
  huaziFontId: "",
  subtitleSize: 28,
  huaziSize: 40,
  huaziColor: "#FFE84A",
  subtitlePositionY: 0.84,
  bgmEnabled: false,
  bgmVolume: 0.25,
  bgmAutoMix: true,
  coverMode: "frame",
  lipsyncEnabled: true,
  lipsyncTimeoutMinutes: 30,
  editInstruction: "",
};

export const steps = ["脚本", "剪辑方式", "成片配置", "后处理", "提交"] as const;

export const emotionOptions = [
  { value: "neutral", label: "自然" },
  { value: "happy", label: "明快" },
  { value: "serious", label: "沉稳" },
  { value: "energetic", label: "有力" },
] as const;

function clampNumber(value: number, min: number, max: number, fallback: number) {
  if (Number.isNaN(value)) return fallback;
  return Math.max(min, Math.min(max, value));
}

function normalizeHexColor(value: unknown, fallback: string) {
  if (typeof value !== "string") return fallback;
  const trimmed = value.trim();
  return /^#[0-9A-Fa-f]{6}$/.test(trimmed) ? trimmed.toUpperCase() : fallback;
}

export const SUBTITLE_RENDER_BASE_HEIGHT = 1080;
export const SUBTITLE_PREVIEW_OUTPUT_WIDTH = 1080;
export const SUBTITLE_PREVIEW_OUTPUT_HEIGHT = 1920;
export const SUBTITLE_PREVIEW_WIDTH = 320;
export const SUBTITLE_PREVIEW_HEIGHT = SUBTITLE_PREVIEW_WIDTH * (SUBTITLE_PREVIEW_OUTPUT_HEIGHT / SUBTITLE_PREVIEW_OUTPUT_WIDTH);
export const ASS_FONT_POINT_TO_CSS_PIXEL = 72 / 96;

export function subtitleAssFontSize(requestedSize: unknown, outputHeight = SUBTITLE_PREVIEW_OUTPUT_HEIGHT) {
  const parsed = Number(requestedSize || 64);
  const baseSize = Number.isFinite(parsed) ? Math.trunc(parsed) : 64;
  const normalizedHeight = Number(outputHeight || SUBTITLE_RENDER_BASE_HEIGHT);
  const scale = Math.max(1, (Number.isFinite(normalizedHeight) ? normalizedHeight : SUBTITLE_RENDER_BASE_HEIGHT) / SUBTITLE_RENDER_BASE_HEIGHT);
  return Math.max(12, Math.round(baseSize * scale));
}

export function subtitlePreviewCssFontSize(
  requestedSize: unknown,
  {
    outputWidth = SUBTITLE_PREVIEW_OUTPUT_WIDTH,
    outputHeight = SUBTITLE_PREVIEW_OUTPUT_HEIGHT,
    previewWidth = SUBTITLE_PREVIEW_WIDTH,
    previewHeight = SUBTITLE_PREVIEW_HEIGHT,
    minCssPx = 4,
  }: {
    outputWidth?: number;
    outputHeight?: number;
    previewWidth?: number;
    previewHeight?: number;
    minCssPx?: number;
  } = {},
) {
  const normalizedOutputWidth = Number.isFinite(outputWidth) && outputWidth > 0 ? outputWidth : SUBTITLE_PREVIEW_OUTPUT_WIDTH;
  const normalizedOutputHeight = Number.isFinite(outputHeight) && outputHeight > 0 ? outputHeight : SUBTITLE_PREVIEW_OUTPUT_HEIGHT;
  const normalizedPreviewWidth =
    Number.isFinite(previewWidth) && previewWidth > 0
      ? previewWidth
      : Number.isFinite(previewHeight) && previewHeight > 0
        ? previewHeight * (normalizedOutputWidth / normalizedOutputHeight)
        : SUBTITLE_PREVIEW_WIDTH;
  // ASS Fontsize is a point-like unit. libass/FreeType rasterizes it at 72/96
  // of the equivalent CSS px size, so treating it as a CSS pixel makes the
  // browser preview about one third larger than the burned subtitle.
  const scaled =
    subtitleAssFontSize(requestedSize, normalizedOutputHeight) *
    ASS_FONT_POINT_TO_CSS_PIXEL *
    (normalizedPreviewWidth / normalizedOutputWidth);
  return Math.round(Math.max(minCssPx, scaled) * 10) / 10;
}

export function subtitlePreviewCssOutlineWidth(
  requestedOutline: unknown,
  {
    outputWidth = SUBTITLE_PREVIEW_OUTPUT_WIDTH,
    previewWidth = SUBTITLE_PREVIEW_WIDTH,
  }: {
    outputWidth?: number;
    previewWidth?: number;
  } = {},
) {
  const parsed = Number(requestedOutline ?? 0);
  const outline = Number.isFinite(parsed) ? Math.max(0, parsed) : 0;
  const normalizedOutputWidth = Number.isFinite(outputWidth) && outputWidth > 0 ? outputWidth : SUBTITLE_PREVIEW_OUTPUT_WIDTH;
  const normalizedPreviewWidth = Number.isFinite(previewWidth) && previewWidth > 0 ? previewWidth : SUBTITLE_PREVIEW_WIDTH;
  return Math.round(outline * (normalizedPreviewWidth / normalizedOutputWidth) * 10) / 10;
}

export function loadStoredForm(): FormState {
  if (typeof window === "undefined") return defaultForm;
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return defaultForm;
    const parsed = JSON.parse(saved) as Record<string, unknown>;
    const storedContentMode = parsed.contentMode;
    const contentMode =
      storedContentMode === "deterministic" ||
      storedContentMode === "digital_human" ||
      storedContentMode === "seedance" ||
      storedContentMode === "editing_agent"
        ? storedContentMode === "digital_human"
          ? "deterministic"
          : storedContentMode
        : defaultForm.contentMode;
    const storedVisualMode = parsed.visualMode;
    const visualMode =
      storedVisualMode === "digital_human" || storedVisualMode === "broll_full_coverage"
        ? storedVisualMode
        : storedContentMode === "broll_only"
          ? "broll_full_coverage"
          : defaultForm.visualMode;
    const seedanceReferenceAssetIds = Array.isArray(parsed.seedanceReferenceAssetIds)
      ? parsed.seedanceReferenceAssetIds.filter((id): id is string => typeof id === "string")
      : defaultForm.seedanceReferenceAssetIds;
    const subtitleStyle = isLegacySubtitleStyle(parsed.subtitleStyle)
      ? parsed.subtitleStyle
      : defaultForm.subtitleStyle;
    const legacySubtitleEnabled = typeof parsed.subtitleEnabled === "boolean" ? parsed.subtitleEnabled : defaultForm.subtitleEnabled;
    const normalSubtitleEnabled =
      typeof parsed.normalSubtitleEnabled === "boolean" ? parsed.normalSubtitleEnabled : legacySubtitleEnabled;
    const huaziEnabled = typeof parsed.huaziEnabled === "boolean" ? parsed.huaziEnabled : legacySubtitleEnabled;
    const subtitleFontId = typeof parsed.subtitleFontId === "string" ? parsed.subtitleFontId : "";
    const huaziFontId = typeof parsed.huaziFontId === "string" ? parsed.huaziFontId : "";
    const huaziColor = normalizeHexColor(parsed.huaziColor, defaultForm.huaziColor);
    return {
      ...defaultForm,
      ...(parsed as Partial<FormState>),
      contentMode,
      visualMode,
      seedanceReferenceAssetIds,
      subtitleEnabled: normalSubtitleEnabled || huaziEnabled,
      normalSubtitleEnabled,
      huaziEnabled,
      subtitleStyle,
      subtitleFontId,
      huaziFontId,
      huaziColor,
      speed: clampNumber(Number(parsed.speed ?? defaultForm.speed), 0.5, 2, defaultForm.speed),
      maxInserts: clampNumber(Number(parsed.maxInserts ?? defaultForm.maxInserts), 0, 20, defaultForm.maxInserts),
      subtitleSize: clampNumber(Number(parsed.subtitleSize ?? defaultForm.subtitleSize), 12, 96, defaultForm.subtitleSize),
      huaziSize: clampNumber(Number(parsed.huaziSize ?? defaultForm.huaziSize), 12, 120, defaultForm.huaziSize),
      subtitlePositionY: clampNumber(
        Number(parsed.subtitlePositionY ?? defaultForm.subtitlePositionY),
        0.72,
        0.92,
        defaultForm.subtitlePositionY,
      ),
      bgmVolume: clampNumber(Number(parsed.bgmVolume ?? defaultForm.bgmVolume), 0, 1, defaultForm.bgmVolume),
      lipsyncTimeoutMinutes: clampNumber(
        Number(parsed.lipsyncTimeoutMinutes ?? defaultForm.lipsyncTimeoutMinutes),
        5,
        90,
        defaultForm.lipsyncTimeoutMinutes,
      ),
    };
  } catch {
    return defaultForm;
  }
}

export function validateStep(step: StudioStep, form: FormState, selectedVoice: string) {
  if (step === 0 && !form.script.trim()) return "请先输入脚本正文";
  // Seedance has no TTS step (no voice, no speed), so neither is required for it.
  if (step === 2 && form.contentMode !== "seedance" && !selectedVoice) return "请选择可用声音";
  if (step === 2 && form.contentMode !== "seedance" && (form.speed < 0.5 || form.speed > 2))
    return "语速需在 0.5 到 2.0 之间";
  if (step === 3 && form.contentMode === "seedance") return null;
  if (step === 3 && form.normalSubtitleEnabled && (form.subtitleSize < 12 || form.subtitleSize > 96)) return "字幕字号需在 12 到 96 之间";
  if (step === 3 && effectiveHuaziEnabled(form) && (form.huaziSize < 12 || form.huaziSize > 120)) return "花字字号需在 12 到 120 之间";
  if (step === 3 && effectiveHuaziEnabled(form) && !/^#[0-9A-Fa-f]{6}$/.test(form.huaziColor)) return "花字颜色需为有效色值";
  if (step === 3 && form.normalSubtitleEnabled && (form.subtitlePositionY < 0.72 || form.subtitlePositionY > 0.92))
    return "字幕位置需在安全区内";
  if (step === 3 && form.bgmEnabled && (form.bgmVolume < 0 || form.bgmVolume > 1)) return "BGM 音量需在 0 到 100% 之间";
  return null;
}

export function validateAll(form: FormState, selectedVoice: string) {
  for (let index = 0; index < steps.length - 1; index += 1) {
    const message = validateStep(index as StudioStep, form, selectedVoice);
    if (message) return { step: index as StudioStep, message };
  }
  return null;
}

export function contentModeLabel(value: FormState["contentMode"]) {
  if (value === "editing_agent") return "Agent智能剪辑";
  if (value === "seedance") return "seedance文生视频";
  return "确定算法剪辑";
}

export function visualModeLabel(value: FormState["visualMode"]) {
  if (value === "broll_full_coverage") return "纯Broll模式";
  return "数字人模式";
}

/**
 * 花字（整句强调 overlay）只有 Agent 智能剪辑链（digital_human_editing_agent_v2）会产出：
 * 确定性链 digital_human_v2 已冻结、不再派生 overlay 事件，Seedance 无字幕层。前端据此
 * 隐藏花字开关/设置并在提交时强制 emphasis_enabled=false，避免向后端请求一个当前模板
 * 不会渲染的花字层。
 */
export function supportsEmphasisCaption(mode: FormState["contentMode"]): boolean {
  return mode === "editing_agent";
}

/** 当前表单实际会提交的花字启用态（叠加了模板能力约束）。 */
export function effectiveHuaziEnabled(form: FormState): boolean {
  return supportsEmphasisCaption(form.contentMode) && form.huaziEnabled;
}

const SUBTITLE_STYLES: LegacySubtitleStyle[] = [
  "douyin",
  "clean",
  "variety",
  "news",
  "movie",
  "youshe_title_black",
];
const COVER_MODES: FormState["coverMode"][] = ["none", "frame", "ai"];

function pickFrom<T extends string>(allowed: T[], value: unknown, fallback: T): T {
  return allowed.includes(value as T) ? (value as T) : fallback;
}

function isLegacySubtitleStyle(value: unknown): value is LegacySubtitleStyle {
  return SUBTITLE_STYLES.includes(value as LegacySubtitleStyle);
}

/**
 * Project the user-tunable subset of a Studio `FormState` into the contract
 * `UserGenerationDefaults` shape (one block per generation aspect). Content
 * fields (title/script/scriptVersionId) are deliberately excluded — defaults
 * are preferences, not content.
 */
export function mapFormToDefaults(form: FormState): UserGenerationDefaults {
  const fullCoverage = form.visualMode === "broll_full_coverage";
  const emphasisEnabled = effectiveHuaziEnabled(form);
  const subtitleEnabled = form.normalSubtitleEnabled || emphasisEnabled;
  return {
    voice: {
      voice_id: form.voiceId,
      speed: form.speed,
      emotion: form.emotion.trim() || "neutral",
      volume: 1,
    },
    broll: {
      enabled: fullCoverage ? true : form.brollEnabled,
      mode: fullCoverage ? "full_coverage" : "insert",
      max_inserts: form.maxInserts,
      min_segment_duration: 3,
      allow_generic_coverage: true,
    },
    subtitle: {
      enabled: subtitleEnabled,
      normal_enabled: form.normalSubtitleEnabled,
      emphasis_enabled: emphasisEnabled,
      style_preset: form.subtitleStyle,
      font_id: form.subtitleFontId.trim() || null,
      emphasis_font_id: form.huaziFontId.trim() || null,
      font_size: form.subtitleSize,
      emphasis_font_size: form.huaziSize,
      emphasis_primary_color: form.huaziColor,
      position: { x: 0.5, y: form.subtitlePositionY },
    },
    bgm: {
      enabled: form.bgmEnabled,
      volume: form.bgmVolume,
      auto_mix: form.bgmAutoMix,
    },
    cover: {
      mode: form.coverMode,
    },
    lipsync: {
      enabled: fullCoverage ? false : form.lipsyncEnabled,
      provider_profile_id: "runninghub.heygem.prod",
      timeout_minutes: form.lipsyncTimeoutMinutes,
    },
  };
}

/**
 * Hydrate a `FormState` from saved `UserGenerationDefaults`, layering each
 * present block over a base form (typically `defaultForm` or the current form).
 * Absent blocks fall back to `base`. Content fields are never touched.
 */
export function mapDefaultsToForm(defaults: UserGenerationDefaults, base: FormState): FormState {
  const next: FormState = { ...base };
  if (defaults.voice) {
    if (defaults.voice.voice_id) next.voiceId = defaults.voice.voice_id;
    next.speed = clampNumber(Number(defaults.voice.speed ?? base.speed), 0.5, 2, base.speed);
    if (defaults.voice.emotion) next.emotion = defaults.voice.emotion;
  }
  if (defaults.broll) {
    if (defaults.broll.mode === "full_coverage") {
      next.visualMode = "broll_full_coverage";
      next.brollEnabled = true;
    } else {
      next.visualMode = "digital_human";
      next.brollEnabled = Boolean(defaults.broll.enabled);
    }
    next.maxInserts = clampNumber(
      Number(defaults.broll.max_inserts ?? base.maxInserts),
      0,
      20,
      base.maxInserts,
    );
  }
  if (defaults.subtitle) {
    const subtitleEnabled = Boolean(defaults.subtitle.enabled);
    next.normalSubtitleEnabled = subtitleEnabled && (defaults.subtitle.normal_enabled ?? true);
    next.huaziEnabled = subtitleEnabled && (defaults.subtitle.emphasis_enabled ?? true);
    next.subtitleEnabled = next.normalSubtitleEnabled || next.huaziEnabled;
    next.subtitleStyle = pickFrom(SUBTITLE_STYLES, defaults.subtitle.style_preset, base.subtitleStyle);
    next.subtitleFontId = defaults.subtitle.font_id ?? "";
    next.huaziFontId = defaults.subtitle.emphasis_font_id ?? "";
    next.huaziColor = normalizeHexColor(defaults.subtitle.emphasis_primary_color, base.huaziColor);
    next.subtitleSize = clampNumber(
      Number(defaults.subtitle.font_size ?? base.subtitleSize),
      12,
      96,
      base.subtitleSize,
    );
    next.huaziSize = clampNumber(
      Number(defaults.subtitle.emphasis_font_size ?? base.huaziSize),
      12,
      120,
      base.huaziSize,
    );
    next.subtitlePositionY = clampNumber(
      Number(defaults.subtitle.position?.y ?? base.subtitlePositionY),
      0.72,
      0.92,
      base.subtitlePositionY,
    );
  }
  if (defaults.bgm) {
    next.bgmEnabled = Boolean(defaults.bgm.enabled);
    next.bgmVolume = clampNumber(Number(defaults.bgm.volume ?? base.bgmVolume), 0, 1, base.bgmVolume);
    next.bgmAutoMix = Boolean(defaults.bgm.auto_mix);
  }
  if (defaults.cover) {
    next.coverMode = pickFrom(COVER_MODES, defaults.cover.mode, base.coverMode);
  }
  if (defaults.lipsync) {
    next.lipsyncEnabled = Boolean(defaults.lipsync.enabled);
    next.lipsyncTimeoutMinutes = clampNumber(
      Number(defaults.lipsync.timeout_minutes ?? base.lipsyncTimeoutMinutes),
      5,
      90,
      base.lipsyncTimeoutMinutes,
    );
  }
  return next;
}
