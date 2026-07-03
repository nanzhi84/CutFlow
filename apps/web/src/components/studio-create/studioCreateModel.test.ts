import { beforeEach, describe, expect, it } from "vitest";
import {
  STORAGE_KEY,
  contentModeLabel,
  loadStoredForm,
  mapDefaultsToForm,
  mapFormToDefaults,
  validateAll,
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

  it("projects user defaults without content fields", () => {
    const defaults = mapFormToDefaults({
      title: "标题",
      script: "脚本",
      scriptVersionId: "script_1",
      contentMode: "digital_human",
      seedanceReferenceAssetIds: [],
      voiceId: "voice_1",
      speed: 1.2,
      emotion: "happy",
      brollEnabled: true,
      maxInserts: 6,
      subtitleEnabled: true,
      subtitleStyle: "movie",
      subtitleSize: 42,
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
    expect(defaults.bgm?.auto_mix).toBe(false);
    expect(defaults.cover?.mode).toBe("ai");
    expect(defaults.lipsync?.timeout_minutes).toBe(45);
    expect(defaults).not.toHaveProperty("script");
    expect(defaults).not.toHaveProperty("edit");
  });

  it("hydrates defaults with clamped values and validates seedance voice exemption", () => {
    const defaults: UserGenerationDefaults = {
      voice: { voice_id: "voice_2", speed: 4, emotion: "serious", volume: 1 },
      subtitle: { enabled: true, style_preset: "news", font_size: 8 },
      lipsync: { enabled: true, provider_profile_id: "runninghub.heygem.prod", timeout_minutes: 2 },
    };
    const form = mapDefaultsToForm(defaults, loadStoredForm());

    expect(form.voiceId).toBe("voice_2");
    expect(form.speed).toBe(2);
    expect(form.subtitleStyle).toBe("news");
    expect(form.subtitleSize).toBe(12);
    expect(form.lipsyncTimeoutMinutes).toBe(5);
    expect(validateAll({ ...form, script: "文案", contentMode: "seedance" }, "")).toBeNull();
    expect(contentModeLabel("editing_agent")).toBe("AI 综合剪辑");
  });
});
