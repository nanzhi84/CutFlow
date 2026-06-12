import { BarChart3, Library, Settings, Sparkles, UserCircle2 } from "lucide-react";
import { Link } from "react-router-dom";
import type { OpsDashboardVm } from "../../api/client";
import { routes } from "../../routes";
import type { OverviewStats } from "./overviewModel";

const quickLinks = [
  { to: routes.studio(), label: "案例中心", icon: Sparkles },
  { to: routes.library(), label: "素材库", icon: Library },
  { to: routes.analytics(), label: "数据统计", icon: BarChart3 },
  { to: routes.account(), label: "账户中心", icon: UserCircle2 },
  { to: routes.settings(), label: "系统设置", icon: Settings },
];

function formatMoney(amount?: string | number | null, currency = "CNY") {
  const numeric = typeof amount === "number" ? amount : Number(amount ?? 0);
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency,
    maximumFractionDigits: numeric >= 1 ? 2 : 6,
  }).format(Number.isFinite(numeric) ? numeric : 0);
}

export function OverviewSidePanel({
  stats,
  dashboard,
}: {
  stats: OverviewStats;
  dashboard?: OpsDashboardVm;
}) {
  const active = stats.processing > 0;
  const usage = dashboard?.usage;
  const alertCount = dashboard?.alerts.filter((item) => item.status === "open").length ?? 0;

  return (
    <div className="space-y-5">
      <section className="card p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-xl font-semibold text-text-primary">运行态势</h2>
            <p className="mt-1 text-sm text-text-secondary">任务和成本聚合来自后端 Ops API</p>
          </div>
          <div
            className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs ${
              active
                ? "border-status-warning/20 bg-status-warning/10 text-status-warning"
                : "border-status-success/15 bg-status-success/10 text-status-success"
            }`}
          >
            <div className={`h-2 w-2 rounded-full ${active ? "bg-status-warning" : "bg-status-success"}`} />
            {active ? "有任务运行" : "当前空闲"}
          </div>
        </div>

        <div className="mt-4 grid grid-cols-2 divide-x divide-border/60 border-y border-border/60">
          <div className="py-3 pr-3">
            <p className="text-sm text-text-tertiary">供应商调用</p>
            <p className="mt-2 font-mono text-2xl font-semibold tabular-nums text-text-primary">
              {(usage?.invocations ?? 0).toLocaleString("zh-CN")}
            </p>
          </div>
          <div className="py-3 pl-3">
            <p className="text-sm text-text-tertiary">未定价调用</p>
            <p className="mt-2 font-mono text-2xl font-semibold tabular-nums text-text-primary">
              {(usage?.unpriced_invocation_count ?? 0).toLocaleString("zh-CN")}
            </p>
          </div>
        </div>

        <div className="mt-4 divide-y divide-border/60 border-t border-border/60 text-sm">
          <div className="flex items-center justify-between gap-3 py-3">
            <span className="text-text-secondary">估算成本</span>
            <span className="font-mono text-text-primary">
              {formatMoney(usage?.estimated_cost.amount, usage?.estimated_cost.currency)}
            </span>
          </div>
          <div className="flex items-center justify-between gap-3 py-3">
            <span className="text-text-secondary">活跃告警</span>
            <span className={alertCount > 0 ? "badge-warning" : "badge-success"}>
              {alertCount > 0 ? `${alertCount} 条待处理` : "无告警"}
            </span>
          </div>
        </div>
      </section>

      <section className="card p-5">
        <h2 className="text-sm font-semibold text-text-primary">快捷入口</h2>
        <div className="mt-3 grid grid-cols-2 gap-2">
          {quickLinks.map((item) => {
            const Icon = item.icon;
            return (
              <Link
                className="inline-flex min-h-11 items-center gap-2 border-l-2 border-border/60 px-3 py-2 text-sm font-medium text-text-secondary transition-colors hover:border-accent/60 hover:bg-hover hover:text-text-primary"
                key={item.to}
                to={item.to}
              >
                <Icon className="h-4 w-4 text-accent" />
                <span className="truncate">{item.label}</span>
              </Link>
            );
          })}
        </div>
      </section>
    </div>
  );
}
