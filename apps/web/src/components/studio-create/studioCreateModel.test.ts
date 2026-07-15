import { beforeEach, describe, expect, it } from "vitest";
import {
  CAPTION_POLICY,
  STORAGE_KEY,
  contentModeLabel,
  effectiveEmphasisEnabled,
  loadStoredForm,
  mapDefaultsToForm,
  mapFormToDefaults,
  subtitleAssFontSize,
  subtitlePreviewCssFontSize,
  subtitlePreviewCssOutlineWidth,
  supportsEmphasisCaption,
  validateAll,
  validateStep,
  visualModeLabel,
} from "./studioCreateModel";
import type { UserGenerationDefaults } from "./studioCreateModel";

describe("studioCreateModel", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("sanitizes persisted form state and keeps editing-agent mode", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        contentMode: "editing_agent",
        seedanceReferenceAssetIds: ["asset_1", 42, "asset_2"],
        speed: 9,
        subtitleSize: 2,
        bgmVolume: -1,
        lipsyncTimeoutMinutes: 999,
      }),
    );

    const form = loadStoredForm();

    expect(form.contentMode).toBe("editing_agent");
    expect(form.seedanceReferenceAssetIds).toEqual(["asset_1", "asset_2"]);
    expect(form.speed).toBe(2);
    expect(form.subtitleSize).toBe(12);
    expect(form.bgmVolume).toBe(0);
    expect(form.lipsyncTimeoutMinutes).toBe(90);
  });

  it("migrates but never retains removed huazi or unknown localStorage keys", () => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        huaziEnabled: true,
        huaziFontId: "font_legacy",
        huaziSize: 72,
        huaziColor: "#FF5C5C",
        unknownLegacyOption: "remove-me",
      }),
    );

    const form = loadStoredForm();
    expect(form.emphasisEnabled).toBe(true);
    expect(form.emphasisFontId).toBe("font_legacy");
    expect(form.emphasisSize).toBe(72);
    expect(form.emphasisColor).toBe("#FF5C5C");
    expect(form).not.toHaveProperty("huaziEnabled");
    expect(form).not.toHaveProperty("huaziFontId");
    expect(form).not.toHaveProperty("huaziSize");
    expect(form).not.toHaveProperty("huaziColor");
    expect(form).not.toHaveProperty("unknownLegacyOption");
  });

  it("migrates removed legacy content modes to deterministic mode", () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ contentMode: "digital_human" }));
    expect(loadStoredForm().contentMode).toBe("deterministic");
    expect(loadStoredForm().visualMode).toBe("digital_human");

    localStorage.setItem(STORAGE_KEY, JSON.stringify({ contentMode: "broll_only" }));
    expect(loadStoredForm().contentMode).toBe("deterministic");
    expect(loadStoredForm().visualMode).toBe("broll_full_coverage");
  });

  it("projects user defaults without content fields", () => {
    const defaults = mapFormToDefaults({
      title: "标题",
      script: "脚本",
      scriptVersionId: "script_1",
      contentMode: "deterministic",
      visualMode: "digital_human",
      seedanceReferenceAssetIds: [],
      voiceId: "voice_1",
      speed: 1.2,
      emotion: "happy",
      brollEnabled: true,
      maxInserts: 6,
      subtitleEnabled: true,
      normalSubtitleEnabled: true,
      emphasisEnabled: false,
      subtitleFontId: "font_yst",
      emphasisFontId: "font_hz",
      subtitleStyle: "movie",
      subtitleSize: 42,
      emphasisSize: 58,
      emphasisColor: "#FF5C5C",
      subtitlePositionY: 0.82,
      bgmEnabled: true,
      bgmVolume: 0.4,
      bgmAutoMix: false,
      coverMode: "ai",
      lipsyncEnabled: true,
      lipsyncTimeoutMinutes: 45,
      editInstruction: "偏生活化",
    });

    expect(defaults.voice?.voice_id).toBe("voice_1");
    expect(defaults.broll?.allow_generic_coverage).toBe(true);
    expect(defaults.subtitle?.style_preset).toBe("movie");
    expect(defaults.subtitle?.enabled).toBe(true);
    expect(defaults.subtitle?.normal_enabled).toBe(true);
    expect(defaults.subtitle?.emphasis_enabled).toBe(false);
    expect(defaults.subtitle?.font_id).toBe("font_yst");
    expect(defaults.subtitle?.emphasis_font_id).toBe("font_hz");
    expect(defaults.subtitle?.emphasis_font_size).toBe(58);
    expect(defaults.subtitle?.emphasis_primary_color).toBe("#FF5C5C");
    expect(defaults.subtitle?.position).toEqual({
      x: CAPTION_POLICY.anchor_x,
      y: 0.82,
    });
    expect(defaults.bgm?.auto_mix).toBe(false);
    expect(defaults.cover?.mode).toBe("ai");
    expect(defaults.lipsync?.timeout_minutes).toBe(45);
    expect(defaults).not.toHaveProperty("script");
    expect(defaults).not.toHaveProperty("edit");
  });

  it("projects full-coverage B-roll mode into defaults", () => {
    const defaults = mapFormToDefaults({
      ...loadStoredForm(),
      visualMode: "broll_full_coverage",
      brollEnabled: false,
      lipsyncEnabled: true,
      maxInserts: 8,
    });

    expect(defaults.broll?.enabled).toBe(true);
    expect(defaults.broll?.mode).toBe("full_coverage");
    expect(defaults.lipsync?.enabled).toBe(false);
  });

  it("hydrates defaults with clamped values and validates seedance voice exemption", () => {
    const defaults: UserGenerationDefaults = {
      voice: { voice_id: "voice_2", speed: 4, emotion: "serious", volume: 1 },
      subtitle: {
        enabled: true,
        normal_enabled: false,
        emphasis_enabled: true,
        style_preset: "news",
        font_size: 8,
        emphasis_font_size: 140,
        emphasis_font_id: "font_hz",
        emphasis_primary_color: "#38d9a9",
        position: { x: 0.5, y: 0.91 },
      },
      lipsync: { enabled: true, provider_profile_id: "runninghub.heygem.prod", timeout_minutes: 2 },
    };
    const form = mapDefaultsToForm(defaults, loadStoredForm());

    expect(form.voiceId).toBe("voice_2");
    expect(form.speed).toBe(2);
    expect(form.subtitleStyle).toBe("news");
    expect(form.subtitleEnabled).toBe(false);
    expect(form.normalSubtitleEnabled).toBe(false);
    expect(form.emphasisEnabled).toBe(false);
    expect(form.subtitleSize).toBe(12);
    expect(form.emphasisSize).toBe(120);
    expect(form.emphasisFontId).toBe("font_hz");
    expect(form.emphasisColor).toBe("#38D9A9");
    expect(form.subtitlePositionY).toBe(0.91);
    expect(form.lipsyncTimeoutMinutes).toBe(5);
    expect(validateAll({ ...form, script: "文案", contentMode: "seedance" }, "")).toBeNull();
    expect(contentModeLabel("editing_agent")).toBe("Agent智能剪辑");
    expect(contentModeLabel("seedance")).toBe("seedance文生视频");
    expect(contentModeLabel("deterministic")).toBe("确定算法剪辑");
    expect(visualModeLabel("digital_human")).toBe("数字人模式");
    expect(visualModeLabel("broll_full_coverage")).toBe("纯Broll模式");
  });

  it("hydrates full-coverage B-roll defaults into pure Broll visual mode", () => {
    const form = mapDefaultsToForm(
      {
        broll: {
          enabled: true,
          mode: "full_coverage",
          max_inserts: 7,
          min_segment_duration: 3,
          allow_generic_coverage: true,
        },
      },
      loadStoredForm(),
    );

    expect(form.visualMode).toBe("broll_full_coverage");
    expect(form.brollEnabled).toBe(true);
    expect(form.maxInserts).toBe(7);
    expect(validateAll({ ...form, script: "文案", voiceId: "voice_1", maxInserts: 0 }, "voice_1")).toBeNull();
  });

  it("supports emphasis Runs on the deterministic template", () => {
    const base = loadStoredForm();
    expect(base.contentMode).toBe("deterministic");
    expect(base.emphasisEnabled).toBe(true);

    const defaults = mapFormToDefaults(base);
    expect(defaults.subtitle?.emphasis_enabled).toBe(true);
    // 普通字幕默认开启 → 字幕层整体仍启用
    expect(defaults.subtitle?.enabled).toBe(true);

    // 关闭普通字幕时，字幕内强调也必须随总闸关闭。
    const emphasisOnly = mapFormToDefaults({ ...base, normalSubtitleEnabled: false });
    expect(emphasisOnly.subtitle?.enabled).toBe(false);
    expect(emphasisOnly.subtitle?.emphasis_enabled).toBe(false);

    expect(effectiveEmphasisEnabled(base)).toBe(true);
    expect(supportsEmphasisCaption("deterministic")).toBe(true);
    expect(supportsEmphasisCaption("seedance")).toBe(false);
  });

  it("keeps emphasis on under the editing-agent template", () => {
    const form = { ...loadStoredForm(), contentMode: "editing_agent" as const, emphasisEnabled: true };
    const defaults = mapFormToDefaults(form);

    expect(defaults.subtitle?.emphasis_enabled).toBe(true);
    expect(effectiveEmphasisEnabled(form)).toBe(true);
    expect(supportsEmphasisCaption("editing_agent")).toBe(true);
  });

  it("validates emphasis style on both active digital-human templates", () => {
    const form = {
      ...loadStoredForm(),
      contentMode: "deterministic" as const,
      emphasisEnabled: true,
      emphasisColor: "not-a-color",
    };
    expect(validateStep(3, form, "voice_1")).toBe("强调颜色需为有效色值");
    expect(validateStep(3, { ...form, contentMode: "editing_agent" }, "voice_1")).toBe("强调颜色需为有效色值");
    expect(validateStep(3, { ...form, contentMode: "seedance" }, "voice_1")).toBeNull();
  });

  it("scales subtitle preview font sizes like the final 1080x1920 render", () => {
    expect(subtitleAssFontSize(38, 1920)).toBe(68);
    expect(subtitlePreviewCssFontSize(17)).toBe(6.7);
    expect(subtitlePreviewCssFontSize(34)).toBe(13.3);
    expect(subtitlePreviewCssFontSize(60)).toBe(23.8);
    expect(subtitlePreviewCssOutlineWidth(4)).toBe(1.2);
    expect(subtitlePreviewCssFontSize(17)).toBeLessThan(12);
  });
});
