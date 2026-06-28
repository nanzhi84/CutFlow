export type RunStatusLike = string;

export type RunLike = {
  runId: string;
  title: string;
  status: RunStatusLike;
};

type TerminalKind = "succeeded" | "failed" | "cancelled";

type TerminalTransition = {
  runId: string;
  title: string;
  kind: TerminalKind;
};

type NotificationPayload = {
  title: string;
  body: string;
};

export type TerminalSummary = {
  transitions: TerminalTransition[];
  succeeded: number;
  failed: number;
  cancelled: number;
  /**
   * A single merged notification covering all terminal transitions in this
   * poll, or `null` when nothing newly reached a terminal state. Many runs
   * collapse into one notification so a batch never spams N system toasts.
   */
  notification: NotificationPayload | null;
};

function isTerminalStatus(status: RunStatusLike): status is TerminalKind {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

/**
 * Compare the current run statuses against the statuses seen in the previous
 * poll and collapse every fresh terminal transition into a single, merged
 * notification payload.
 *
 * A run counts as a transition only when it was previously tracked with a
 * different, non-terminal-to-terminal status change (i.e. we saw it before and
 * it just crossed into a terminal state). Runs that were already terminal in
 * the previous map, or were never seen before, do not fire.
 */
export function summarizeTerminalTransitions(
  runs: RunLike[],
  previous: Map<string, RunStatusLike>,
): TerminalSummary {
  const transitions: TerminalTransition[] = [];
  for (const run of runs) {
    const lastStatus = previous.get(run.runId);
    if (lastStatus && lastStatus !== run.status && isTerminalStatus(run.status)) {
      transitions.push({ runId: run.runId, title: run.title, kind: run.status });
    }
  }

  const succeeded = transitions.filter((item) => item.kind === "succeeded").length;
  const failed = transitions.filter((item) => item.kind === "failed").length;
  const cancelled = transitions.filter((item) => item.kind === "cancelled").length;

  return {
    transitions,
    succeeded,
    failed,
    cancelled,
    notification: buildMergedNotification(transitions, succeeded, failed, cancelled),
  };
}

function buildMergedNotification(
  transitions: TerminalTransition[],
  succeeded: number,
  failed: number,
  cancelled: number,
): NotificationPayload | null {
  if (transitions.length === 0) return null;

  if (transitions.length === 1) {
    const only = transitions[0];
    const label =
      only.kind === "succeeded" ? "任务已完成" : only.kind === "cancelled" ? "任务已取消" : "任务失败";
    return { title: label, body: only.title };
  }

  const parts: string[] = [];
  if (succeeded > 0) parts.push(`${succeeded} 个完成`);
  if (failed > 0) parts.push(`${failed} 个失败`);
  if (cancelled > 0) parts.push(`${cancelled} 个取消`);
  return { title: "批量任务更新", body: parts.join(" · ") };
}

/** Advance the tracking map in place so the next poll only fires on new changes. */
export function recordStatuses(runs: RunLike[], previous: Map<string, RunStatusLike>): void {
  for (const run of runs) {
    previous.set(run.runId, run.status);
  }
}
