import type { RunCard, RunOverviewResponse } from "../../api/client";

export type OverviewStats = {
  total: number;
  processing: number;
  completed: number;
  failed: number;
};

type RunStatusBucket = "processing" | "completed" | "failed" | "other";

function bucketRunStatus(status: string): RunStatusBucket {
  if (status === "succeeded" || status === "completed") return "completed";
  if (status === "failed" || status === "cancelled") return "failed";
  if (status === "created" || status === "admitted" || status === "running" || status === "cancelling") {
    return "processing";
  }
  return "other";
}

function statsFromRunOverview(overview: RunOverviewResponse | undefined): OverviewStats | null {
  if (!overview) return null;
  const statusCounts = Object.entries(overview.statusCounts ?? {});
  if (overview.total_hint == null && statusCounts.length === 0) return null;

  const stats: OverviewStats = {
    total: overview.total_hint ?? statusCounts.reduce((sum, [, count]) => sum + count, 0),
    processing: 0,
    completed: 0,
    failed: 0,
  };
  statusCounts.forEach(([status, count]) => {
    const bucket = bucketRunStatus(status);
    if (bucket !== "other") stats[bucket] += count;
  });
  return stats;
}

function statsFromRunCards(runs: RunCard[]): OverviewStats {
  const stats: OverviewStats = { total: runs.length, processing: 0, completed: 0, failed: 0 };
  runs.forEach((run) => {
    const bucket = bucketRunStatus(run.status);
    if (bucket !== "other") stats[bucket] += 1;
  });
  return stats;
}

export function buildOverviewStats(overview: RunOverviewResponse | undefined): OverviewStats {
  return statsFromRunOverview(overview) ?? statsFromRunCards(overview?.items ?? []);
}

export function sortRecentRuns(runs: RunCard[]) {
  return [...runs].sort((left, right) => {
    const leftTime = Date.parse(left.updatedAt ?? left.startedAt ?? "");
    const rightTime = Date.parse(right.updatedAt ?? right.startedAt ?? "");
    return (Number.isNaN(rightTime) ? 0 : rightTime) - (Number.isNaN(leftTime) ? 0 : leftTime);
  });
}
