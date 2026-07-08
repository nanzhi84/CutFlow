import {
  Captions,
  Film,
  Mic2,
  Music,
  Play,
  Settings2,
  Sparkles,
  Volume2,
  type LucideIcon,
} from "lucide-react";
import type { ReactNode } from "react";
import {
  contentModeLabel,
  emotionOptions,
  subtitleLabel,
  visualModeLabel,
  type FormState,
} from "./studioCreateModel";
import { SeedanceReferencePicker } from "./SeedanceReferencePicker";
import { voiceDisplayLabel } from "../library/libraryModel";

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
    { value: "deterministic", label: "确定算法剪辑", detail: "规则算法按脚本、时间线和案例素材稳定规划人像 / B-roll / 字体 / BGM。" },
    { value: "editing_agent", label: "Agent智能剪辑", detail: "剪辑 Agent 结合额外要求统一规划人像 / B-roll / 字体 / BGM。" },
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
            剪辑 Agent 会在生成这条视频时参考它，统一规划人像 / B-roll / 字体 / BGM；留空则按通用最佳实践。
          </span>
        </label>
      ) : isDeterministic ? (
        <div className="stateBox muted">
          <span>确定算法剪辑会用规则算法稳定选择人像、B-roll、字幕和 BGM。</span>
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
    <div className="grid gap-4">
      <SectionTitle icon={Settings2} title="后处理" description="配置字幕、BGM 和封面策略。" />
      <ToggleLine checked={form.subtitleEnabled} onChange={(checked) => setField("subtitleEnabled", checked)} label="启用字幕" />
      {form.subtitleEnabled ? (
        <div className="grid gap-3 md:grid-cols-2">
          <label>
            <span>字幕样式</span>
            <select value={form.subtitleStyle} onChange={(event) => setField("subtitleStyle", event.target.value as FormState["subtitleStyle"])}>
              <option value="douyin">抖音风</option>
              <option value="clean">简洁风</option>
              <option value="variety">综艺风</option>
              <option value="news">新闻风</option>
              <option value="movie">电影风</option>
              <option value="youshe_title_black">标题黑风</option>
            </select>
          </label>
          <label>
            <span>字幕字号</span>
            <input type="number" min={12} max={96} value={form.subtitleSize} onChange={(event) => setField("subtitleSize", Number(event.target.value))} />
          </label>
        </div>
      ) : null}
      <ToggleLine checked={form.bgmEnabled} onChange={(checked) => setField("bgmEnabled", checked)} label="启用 BGM" />
      {form.bgmEnabled ? (
        <div className="grid gap-3 border-t border-border/60 pt-4">
          <VolumeSlider value={form.bgmVolume} onChange={(value) => setField("bgmVolume", value)} />
          <ToggleLine checked={form.bgmAutoMix} onChange={(checked) => setField("bgmAutoMix", checked)} label="自动混音" />
        </div>
      ) : null}
      <label>
        <span>封面</span>
        <select value={form.coverMode} onChange={(event) => setField("coverMode", event.target.value as FormState["coverMode"])}>
          <option value="frame">取帧</option>
          <option value="ai">AI 生成</option>
          <option value="none">不生成</option>
        </select>
      </label>
    </div>
  );
}

export function SubmitStep({ form, selectedVoiceLabel, scriptCount }: { form: FormState; selectedVoiceLabel: string; scriptCount: number }) {
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
            <ReviewItem label="字幕" value={form.subtitleEnabled ? `${subtitleLabel(form.subtitleStyle)} · ${form.subtitleSize}px` : "关闭"} />
            <ReviewItem label="BGM" value={form.bgmEnabled ? `${Math.round(form.bgmVolume * 100)}%` : "关闭"} />
          </>
        )}
      </div>
    </div>
  );
}

export function ConfigSummary({ form, selectedVoiceLabel, scriptCount }: { form: FormState; selectedVoiceLabel: string; scriptCount: number }) {
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
            <SummaryRow icon={Captions} label="字幕" value={form.subtitleEnabled ? `${subtitleLabel(form.subtitleStyle)} · ${form.subtitleSize}px` : "关闭"} />
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

function VolumeSlider({ value, onChange }: { value: number; onChange: (value: number) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-3 sm:flex-nowrap">
      <span className="w-16 shrink-0 text-sm text-text-secondary">音量</span>
      <Volume2 className="h-4 w-4 text-text-tertiary" />
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="min-w-[150px] flex-1 accent-accent"
      />
      <span className="w-12 shrink-0 text-right font-mono text-sm text-text-primary">{Math.round(value * 100)}%</span>
    </div>
  );
}

function SummaryRow({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: string }) {
  return (
    <div className="flex items-center gap-3 py-3">
      <Icon className="h-4 w-4 text-accent" />
      <div className="min-w-0">
        <p className="text-xs text-text-tertiary">{label}</p>
        <p className="truncate text-sm font-medium text-text-primary">{value}</p>
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
