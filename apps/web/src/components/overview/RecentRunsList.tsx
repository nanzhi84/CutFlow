import { ArrowRight, PlayCircle } from "lucide-react";
import { Link } from "react-router-dom";
import type { RunCard } from "../../api/client";
import { StatusPill } from "../ui/StatusPill";
import { TimeText } from "../TimeText";
import { Skeleton } from "../ui/Skeleton";
import { EmptyState } from "../ui/State";
import { routes } from "../../routes";
import { shortId } from "../../lib/format";

function runLink(run: RunCard) {
  if (!run.caseId) return routes.studio();
  return `${routes.caseOutputs(run.caseId)}?run=${encodeURIComponent(run.runId)}`;
}

function progressLabel(progress: number) {
  return `${Math.round(Math.max(0, Math.min(1, progress)) * 100)}%`;
}

export function RecentRunsList({
  runs,
  isLoading,
}: {
  runs: RunCard[];
  isLoading: boolean;
}) {
  return (
    <section className="card p-5 md:p-6">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-semibold text-text-primary">最近运行</h2>
          <p className="mt-1 text-sm text-text-secondary">最近 8 条成片任务，点击进入对应工作台</p>
        </div>
        <Link to={routes.studio()} className="text-sm font-medium text-accent hover:underline">
          案例中心
        </Link>
      </div>

      <div className="divide-y divide-border/60">
        {isLoading ? (
          Array.from({ length: 5 }).map((_, index) => <Skeleton key={index} className="h-20 w-full rounded-2xl" />)
        ) : runs.length > 0 ? (
          runs.slice(0, 8).map((run) => (
            <Link
              className="block py-3 transition-colors hover:bg-hover"
              key={run.runId}
              to={runLink(run)}
            >
              <div className="flex min-w-0 items-start gap-3">
                <div className="mt-0.5 flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-background-secondary/80 text-accent">
                  <PlayCircle className="h-5 w-5" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <StatusPill status={run.status} />
                      <code className="truncate rounded-full bg-accent/10 px-2 py-0.5 text-xs font-semibold text-accent">
                        {shortId(run.runId)}
                      </code>
                    </div>
                    <span className="shrink-0 text-xs text-text-tertiary">
                      <TimeText value={run.updatedAt ?? run.startedAt} />
                    </span>
                  </div>
                  <div className="mt-2 flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate font-medium text-text-primary">{run.title}</p>
                      <p className="mt-1 truncate text-sm text-text-secondary">{run.currentNodeLabel || "等待节点推进"}</p>
                    </div>
                    <ArrowRight className="mt-1 h-4 w-4 shrink-0 text-text-tertiary" />
                  </div>
                  <div className="mt-3 flex items-center gap-3">
                    <div className="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-hover">
                      <div className="h-full rounded-full bg-accent transition-all" style={{ width: progressLabel(run.progress) }} />
                    </div>
                    <span className="w-10 text-right font-mono text-xs font-semibold tabular-nums text-text-secondary">
                      {progressLabel(run.progress)}
                    </span>
                  </div>
                </div>
              </div>
            </Link>
          ))
        ) : (
          <EmptyState icon={PlayCircle} title="暂无运行记录" detail="创建视频任务后，这里会显示最新进度。" />
        )}
      </div>
    </section>
  );
}
