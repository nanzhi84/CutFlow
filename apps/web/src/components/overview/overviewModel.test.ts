import { describe, expect, it } from "vitest";
import type { RunCard, RunOverviewResponse } from "../../api/client";
import { buildOverviewStats } from "./overviewModel";

function runCard(status: RunCard["status"], index: number): RunCard {
  return {
    runId: `run_${index}`,
    jobId: `job_${index}`,
    caseId: "case_1",
    status,
    progress: 1,
    currentNodeLabel: null,
    title: `任务 ${index}`,
    warnings: [],
    canResume: false,
    canRetry: false,
    canPublish: false,
    startedAt: null,
    updatedAt: null,
  };
}

function overview(overrides: Partial<RunOverviewResponse>): RunOverviewResponse {
  return {
    items: [],
    request_id: "req_test",
    ...overrides,
  };
}

describe("buildOverviewStats", () => {
  it("uses the full run summary instead of the eight displayed rows", () => {
    const items = Array.from({ length: 8 }, (_, index) =>
      runCard(index < 6 ? "succeeded" : "failed", index),
    );

    expect(
      buildOverviewStats(
        overview({
          items,
          total_hint: 78,
          statusCounts: { succeeded: 50, failed: 24, cancelled: 3, running: 1 },
        }),
      ),
    ).toEqual({ total: 78, processing: 1, completed: 50, failed: 27 });
  });

  it("sums status counts when total_hint is unavailable", () => {
    expect(
      buildOverviewStats(
        overview({ statusCounts: { created: 2, admitted: 3, running: 1, succeeded: 4 } }),
      ),
    ).toEqual({ total: 10, processing: 6, completed: 4, failed: 0 });
  });

  it("falls back to returned rows for a legacy response without summary fields", () => {
    expect(
      buildOverviewStats(
        overview({ items: [runCard("succeeded", 1), runCard("cancelled", 2)] }),
      ),
    ).toEqual({ total: 2, processing: 0, completed: 1, failed: 1 });
  });
});
