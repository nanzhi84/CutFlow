import { Loader2, Play, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Modal } from "../ui/Modal";
import type { ScriptToolItem } from "./scriptToolModel";

type Props = {
  isOpen: boolean;
  candidates: ScriptToolItem[];
  isBatchCreating: boolean;
  onClose: () => void;
  onUse: (item: ScriptToolItem) => void;
  onRemove: (id: string) => void;
  onClear: () => void;
  onBatchCreate: (items: ScriptToolItem[]) => void;
};

export function CandidatePoolModal({
  isOpen,
  candidates,
  isBatchCreating,
  onClose,
  onUse,
  onRemove,
  onClear,
  onBatchCreate,
}: Props) {
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const selectedItems = useMemo(() => candidates.filter((item) => selectedIds.includes(item.id)), [candidates, selectedIds]);

  function toggle(id: string, checked: boolean) {
    setSelectedIds((current) => (checked ? [...new Set([...current, id])] : current.filter((item) => item !== id)));
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={`候选脚本池（${candidates.length} 条）`} size="2xl">
      <div className="grid gap-4">
        {candidates.length === 0 ? (
          <div className="stateBox">
            <strong>暂无候选脚本</strong>
            <span>AI 生成结果可加入候选池后再批量出片。</span>
          </div>
        ) : (
          <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-text-secondary">
            <span>{selectedIds.length} / {candidates.length} 已选</span>
            <div className="flex gap-2">
              <button className="btn-secondary text-sm" type="button" onClick={() => setSelectedIds(candidates.map((item) => item.id))}>全选</button>
              <button className="btn-secondary text-sm" type="button" onClick={() => setSelectedIds([])}>取消</button>
              <button className="btn-secondary text-sm dangerButton" type="button" onClick={onClear}>清空</button>
            </div>
          </div>
        )}

        <div className="max-h-[56vh] divide-y divide-border/60 overflow-y-auto pr-1">
          {candidates.map((item) => (
            <article className="py-4 first:pt-0 last:pb-0" key={item.id}>
              <div className="mb-2 flex items-start gap-3">
                <input
                  className="mt-1 h-4 w-4 accent-accent"
                  type="checkbox"
                  checked={selectedIds.includes(item.id)}
                  onChange={(event) => toggle(item.id, event.target.checked)}
                />
                <div className="min-w-0 flex-1">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <strong className="truncate text-text-primary">{item.title}</strong>
                    <span className="badge-warning">沙箱生成</span>
                  </div>
                  <p className="line-clamp-4 whitespace-pre-wrap text-sm leading-relaxed text-text-secondary">{item.script}</p>
                </div>
                <div className="flex shrink-0 gap-1">
                  <button className="icon-button" type="button" title="选用" onClick={() => onUse(item)}>
                    <Play className="h-4 w-4" />
                  </button>
                  <button className="icon-button dangerButton" type="button" title="移除" onClick={() => onRemove(item.id)}>
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </article>
          ))}
        </div>

        <div className="flex justify-end">
          <button className="btn-primary" type="button" disabled={selectedItems.length === 0 || isBatchCreating} onClick={() => onBatchCreate(selectedItems)}>
            {isBatchCreating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
            <span>批量出片 {selectedItems.length ? `(${selectedItems.length})` : ""}</span>
          </button>
        </div>
      </div>
    </Modal>
  );
}
