import { BarChart3 } from "lucide-react";
import type { MaterialUsageRankingReport } from "../../api/client";
import { formatRelativeTime, shortId } from "../../lib/format";
import { EmptyState, ErrorState, LoadingState } from "../ui/State";

export function UsageRankingPanel({
  report,
  isLoading,
  error,
  onItemClick,
}: {
  report?: MaterialUsageRankingReport;
  isLoading: boolean;
  error: unknown;
  /** When provided, each ranking item becomes clickable and jumps to that asset. */
  onItemClick?: (assetId: string) => void;
}) {
  const items = report?.items ?? [];
  return (
    <section className="grid content-start gap-3 rounded-2xl border border-border/70 bg-white/55 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-semibold text-text-primary">使用排行</h3>
        </div>
        <span className="badge bg-white/70 text-text-secondary">{items.length} 条</span>
      </div>
      {isLoading ? <LoadingState label="加载使用排行" /> : null}
      {error ? <ErrorState error={error} /> : null}
      {!isLoading && !error && items.length === 0 ? <EmptyState title="暂无使用记录" detail="素材被生产链路命中后会进入排行。" /> : null}
      {items.length > 0 ? (
        <div className="flex max-h-[70vh] flex-col gap-2 overflow-y-auto overflow-x-hidden pr-1">
          {items.map((item, index) => {
            const title = item.asset?.title ?? shortId(item.asset_id, 12);
            const inner = (
              <>
                <span className="mt-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-lg bg-accent/10 text-xs font-semibold text-accent">
                  {index + 1}
                </span>
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <div className="flex min-w-0 items-center justify-between gap-2">
                    <p className="min-w-0 truncate text-sm font-semibold text-text-primary" title={item.asset?.title ?? item.asset_id}>
                      {title}
                    </p>
                    <span className="badge shrink-0 bg-accent/10 text-accent">{item.task_use_count} 次</span>
                  </div>
                  <p className="min-w-0 truncate font-mono text-xs text-text-tertiary">{shortId(item.asset_id, 14)}</p>
                  <p className="min-w-0 truncate text-xs text-text-secondary">
                    片段 {item.segment_use_count} · 最近 {formatRelativeTime(item.last_used_at)}
                  </p>
                </div>
              </>
            );
            const className = "flex w-full min-w-0 items-start gap-3 rounded-xl border border-border/60 bg-white/65 p-3 text-left";
            return onItemClick ? (
              <button
                key={item.asset_id}
                type="button"
                className={`${className} transition-all hover:-translate-y-0.5 hover:border-accent/30 hover:bg-white/85`}
                onClick={() => onItemClick(item.asset_id)}
                title="点击跳转到该素材"
              >
                {inner}
              </button>
            ) : (
              <div key={item.asset_id} className={className}>
                {inner}
              </div>
            );
          })}
        </div>
      ) : null}
    </section>
  );
}
