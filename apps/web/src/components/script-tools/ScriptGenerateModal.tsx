import { Check, Loader2, Plus, Sparkles } from "lucide-react";
import { useState } from "react";
import { caseAgentApi } from "../../api/r6";
import { Modal } from "../ui/Modal";
import { buildGenerationBrief, newScriptToolId, type ScriptToolItem, type ScriptToolMode } from "./scriptToolModel";

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
  const [goal, setGoal] = useState(mode === "polish" ? "表达更顺、更有转化力" : "生成可直接拍摄的数字人口播脚本");
  const [topic, setTopic] = useState("");
  const [count, setCount] = useState(3);
  const [results, setResults] = useState<ScriptToolItem[]>([]);
  const [candidateIds, setCandidateIds] = useState<Set<string>>(new Set());
  const [isGenerating, setIsGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function generate() {
    if (!caseId) return;
    setIsGenerating(true);
    setError(null);
    setResults([]);
    try {
      const drafts = await Promise.all(
        Array.from({ length: count }, (_, index) =>
          caseAgentApi.generateScript(caseId, {
            brief: buildGenerationBrief({ mode, goal, topic, currentScript, index }),
            memory_ids: [],
          }),
        ),
      );
      const items = drafts.map((draft) => ({
        id: draft.id || newScriptToolId("draft"),
        caseId,
        title: draft.title || (mode === "polish" ? "润色脚本" : "AI 生成脚本"),
        script: draft.script,
        source: "sandbox" as const,
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
    <Modal isOpen={isOpen} onClose={onClose} title={mode === "polish" ? "AI 润色脚本" : "AI 生成脚本"} size="2xl">
      <div className="grid gap-4">
        <div className="grid gap-3 border-t border-border/60 pt-4">
          <label>
            <span>目标</span>
            <input value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="例如：前三秒给痛点，结尾引导私信" />
          </label>
          <label>
            <span>主题提示</span>
            <textarea
              value={topic}
              onChange={(event) => setTopic(event.target.value)}
              className="min-h-[86px]"
              placeholder="补充产品卖点、受众、禁用表达或参考方向"
            />
          </label>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <label className="max-w-[220px]">
              <span>生成数量</span>
              <select value={count} onChange={(event) => setCount(Number(event.target.value))}>
                {[1, 2, 3, 5].map((value) => (
                  <option value={value} key={value}>
                    {value} 个
                  </option>
                ))}
              </select>
            </label>
            <button className="btn-primary" type="button" onClick={() => void generate()} disabled={isGenerating}>
              {isGenerating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
              <span>{isGenerating ? "生成中" : "开始生成"}</span>
            </button>
          </div>
          <p className="text-xs text-text-tertiary">结果由 sandbox LLM 生成，采用前请人工复核事实、敏感词和平台规则。</p>
          {error ? <p className="text-sm text-status-error">{error}</p> : null}
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          {results.map((item) => (
            <GeneratedCard
              item={item}
              key={item.id}
              isCandidate={candidateIds.has(item.id)}
              onChange={(script) => setResults((current) => current.map((entry) => (entry.id === item.id ? { ...entry, script } : entry)))}
              onAdopt={() => onAdopt(item)}
              onAddCandidate={() => addCandidate(item)}
            />
          ))}
        </div>
      </div>
    </Modal>
  );
}

function GeneratedCard({
  item,
  isCandidate,
  onChange,
  onAdopt,
  onAddCandidate,
}: {
  item: ScriptToolItem;
  isCandidate: boolean;
  onChange: (script: string) => void;
  onAdopt: () => void;
  onAddCandidate: () => void;
}) {
  return (
    <article className="grid gap-3 border-t border-border/60 pt-4">
      <div className="flex items-center justify-between gap-3">
        <strong className="truncate text-text-primary">{item.title}</strong>
        <span className="badge-warning">沙箱生成</span>
      </div>
      <textarea value={item.script} onChange={(event) => onChange(event.target.value)} className="min-h-[220px] text-sm leading-relaxed" />
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
