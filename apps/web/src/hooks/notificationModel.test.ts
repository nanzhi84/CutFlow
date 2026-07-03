import { describe, expect, it } from "vitest";
import { recordStatuses, summarizeTerminalTransitions, type RunLike } from "./notificationModel";

describe("notificationModel", () => {
  it("does not notify for first-seen terminal runs", () => {
    const previous = new Map<string, string>();
    const runs: RunLike[] = [{ runId: "run_1", title: "首轮已完成", status: "succeeded" }];

    expect(summarizeTerminalTransitions(runs, previous).notification).toBeNull();
  });

  it("builds a single notification for a fresh terminal transition", () => {
    const previous = new Map<string, string>([["run_1", "running"]]);
    const summary = summarizeTerminalTransitions(
      [{ runId: "run_1", title: "案例视频", status: "failed" }],
      previous,
    );

    expect(summary.failed).toBe(1);
    expect(summary.notification).toEqual({ title: "任务失败", body: "案例视频" });
  });

  it("collapses batch terminal transitions into one payload and advances statuses", () => {
    const previous = new Map<string, string>([
      ["run_1", "running"],
      ["run_2", "running"],
      ["run_3", "running"],
    ]);
    const runs: RunLike[] = [
      { runId: "run_1", title: "A", status: "succeeded" },
      { runId: "run_2", title: "B", status: "failed" },
      { runId: "run_3", title: "C", status: "cancelled" },
    ];

    const summary = summarizeTerminalTransitions(runs, previous);
    recordStatuses(runs, previous);

    expect(summary.notification).toEqual({
      title: "批量任务更新",
      body: "1 个完成 · 1 个失败 · 1 个取消",
    });
    expect(previous.get("run_1")).toBe("succeeded");
    expect(previous.get("run_2")).toBe("failed");
    expect(previous.get("run_3")).toBe("cancelled");
  });
});
