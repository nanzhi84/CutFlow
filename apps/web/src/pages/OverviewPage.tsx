import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, BarChart3, Bell, BellOff, RefreshCw, Sparkles } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";
import { Link } from "react-router-dom";
import { api, type RunCard } from "../api/client";
import { ErrorState } from "../components/ui/State";
import { useToast } from "../components/ui/Toast";
import { OverviewSidePanel } from "../components/overview/OverviewSidePanel";
import { OverviewStatCards } from "../components/overview/OverviewStatCards";
import { RecentRunsList } from "../components/overview/RecentRunsList";
import { buildOverviewStats, sortRecentRuns } from "../components/overview/overviewModel";
import { usePageVisible } from "../hooks/usePageVisible";
import { useTaskNotifications } from "../hooks/useTaskNotifications";
import { shortId } from "../lib/format";
import { routes } from "../routes";

async function loadOverviewRuns() {
  const overview = await api.runs.overview({ limit: 8 });
  return {
    ...overview,
    items: sortRecentRuns(overview.items).slice(0, 8),
  };
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
  const runOverview = useQuery({
    queryKey: ["overview", "runs"],
    queryFn: loadOverviewRuns,
    refetchInterval: pageVisible ? 15000 : false,
  });
  const runs = runOverview.data?.items ?? [];
  const stats = buildOverviewStats(runOverview.data);

  // System notifications (fire regardless of focus, batch transitions merged
  // into one). Permission is requested only via the toggle's click handler.
  const notificationRuns = useMemo(
    () => runs.map((run) => ({ runId: run.runId, title: run.title, status: run.status })),
    [runs],
  );
  const notifications = useTaskNotifications({ runs: notificationRuns });

  useEffect(() => {
    if (!runOverview.data) return;
    const previous = previousStatuses.current;
    runOverview.data.items.forEach((run) => {
      const lastStatus = previous.get(run.runId);
      if (lastStatus && lastStatus !== run.status && isTerminalStatus(run.status)) {
        const message = `${run.title} · ${shortId(run.runId)}`;
        if (run.status === "succeeded") toast.success("任务已完成", message);
        else toast.error(run.status === "cancelled" ? "任务已取消" : "任务失败", message);
      }
      previous.set(run.runId, run.status);
    });
  }, [runOverview.data, toast]);

  async function toggleNotifications() {
    const next = !notifications.enabled;
    const resolved = await notifications.toggle(next);
    if (next && resolved === "unsupported") {
      toast.warning("当前浏览器不支持系统通知", "已保留页内提醒");
    } else if (next && resolved === "denied") {
      toast.warning("通知权限被拒绝", "可在浏览器站点设置中开启，页内提醒仍生效");
    } else if (next && resolved === "granted") {
      toast.success("已开启完成通知", "任务结束时会弹出系统通知");
    }
  }

  function refreshAll() {
    void queryClient.invalidateQueries({ queryKey: ["ops", "dashboard"] });
    void queryClient.invalidateQueries({ queryKey: ["overview", "runs"] });
  }

  return (
    <div className="space-y-5 pb-2">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="font-display text-3xl text-text-primary">概览</h1>
          <p className="mt-1 text-sm text-text-secondary">任务运行、成本用量和生产状态概览</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {notifications.supported ? (
            <button
              className="btn-secondary text-sm"
              type="button"
              onClick={toggleNotifications}
              title={notifications.enabled ? "关闭完成时的系统通知" : "任务完成时通过系统通知提醒我"}
            >
              {notifications.enabled ? <Bell className="h-4 w-4" /> : <BellOff className="h-4 w-4" />}
              {notifications.enabled ? "完成时通知我" : "开启完成通知"}
            </button>
          ) : null}
          <button className="btn-secondary text-sm" type="button" onClick={refreshAll}>
            <RefreshCw className={`h-4 w-4 ${dashboard.isFetching || runOverview.isFetching ? "animate-spin" : ""}`} />
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
      {runOverview.error ? <ErrorState error={runOverview.error} /> : null}

      <OverviewStatCards stats={stats} isLoading={runOverview.isLoading} />

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.65fr)_360px]">
        <RecentRunsList runs={runs} isLoading={runOverview.isLoading} />
        <OverviewSidePanel stats={stats} dashboard={dashboard.data} />
      </div>

      {stats.total === 0 && !dashboard.isLoading && !runOverview.isLoading ? (
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
