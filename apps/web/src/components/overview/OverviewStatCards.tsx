import { Activity, AlertCircle, CheckCircle2, Video } from "lucide-react";
import { Skeleton } from "../ui/Skeleton";
import type { OverviewStats } from "./overviewModel";

type StatItem = {
  key: keyof OverviewStats;
  label: string;
  helper: string;
  className: string;
  icon: typeof Video;
};

const STAT_ITEMS: StatItem[] = [
  { key: "total", label: "总任务", helper: "真实运行记录", className: "text-accent bg-accent/10", icon: Video },
  { key: "processing", label: "处理中", helper: "提交/入队/运行/节点中", className: "text-status-warning bg-status-warning/10", icon: Activity },
  { key: "completed", label: "已完成", helper: "成片或发布完成", className: "text-status-success bg-status-success/10", icon: CheckCircle2 },
  { key: "failed", label: "失败", helper: "节点/QC/发布失败", className: "text-status-error bg-status-error/10", icon: AlertCircle },
];

export function OverviewStatCards({ stats, isLoading }: { stats: OverviewStats; isLoading: boolean }) {
  if (isLoading) {
    return (
      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {STAT_ITEMS.map((item) => (
          <div className="card p-5" key={item.key}>
            <Skeleton className="h-20 w-full rounded-2xl" />
          </div>
        ))}
      </section>
    );
  }

  return (
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {STAT_ITEMS.map((item) => {
        const Icon = item.icon;
        return (
          <div className="card p-5" key={item.key}>
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm text-text-tertiary">{item.label}</p>
                <p className="mt-2 font-mono text-[2rem] font-semibold leading-none text-text-primary tabular-nums">
                  {stats[item.key].toLocaleString("zh-CN")}
                </p>
                <p className="mt-2 truncate text-xs text-text-tertiary">{item.helper}</p>
              </div>
              <div className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl ${item.className}`}>
                <Icon className="h-5 w-5" />
              </div>
            </div>
          </div>
        );
      })}
    </section>
  );
}
