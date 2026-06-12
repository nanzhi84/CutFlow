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
  emotionOptions,
  lipsyncPresets,
  portraitModeLabel,
  rhythmLabel,
  subtitleLabel,
  type FormState,
  type LipSyncPreset,
} from "./studioCreateModel";

type SetField = <Key extends keyof FormState>(key: Key, value: FormState[Key]) => void;

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

export function TemplateStep({ form, setField }: { form: FormState; setField: SetField }) {
  const options: Array<{ value: FormState["portraitMode"]; label: string; detail: string }> = [
    { value: "agent", label: "自动模板", detail: "由系统按脚本和案例素材选择人像模板。" },
    { value: "specific", label: "指定模板", detail: "素材库接入后可选择固定模板。" },
    { value: "sequence", label: "模板序列", detail: "素材库接入后可编排模板序列。" },
  ];
  return (
    <div className="grid gap-4">
      <SectionTitle icon={Film} title="模板" description="本阶段沿用 M6a-1 的自动模板策略，指定模板与序列后续接入素材库。" />
      <div className="divide-y divide-border/60 border-y border-border/60 md:grid md:grid-cols-3 md:divide-x md:divide-y-0">
        {options.map((option) => (
          <button
            type="button"
            key={option.value}
            onClick={() => setField("portraitMode", option.value)}
            className={`px-3 py-4 text-left transition-colors ${
              form.portraitMode === option.value ? "bg-accent/10 text-accent" : "hover:bg-hover"
            }`}
          >
            <span className="font-semibold text-text-primary">{option.label}</span>
            <span className="mt-2 block text-sm text-text-secondary">{option.detail}</span>
          </button>
        ))}
      </div>
      {form.portraitMode !== "agent" ? (
        <div className="stateBox danger">
          <span>当前版本请切回自动模板后继续。</span>
        </div>
      ) : null}
      <label>
        <span>剪辑节奏</span>
        <select value={form.rhythmPreset} onChange={(event) => setField("rhythmPreset", event.target.value as FormState["rhythmPreset"])}>
          <option value="steady">稳</option>
          <option value="balanced">均衡</option>
          <option value="fast">快</option>
        </select>
      </label>
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
  voiceOptions: Array<{ id: string; display_name: string }>;
}) {
  return (
    <div className="grid gap-4">
      <SectionTitle icon={Mic2} title="成片配置" description="配置声音、口型同步和 B-roll 策略。" />
      <label>
        <span className={!selectedVoice ? "text-status-error" : undefined}>声音</span>
        <select value={selectedVoice} onChange={(event) => setField("voiceId", event.target.value)}>
          {voiceOptions.length === 0 ? <option value="voice_sandbox">沙盒声音</option> : null}
          {voiceOptions.map((voice) => (
            <option value={voice.id} key={voice.id}>
              {voice.display_name}
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
      <ToggleLine checked={form.lipsyncEnabled} onChange={(checked) => setField("lipsyncEnabled", checked)} label="启用口型同步" />
      {form.lipsyncEnabled ? (
        <div className="grid gap-3 border-t border-border/60 pt-4">
          <div className="grid gap-3 md:grid-cols-2">
            {(Object.keys(lipsyncPresets) as LipSyncPreset[]).map((preset) => (
              <button
                type="button"
                key={preset}
                onClick={() => {
                  setField("lipsyncPreset", preset);
                  setField("lipsyncVideoExtension", lipsyncPresets[preset].videoExtension);
                }}
                className={`border-l-2 px-3 py-2 text-left transition-colors ${
                  form.lipsyncPreset === preset ? "border-accent bg-accent/10" : "border-border/60 hover:bg-hover"
                }`}
              >
                <span className="font-medium text-text-primary">{lipsyncPresets[preset].label}</span>
                <span className="mt-1 block text-xs text-text-secondary">{lipsyncPresets[preset].description}</span>
              </button>
            ))}
          </div>
          <ToggleLine checked={form.lipsyncVideoExtension} onChange={(checked) => setField("lipsyncVideoExtension", checked)} label="允许视频时长扩展" />
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
    </div>
  );
}

export function PostProcessStep({ form, setField }: { form: FormState; setField: SetField }) {
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
        <ReviewItem label="脚本" value={`${scriptCount} 字`} />
        <ReviewItem label="声音" value={`${selectedVoiceLabel} · ${form.speed.toFixed(1)}x`} />
        <ReviewItem label="模板" value={`${portraitModeLabel(form.portraitMode)} · ${rhythmLabel(form.rhythmPreset)}`} />
        <ReviewItem label="口型" value={form.lipsyncEnabled ? lipsyncPresets[form.lipsyncPreset].label : "关闭"} />
        <ReviewItem label="字幕" value={form.subtitleEnabled ? `${subtitleLabel(form.subtitleStyle)} · ${form.subtitleSize}px` : "关闭"} />
        <ReviewItem label="BGM" value={form.bgmEnabled ? `${Math.round(form.bgmVolume * 100)}%` : "关闭"} />
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
        <SummaryRow icon={Mic2} label="声音" value={`${selectedVoiceLabel} · ${form.speed.toFixed(1)}x`} />
        <SummaryRow icon={Film} label="模板" value={`${portraitModeLabel(form.portraitMode)} · ${rhythmLabel(form.rhythmPreset)}`} />
        <SummaryRow icon={Sparkles} label="口型" value={form.lipsyncEnabled ? lipsyncPresets[form.lipsyncPreset].label : "关闭"} />
        <SummaryRow icon={Captions} label="字幕" value={form.subtitleEnabled ? `${subtitleLabel(form.subtitleStyle)} · ${form.subtitleSize}px` : "关闭"} />
        <SummaryRow icon={Music} label="BGM" value={form.bgmEnabled ? `${Math.round(form.bgmVolume * 100)}%` : "关闭"} />
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
