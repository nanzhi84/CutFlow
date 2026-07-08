import type { components } from "../../api/schema";

export type UserGenerationDefaults = components["schemas"]["UserGenerationDefaults"];

export type StudioStep = 0 | 1 | 2 | 3 | 4;

type ContentMode = "deterministic" | "editing_agent" | "seedance";
type VisualMode = "digital_human" | "broll_full_coverage";

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
  subtitleStyle: "douyin" | "clean" | "variety" | "news" | "movie" | "youshe_title_black";
  subtitleSize: number;
  bgmEnabled: boolean;
  bgmVolume: number;
  bgmAutoMix: boolean;
  coverMode: "none" | "frame" | "ai";
  lipsyncEnabled: boolean;
  lipsyncTimeoutMinutes: number;
  // Per-video extra editing instruction for the LLM editing-agent template
  // (contentMode === "editing_agent" -> digital_human_editing_agent_v1). Free text,
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
  subtitleStyle: "douyin",
  subtitleSize: 28,
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
    return {
      ...defaultForm,
      ...(parsed as Partial<FormState>),
      contentMode,
      visualMode,
      seedanceReferenceAssetIds,
      speed: clampNumber(Number(parsed.speed ?? defaultForm.speed), 0.5, 2, defaultForm.speed),
      maxInserts: clampNumber(Number(parsed.maxInserts ?? defaultForm.maxInserts), 0, 20, defaultForm.maxInserts),
      subtitleSize: clampNumber(Number(parsed.subtitleSize ?? defaultForm.subtitleSize), 12, 96, defaultForm.subtitleSize),
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
  if (step === 3 && form.subtitleEnabled && (form.subtitleSize < 12 || form.subtitleSize > 96)) return "字幕字号需在 12 到 96 之间";
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

export function subtitleLabel(value: FormState["subtitleStyle"]) {
  if (value === "clean") return "简洁风";
  if (value === "variety") return "综艺风";
  if (value === "news") return "新闻风";
  if (value === "movie") return "电影风";
  if (value === "youshe_title_black") return "标题黑风";
  return "抖音风";
}

const SUBTITLE_STYLES: FormState["subtitleStyle"][] = [
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

/**
 * Project the user-tunable subset of a Studio `FormState` into the contract
 * `UserGenerationDefaults` shape (one block per generation aspect). Content
 * fields (title/script/scriptVersionId) are deliberately excluded — defaults
 * are preferences, not content.
 */
export function mapFormToDefaults(form: FormState): UserGenerationDefaults {
  const fullCoverage = form.visualMode === "broll_full_coverage";
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
      enabled: form.subtitleEnabled,
      style_preset: form.subtitleStyle,
      font_size: form.subtitleSize,
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
    next.subtitleEnabled = Boolean(defaults.subtitle.enabled);
    next.subtitleStyle = pickFrom(SUBTITLE_STYLES, defaults.subtitle.style_preset, base.subtitleStyle);
    next.subtitleSize = clampNumber(
      Number(defaults.subtitle.font_size ?? base.subtitleSize),
      12,
      96,
      base.subtitleSize,
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
