import { AlertCircle, BarChart3, CheckCircle2, Loader2 } from "lucide-react";
import type { YieldFunnelResponse } from "../../api/client";
import { summarizeWorkflowStats } from "./analyticsModel";

const COLORS = {
  processing: "#5e6d51",
  completed: "#4c8d62",
  failed: "#c56a5d",
};

const EVENT_LABELS: Record<string, string> = {
  submitted: "已提交",
  admitted: "已入队",
  started: "运行中",
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

function eventLabel(type: string) {
  return EVENT_LABELS[type] ?? (type.includes("publish") ? "发布事件" : type.includes("finished_video") ? "成片事件" : "其他事件");
}

function workflowEventCounts(funnel?: YieldFunnelResponse) {
  const counts = new Map<string, number>();
  (funnel?.events ?? []).forEach((event) => {
    counts.set(event.event_type, (counts.get(event.event_type) ?? 0) + 1);
  });
  return Array.from(counts.entries())
    .map(([type, count]) => ({ type, label: eventLabel(type), count }))
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label, "zh-Hans-CN"));
}

function TaskStatusChart({ stats }: { stats: ReturnType<typeof summarizeWorkflowStats> }) {
  const bars = [
    { key: "processing", label: "处理中", value: stats.processing, color: COLORS.processing },
    { key: "completed", label: "已完成", value: stats.completed, color: COLORS.completed },
    { key: "failed", label: "失败", value: stats.failed, color: COLORS.failed },
  ];
  const maxValue = Math.max(0, ...bars.map((item) => item.value));
  if (maxValue === 0) {
    return (
      <div className="flex min-h-[220px] items-center justify-center rounded-[22px] border border-dashed border-border bg-white/45 text-sm text-text-tertiary">
        暂无任务统计
      </div>
    );
  }

  const width = 520;
  const height = 240;
  const baseline = 190;
  const barWidth = 76;
  const gap = 78;
  const startX = 78;

  return (
    <svg className="h-[260px] w-full overflow-visible" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="任务状态统计图">
      <line x1="42" x2="478" y1={baseline} y2={baseline} stroke="#d9ddd2" strokeWidth="2" />
      {[0.25, 0.5, 0.75, 1].map((ratio) => {
        const y = baseline - ratio * 150;
        return <line key={ratio} x1="42" x2="478" y1={y} y2={y} stroke="#d9ddd2" strokeDasharray="4 7" />;
      })}
      {bars.map((bar, index) => {
        const x = startX + index * (barWidth + gap);
        const barHeight = Math.max(12, (bar.value / maxValue) * 150);
        const y = baseline - barHeight;
        return (
          <g key={bar.key}>
            <rect x={x} y={y} width={barWidth} height={barHeight} rx="16" fill={bar.color} opacity="0.9">
              <title>{`${bar.label}: ${bar.value}`}</title>
            </rect>
            <text x={x + barWidth / 2} y={y - 10} textAnchor="middle" className="fill-text-primary text-[14px] font-semibold">
              {bar.value.toLocaleString("zh-CN")}
            </text>
            <text x={x + barWidth / 2} y={baseline + 28} textAnchor="middle" className="fill-text-secondary text-[13px]">
              {bar.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export function TaskStatsTab({ funnel }: { funnel?: YieldFunnelResponse }) {
  const stats = summarizeWorkflowStats(funnel?.events ?? []);
  const eventCounts = workflowEventCounts(funnel);

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1.35fr)_390px]">
      <section className="card p-5 md:p-6">
        <div className="mb-5 flex items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-semibold text-text-primary">
              <BarChart3 className="h-5 w-5 text-accent" />
              任务状态统计
            </h2>
            <p className="mt-1 text-sm text-text-secondary">按每个工作流最新 yield 事件聚合</p>
          </div>
        </div>
        <TaskStatusChart stats={stats} />
      </section>

      <aside className="space-y-5">
        <section className="card p-5">
          <h2 className="text-xl font-semibold text-text-primary">状态摘要</h2>
          <div className="mt-4 space-y-3">
            <div className="flex items-center justify-between rounded-2xl border border-border/70 bg-white/55 px-4 py-3">
              <span className="inline-flex items-center gap-2 text-sm text-text-secondary">
                <Loader2 className="h-4 w-4 text-accent" />
                处理中
              </span>
              <span className="font-mono font-semibold text-text-primary">{stats.processing}</span>
            </div>
            <div className="flex items-center justify-between rounded-2xl border border-border/70 bg-white/55 px-4 py-3">
              <span className="inline-flex items-center gap-2 text-sm text-text-secondary">
                <CheckCircle2 className="h-4 w-4 text-status-success" />
                已完成
              </span>
              <span className="font-mono font-semibold text-text-primary">{stats.completed}</span>
            </div>
            <div className="flex items-center justify-between rounded-2xl border border-border/70 bg-white/55 px-4 py-3">
              <span className="inline-flex items-center gap-2 text-sm text-text-secondary">
                <AlertCircle className="h-4 w-4 text-status-error" />
                失败
              </span>
              <span className="font-mono font-semibold text-text-primary">{stats.failed}</span>
            </div>
          </div>
        </section>

        <section className="card p-5">
          <h2 className="text-xl font-semibold text-text-primary">事件类型</h2>
          {eventCounts.length === 0 ? (
            <p className="mt-4 rounded-[18px] border border-dashed border-border bg-white/45 px-4 py-6 text-center text-sm text-text-tertiary">
              暂无事件类型
            </p>
          ) : (
            <div className="mt-4 space-y-2.5">
              {eventCounts.map((item) => (
                <div className="flex items-center justify-between rounded-[18px] border border-border/70 bg-white/50 px-4 py-3" key={item.type}>
                  <span className="text-sm font-medium text-text-primary">{item.label}</span>
                  <span className="font-mono text-sm font-semibold text-text-primary">{item.count.toLocaleString("zh-CN")}</span>
                </div>
              ))}
            </div>
          )}
        </section>
      </aside>
    </div>
  );
}
