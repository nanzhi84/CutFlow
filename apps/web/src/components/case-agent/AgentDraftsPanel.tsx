import { CheckCircle2, Loader2 } from "lucide-react";
import type { AgentDraft } from "../../api/r6";
import { EmptyState, LoadingState } from "../State";
import { StatusPill } from "../Status";
import { TimeText } from "../TimeText";

type Props = {
  drafts: AgentDraft[];
  isLoading: boolean;
  adoptingDraftId?: string | null;
  onAdopt: (draft: AgentDraft) => void;
};

export function AgentDraftsPanel({ drafts, isLoading, adoptingDraftId, onAdopt }: Props) {
  return (
    <section className="card grid gap-4">
      <div className="sectionHeader">
        <div>
          <h2>草稿列表</h2>
          <p>采用后会写入脚本版本，并带来源回填到创作页。</p>
        </div>
        <span className="badge-warning">沙箱生成</span>
      </div>
      {isLoading ? <LoadingState label="加载草稿" /> : null}
      {!isLoading && drafts.length === 0 ? <EmptyState title="暂无草稿" detail="运行“生成脚本草稿”后会出现候选。" /> : null}
      <div className="divide-y divide-border/60">
        {drafts.map((draft) => (
          <article className="py-4 first:pt-0 last:pb-0" key={draft.id}>
            <div className="mb-2 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <h3 className="truncate text-base font-semibold text-text-primary">{draft.title}</h3>
                <p className="mt-1 text-xs text-text-tertiary"><TimeText value={draft.updated_at} /></p>
              </div>
              <StatusPill status={draft.status} />
            </div>
            <p className="line-clamp-5 whitespace-pre-wrap text-sm leading-relaxed text-text-secondary">{draft.script}</p>
            <button
              className="btn-primary mt-3 w-full justify-center"
              type="button"
              disabled={draft.status === "adopted" || adoptingDraftId === draft.id}
              onClick={() => onAdopt(draft)}
            >
              {adoptingDraftId === draft.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
              <span>{draft.status === "adopted" ? "已采用" : "采用到创作页"}</span>
            </button>
          </article>
        ))}
      </div>
    </section>
  );
}
