import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import { providerObservabilityApi } from "../api/r6";
import { ErrorState } from "../components/ui/State";
import { AnalyticsTabs, RangeSegmentedControl } from "../components/analytics/AnalyticsControls";
import { AnalyticsKpiCards } from "../components/analytics/AnalyticsKpiCards";
import { BalanceQuotaTab } from "../components/analytics/BalanceQuotaTab";
import { CostUsageTab } from "../components/analytics/CostUsageTab";
import { NetworkDiagnosticsPanel } from "../components/analytics/NetworkDiagnosticsPanel";
import { ProviderUsageMetricsTab } from "../components/analytics/ProviderUsageMetricsTab";
import { TaskStatsTab } from "../components/analytics/TaskStatsTab";
import { YieldFunnelTab } from "../components/analytics/YieldFunnelTab";
import { rangeWindow, summarizeWorkflowStats, usageHasData, type AnalyticsTab, type TimeRange } from "../components/analytics/analyticsModel";
import { usePageVisible } from "../hooks/usePageVisible";

export default function AnalyticsPage() {
  const [range, setRange] = useState<TimeRange>("24h");
  const [tab, setTab] = useState<AnalyticsTab>("cost");
  const queryClient = useQueryClient();
  const pageVisible = usePageVisible();
  const timeWindow = useMemo(() => rangeWindow(range), [range]);
  const queryParams = { window_start: timeWindow.window_start, window_end: timeWindow.window_end };

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
  const balances = useQuery({
    queryKey: ["analytics", "provider-balances"],
    queryFn: () => providerObservabilityApi.providers.balances(),
    refetchInterval: pageVisible ? 60000 : false,
  });
  const providerUsageMetrics = useQuery({
    queryKey: ["analytics", "provider-usage-metrics", timeWindow.hours],
    queryFn: () => providerObservabilityApi.ops.providerUsageMetrics({ window_hours: timeWindow.hours }),
    refetchInterval: pageVisible ? 30000 : false,
  });
  const refreshBalances = useMutation({
    mutationFn: () => providerObservabilityApi.providers.refreshBalances(),
    onSuccess: (report) => {
      queryClient.setQueryData(["analytics", "provider-balances"], report);
    },
  });

  const usageData = usage.data ?? dashboard.data?.usage;
  const funnelData = yieldFunnel.data ?? dashboard.data?.yield_funnel;
  const rollups = costRollups.data?.items ?? dashboard.data?.cost_rollups ?? [];
  const stats = summarizeWorkflowStats(funnelData?.events ?? []);
  const isFetching =
    dashboard.isFetching ||
    usage.isFetching ||
    costRollups.isFetching ||
    yieldFunnel.isFetching ||
    balances.isFetching ||
    providerUsageMetrics.isFetching ||
    refreshBalances.isPending;
  const dataWaiting =
    !isFetching &&
    stats.total === 0 &&
    !usageHasData(usageData) &&
    rollups.length === 0 &&
    (funnelData?.events.length ?? 0) === 0 &&
    (balances.data?.items.length ?? 0) === 0 &&
    (providerUsageMetrics.data?.items.length ?? 0) === 0;

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
      {balances.error ? <ErrorState error={balances.error} /> : null}
      {providerUsageMetrics.error ? <ErrorState error={providerUsageMetrics.error} /> : null}
      {refreshBalances.error ? <ErrorState error={refreshBalances.error} /> : null}

      <AnalyticsKpiCards stats={stats} usage={usageData} funnel={funnelData} />
      {dataWaiting ? (
        <section className="rounded-[24px] border border-dashed border-border bg-white/45 px-6 py-8">
          <h2 className="font-semibold text-text-primary">数据等待中</h2>
          <p className="mt-1 text-sm text-text-secondary">平台指标尚未回流，当前时间范围内没有可统计的成本、用量或成品率事件。</p>
        </section>
      ) : null}
      <AnalyticsTabs value={tab} onChange={setTab} />

      {tab === "cost" ? <CostUsageTab usage={usageData} rollups={rollups} days={timeWindow.days} /> : null}
      {tab === "yield" ? <YieldFunnelTab funnel={funnelData} /> : null}
      {tab === "tasks" ? <TaskStatsTab funnel={funnelData} /> : null}
      {tab === "balances" ? (
        <BalanceQuotaTab
          report={balances.data}
          isRefreshing={refreshBalances.isPending || balances.isFetching}
          onRefresh={() => refreshBalances.mutate()}
        />
      ) : null}
      {tab === "apiUsage" ? <ProviderUsageMetricsTab report={providerUsageMetrics.data} windowHours={timeWindow.hours} /> : null}
      {tab === "diagnostics" ? <NetworkDiagnosticsPanel /> : null}
    </div>
  );
}
