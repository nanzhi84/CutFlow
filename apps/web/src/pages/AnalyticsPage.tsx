import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import { ErrorState } from "../components/State";
import { AnalyticsTabs, RangeSegmentedControl } from "../components/analytics/AnalyticsControls";
import { AnalyticsKpiCards } from "../components/analytics/AnalyticsKpiCards";
import { CostUsageTab } from "../components/analytics/CostUsageTab";
import { TaskStatsTab } from "../components/analytics/TaskStatsTab";
import { YieldFunnelTab } from "../components/analytics/YieldFunnelTab";
import { rangeWindow, summarizeWorkflowStats, usageHasData, type AnalyticsTab, type TimeRange } from "../components/analytics/analyticsModel";
import { usePageVisible } from "../hooks/usePageVisible";

export default function AnalyticsPage() {
  const [range, setRange] = useState<TimeRange>("7d");
  const [tab, setTab] = useState<AnalyticsTab>("cost");
  const queryClient = useQueryClient();
  const pageVisible = usePageVisible();
  const window = useMemo(() => rangeWindow(range), [range]);
  const queryParams = { window_start: window.window_start, window_end: window.window_end };

  const dashboard = useQuery({
    queryKey: ["analytics", "dashboard", queryParams],
    queryFn: () => api.ops.dashboard(queryParams),
    refetchInterval: pageVisible ? 30000 : false,
  });
  const usage = useQuery({
    queryKey: ["analytics", "provider-usage", queryParams],
    queryFn: () => api.providers.usage(queryParams),
    refetchInterval: pageVisible ? 30000 : false,
  });
  const costRollups = useQuery({
    queryKey: ["analytics", "cost-rollups", queryParams],
    queryFn: () => api.ops.costRollups({ ...queryParams, group_by: "provider", limit: 20 }),
    refetchInterval: pageVisible ? 30000 : false,
  });
  const yieldFunnel = useQuery({
    queryKey: ["analytics", "yield-funnel", queryParams],
    queryFn: () => api.ops.yieldFunnel(queryParams),
    refetchInterval: pageVisible ? 30000 : false,
  });

  const usageData = usage.data ?? dashboard.data?.usage;
  const funnelData = yieldFunnel.data ?? dashboard.data?.yield_funnel;
  const rollups = costRollups.data?.items ?? dashboard.data?.cost_rollups ?? [];
  const stats = summarizeWorkflowStats(funnelData?.events ?? []);
  const isFetching = dashboard.isFetching || usage.isFetching || costRollups.isFetching || yieldFunnel.isFetching;
  const dataWaiting =
    !isFetching && stats.total === 0 && !usageHasData(usageData) && rollups.length === 0 && (funnelData?.events.length ?? 0) === 0;

  function refreshAll() {
    void queryClient.invalidateQueries({ queryKey: ["analytics"] });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="font-display text-3xl text-text-primary">数据统计</h1>
          <p className="mt-1 text-sm text-text-secondary">成本、用量、成品率与任务状态的运维视图</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <RangeSegmentedControl value={range} onChange={setRange} />
          <button className="btn-secondary text-sm" type="button" onClick={refreshAll}>
            <RefreshCw className={`h-4 w-4 ${isFetching ? "animate-spin" : ""}`} />
            刷新
          </button>
        </div>
      </div>

      {dashboard.error ? <ErrorState error={dashboard.error} /> : null}
      {usage.error ? <ErrorState error={usage.error} /> : null}
      {costRollups.error ? <ErrorState error={costRollups.error} /> : null}
      {yieldFunnel.error ? <ErrorState error={yieldFunnel.error} /> : null}

      <AnalyticsKpiCards stats={stats} usage={usageData} funnel={funnelData} />
      {dataWaiting ? (
        <section className="rounded-[24px] border border-dashed border-border bg-white/45 px-6 py-8">
          <h2 className="font-semibold text-text-primary">数据等待中</h2>
          <p className="mt-1 text-sm text-text-secondary">平台指标尚未回流，当前时间范围内没有可统计的成本、用量或成品率事件。</p>
        </section>
      ) : null}
      <AnalyticsTabs value={tab} onChange={setTab} />

      {tab === "cost" ? <CostUsageTab usage={usageData} rollups={rollups} days={window.days} /> : null}
      {tab === "yield" ? <YieldFunnelTab funnel={funnelData} /> : null}
      {tab === "tasks" ? <TaskStatsTab funnel={funnelData} /> : null}
    </div>
  );
}
