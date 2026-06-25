import { CheckCircle2, Filter, TimerReset } from "lucide-react";
import type { YieldFunnelResponse } from "../../api/client";
import { TimeText } from "../TimeText";
import { buildFunnelSteps, successRate, summarizeWorkflowStats } from "./analyticsModel";

function eventLabel(type: string) {
  const labels: Record<string, string> = {
    submitted: "任务已提交",
    admitted: "任务已入队",
    started: "任务运行中",
    node_started: "节点开始",
    node_succeeded: "节点完成",
    node_failed: "节点失败",
    finished_video_created: "成片产出",
    qc_started: "质检开始",
    qc_passed: "质检通过",
    qc_failed: "质检失败",
    manual_approved: "人工通过",
    manual_rejected: "人工拒绝",
    publish_started: "发布开始",
    published: "发布完成",
    publish_failed: "发布失败",
  };
  if (labels[type]) return labels[type];
  if (type.includes("finished_video")) return "成片产出";
  if (type.includes("publish")) return "发布事件";
  return "其他事件";
}

function formatRate(value: number | null) {
  return value === null ? "暂无" : `${(value * 100).toFixed(1)}%`;
}

export function YieldFunnelTab({ funnel }: { funnel?: YieldFunnelResponse }) {
  const steps = buildFunnelSteps(funnel);
  const stats = summarizeWorkflowStats(funnel?.events ?? []);
  const rate = successRate(funnel, stats);
  const maxValue = Math.max(0, ...steps.map((item) => item.value));
  const recentEvents = [...(funnel?.events ?? [])]
    .sort((left, right) => Date.parse(right.event_time) - Date.parse(left.event_time))
    .slice(0, 8);

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1.35fr)_390px]">
      <section className="card p-5 md:p-6">
        <div className="mb-5 flex items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-semibold text-text-primary">
              <Filter className="h-5 w-5 text-accent" />
              成品率漏斗
            </h2>
            <p className="mt-1 text-sm text-text-secondary">使用 /api/ops/yield-funnel 的真实事件流</p>
          </div>
          <div className="rounded-2xl border border-status-success/20 bg-status-success/10 px-4 py-2 text-right">
            <p className="text-xs text-status-success">true yield</p>
            <p className="font-mono text-xl font-semibold tabular-nums text-status-success">{formatRate(rate)}</p>
          </div>
        </div>

        {maxValue === 0 ? (
          <div className="rounded-[22px] border border-dashed border-border bg-white/45 px-6 py-10 text-center text-sm text-text-tertiary">
            暂无漏斗事件
          </div>
        ) : (
          <div className="space-y-4">
            {steps.map((step, index) => {
              const width = maxValue > 0 ? Math.max(6, (step.value / maxValue) * 100) : 0;
              return (
                <div className="grid gap-2" key={step.key}>
                  <div className="flex items-center justify-between gap-3 text-sm">
                    <span className="font-medium text-text-primary">{index + 1}. {step.label}</span>
                    <span className="font-mono font-semibold tabular-nums text-text-primary">
                      {step.value.toLocaleString("zh-CN")}
                    </span>
                  </div>
                  <div className="h-7 overflow-hidden rounded-full bg-surface-hover">
                    <div
                      className="flex h-full items-center justify-end rounded-full bg-gradient-brand pr-3 text-xs font-semibold text-[#1b1d1a]"
                      style={{ width: `${width}%` }}
                    >
                      {step.value > 0 ? `${Math.round((step.value / maxValue) * 100)}%` : ""}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <aside className="space-y-5">
        <section className="card p-5">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="h-5 w-5 text-status-success" />
            <h2 className="text-xl font-semibold text-text-primary">工作流结果</h2>
          </div>
          <div className="mt-4 grid grid-cols-3 gap-2 text-center">
            <div className="rounded-2xl border border-border/70 bg-white/55 p-3">
              <p className="text-xs text-text-tertiary">处理中</p>
              <p className="mt-1 font-mono text-xl font-semibold text-text-primary">{stats.processing}</p>
            </div>
            <div className="rounded-2xl border border-border/70 bg-white/55 p-3">
              <p className="text-xs text-text-tertiary">成功</p>
              <p className="mt-1 font-mono text-xl font-semibold text-text-primary">{stats.completed}</p>
            </div>
            <div className="rounded-2xl border border-border/70 bg-white/55 p-3">
              <p className="text-xs text-text-tertiary">失败</p>
              <p className="mt-1 font-mono text-xl font-semibold text-text-primary">{stats.failed}</p>
            </div>
          </div>
        </section>

        <section className="card p-5">
          <div className="flex items-center gap-2">
            <TimerReset className="h-5 w-5 text-accent" />
            <h2 className="text-xl font-semibold text-text-primary">最近事件</h2>
          </div>
          {recentEvents.length === 0 ? (
            <p className="mt-4 rounded-[18px] border border-dashed border-border bg-white/45 px-4 py-6 text-center text-sm text-text-tertiary">
              暂无事件
            </p>
          ) : (
            <div className="mt-4 space-y-2.5">
              {recentEvents.map((event) => (
                <div className="rounded-[18px] border border-border/70 bg-white/50 px-4 py-3" key={event.id}>
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-medium text-text-primary">{eventLabel(event.event_type)}</span>
                    <span className="text-xs text-text-tertiary">
                      <TimeText value={event.event_time} />
                    </span>
                  </div>
                  <p className="mt-1 truncate font-mono text-xs text-text-tertiary">{event.run_id || event.job_id || event.dedupe_key}</p>
                </div>
              ))}
            </div>
          )}
        </section>
      </aside>
    </div>
  );
}
