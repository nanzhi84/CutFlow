import { CheckSquare, Loader2, RotateCcw, Send, Square } from "lucide-react";
import type { PublishAttempt, PublishBatch } from "../../api/client";
import { TimeText } from "../TimeText";
import { StatusPill } from "../ui/StatusPill";
import { PlatformChips } from "./PlatformChips";
import { type PublishDraft, itemCanPublish, itemCanRetry, platformLabel } from "./publishModel";

type PublishReviewStepProps = {
  batch: PublishBatch;
  drafts: Record<string, PublishDraft>;
  attempts: PublishAttempt[];
  isSubmitting?: boolean;
  isRetrying?: boolean;
  onDraftChange: (itemId: string, patch: Partial<PublishDraft>) => void;
  onSubmit: (mode: "manual" | "auto") => void;
  onRetry: (itemId: string) => void;
  onBack: () => void;
};

export function PublishReviewStep({
  batch,
  drafts,
  attempts,
  isSubmitting = false,
  isRetrying = false,
  onDraftChange,
  onSubmit,
  onRetry,
  onBack,
}: PublishReviewStepProps) {
  const items = batch.items ?? [];
  const selectedItems = items.filter((item) => drafts[item.id]?.selected ?? item.selected);
  const allSelected = items.length > 0 && selectedItems.length === items.length;
  const publishableCount = selectedItems.filter(itemCanPublish).length;

  function toggleAll() {
    items.forEach((item) => onDraftChange(item.id, { selected: !allSelected }));
  }

  return (
    <section className="grid gap-4">
      <div className="card grid gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold text-text-primary">确认发布</h2>
            <p className="mt-1 text-sm text-text-secondary">
              已选中 {selectedItems.length} 条，可提交 {publishableCount} 条。发布仅生成内部发布记录。
            </p>
          </div>
          <StatusPill status={batch.status} />
        </div>
        <div className="rounded-2xl border border-status-info/25 bg-status-info/10 p-4 text-sm leading-6 text-status-info">
          半自动会生成待人工处理结果；全自动按已配置的发布适配器提交（默认沙盒适配器只写入本地发布记录，启用小V猫适配器后才会真正触达平台）。
        </div>
        <div className="flex flex-wrap justify-between gap-3 border-t border-border/70 pt-4">
          <button className="btn-secondary" type="button" onClick={onBack}>
            返回编辑
          </button>
          <div className="flex flex-wrap gap-2">
            <button className="btn-secondary" type="button" onClick={toggleAll}>
              {allSelected ? <CheckSquare className="h-4 w-4" /> : <Square className="h-4 w-4" />}
              全选
            </button>
            <button className="btn-primary" type="button" disabled={isSubmitting || publishableCount === 0} onClick={() => onSubmit("manual")}>
              {isSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              半自动发布
            </button>
            <button className="btn-secondary" type="button" disabled={isSubmitting || publishableCount === 0} onClick={() => onSubmit("auto")}>
              {isSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              全自动发布
            </button>
          </div>
        </div>
      </div>

      <div className="card grid gap-3">
        <h3 className="text-base font-semibold text-text-primary">发布清单</h3>
        {items.map((item) => {
          const draft = drafts[item.id];
          const selected = draft?.selected ?? item.selected;
          return (
            <div key={item.id} className={`rounded-2xl border p-4 ${selected ? "border-border/80 bg-white/60" : "border-border/60 bg-surface-hover/35 opacity-70"}`}>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <label className="flex cursor-pointer grid-cols-[auto_minmax(0,1fr)] items-start gap-3">
                  <input type="checkbox" checked={selected} onChange={(event) => onDraftChange(item.id, { selected: event.target.checked })} />
                  <span>
                    <span className="block text-sm font-semibold text-text-primary">{draft?.title || item.title}</span>
                    <span className="mt-1 block text-xs font-normal text-text-secondary">{draft?.description || item.description || "无正文"}</span>
                  </span>
                </label>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <StatusPill status={item.status} />
                  <PlatformChips value={[item.platform]} compact />
                </div>
              </div>
              {itemCanRetry(item) ? (
                <button className="btn-secondary mt-3 min-h-9 px-3" type="button" disabled={isRetrying} onClick={() => onRetry(item.id)}>
                  {isRetrying ? <Loader2 className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
                  重试失败条目
                </button>
              ) : null}
            </div>
          );
        })}
      </div>

      <div className="card grid gap-3">
        <h3 className="text-base font-semibold text-text-primary">发布结果</h3>
        {attempts.map((attempt) => (
          <div key={attempt.id} className="rounded-2xl border border-border/80 bg-white/60 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="min-w-0">
                <p className="font-mono text-xs text-text-tertiary">{attempt.id}</p>
                <p className="mt-1 text-sm text-text-secondary">
                  {attempt.platforms.map(platformLabel).join(" / ")} · {attempt.manual_review ? "半自动" : "全自动"} · 内部发布
                </p>
              </div>
              <StatusPill status={attempt.status} />
            </div>
            <div className="mt-2 flex flex-wrap gap-3 text-xs text-text-tertiary">
              <span>adapter: {attempt.adapter_id}</span>
              <span>创建 <TimeText value={attempt.created_at} /></span>
              {attempt.finished_at ? <span>完成 <TimeText value={attempt.finished_at} /></span> : null}
            </div>
            {attempt.error ? <p className="mt-2 text-sm text-status-error">{attempt.error.message}</p> : null}
          </div>
        ))}
        {attempts.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-border/80 bg-white/50 p-6 text-center text-sm text-text-secondary">
            尚无发布尝试；提交后会显示 PublishAttempt 状态。
          </div>
        ) : null}
      </div>
    </section>
  );
}
