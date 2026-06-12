import { Database, Loader2, PlayCircle, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import type { AgentSourceBinding } from "../../api/r6";
import { EmptyState, LoadingState } from "../State";
import { TimeText } from "../TimeText";
import { sourceTypeHint, sourceTypeLabel, sourceTypeOptions, validateSourceRef, type SourceType } from "./caseAgentModel";

type Props = {
  bindings: AgentSourceBinding[];
  isLoading: boolean;
  selectedIds: string[];
  isCreating: boolean;
  busyBindingId?: string | null;
  onSelect: (bindingId: string, selected: boolean) => void;
  onCreate: (payload: { source_type: SourceType; source_ref: string; title?: string | null }) => Promise<unknown>;
  onImport: (binding: AgentSourceBinding) => void;
  onDelete: (binding: AgentSourceBinding) => void;
};

export function SourceBindingPanel({
  bindings,
  isLoading,
  selectedIds,
  isCreating,
  busyBindingId,
  onSelect,
  onCreate,
  onImport,
  onDelete,
}: Props) {
  const [sourceType, setSourceType] = useState<SourceType>("manual_note");
  const [title, setTitle] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const message = validateSourceRef(sourceType, sourceRef);
    if (message) {
      setError(message);
      return;
    }
    setError(null);
    await onCreate({ source_type: sourceType, source_ref: sourceRef.trim(), title: title.trim() || null });
    setTitle("");
    setSourceRef("");
  }

  return (
    <section className="card grid gap-4">
      <div className="sectionHeader">
        <div>
          <h2>数据源绑定</h2>
          <p>手动录入或引用外部数据，再选择是否参与本次智能体运行。</p>
        </div>
        <span className="badge-info">{bindings.length} 条</span>
      </div>

      <div className="grid gap-3 border-t border-border/60 pt-4">
        <label>
          <span>数据源类型</span>
          <select value={sourceType} onChange={(event) => setSourceType(event.target.value as SourceType)}>
            {sourceTypeOptions.map((option) => (
              <option value={option.value} key={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <p className="text-xs text-text-tertiary">{sourceTypeHint(sourceType)}</p>
        <label>
          <span>标题</span>
          <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="例如：投流复盘 6 月第一周" />
        </label>
        <label>
          <span>内容或引用</span>
          <textarea
            value={sourceRef}
            onChange={(event) => setSourceRef(event.target.value)}
            className="min-h-[96px]"
            placeholder={sourceType === "url" ? "https://example.com/report" : "粘贴资料正文、artifact id 或文件引用"}
          />
        </label>
        {error ? <p className="text-sm text-status-error">{error}</p> : null}
        <button className="btn-primary justify-center" type="button" onClick={() => void submit()} disabled={isCreating}>
          {isCreating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
          <span>创建绑定</span>
        </button>
      </div>

      {isLoading ? <LoadingState label="加载数据源" /> : null}
      {!isLoading && bindings.length === 0 ? <EmptyState title="暂无数据源" detail="先创建一个手动备注或 URL 数据源。" /> : null}
      <div className="divide-y divide-border/60">
        {bindings.map((binding) => (
          <article className="py-3 first:pt-0 last:pb-0" key={binding.id}>
            <div className="flex items-start justify-between gap-3">
              <label className="flex min-w-0 flex-1 items-start gap-3">
                <input
                  type="checkbox"
                  className="mt-1 h-4 w-4 accent-accent"
                  checked={selectedIds.includes(binding.id)}
                  onChange={(event) => onSelect(binding.id, event.target.checked)}
                />
                <span className="min-w-0">
                  <strong className="block truncate text-text-primary">{binding.title || sourceTypeLabel(binding.source_type)}</strong>
                  <span className="mt-1 block truncate text-xs text-text-secondary">{sourceTypeLabel(binding.source_type)} · {binding.source_ref}</span>
                  <span className="mt-1 block text-xs text-text-tertiary"><TimeText value={binding.updated_at} /></span>
                </span>
              </label>
              <div className="flex shrink-0 items-center gap-1">
                <button className="icon-button" type="button" onClick={() => onImport(binding)} title="导入数据源" disabled={busyBindingId === binding.id}>
                  {busyBindingId === binding.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <PlayCircle className="h-4 w-4" />}
                </button>
                <button className="icon-button dangerButton" type="button" onClick={() => onDelete(binding)} title="删除绑定">
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </div>
          </article>
        ))}
      </div>
      <div className="stateBox muted">
        <Database size={16} />
        <span>删除绑定不会删除已生成的草稿或记忆；后续运行不再引用该数据源。</span>
      </div>
    </section>
  );
}
