import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import type { AgentMemoryProposal } from "../../api/r6";
import { EmptyState, LoadingState } from "../State";
import { TimeText } from "../TimeText";
import { memoryStatusLabel } from "./caseAgentModel";

type Props = {
  proposals: AgentMemoryProposal[];
  isLoading: boolean;
  busyProposalId?: string | null;
  onApprove: (proposal: AgentMemoryProposal) => void;
  onReject: (proposal: AgentMemoryProposal) => void;
};

export function AgentMemoryPanel({ proposals, isLoading, busyProposalId, onApprove, onReject }: Props) {
  return (
    <section className="card grid gap-4">
      <div className="sectionHeader">
        <div>
          <h2>记忆提案</h2>
          <p>批准或拒绝都会调用后端记忆 API，状态以服务端为准。</p>
        </div>
        <span className="badge-info">{proposals.length} 条</span>
      </div>
      {isLoading ? <LoadingState label="加载记忆提案" /> : null}
      {!isLoading && proposals.length === 0 ? <EmptyState title="暂无记忆提案" detail="运行“提出记忆提案”后再处理。" /> : null}
      <div className="divide-y divide-border/60">
        {proposals.map((proposal) => {
          const busy = busyProposalId === proposal.id;
          const actionable = proposal.status === "proposed";
          return (
            <article className="py-4 first:pt-0 last:pb-0" key={proposal.id}>
              <div className="mb-2 flex items-start justify-between gap-3">
                <span className="rounded-full border border-border/70 bg-surface px-2.5 py-1 text-xs text-text-secondary">
                  {memoryStatusLabel(proposal.status)}
                </span>
                <span className="text-xs text-text-tertiary"><TimeText value={proposal.updated_at} /></span>
              </div>
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-text-primary">{proposal.insight}</p>
              {proposal.evidence?.length ? (
                <ul className="mt-3 grid gap-1 text-xs text-text-secondary">
                  {proposal.evidence.slice(0, 3).map((item) => (
                    <li key={item}>• {item}</li>
                  ))}
                </ul>
              ) : null}
              {actionable ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  <button className="btn-primary" type="button" onClick={() => onApprove(proposal)} disabled={busy}>
                    {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                    <span>批准入库</span>
                  </button>
                  <button className="btn-secondary" type="button" onClick={() => onReject(proposal)} disabled={busy}>
                    <XCircle className="h-4 w-4" />
                    <span>拒绝</span>
                  </button>
                </div>
              ) : null}
            </article>
          );
        })}
      </div>
    </section>
  );
}
