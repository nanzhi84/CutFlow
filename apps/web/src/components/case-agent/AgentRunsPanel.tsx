import { Loader2, PlayCircle } from "lucide-react";
import type { ReactNode } from "react";
import type { AgentRun, AgentRunDetail } from "../../api/r6";
import { EmptyState, LoadingState } from "../State";
import { StatusPill } from "../Status";
import { TimeText } from "../TimeText";
import { shortId } from "../../lib/format";
import { agentGoalLabel, agentGoalOptions } from "./caseAgentModel";

type Props = {
  runs: AgentRun[];
  detail?: AgentRunDetail;
  selectedRunId: string | null;
  selectedBindingCount: number;
  isLoading: boolean;
  isDetailLoading: boolean;
  isStarting: boolean;
  goal: AgentRun["goal"];
  onGoalChange: (goal: AgentRun["goal"]) => void;
  onSelectRun: (runId: string) => void;
  onStartRun: () => void;
};

export function AgentRunsPanel({
  runs,
  detail,
  selectedRunId,
  selectedBindingCount,
  isLoading,
  isDetailLoading,
  isStarting,
  goal,
  onGoalChange,
  onSelectRun,
  onStartRun,
}: Props) {
  return (
    <section className="card grid gap-4">
      <div className="sectionHeader">
        <div>
          <h2>智能体运行</h2>
          <p>运行历史与当前结果会按案例隔离保存。</p>
        </div>
        <span className="badge-info">{runs.length} 次</span>
      </div>

      <div className="grid gap-3 border-t border-border/60 pt-4 md:grid-cols-[minmax(0,1fr)_auto]">
        <label>
          <span>运行目标</span>
          <select value={goal} onChange={(event) => onGoalChange(event.target.value as AgentRun["goal"])}>
            {agentGoalOptions.map((option) => (
              <option value={option.value} key={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <button className="btn-primary self-end" type="button" onClick={onStartRun} disabled={isStarting}>
          {isStarting ? <Loader2 className="h-4 w-4 animate-spin" /> : <PlayCircle className="h-4 w-4" />}
          <span>立即运行智能体</span>
        </button>
        <p className="text-xs text-text-tertiary md:col-span-2">
          已选择 {selectedBindingCount} 个数据源；未选择时由后端使用案例上下文与可用记忆。
        </p>
      </div>

      {isLoading ? <LoadingState label="加载运行历史" /> : null}
      {!isLoading && runs.length === 0 ? <EmptyState title="暂无运行" detail="导入数据源或点击立即运行后会出现记录。" /> : null}
      <div className="grid gap-3 lg:grid-cols-[260px_minmax(0,1fr)]">
        <div className="divide-y divide-border/60 border-y border-border/60">
          {runs.map((run) => (
            <button
              className={`block w-full px-3 py-3 text-left transition-colors ${
                selectedRunId === run.id ? "bg-accent/10" : "hover:bg-hover"
              }`}
              type="button"
              key={run.id}
              onClick={() => onSelectRun(run.id)}
            >
              <div className="flex items-center justify-between gap-2">
                <strong className="font-mono text-sm text-text-primary">{shortId(run.id)}</strong>
                <StatusPill status={run.status} />
              </div>
              <p className="mt-2 text-xs text-text-secondary">{agentGoalLabel(run.goal)}</p>
              <p className="mt-1 text-xs text-text-tertiary"><TimeText value={run.updated_at} /></p>
            </button>
          ))}
        </div>
        <RunResult detail={detail} isLoading={isDetailLoading} />
      </div>
    </section>
  );
}

function RunResult({ detail, isLoading }: { detail?: AgentRunDetail; isLoading: boolean }) {
  if (isLoading) return <LoadingState label="加载运行结果" />;
  if (!detail) return <EmptyState title="未选择运行" detail="选择左侧运行后查看简报、草稿和记忆提案。" />;
  return (
    <div className="grid gap-3">
      <div className="border-b border-border/60 pb-3">
        <div className="mb-2 flex items-center justify-between gap-3">
          <h3 className="text-base font-semibold text-text-primary">{agentGoalLabel(detail.run.goal)}</h3>
          <StatusPill status={detail.run.status} />
        </div>
        <p className="text-xs text-text-tertiary">数据源：{detail.run.source_binding_ids?.length ?? 0} 个</p>
      </div>
      <ResultGroup title="创意简报" empty="本次运行未产出简报">
        {detail.briefs?.map((brief) => (
          <p className="whitespace-pre-wrap border-t border-border/40 py-2 text-sm leading-relaxed first:border-t-0" key={brief.id}>
            {brief.summary}
          </p>
        ))}
      </ResultGroup>
      <ResultGroup title="脚本草稿" empty="本次运行未产出脚本草稿">
        {detail.drafts?.map((draft) => (
          <div className="border-t border-border/40 py-2 first:border-t-0" key={draft.id}>
            <div className="mb-2 flex items-center justify-between gap-2">
              <strong className="text-sm text-text-primary">{draft.title}</strong>
              <span className="badge-warning">沙箱生成</span>
            </div>
            <p className="line-clamp-4 whitespace-pre-wrap text-sm leading-relaxed">{draft.script}</p>
          </div>
        ))}
      </ResultGroup>
      <ResultGroup title="记忆提案" empty="本次运行未提出记忆">
        {detail.memory_proposals?.map((proposal) => (
          <p className="border-t border-border/40 py-2 text-sm leading-relaxed first:border-t-0" key={proposal.id}>
            {proposal.insight}
          </p>
        ))}
      </ResultGroup>
    </div>
  );
}

function ResultGroup({ title, empty, children }: { title: string; empty: string; children: ReactNode }) {
  const content = Array.isArray(children) ? children.filter(Boolean) : children;
  const isEmpty = Array.isArray(content) ? content.length === 0 : !content;
  return (
    <div className="border-t border-border/60 pt-4">
      <h3 className="mb-3 text-sm font-semibold text-text-primary">{title}</h3>
      {isEmpty ? <p className="text-sm text-text-secondary">{empty}</p> : <div className="grid gap-2">{content}</div>}
    </div>
  );
}
