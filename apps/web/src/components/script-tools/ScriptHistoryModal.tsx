import { Check, Copy, ListPlus } from "lucide-react";
import { useState } from "react";
import { Modal } from "../ui/Modal";
import { TimeText } from "../TimeText";
import type { ScriptToolItem } from "./scriptToolModel";

type Props = {
  isOpen: boolean;
  history: ScriptToolItem[];
  onClose: () => void;
  onInsert: (item: ScriptToolItem) => void;
};

export function ScriptHistoryModal({ isOpen, history, onClose, onInsert }: Props) {
  const [copiedId, setCopiedId] = useState<string | null>(null);
  return (
    <Modal isOpen={isOpen} onClose={onClose} title="脚本生成历史（最近 30 条）" size="xl">
      <div className="divide-y divide-border/60">
        {history.length === 0 ? (
          <div className="stateBox">
            <strong>暂无历史</strong>
            <span>生成或润色脚本后会自动记录。</span>
          </div>
        ) : null}
        {history.map((item) => (
          <article className="py-4 first:pt-0 last:pb-0" key={item.id}>
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-2">
                <strong className="truncate text-text-primary">{item.title}</strong>
                <span className="badge-warning">沙箱生成</span>
              </div>
              <span className="text-xs text-text-tertiary"><TimeText value={item.createdAt} /></span>
            </div>
            <p className="line-clamp-4 whitespace-pre-wrap text-sm leading-relaxed text-text-secondary">{item.script}</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                className="btn-secondary text-sm"
                type="button"
                onClick={() => {
                  void navigator.clipboard.writeText(item.script);
                  setCopiedId(item.id);
                }}
              >
                {copiedId === item.id ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                <span>{copiedId === item.id ? "已复制" : "复制"}</span>
              </button>
              <button className="btn-primary text-sm" type="button" onClick={() => onInsert(item)}>
                <ListPlus className="h-4 w-4" />
                <span>插入</span>
              </button>
            </div>
          </article>
        ))}
      </div>
    </Modal>
  );
}
