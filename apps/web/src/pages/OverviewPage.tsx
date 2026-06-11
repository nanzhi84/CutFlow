import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, BarChart3, RefreshCw, Sparkles } from "lucide-react";
import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { api, type RunCard } from "../api/client";
import { ErrorState } from "../components/State";
import { useToast } from "../components/Toast";
import { OverviewSidePanel } from "../components/overview/OverviewSidePanel";
import { OverviewStatCards } from "../components/overview/OverviewStatCards";
import { RecentRunsList } from "../components/overview/RecentRunsList";
import { buildOverviewStats, sortRecentRuns } from "../components/overview/overviewModel";
import { usePageVisible } from "../hooks/usePageVisible";
import { shortId } from "../lib/format";
import { routes } from "../routes";

async function loadRecentRuns() {
  const cases = await api.cases.list({ limit: 20 });
  if (cases.items.length === 0) return [];
  const pages = await Promise.all(cases.items.map((item) => api.cases.runs(item.id, { limit: 8 })));
  return sortRecentRuns(pages.flatMap((page) => page.items)).slice(0, 8);
}

export default function OverviewPage() {
  const queryClient = useQueryClient();
  const pageVisible = usePageVisible();
  const toast = useToast();
  const previousStatuses = useRef<Map<string, RunCard["status"]>>(new Map());
  const dashboard = useQuery({
    queryKey: ["ops", "dashboard"],
    queryFn: () => api.ops.dashboard({}),
    refetchInterval: pageVisible ? 15000 : false,
  });
  const recentRuns = useQuery<RunCard[]>({
    queryKey: ["overview", "recent-runs"],
    queryFn: loadRecentRuns,
    refetchInterval: pageVisible ? 15000 : false,
  });
  const runs = recentRuns.data ?? [];
  const stats = buildOverviewStats(dashboard.data, runs);

  useEffect(() => {
    if (!recentRuns.data) return;
    const previous = previousStatuses.current;
    recentRuns.data.forEach((run) => {
      const lastStatus = previous.get(run.runId);
      if (lastStatus && lastStatus !== run.status && isTerminalStatus(run.status)) {
        const message = `${run.title} · ${shortId(run.runId)}`;
        if (run.status === "succeeded") toast.success("任务已完成", message);
        else toast.error(run.status === "cancelled" ? "任务已取消" : "任务失败", message);
      }
      previous.set(run.runId, run.status);
    });
  }, [recentRuns.data, toast]);

  function refreshAll() {
    void queryClient.invalidateQueries({ queryKey: ["ops", "dashboard"] });
    void queryClient.invalidateQueries({ queryKey: ["overview", "recent-runs"] });
  }

  return (
    <div className="space-y-5 pb-2">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="font-display text-3xl text-text-primary">概览</h1>
          <p className="mt-1 text-sm text-text-secondary">任务运行、成本用量和生产状态概览</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn-secondary text-sm" type="button" onClick={refreshAll}>
            <RefreshCw className={`h-4 w-4 ${dashboard.isFetching || recentRuns.isFetching ? "animate-spin" : ""}`} />
            刷新
          </button>
          <Link to={routes.analytics()} className="btn-secondary text-sm">
            <BarChart3 className="h-4 w-4" />
            详细统计
          </Link>
          <Link to={routes.studio()} className="btn-primary text-sm">
            进入工作台
            <ArrowRight className="h-4 w-4" />
          </Link>
        </div>
      </div>

      {dashboard.error ? <ErrorState error={dashboard.error} /> : null}
      {recentRuns.error ? <ErrorState error={recentRuns.error} /> : null}

      <OverviewStatCards stats={stats} isLoading={dashboard.isLoading && recentRuns.isLoading} />

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.65fr)_360px]">
        <RecentRunsList runs={runs} isLoading={recentRuns.isLoading} />
        <OverviewSidePanel stats={stats} dashboard={dashboard.data} />
      </div>

      {stats.total === 0 && !dashboard.isLoading && !recentRuns.isLoading ? (
        <section className="rounded-[24px] border border-dashed border-border bg-white/45 px-6 py-8">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-start gap-3">
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-accent/10 text-accent">
                <Sparkles className="h-5 w-5" />
              </div>
              <div>
                <h2 className="font-semibold text-text-primary">暂无生产数据</h2>
                <p className="mt-1 text-sm text-text-secondary">完成第一条案例任务后，这里会自动展示真实统计。</p>
              </div>
            </div>
            <Link to={routes.studio()} className="btn-primary text-sm">
              新建案例
              <ArrowRight className="h-4 w-4" />
            </Link>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function isTerminalStatus(status: RunCard["status"]) {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}
