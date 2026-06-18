import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronUp,
  Link2,
  Loader2,
  Megaphone,
  Plus,
  SlidersHorizontal,
  Sparkles,
  Tag,
  User,
} from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../../api/client";
import { caseAgentApi } from "../../api/r6";
import { Modal } from "../ui/Modal";
import {
  CREATION_MODE_META,
  CREATION_MODE_OPTIONS,
  DEFAULT_CREATION_MODE,
  DEFAULT_SCENE,
  DURATION_OPTIONS,
  GENERATION_COUNTS,
  SCENE_META,
  SCENE_OPTIONS,
  STRATEGY_TAGS,
  buildGenerationBrief,
  newScriptToolId,
  operationFor,
  type CreationMode,
  type SceneType,
  type ScriptToolItem,
  type ScriptToolMode,
} from "./scriptToolModel";

type Props = {
  isOpen: boolean;
  mode: ScriptToolMode;
  caseId: string;
  currentScript: string;
  onClose: () => void;
  onAdopt: (item: ScriptToolItem) => void;
  onAddCandidate: (item: ScriptToolItem) => void;
  onHistory: (items: ScriptToolItem[]) => void;
};

export function ScriptGenerateModal({
  isOpen,
  mode,
  caseId,
  currentScript,
  onClose,
  onAdopt,
  onAddCandidate,
  onHistory,
}: Props) {
  const isPolish = mode === "polish";
  const [scene, setScene] = useState<SceneType>(DEFAULT_SCENE);
  const [creationMode, setCreationMode] = useState<CreationMode>(DEFAULT_CREATION_MODE);
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [benchmarkUrl, setBenchmarkUrl] = useState("");
  const [referenceScript, setReferenceScript] = useState("");
  const [duration, setDuration] = useState(DURATION_OPTIONS[0].value);
  const [count, setCount] = useState(3);
  const [goal, setGoal] = useState("");
  const [avoid, setAvoid] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);

  const [extracting, setExtracting] = useState(false);
  const [extractIssue, setExtractIssue] = useState<string | null>(null);
  const [extractMeta, setExtractMeta] = useState<string | null>(null);

  const [results, setResults] = useState<ScriptToolItem[]>([]);
  const [candidateIds, setCandidateIds] = useState<Set<string>>(new Set());
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const modeMeta = CREATION_MODE_META[creationMode];
  const sceneTags = STRATEGY_TAGS[scene];
  const needsReference = !isPolish && modeMeta.requiresReference;
  const referenceMissing = needsReference && !referenceScript.trim();

  const referenceLabel = isPolish ? "润色方向" : creationMode === "fresh" ? "创作补充" : "参考文案";
  const referenceHint = isPolish
    ? "（可选，告诉系统这次想强化什么、规避什么）"
    : modeMeta.inputHint;
  const referencePlaceholder = isPolish
    ? "例如：开头更抓人，结尾加一句行动召唤；保留产品卖点不变…"
    : modeMeta.placeholder;

  function toggleScene(next: SceneType) {
    setScene(next);
    const validNames = new Set(STRATEGY_TAGS[next].map((tag) => tag.name));
    setSelectedTags((prev) => prev.filter((name) => validNames.has(name)));
  }

  function toggleTag(name: string) {
    setSelectedTags((prev) => (prev.includes(name) ? prev.filter((t) => t !== name) : [...prev, name]));
  }

  async function extractReference() {
    const url = benchmarkUrl.trim();
    if (!url) return;
    setExtracting(true);
    setExtractIssue(null);
    setExtractMeta(null);
    try {
      const result = await api.creative.extractReference({ url, language: "zh" });
      setReferenceScript(result.reference_script);
      if (result.resolved_url) setBenchmarkUrl(result.resolved_url);
      const via = result.source === "subtitle" ? "字幕" : "语音转写";
      setExtractMeta(`已提取${result.platform ? `「${result.platform}」` : ""}文案（来源：${via}）`);
    } catch (err) {
      setExtractIssue(err instanceof Error ? err.message : "提取失败，请检查链接或手动粘贴文案");
    } finally {
      setExtracting(false);
    }
  }

  const composedGoal = useMemo(() => {
    const parts = [goal.trim(), avoid.trim() ? `规避表达：${avoid.trim()}` : ""].filter(Boolean);
    return parts.join("\n");
  }, [goal, avoid]);

  async function generate() {
    if (!caseId || referenceMissing) return;
    setIsGenerating(true);
    setError(null);
    setResults([]);
    const operation = operationFor(mode, creationMode);
    try {
      const drafts = await Promise.all(
        Array.from({ length: count }, (_, index) =>
          caseAgentApi.generateScript(caseId, {
            brief: buildGenerationBrief({
              mode,
              scene,
              creationMode,
              strategyTags: selectedTags,
              referenceScript,
              goal: composedGoal,
              duration,
              currentScript,
              index,
            }),
            memory_ids: [],
            variation_count: 1,
            persona_mode: scene,
            operation,
            strategy_tags: selectedTags,
            reference_script: referenceScript.trim() || null,
            duration: scene === "ip_persona" ? duration : null,
          }),
        ),
      );
      const friendlyTitle = isPolish ? "润色脚本" : "AI 生成脚本";
      const items = drafts.map((draft) => ({
        id: draft.id || newScriptToolId("draft"),
        caseId,
        title:
          !draft.title || draft.title === "Memory-guided draft" || draft.title === "Rubric-scored draft"
            ? friendlyTitle
            : draft.title,
        script: draft.script,
        source: "ai" as const,
        createdAt: draft.created_at ?? new Date().toISOString(),
      }));
      setResults(items);
      onHistory(items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成失败");
    } finally {
      setIsGenerating(false);
    }
  }

  function addCandidate(item: ScriptToolItem) {
    onAddCandidate({ ...item, source: "candidate" });
    setCandidateIds((current) => new Set([...current, item.id]));
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={isPolish ? "AI 润色脚本" : "AI 生成脚本"} size="2xl">
      <div className="grid gap-5">
        <Field label="场景选择">
          <div className="grid grid-cols-2 gap-3">
            {SCENE_OPTIONS.map((value) => {
              const selected = scene === value;
              const Icon = value === "hard_ad" ? Megaphone : User;
              return (
                <button
                  key={value}
                  type="button"
                  onClick={() => toggleScene(value)}
                  className={`flex items-center gap-3 rounded-2xl border-2 p-4 text-left transition-all ${
                    selected ? "border-accent bg-accent/5 ring-1 ring-accent/30" : "border-border/70 hover:border-accent/50"
                  }`}
                >
                  <span
                    className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${
                      selected ? "bg-accent/15 text-accent" : "bg-surface-hover text-text-tertiary"
                    }`}
                  >
                    <Icon className="h-5 w-5" />
                  </span>
                  <span className="min-w-0">
                    <span className={`block text-sm font-semibold ${selected ? "text-accent" : "text-text-primary"}`}>
                      {SCENE_META[value].label}
                    </span>
                    <span className="mt-0.5 block text-xs text-text-tertiary">{SCENE_META[value].description}</span>
                  </span>
                </button>
              );
            })}
          </div>
        </Field>

        {!isPolish ? (
          <Field label="创作模式">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              {CREATION_MODE_OPTIONS.map((value) => {
                const meta = CREATION_MODE_META[value];
                const selected = creationMode === value;
                return (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setCreationMode(value)}
                    className={`rounded-2xl border-2 p-4 text-left transition-all ${
                      selected ? "border-accent bg-accent/5 ring-1 ring-accent/30" : "border-border/70 hover:border-accent/50"
                    }`}
                  >
                    <span className={`block text-sm font-semibold ${selected ? "text-accent" : "text-text-primary"}`}>
                      {meta.title}
                    </span>
                    <span className="mt-1 block text-xs leading-relaxed text-text-tertiary">{meta.description}</span>
                  </button>
                );
              })}
            </div>
          </Field>
        ) : null}

        <Field label="策略标签（可多选）">
          <div className="flex flex-wrap gap-2">
            {sceneTags.map((tag) => {
              const selected = selectedTags.includes(tag.name);
              return (
                <button
                  key={tag.id}
                  type="button"
                  title={tag.description}
                  onClick={() => toggleTag(tag.name)}
                  className={`inline-flex items-center gap-1 rounded-full border px-3 py-1 text-sm transition-colors ${
                    selected ? "border-accent bg-accent text-white" : "border-border/70 bg-white/70 text-text-primary hover:border-accent"
                  }`}
                >
                  <Tag className="h-3 w-3" />
                  {tag.name}
                </button>
              );
            })}
          </div>
        </Field>

        {needsReference ? (
          <div className="grid gap-2 rounded-2xl border border-border/70 bg-surface-hover/40 p-3">
            <span className="text-sm font-semibold text-text-secondary">对标视频链接</span>
            <div className="flex gap-2">
              <input
                value={benchmarkUrl}
                onChange={(event) => setBenchmarkUrl(event.target.value)}
                placeholder="https://v.douyin.com/..."
                className="flex-1"
              />
              <button
                type="button"
                className="btn-secondary shrink-0"
                disabled={!benchmarkUrl.trim() || extracting}
                onClick={() => void extractReference()}
              >
                {extracting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Link2 className="h-4 w-4" />}
                <span>提取</span>
              </button>
            </div>
            {extractMeta ? <p className="text-xs text-status-success">{extractMeta}</p> : null}
            {extractIssue ? (
              <p className="flex items-start gap-1.5 text-xs text-status-warning">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{extractIssue}</span>
              </p>
            ) : null}
          </div>
        ) : null}

        <Field
          label={
            <span>
              {referenceLabel}
              {needsReference ? <span className="ml-1 text-status-error">*</span> : null}
              <span className="ml-1 font-normal text-text-tertiary">{referenceHint}</span>
            </span>
          }
        >
          <textarea
            value={referenceScript}
            onChange={(event) => setReferenceScript(event.target.value)}
            className="min-h-[80px]"
            placeholder={referencePlaceholder}
          />
        </Field>

        <div className="flex flex-wrap items-end gap-6">
          {!isPolish && scene === "ip_persona" ? (
            <Field label="预估时长" className="max-w-[220px]">
              <select value={duration} onChange={(event) => setDuration(event.target.value)}>
                {DURATION_OPTIONS.map((option) => (
                  <option value={option.value} key={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
          <Field label="生成数量">
            <div className="flex items-center gap-2">
              {GENERATION_COUNTS.map((value) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setCount(value)}
                  className={`rounded-xl border px-4 py-1.5 text-sm transition-colors ${
                    count === value ? "border-accent bg-accent text-white" : "border-border/70 bg-white/70 text-text-primary hover:border-accent"
                  }`}
                >
                  {value} 个
                </button>
              ))}
            </div>
          </Field>
        </div>

        <div className="overflow-hidden rounded-2xl border border-border/70">
          <button
            type="button"
            onClick={() => setShowAdvanced((value) => !value)}
            className="flex w-full items-center justify-between px-4 py-2.5 text-sm transition-colors hover:bg-surface-hover"
          >
            <span className="flex items-center gap-2 font-semibold text-text-primary">
              <SlidersHorizontal className="h-4 w-4 text-accent" />
              高级生成参数
            </span>
            {showAdvanced ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </button>
          {showAdvanced ? (
            <div className="grid gap-3 border-t border-border/70 bg-white/40 p-4">
              <label className="grid gap-1.5">
                <span>创作目标（可选）</span>
                <input value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="例如：前三秒给痛点，结尾引导私信" />
              </label>
              <label className="grid gap-1.5">
                <span>规避表达（可选）</span>
                <input value={avoid} onChange={(event) => setAvoid(event.target.value)} placeholder="例如：避免「最」「第一」等极限词" />
              </label>
            </div>
          ) : null}
        </div>

        <div className="grid gap-2">
          <button
            className="btn-primary w-full justify-center disabled:opacity-50"
            type="button"
            onClick={() => void generate()}
            disabled={isGenerating || !caseId || referenceMissing}
          >
            {isGenerating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
            <span>{isGenerating ? "生成中…" : "开始生成"}</span>
          </button>
          {referenceMissing ? (
            <p className="text-xs text-status-warning">「{modeMeta.title}」需要先填写或提取参考文案。</p>
          ) : null}
          <p className="text-xs text-text-tertiary">结果由 AI 生成，采用前请人工复核事实、敏感词和平台规则。</p>
          {error ? <p className="text-sm text-status-error">{error}</p> : null}
        </div>

        {results.length > 0 ? (
          <div className="grid gap-3 border-t border-border/60 pt-4">
            <p className="text-sm font-semibold text-text-secondary">生成结果（{results.length} 个版本）</p>
            <div className="grid gap-3 md:grid-cols-2">
              {results.map((item, index) => (
                <GeneratedCard
                  item={item}
                  index={index}
                  key={item.id}
                  isCandidate={candidateIds.has(item.id)}
                  onChange={(script) =>
                    setResults((current) => current.map((entry) => (entry.id === item.id ? { ...entry, script } : entry)))
                  }
                  onAdopt={() => onAdopt(item)}
                  onAddCandidate={() => addCandidate(item)}
                />
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </Modal>
  );
}

function Field({
  label,
  className = "",
  children,
}: {
  label: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`grid gap-2 ${className}`}>
      <span className="text-sm font-semibold text-text-secondary">{label}</span>
      {children}
    </div>
  );
}

function GeneratedCard({
  item,
  index,
  isCandidate,
  onChange,
  onAdopt,
  onAddCandidate,
}: {
  item: ScriptToolItem;
  index: number;
  isCandidate: boolean;
  onChange: (script: string) => void;
  onAdopt: () => void;
  onAddCandidate: () => void;
}) {
  return (
    <article className="grid gap-3 rounded-2xl border border-border/70 bg-white/55 p-4">
      <div className="flex items-center justify-between gap-3">
        <strong className="truncate text-text-primary">{item.title}</strong>
        <span className="badge-info">版本 {index + 1}</span>
      </div>
      <textarea
        value={item.script}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-[200px] text-sm leading-relaxed"
      />
      <div className="flex flex-wrap gap-2">
        <button className="btn-primary" type="button" onClick={onAdopt}>
          <Check className="h-4 w-4" />
          <span>采用</span>
        </button>
        <button className="btn-secondary" type="button" onClick={onAddCandidate} disabled={isCandidate}>
          <Plus className="h-4 w-4" />
          <span>{isCandidate ? "已加入候选" : "加入候选"}</span>
        </button>
      </div>
    </article>
  );
}
