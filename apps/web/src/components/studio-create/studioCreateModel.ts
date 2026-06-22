import type { components } from "../../api/schema";

export type UserGenerationDefaults = components["schemas"]["UserGenerationDefaults"];

export type StudioStep = 0 | 1 | 2 | 3 | 4;

export type LipSyncPreset = "balanced" | "large_motion" | "strict_face" | "audio_priority";
type ContentMode = "digital_human" | "broll_only" | "seedance";

export type FormState = {
  title: string;
  script: string;
  // Adopted script version id (E-UI): set when a script is adopted from the case agent
  // or a generated script version, so the digital-human job carries the canonical
  // script_version_id instead of only the raw text. Cleared on manual script edits.
  scriptVersionId: string | null;
  contentMode: ContentMode;
  // Seedance (文生/图生视频) reference-image media-asset ids. Only used when
  // contentMode === "seedance"; submitted as DigitalHumanVideoRequest.reference_asset_ids.
  seedanceReferenceAssetIds: string[];
  voiceId: string;
  speed: number;
  emotion: string;
  portraitMode: "agent" | "specific" | "sequence";
  rhythmPreset: "steady" | "balanced" | "fast";
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
  lipsyncPreset: LipSyncPreset;
  lipsyncVideoExtension: boolean;
  lipsyncTimeoutMinutes: number;
};

export const STORAGE_KEY = "m6ar_studio_create_preferences_v1";

export const defaultForm: FormState = {
  title: "",
  script: "先指出内容生产低效。再展示 Case Memory 如何复用经验。最后推动发布复盘。",
  scriptVersionId: null,
  contentMode: "digital_human",
  seedanceReferenceAssetIds: [],
  voiceId: "",
  speed: 1,
  emotion: "neutral",
  portraitMode: "agent",
  rhythmPreset: "balanced",
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
  lipsyncPreset: "balanced",
  lipsyncVideoExtension: false,
  lipsyncTimeoutMinutes: 30,
};

export const steps = ["脚本", "模板", "成片配置", "后处理", "提交"] as const;

export const emotionOptions = [
  { value: "neutral", label: "自然" },
  { value: "happy", label: "明快" },
  { value: "serious", label: "沉稳" },
  { value: "energetic", label: "有力" },
] as const;

export const lipsyncPresets: Record<LipSyncPreset, { label: string; description: string; videoExtension: boolean }> = {
  balanced: { label: "标准均衡", description: "通用场景默认策略，兼顾锁脸稳定性与匹配成功率。", videoExtension: false },
  large_motion: { label: "大幅头动", description: "适合转头、抬头、位移较大场景，提高匹配宽容度。", videoExtension: false },
  strict_face: { label: "严格锁脸", description: "适合固定机位单人视频，减少误匹配。", videoExtension: false },
  audio_priority: { label: "时长优先", description: "音频较长时自动延长视频，避免尾部被截断。", videoExtension: true },
};

function clampNumber(value: number, min: number, max: number, fallback: number) {
  if (Number.isNaN(value)) return fallback;
  return Math.max(min, Math.min(max, value));
}

export function loadStoredForm(): FormState {
  if (typeof window === "undefined") return defaultForm;
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return defaultForm;
    const parsed = JSON.parse(saved) as Partial<FormState>;
    const contentMode =
      parsed.contentMode === "broll_only" || parsed.contentMode === "seedance"
        ? parsed.contentMode
        : defaultForm.contentMode;
    const seedanceReferenceAssetIds = Array.isArray(parsed.seedanceReferenceAssetIds)
      ? parsed.seedanceReferenceAssetIds.filter((id): id is string => typeof id === "string")
      : defaultForm.seedanceReferenceAssetIds;
    return {
      ...defaultForm,
      ...parsed,
      contentMode,
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
  if (step === 1 && form.contentMode === "digital_human" && form.portraitMode !== "agent") {
    return "当前版本请使用自动模板，指定模板和序列将在素材库里程碑接入";
  }
  if (step === 1 && form.contentMode === "seedance" && form.seedanceReferenceAssetIds.length === 0) {
    return "Seedance 模式请至少选择一张参考图";
  }
  // Seedance has no TTS step, so a voice is never required for it.
  if (step === 2 && form.contentMode !== "seedance" && !selectedVoice) return "请选择可用声音";
  if (step === 2 && (form.speed < 0.5 || form.speed > 2)) return "语速需在 0.5 到 2.0 之间";
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
  if (value === "broll_only") return "仅 B_roll 画外音";
  if (value === "seedance") return "Seedance 文生视频";
  return "数字人口播";
}

export function portraitModeLabel(value: FormState["portraitMode"]) {
  if (value === "agent") return "自动模板";
  if (value === "specific") return "指定模板";
  return "模板序列";
}

export function rhythmLabel(value: FormState["rhythmPreset"]) {
  if (value === "steady") return "稳";
  if (value === "fast") return "快";
  return "均衡";
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
const PORTRAIT_MODES: FormState["portraitMode"][] = ["agent", "specific", "sequence"];
const RHYTHM_PRESETS: FormState["rhythmPreset"][] = ["steady", "balanced", "fast"];
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
  return {
    voice: {
      voice_id: form.voiceId,
      speed: form.speed,
      emotion: form.emotion.trim() || "neutral",
      volume: 1,
    },
    portrait: {
      template_mode: form.portraitMode,
      rhythm_preset: form.rhythmPreset,
      template_sequence_ids: [],
    },
    broll: {
      enabled: form.brollEnabled,
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
      enabled: form.lipsyncEnabled,
      provider_profile_id: "runninghub.heygem.prod",
      video_extension: form.lipsyncVideoExtension,
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
  if (defaults.portrait) {
    next.portraitMode = pickFrom(PORTRAIT_MODES, defaults.portrait.template_mode, base.portraitMode);
    next.rhythmPreset = pickFrom(RHYTHM_PRESETS, defaults.portrait.rhythm_preset, base.rhythmPreset);
  }
  if (defaults.broll) {
    next.brollEnabled = Boolean(defaults.broll.enabled);
    next.maxInserts = clampNumber(Number(defaults.broll.max_inserts ?? base.maxInserts), 0, 20, base.maxInserts);
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
    next.lipsyncVideoExtension = Boolean(defaults.lipsync.video_extension);
    next.lipsyncTimeoutMinutes = clampNumber(
      Number(defaults.lipsync.timeout_minutes ?? base.lipsyncTimeoutMinutes),
      5,
      90,
      base.lipsyncTimeoutMinutes,
    );
  }
  return next;
}
