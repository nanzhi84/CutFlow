import {
  Captions,
  Film,
  ImageOff,
  Mic2,
  Music,
  Play,
  Settings2,
  Sparkles,
  Type,
  Volume2,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type MediaAssetRecord } from "../../api/client";
import { FontFaceStyle } from "../library/FontFaceStyle";
import { fontFamilyName, voiceDisplayLabel } from "../library/libraryModel";
import { toDisplayUrl } from "../../lib/url";
import {
  SUBTITLE_PREVIEW_OUTPUT_HEIGHT,
  SUBTITLE_PREVIEW_OUTPUT_WIDTH,
  SUBTITLE_PREVIEW_WIDTH,
  CAPTION_POLICY,
  contentModeLabel,
  effectiveEmphasisEnabled,
  emotionOptions,
  subtitlePreviewSvgFontSize,
  supportsEmphasisCaption,
  visualModeLabel,
  type FormState,
} from "./studioCreateModel";
import { SeedanceReferencePicker } from "./SeedanceReferencePicker";

type SetField = <Key extends keyof FormState>(key: Key, value: FormState[Key]) => void;
type VoiceOption = {
  id: string;
  display_name: string;
  vendor: string;
  provider_profile_id?: string | null;
};

function seedanceReferenceSummary(count: number) {
  return count > 0 ? `${count} 张参考图` : "无参考图";
}

function lipsyncSummary(form: FormState) {
  return form.lipsyncEnabled ? `开启 · ${form.lipsyncTimeoutMinutes} 分钟` : "关闭";
}

function shortAssetId(assetId: string) {
  return assetId.length > 12 ? `${assetId.slice(0, 8)}…` : assetId;
}

type SubtitleFontLabels = {
  normal: string;
  emphasis: string;
};

function fontLabelFor(fonts: MediaAssetRecord[], fontId: string, fallback: string) {
  if (!fontId) return fallback;
  const font = fonts.find((item) => item.id === fontId);
  return font?.title ?? shortAssetId(fontId);
}

function useSubtitleFontLabels(form: FormState): SubtitleFontLabels {
  const fontsQuery = useQuery({
    queryKey: ["studio-create", "font-assets"],
    queryFn: () => api.mediaAssets.list({ limit: 200, kind: "font" }),
    enabled:
      form.contentMode !== "seedance" &&
      subtitleLayersEnabled(form) &&
      (Boolean(form.subtitleFontId) || Boolean(form.emphasisFontId)),
  });
  const fonts = (fontsQuery.data?.items ?? []).map((item) => item.asset);
  const normal = fontLabelFor(fonts, form.subtitleFontId, "默认字体");
  return {
    normal,
    emphasis: form.emphasisFontId ? fontLabelFor(fonts, form.emphasisFontId, "默认字体") : normal,
  };
}

function subtitleLayersEnabled(form: FormState) {
  return form.normalSubtitleEnabled || effectiveEmphasisEnabled(form);
}

function captionConfigSummary(form: FormState, fontLabels: SubtitleFontLabels) {
  const parts = [];
  if (form.normalSubtitleEnabled) parts.push(`正文 ${fontLabels.normal} ${form.subtitleSize}px`);
  if (effectiveEmphasisEnabled(form)) parts.push(`字幕内强调 ${fontLabels.emphasis} ${form.emphasisSize}px`);
  return parts.length > 0 ? parts.join(" / ") : "关闭";
}

const normalCaptionPreviewStyle = {
  fontWeight: 400,
  color: "#FFFFFF",
  outlineColor: "#000000",
  outline: 4,
};

const emphasisCaptionPreviewStyle = {
  fontWeight: 400,
  outlineColor: "#000000",
  outline: 4,
};

const emphasisColorOptions = ["#FFE84A", "#FF5C5C", "#38D9A9", "#4DABF7", "#FFFFFF"] as const;

export function ScriptStep({
  form,
  setField,
  scriptCount,
  tools,
}: {
  form: FormState;
  setField: SetField;
  scriptCount: number;
  tools?: ReactNode;
}) {
  return (
    <div className="grid gap-4">
      <SectionTitle icon={Sparkles} title="脚本" description="先确定标题和正文，脚本为空时不能进入下一步。" />
      {tools}
      <label>
        <span>标题</span>
        <input value={form.title} onChange={(event) => setField("title", event.target.value)} placeholder="留空时使用脚本摘要" />
      </label>
      <label>
        <span className={scriptCount === 0 ? "text-status-error" : undefined}>脚本正文</span>
        <textarea
          value={form.script}
          onChange={(event) => setField("script", event.target.value)}
          required
          className={scriptCount === 0 ? "border-status-error/40 bg-status-error/5" : undefined}
        />
      </label>
      <div className="flex items-center justify-between text-xs text-text-secondary">
        <span>{scriptCount === 0 ? "请输入脚本后继续" : "脚本已就绪"}</span>
        <span className="font-mono tabular-nums">{scriptCount} 字</span>
      </div>
    </div>
  );
}

export function TemplateStep({ form, setField, caseId }: { form: FormState; setField: SetField; caseId: string }) {
  const contentModeOptions: Array<{ value: FormState["contentMode"]; label: string; detail: string }> = [
    { value: "deterministic", label: "确定算法剪辑", detail: "规则算法按脚本、时间线和案例素材稳定规划人像 / B-roll，并完成普通字幕与 BGM。" },
    { value: "editing_agent", label: "Agent智能剪辑", detail: "媒体 Agent 选择人像 / B-roll，独立 BGM Agent 只选择配乐；字幕由固定字幕带确定性编排。" },
    { value: "seedance", label: "seedance文生视频", detail: "一次性生成 15s / 3:4 / 720p 短片，可纯文本出片，也可附参考图。" },
  ];
  const isDeterministic = form.contentMode === "deterministic";
  const isSeedance = form.contentMode === "seedance";
  const isEditingAgent = form.contentMode === "editing_agent";
  return (
    <div className="grid gap-4">
      <SectionTitle icon={Film} title="剪辑方式" description="选择本次视频的剪辑生成方式。" />
      <div className="divide-y divide-border/60 border-y border-border/60 md:grid md:grid-cols-3 md:divide-x">
        {contentModeOptions.map((option) => (
          <button
            type="button"
            key={option.value}
            onClick={() => setField("contentMode", option.value)}
            className={`px-3 py-4 text-left transition-colors ${
              form.contentMode === option.value ? "bg-accent/10 text-accent" : "hover:bg-hover"
            }`}
          >
            <span className="font-semibold text-text-primary">{option.label}</span>
            <span className="mt-2 block text-sm text-text-secondary">{option.detail}</span>
          </button>
        ))}
      </div>
      {isSeedance ? (
        <SeedanceReferencePicker
          caseId={caseId}
          selectedIds={form.seedanceReferenceAssetIds}
          onChange={(ids) => setField("seedanceReferenceAssetIds", ids)}
        />
      ) : isEditingAgent ? (
        <label className="grid gap-1.5">
          <span className="text-sm font-medium text-text-primary">剪辑要求（可选）</span>
          <textarea
            className="input min-h-[80px]"
            placeholder="例如：尽量使用穿搭相近的人像素材，B-roll 多展示施工细节。"
            value={form.editInstruction}
            onChange={(event) => setField("editInstruction", event.target.value)}
          />
          <span className="text-xs text-text-secondary">
            剪辑 Agent 会在生成这条视频时参考它来选择人像 / B-roll；字幕内强调由脚本语义与固定算法编排，BGM 由独立 Agent 选择。
          </span>
        </label>
      ) : isDeterministic ? (
        <div className="stateBox muted">
          <span>确定算法剪辑会用规则稳定选择人像、B-roll、固定字幕带与 BGM。</span>
        </div>
      ) : null}
    </div>
  );
}

export function ProductionStep({
  form,
  setField,
  selectedVoice,
  voiceOptions,
}: {
  form: FormState;
  setField: SetField;
  selectedVoice: string;
  voiceOptions: VoiceOption[];
}) {
  const isFullCoverageBroll = form.visualMode === "broll_full_coverage";
  if (form.contentMode === "seedance") {
    return (
      <div className="grid gap-4">
        <SectionTitle icon={Mic2} title="成片配置" description="Seedance 文生视频按提示词直接出片，参考图只用于辅助画面一致性。" />
        <div className="stateBox muted">
          <span>Seedance 模式无需配音、口型与 B-roll：画面固定 15s / 3:4 / 720p，由模型生成。</span>
        </div>
      </div>
    );
  }
  return (
    <div className="grid gap-4">
      <SectionTitle icon={Mic2} title="成片配置" description="配置声音、画面模式和 B-roll 策略。" />
      <label>
        <span className={!selectedVoice ? "text-status-error" : undefined}>声音</span>
        <select value={selectedVoice} onChange={(event) => setField("voiceId", event.target.value)}>
          {voiceOptions.length === 0 ? (
            <option value="" disabled>
              当前案例暂无可用音色，请先在音色库绑定
            </option>
          ) : null}
          {voiceOptions.map((voice) => (
            <option value={voice.id} key={voice.id}>
              {voiceDisplayLabel(voice)}
            </option>
          ))}
        </select>
      </label>
      <div className="grid gap-3 md:grid-cols-2">
        <label>
          <span>语速</span>
          <input type="number" min={0.5} max={2} step={0.1} value={form.speed} onChange={(event) => setField("speed", Number(event.target.value))} />
        </label>
        <label>
          <span>情绪</span>
          <select value={form.emotion} onChange={(event) => setField("emotion", event.target.value)}>
            {emotionOptions.map((option) => (
              <option value={option.value} key={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="grid gap-2">
        <span className="text-sm font-semibold text-text-primary">画面模式</span>
        <div className="grid grid-cols-2 overflow-hidden rounded-2xl border border-border/70">
          {[
            { value: "digital_human", label: "数字人模式", detail: "数字人主轨 + 可选 B-roll 插入。" },
            { value: "broll_full_coverage", label: "纯Broll模式", detail: "B-roll 铺满主画面，跳过口型同步。" },
          ].map((option) => (
            <button
              type="button"
              key={option.value}
              onClick={() => setField("visualMode", option.value as FormState["visualMode"])}
              className={`px-3 py-3 text-left transition-colors ${
                form.visualMode === option.value ? "bg-accent/10 text-accent" : "hover:bg-hover"
              }`}
            >
              <span className="block font-semibold text-text-primary">{option.label}</span>
              <span className="mt-1 block text-xs text-text-secondary">{option.detail}</span>
            </button>
          ))}
        </div>
      </div>
      {isFullCoverageBroll ? (
        <div className="stateBox muted">
          <span>纯Broll模式会使用 B-roll 按时间线窗口铺满全片，并跳过数字人轨道与口型同步。</span>
        </div>
      ) : (
        <>
          <ToggleLine checked={form.lipsyncEnabled} onChange={(checked) => setField("lipsyncEnabled", checked)} label="启用口型同步" />
          {form.lipsyncEnabled ? (
            <div className="grid gap-3 border-t border-border/60 pt-4">
              <label>
                <span>超时时间（分钟）</span>
                <input
                  type="number"
                  min={5}
                  max={90}
                  value={form.lipsyncTimeoutMinutes}
                  onChange={(event) => setField("lipsyncTimeoutMinutes", Number(event.target.value))}
                />
              </label>
            </div>
          ) : null}
          <ToggleLine checked={form.brollEnabled} onChange={(checked) => setField("brollEnabled", checked)} label="启用 B-roll 插入" />
          {form.brollEnabled ? (
            <label>
              <span>B-roll 最大插入数</span>
              <input type="number" min={0} max={20} value={form.maxInserts} onChange={(event) => setField("maxInserts", Number(event.target.value))} />
            </label>
          ) : null}
        </>
      )}
    </div>
  );
}

export function PostProcessStep({ form, setField }: { form: FormState; setField: SetField }) {
  const subtitleEnabled = subtitleLayersEnabled(form);
  const emphasisConfigurable = supportsEmphasisCaption(form.contentMode);
  const emphasisVisible = effectiveEmphasisEnabled(form);
  const fontsQuery = useQuery({
    queryKey: ["studio-create", "font-assets"],
    queryFn: () => api.mediaAssets.list({ limit: 200, kind: "font" }),
    enabled: form.contentMode !== "seedance" && subtitleEnabled,
  });
  const fonts = useMemo(
    () => (fontsQuery.data?.items ?? []).map((item) => item.asset),
    [fontsQuery.data?.items],
  );
  const selectedSubtitleFont = fonts.find((font) => font.id === form.subtitleFontId) ?? null;
  const selectedEmphasisFont = form.emphasisFontId ? fonts.find((font) => font.id === form.emphasisFontId) ?? null : null;
  const subtitlePreviewUrl = useFontPreviewUrl(selectedSubtitleFont);
  const emphasisPreviewUrl = useFontPreviewUrl(selectedEmphasisFont);

  if (form.contentMode === "seedance") {
    return (
      <div className="grid gap-4">
        <SectionTitle icon={Settings2} title="后处理" description="Seedance 模式跳过字幕、BGM 和封面配置。" />
        <div className="stateBox muted">
          <span>Seedance 会一次性生成 15s / 3:4 / 720p 成片；成片按无字版交付，本地流水线也不再混字幕、配乐或生成 AI 封面。</span>
        </div>
      </div>
    );
  }
  return (
    <div className="grid gap-3">
      <SectionTitle icon={Settings2} title="后处理" description="配置字幕、BGM 和封面策略。" />
      <div className={emphasisConfigurable ? "grid gap-2 sm:grid-cols-2" : "grid gap-2"}>
        <CompactToggle
          checked={form.normalSubtitleEnabled}
          onChange={(checked) => setField("normalSubtitleEnabled", checked)}
          label="普通字幕"
        />
        {emphasisConfigurable ? (
          <CompactToggle
            checked={form.emphasisEnabled}
            onChange={(checked) => setField("emphasisEnabled", checked)}
            label="字幕内强调"
            disabled={!form.normalSubtitleEnabled}
          />
        ) : null}
      </div>
      {subtitleEnabled ? (
        <div className="grid gap-4 border-y border-border/60 py-3 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div className="grid content-start gap-3">
            <div className="grid gap-3" data-testid="caption-style-groups">
              {form.normalSubtitleEnabled ? (
                <section className="grid gap-2" aria-labelledby="normal-caption-style-heading">
                  <h3 id="normal-caption-style-heading" className="text-xs font-semibold text-text-tertiary">
                    普通字幕
                  </h3>
                  <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_92px]">
                    <label>
                      <span>字体</span>
                      <select value={form.subtitleFontId} onChange={(event) => setField("subtitleFontId", event.target.value)}>
                        <option value="">默认字体</option>
                        {fonts.map((font) => (
                          <option value={font.id} key={font.id}>
                            {font.title}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span>字号</span>
                      <input
                        type="number"
                        min={12}
                        max={96}
                        value={form.subtitleSize}
                        onChange={(event) => setField("subtitleSize", Number(event.target.value))}
                      />
                    </label>
                  </div>
                  <label>
                    <span>位置</span>
                    <div className="flex items-center gap-3">
                      <input
                        type="range"
                        min={0.72}
                        max={0.92}
                        step={0.01}
                        value={form.subtitlePositionY}
                        onChange={(event) => setField("subtitlePositionY", Number(event.target.value))}
                        className="min-w-0 flex-1 accent-accent"
                      />
                      <span className="w-10 text-right font-mono text-xs text-text-secondary">
                        {Math.round(form.subtitlePositionY * 100)}
                      </span>
                    </div>
                  </label>
                </section>
              ) : null}
              {emphasisVisible ? (
                <section className="grid gap-2" aria-labelledby="emphasis-caption-style-heading">
                  <h3 id="emphasis-caption-style-heading" className="text-xs font-semibold text-text-tertiary">
                    强调文字
                  </h3>
                  <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_92px]">
                    <label>
                      <span>字体</span>
                      <select value={form.emphasisFontId} onChange={(event) => setField("emphasisFontId", event.target.value)}>
                        <option value="">跟随普通字幕</option>
                        {fonts.map((font) => (
                          <option value={font.id} key={font.id}>
                            {font.title}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span>字号</span>
                      <input
                        type="number"
                        min={12}
                        max={120}
                        value={form.emphasisSize}
                        onChange={(event) => setField("emphasisSize", Number(event.target.value))}
                      />
                    </label>
                  </div>
                  <label>
                    <span>颜色</span>
                    <div className="flex flex-wrap items-center gap-2 py-1 pl-1">
                      {emphasisColorOptions.map((color) => (
                        <button
                          type="button"
                          key={color}
                          aria-label={`强调颜色 ${color}`}
                          onClick={() => setField("emphasisColor", color)}
                          className={`h-8 w-8 rounded-full border shadow-sm transition ${
                            form.emphasisColor.toUpperCase() === color ? "border-accent ring-2 ring-accent/25" : "border-border/70"
                          }`}
                          style={{ backgroundColor: color }}
                        />
                      ))}
                      <input
                        type="color"
                        value={form.emphasisColor}
                        onChange={(event) => setField("emphasisColor", event.target.value.toUpperCase())}
                        className="h-8 w-10 cursor-pointer rounded-md border border-border/70 bg-white p-1"
                        aria-label="自定义强调颜色"
                      />
                      <span className="font-mono text-xs text-text-secondary">{form.emphasisColor.toUpperCase()}</span>
                    </div>
                  </label>
                </section>
              ) : null}
            </div>
            {fontsQuery.isLoading ? (
              <div className="stateBox muted">
                <span>字体库加载中</span>
              </div>
            ) : fonts.length === 0 ? (
              <div className="stateBox muted">
                <Type className="h-4 w-4" />
                <span>字体库为空，可先使用默认字体。</span>
              </div>
            ) : null}
          </div>
          <CaptionPreview
            form={form}
            selectedSubtitleFont={selectedSubtitleFont}
            subtitlePreviewUrl={subtitlePreviewUrl}
            selectedEmphasisFont={selectedEmphasisFont}
            emphasisPreviewUrl={emphasisPreviewUrl}
          />
        </div>
      ) : (
        <div className="stateBox muted">
          <Captions className="h-4 w-4" />
          <span>未启用字幕。</span>
        </div>
      )}
      <div className="grid gap-3 pt-2 md:grid-cols-2">
        <BgmControlPanel form={form} setField={setField} />
        <CoverModePanel value={form.coverMode} onChange={(value) => setField("coverMode", value)} />
      </div>
    </div>
  );
}

function BgmControlPanel({ form, setField }: { form: FormState; setField: SetField }) {
  return (
    <section className="grid content-start gap-3 rounded-lg border border-border/60 bg-white/45 p-3">
      <div className="grid grid-cols-[minmax(0,1fr)_52px] items-center gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Music className="h-4 w-4 shrink-0 text-accent" />
          <div className="min-w-0">
            <p className="text-sm font-semibold text-text-primary">BGM</p>
            <p className="text-xs text-text-tertiary">{form.bgmEnabled ? `${Math.round(form.bgmVolume * 100)}%` : "关闭"}</p>
          </div>
        </div>
        <div className="justify-self-end">
          <MiniSwitch checked={form.bgmEnabled} onChange={(checked) => setField("bgmEnabled", checked)} label="启用 BGM" />
        </div>
      </div>
      {form.bgmEnabled ? (
        <div className="grid gap-2 border-t border-border/50 pt-3">
          <div className="grid grid-cols-[auto_minmax(0,1fr)_44px] items-center gap-2">
            <Volume2 className="h-4 w-4 text-text-tertiary" />
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={form.bgmVolume}
              onChange={(event) => setField("bgmVolume", Number(event.target.value))}
              className="min-w-0 accent-accent"
              aria-label="BGM 音量"
            />
            <span className="text-right font-mono text-xs font-medium text-text-secondary">{Math.round(form.bgmVolume * 100)}%</span>
          </div>
          <div className="grid min-h-9 grid-cols-[minmax(0,1fr)_52px] items-center gap-3 rounded-md bg-surface-hover/70 py-1 pl-3 pr-0">
            <span className="text-sm font-medium text-text-primary">自动混音</span>
            <div className="justify-self-end">
              <MiniSwitch checked={form.bgmAutoMix} onChange={(checked) => setField("bgmAutoMix", checked)} label="自动混音" />
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}

const coverModeOptions: Array<{ value: FormState["coverMode"]; label: string; icon: LucideIcon }> = [
  { value: "frame", label: "取帧", icon: Film },
  { value: "ai", label: "AI 生成", icon: Sparkles },
  { value: "none", label: "不生成", icon: ImageOff },
];

function CoverModePanel({ value, onChange }: { value: FormState["coverMode"]; onChange: (value: FormState["coverMode"]) => void }) {
  const activeLabel = coverModeOptions.find((option) => option.value === value)?.label ?? "取帧";
  return (
    <section className="grid content-start gap-3 rounded-lg border border-border/60 bg-white/45 p-3">
      <div className="flex min-h-9 items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <Film className="h-4 w-4 shrink-0 text-accent" />
          <div className="min-w-0">
            <p className="text-sm font-semibold text-text-primary">封面</p>
            <p className="text-xs text-text-tertiary">{activeLabel}</p>
          </div>
        </div>
      </div>
      <div className="h-2" aria-hidden="true" />
      <div className="grid grid-cols-3 gap-1 rounded-lg bg-surface-hover p-1">
        {coverModeOptions.map(({ value: optionValue, label, icon: Icon }) => (
          <button
            type="button"
            key={optionValue}
            onClick={() => onChange(optionValue)}
            className={`flex min-h-9 items-center justify-center gap-1.5 rounded-md px-2 text-sm font-medium transition-colors ${
              value === optionValue ? "bg-white text-text-primary shadow-sm" : "text-text-secondary hover:bg-white/60 hover:text-text-primary"
            }`}
          >
            <Icon className="h-3.5 w-3.5 shrink-0" />
            <span className="truncate">{label}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function useFontPreviewUrl(selectedFont: MediaAssetRecord | null) {
  const selectedAssetId = selectedFont?.id ?? null;
  const [preview, setPreview] = useState<{ assetId: string; url: string } | null>(null);
  useEffect(() => {
    if (!selectedAssetId) {
      setPreview(null);
      return;
    }
    let cancelled = false;
    api.mediaAssets
      .previewUrl(selectedAssetId)
      .then((response) => {
        const url = toDisplayUrl(response.url);
        if (!cancelled) setPreview(url ? { assetId: selectedAssetId, url } : null);
      })
      .catch(() => {
        if (!cancelled) setPreview(null);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedAssetId]);
  return preview?.assetId === selectedAssetId ? preview.url : null;
}

function CaptionPreview({
  form,
  selectedSubtitleFont,
  subtitlePreviewUrl,
  selectedEmphasisFont,
  emphasisPreviewUrl,
}: {
  form: FormState;
  selectedSubtitleFont: MediaAssetRecord | null;
  subtitlePreviewUrl: string | null;
  selectedEmphasisFont: MediaAssetRecord | null;
  emphasisPreviewUrl: string | null;
}) {
  const [previewMode, setPreviewMode] = useState<"inline" | "whole_cue">("inline");
  const showEmphasis = effectiveEmphasisEnabled(form);
  const normalFamily = selectedSubtitleFont && subtitlePreviewUrl ? fontFamilyName(selectedSubtitleFont.id) : undefined;
  const emphasisFamily = selectedEmphasisFont && emphasisPreviewUrl ? fontFamilyName(selectedEmphasisFont.id) : normalFamily;
  const normalSize = Math.max(12, Math.min(96, form.subtitleSize));
  const emphasisSize = Math.max(12, Math.min(120, form.emphasisSize));
  const normalPreviewSize = subtitlePreviewSvgFontSize(normalSize);
  const emphasisPreviewSize = subtitlePreviewSvgFontSize(emphasisSize);
  return (
    <div className="grid gap-2">
      {selectedSubtitleFont && subtitlePreviewUrl ? <FontFaceStyle assetId={selectedSubtitleFont.id} url={subtitlePreviewUrl} /> : null}
      {selectedEmphasisFont && emphasisPreviewUrl && selectedEmphasisFont.id !== selectedSubtitleFont?.id ? (
        <FontFaceStyle assetId={selectedEmphasisFont.id} url={emphasisPreviewUrl} />
      ) : null}
      {showEmphasis ? (
        <div className="mx-auto flex rounded-lg bg-surface-hover p-1 text-xs">
          {(["inline", "whole_cue"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              className={`rounded-md px-2.5 py-1 ${previewMode === mode ? "bg-white text-text-primary shadow-sm" : "text-text-tertiary"}`}
              onClick={() => setPreviewMode(mode)}
            >
              {mode === "inline" ? "行内强调" : "整句强调"}
            </button>
          ))}
        </div>
      ) : null}
      <div className="mx-auto w-full" style={{ maxWidth: SUBTITLE_PREVIEW_WIDTH }}>
        <div
          className="relative overflow-hidden rounded-md bg-[#07131f] shadow-glow"
          style={{ aspectRatio: `${SUBTITLE_PREVIEW_OUTPUT_WIDTH} / ${SUBTITLE_PREVIEW_OUTPUT_HEIGHT}` }}
        >
          {form.normalSubtitleEnabled ? (
            <svg
              className="absolute inset-0 h-full w-full"
              viewBox={`0 0 ${SUBTITLE_PREVIEW_OUTPUT_WIDTH} ${SUBTITLE_PREVIEW_OUTPUT_HEIGHT}`}
              data-testid="fixed-caption-band-preview"
              data-anchor-x={CAPTION_POLICY.anchor_x}
              data-baseline-y={form.subtitlePositionY}
              data-line-height-ratio={CAPTION_POLICY.line_height_ratio}
              data-max-width-ratio={CAPTION_POLICY.max_width_ratio}
            >
              <text
                x={CAPTION_POLICY.anchor_x * SUBTITLE_PREVIEW_OUTPUT_WIDTH}
                y={form.subtitlePositionY * SUBTITLE_PREVIEW_OUTPUT_HEIGHT}
                textAnchor="middle"
                data-testid="fixed-caption-band-baseline"
              >
                {previewMode === "inline" ? (
                  <tspan
                    style={{
                      fontFamily: normalFamily,
                      fontSize: normalPreviewSize,
                      fontWeight: normalCaptionPreviewStyle.fontWeight,
                      fill: normalCaptionPreviewStyle.color,
                      stroke: normalCaptionPreviewStyle.outlineColor,
                      strokeWidth: normalCaptionPreviewStyle.outline,
                      paintOrder: "stroke fill",
                    }}
                  >
                    高端定制也能
                  </tspan>
                ) : null}
                <tspan
                  style={showEmphasis ? {
                    fontFamily: emphasisFamily,
                    fontSize: emphasisPreviewSize,
                    fontWeight: emphasisCaptionPreviewStyle.fontWeight,
                    fill: form.emphasisColor,
                    stroke: emphasisCaptionPreviewStyle.outlineColor,
                    strokeWidth: emphasisCaptionPreviewStyle.outline,
                    paintOrder: "stroke fill",
                  } : undefined}
                >
                  {previewMode === "inline" ? "字幕内强调" : "整句强调仍在固定字幕带"}
                </tspan>
              </text>
            </svg>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function SubmitStep({ form, selectedVoiceLabel, scriptCount }: { form: FormState; selectedVoiceLabel: string; scriptCount: number }) {
  const subtitleFontLabels = useSubtitleFontLabels(form);
  return (
    <div className="grid gap-4">
      <SectionTitle icon={Play} title="提交" description="确认配置后提交生产任务，成功后自动跳转到成片页。" />
      <div className="grid gap-3 md:grid-cols-2">
        <ReviewItem label="脚本" value={form.contentMode === "seedance" ? `提示词 ${scriptCount} 字` : `${scriptCount} 字`} />
        <ReviewItem label="剪辑方式" value={contentModeLabel(form.contentMode)} />
        {form.contentMode === "seedance" ? (
          <ReviewItem
            label="画面"
            value={`Seedance 文生 · 15s 3:4 720p · ${seedanceReferenceSummary(form.seedanceReferenceAssetIds.length)}`}
          />
        ) : (
          <ReviewItem label="声音" value={`${selectedVoiceLabel} · ${form.speed.toFixed(1)}x`} />
        )}
        {form.contentMode === "deterministic" || form.contentMode === "editing_agent" ? (
          <>
            <ReviewItem label="画面模式" value={visualModeLabel(form.visualMode)} />
            <ReviewItem
              label="口型"
              value={form.visualMode === "broll_full_coverage" ? "跳过" : lipsyncSummary(form)}
            />
            <ReviewItem
              label="B-roll"
              value={
                form.visualMode === "broll_full_coverage"
                  ? "全覆盖 · 按时间线窗口"
                  : form.brollEnabled
                    ? `插入 · 最多 ${form.maxInserts} 段`
                    : "关闭"
              }
            />
          </>
        ) : null}
        {form.contentMode === "seedance" ? (
          <ReviewItem label="后处理" value="不生成字幕 / 跳过本地 BGM / AI 封面" />
        ) : (
          <>
            <ReviewItem label="字幕" value={captionConfigSummary(form, subtitleFontLabels)} />
            <ReviewItem label="BGM" value={form.bgmEnabled ? `${Math.round(form.bgmVolume * 100)}%` : "关闭"} />
          </>
        )}
      </div>
    </div>
  );
}

export function ConfigSummary({ form, selectedVoiceLabel, scriptCount }: { form: FormState; selectedVoiceLabel: string; scriptCount: number }) {
  const subtitleFontLabels = useSubtitleFontLabels(form);
  return (
    <aside className="card grid content-start gap-4">
      <div>
        <h2 className="text-lg font-semibold text-text-primary">配置摘要</h2>
        <p className="text-sm">偏好会自动保存，刷新页面后继续沿用。</p>
      </div>
      <div className="divide-y divide-border/60">
        <SummaryRow icon={Film} label="剪辑方式" value={contentModeLabel(form.contentMode)} />
        {form.contentMode === "seedance" ? (
          <SummaryRow
            icon={Sparkles}
            label="画面"
            value={`15s 3:4 720p · ${seedanceReferenceSummary(form.seedanceReferenceAssetIds.length)}`}
          />
        ) : (
          <SummaryRow icon={Mic2} label="声音" value={`${selectedVoiceLabel} · ${form.speed.toFixed(1)}x`} />
        )}
        {form.contentMode === "deterministic" || form.contentMode === "editing_agent" ? (
          <>
            <SummaryRow icon={Film} label="画面模式" value={visualModeLabel(form.visualMode)} />
            <SummaryRow
              icon={Sparkles}
              label="口型"
              value={form.visualMode === "broll_full_coverage" ? "跳过" : lipsyncSummary(form)}
            />
            <SummaryRow
              icon={Film}
              label="B-roll"
              value={
                form.visualMode === "broll_full_coverage"
                  ? "全覆盖 · 按时间线窗口"
                  : form.brollEnabled
                    ? `插入 · 最多 ${form.maxInserts} 段`
                    : "关闭"
              }
            />
          </>
        ) : null}
        {form.contentMode === "seedance" ? null : (
          <>
            <SummaryRow icon={Captions} label="字幕" value={captionConfigSummary(form, subtitleFontLabels)} />
            <SummaryRow icon={Music} label="BGM" value={form.bgmEnabled ? `${Math.round(form.bgmVolume * 100)}%` : "关闭"} />
          </>
        )}
        <div className="flex items-baseline justify-between gap-3 py-3">
          <p className="text-xs text-text-tertiary">脚本字符数</p>
          <p className={`font-mono text-2xl font-bold ${scriptCount === 0 ? "text-status-error" : "text-text-primary"}`}>{scriptCount}</p>
        </div>
      </div>
    </aside>
  );
}

function SectionTitle({ icon: Icon, title, description }: { icon: LucideIcon; title: string; description: string }) {
  return (
    <div className="flex items-start gap-3">
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-accent/10 text-accent">
        <Icon className="h-5 w-5" />
      </span>
      <div>
        <h2 className="text-lg font-semibold text-text-primary">{title}</h2>
        <p className="text-sm">{description}</p>
      </div>
    </div>
  );
}

function ToggleLine({ checked, onChange, label }: { checked: boolean; onChange: (checked: boolean) => void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="-mx-2 flex items-center justify-between gap-3 border-t border-border/60 px-2 py-3 text-left transition-colors first:border-t-0 hover:bg-hover"
    >
      <span className="font-medium text-text-primary">{label}</span>
      <span className={`relative inline-flex h-7 w-12 shrink-0 items-center rounded-full transition-colors ${checked ? "bg-accent" : "bg-surface-hover"}`}>
        <span className={`inline-block h-5 w-5 rounded-full bg-white transition-transform ${checked ? "translate-x-6" : "translate-x-1"}`} />
      </span>
    </button>
  );
}

function CompactToggle({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="flex min-h-10 items-center justify-between gap-3 rounded-lg border border-border/60 bg-white/45 px-3 py-2 text-left transition-colors hover:bg-white/75 disabled:cursor-not-allowed disabled:opacity-45"
    >
      <span className="text-sm font-medium text-text-primary">{label}</span>
      <MiniSwitch checked={checked} onChange={onChange} label={label} isNested />
    </button>
  );
}

function MiniSwitch({
  checked,
  onChange,
  label,
  isNested = false,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  isNested?: boolean;
}) {
  const switchClassName = `relative inline-flex h-[28px] w-[52px] shrink-0 items-center rounded-full border p-[3px] shadow-inner transition-colors ${
    checked ? "border-accent/60 bg-accent/90" : "border-border/80 bg-surface-hover"
  }`;
  const knob = (
    <span
      className={`h-[22px] w-[22px] rounded-full bg-white shadow-[0_1px_4px_rgba(52,62,47,0.22)] transition-transform ${
        checked ? "translate-x-[24px]" : "translate-x-0"
      }`}
    />
  );
  if (isNested) {
    return (
      <span aria-hidden="true" className={switchClassName}>
        {knob}
      </span>
    );
  }
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={(event) => {
        if (isNested) event.stopPropagation();
        onChange(!checked);
      }}
      className={`${switchClassName} focus:outline-none focus:ring-2 focus:ring-accent/25`}
    >
      {knob}
    </button>
  );
}

function SummaryRow({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <div className="flex items-center gap-3 py-3">
      <Icon className="h-4 w-4 shrink-0 text-accent" />
      <div className="min-w-0 flex-1">
        <p className="text-xs text-text-tertiary">{label}</p>
        <p className="mt-0.5 max-w-full break-words text-sm font-medium leading-snug text-text-primary">{value}</p>
      </div>
    </div>
  );
}

function ReviewItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-t border-border/60 py-3 first:border-t-0">
      <p className="text-xs text-text-tertiary">{label}</p>
      <p className="mt-1 font-medium text-text-primary">{value}</p>
    </div>
  );
}
