import { Activity, DollarSign, Percent, Tag } from "lucide-react";
import type { ProviderUsageReport } from "../../api/client";
import type { OverviewStats } from "../overview/overviewModel";
import { formatMoney, successRate } from "./analyticsModel";
import type { YieldFunnelResponse } from "../../api/client";

function formatPercent(value: number | null) {
  return value === null ? "暂无" : `${(value * 100).toFixed(1)}%`;
}

export function AnalyticsKpiCards({
  stats,
  usage,
  funnel,
}: {
  stats: OverviewStats;
  usage?: ProviderUsageReport;
  funnel?: YieldFunnelResponse;
}) {
  const rate = successRate(funnel, stats);
  const kpis = [
    {
      label: "任务数",
      value: stats.total.toLocaleString("zh-CN"),
      helper: "当前时间范围内工作流",
      icon: Activity,
      className: "bg-accent/10 text-accent",
    },
    {
      label: "成功率",
      value: formatPercent(rate),
      helper: rate === null ? "暂无成功率样本" : "真实完成率",
      icon: Percent,
      className: "bg-status-success/10 text-status-success",
    },
    {
      label: "估算成本",
      value: formatMoney(usage?.estimated_cost),
      helper: `${(usage?.invocations ?? 0).toLocaleString("zh-CN")} 次供应商调用`,
      icon: DollarSign,
      className: "bg-status-info/10 text-status-info",
    },
    {
      label: "未定价调用",
      value: (usage?.unpriced_invocation_count ?? 0).toLocaleString("zh-CN"),
      helper: "需补价格目录或复核账单",
      icon: Tag,
      className: "bg-status-warning/10 text-status-warning",
    },
  ];

  return (
    <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {kpis.map((kpi) => {
        const Icon = kpi.icon;
        return (
          <div className="card p-5" key={kpi.label}>
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm text-text-tertiary">{kpi.label}</p>
                <p className="mt-2 truncate font-mono text-[1.85rem] font-semibold leading-none tabular-nums text-text-primary">
                  {kpi.value}
                </p>
                <p className="mt-2 truncate text-xs text-text-tertiary">{kpi.helper}</p>
              </div>
              <div className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl ${kpi.className}`}>
                <Icon className="h-5 w-5" />
              </div>
            </div>
          </div>
        );
      })}
    </section>
  );
}
