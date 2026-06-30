import type { CostRollup, ProviderUsageReport, YieldFunnelEvent, YieldFunnelResponse } from "../../api/client";
import type { OverviewStats } from "../overview/overviewModel";

export type TimeRange = "24h" | "7d" | "30d";
export type AnalyticsTab = "cost" | "yield" | "tasks" | "balances" | "apiUsage" | "diagnostics";

export const rangeOptions: Array<{ key: TimeRange; label: string; days: number; hours: number }> = [
  { key: "24h", label: "24 小时", days: 1, hours: 24 },
  { key: "7d", label: "7 天", days: 7, hours: 7 * 24 },
  { key: "30d", label: "30 天", days: 30, hours: 30 * 24 },
];

export const analyticsTabs: Array<{ key: AnalyticsTab; label: string }> = [
  { key: "cost", label: "成本 & 用量" },
  { key: "yield", label: "成品率漏斗" },
  { key: "tasks", label: "任务统计" },
  { key: "balances", label: "余额&配额" },
  { key: "apiUsage", label: "API 用量监控" },
  { key: "diagnostics", label: "网络诊断" },
];

export function rangeWindow(range: TimeRange) {
  const days = rangeOptions.find((item) => item.key === range)?.days ?? 7;
  const end = new Date();
  const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000);
  const option = rangeOptions.find((item) => item.key === range);
  return { window_start: start.toISOString(), window_end: end.toISOString(), days, hours: option?.hours ?? days * 24 };
}

function moneyAmount(value?: { amount: string; currency: string } | null) {
  const amount = Number(value?.amount ?? 0);
  return Number.isFinite(amount) ? amount : 0;
}

export function formatMoney(value?: { amount: string; currency: string } | null) {
  const amount = moneyAmount(value);
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency: value?.currency ?? "CNY",
    maximumFractionDigits: amount >= 1 ? 2 : 6,
  }).format(amount);
}

function eventIdentity(event: YieldFunnelEvent) {
  return event.run_id || event.job_id || event.dedupe_key || event.id;
}

const RUN_PROGRESS_EVENTS = new Set([
  "submitted",
  "admitted",
  "started",
  "node_started",
  "node_succeeded",
  "node_failed",
  "finished_video_created",
  "qc_failed",
  "manual_rejected",
  "publish_failed",
  "published",
]);

function bucketRunEvent(eventType: string): keyof OverviewStats | "other" {
  if (eventType === "published" || eventType === "finished_video_created") return "completed";
  if (eventType === "node_failed" || eventType === "qc_failed" || eventType === "manual_rejected" || eventType === "publish_failed") {
    return "failed";
  }
  if (eventType === "submitted" || eventType === "admitted" || eventType === "started" || eventType === "node_started" || eventType === "node_succeeded") {
    return "processing";
  }
  return "other";
}

function latestRunEvents(events: YieldFunnelEvent[]) {
  const latestByRun = new Map<string, YieldFunnelEvent>();
  events
    .filter((event) => RUN_PROGRESS_EVENTS.has(event.event_type))
    .forEach((event) => {
      const key = eventIdentity(event);
      const current = latestByRun.get(key);
      if (!current || Date.parse(event.event_time) >= Date.parse(current.event_time)) {
        latestByRun.set(key, event);
      }
    });
  return Array.from(latestByRun.values());
}

export function summarizeWorkflowStats(events: YieldFunnelEvent[]): OverviewStats {
  const stats: OverviewStats = { total: 0, processing: 0, completed: 0, failed: 0 };
  latestRunEvents(events).forEach((event) => {
    const bucket = bucketRunEvent(event.event_type);
    if (bucket === "other") return;
    stats.total += 1;
    stats[bucket] += 1;
  });
  return stats;
}

export function successRate(funnel?: YieldFunnelResponse, stats?: OverviewStats) {
  if (typeof funnel?.true_yield_rate === "number") return funnel.true_yield_rate;
  if (!stats || stats.total === 0) return null;
  return stats.completed / stats.total;
}

export function buildFunnelSteps(funnel?: YieldFunnelResponse) {
  const events = funnel?.events ?? [];
  const workflowTotal = latestRunEvents(events).length;
  const finishedVideos = events.filter((event) => event.finished_video_id || event.event_type.includes("finished_video")).length;
  const publishAttempts = events.filter((event) => event.publish_attempt_id || event.event_type.includes("publish")).length;
  return [
    { key: "workflow", label: "工作流运行", value: workflowTotal },
    { key: "finished", label: "成片产出", value: finishedVideos },
    { key: "published", label: "发布触达", value: publishAttempts },
  ];
}

export function buildCostBars(rollups: CostRollup[]) {
  return rollups
    .map((item) => ({
      key: item.id,
      label: item.group_key || item.group_by || "全部",
      cost: moneyAmount(item.actual_cost ?? item.estimated_cost),
      invocations: item.invocations,
      currency: item.actual_cost?.currency ?? item.estimated_cost.currency,
    }))
    .filter((item) => item.cost > 0 || item.invocations > 0)
    .sort((left, right) => right.cost - left.cost || right.invocations - left.invocations);
}

export function usageHasData(usage?: ProviderUsageReport) {
  return Boolean(usage && (usage.invocations > 0 || moneyAmount(usage.estimated_cost) > 0 || usage.unpriced_invocation_count > 0));
}

export const providerBalanceStatusLabels = {
  ok: "正常",
  unconfigured: "未配置",
  unsupported: "不支持",
  unauthorized: "未授权",
  error: "错误",
  pending: "等待快照",
} as const;

export function formatRate(value: number | null | undefined) {
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "暂无";
}
