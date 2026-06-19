import { DollarSign, Gauge, Tag } from "lucide-react";
import type { CostRollup, ProviderUsageReport } from "../../api/client";
import { buildCostBars, formatMoney, usageHasData } from "./analyticsModel";

function formatCurrency(value: number, currency = "CNY") {
  return new Intl.NumberFormat("zh-CN", {
    style: "currency",
    currency,
    maximumFractionDigits: value >= 1 ? 2 : 6,
  }).format(value);
}

function EmptyPanel({ label }: { label: string }) {
  return (
    <div className="rounded-[22px] border border-dashed border-border bg-white/45 px-6 py-10 text-center text-sm text-text-tertiary">
      {label}
    </div>
  );
}

export function CostUsageTab({
  usage,
  rollups,
  days,
}: {
  usage?: ProviderUsageReport;
  rollups: CostRollup[];
  days: number;
}) {
  const bars = buildCostBars(rollups);
  const maxCost = Math.max(0, ...bars.map((item) => item.cost));
  const maxInvocations = Math.max(0, ...bars.map((item) => item.invocations));

  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1.45fr)_360px]">
      <section className="card p-5 md:p-6">
        <div className="mb-5 flex items-start justify-between gap-3">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-semibold text-text-primary">
              <DollarSign className="h-5 w-5 text-accent" />
              成本分布
            </h2>
            <p className="mt-1 text-sm text-text-secondary">最近 {days} 天，来自 /api/ops/cost-rollups</p>
          </div>
        </div>

        {bars.length === 0 ? (
          <EmptyPanel label="暂无成本或调用数据" />
        ) : (
          <div className="divide-y divide-border/60">
            {bars.map((item) => {
              const costWidth = maxCost > 0 ? (item.cost / maxCost) * 100 : 0;
              const invocationWidth = maxInvocations > 0 ? (item.invocations / maxInvocations) * 100 : 0;
              return (
                <div className="py-4 first:pt-0 last:pb-0" key={item.key}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate font-medium text-text-primary">{item.label}</p>
                      <p className="mt-1 text-xs text-text-tertiary">{item.invocations.toLocaleString("zh-CN")} 次调用</p>
                    </div>
                    <p className="font-mono text-sm font-semibold tabular-nums text-text-primary">
                      {formatCurrency(item.cost, item.currency)}
                    </p>
                  </div>
                  <div className="mt-3 grid gap-2">
                    <div className="h-2 overflow-hidden rounded-full bg-surface-hover">
                      <div className="h-full rounded-full bg-accent" style={{ width: `${costWidth}%` }} />
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-surface-hover">
                      <div className="h-full rounded-full bg-status-info/70" style={{ width: `${invocationWidth}%` }} />
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
            <Gauge className="h-5 w-5 text-accent" />
            <h2 className="text-xl font-semibold text-text-primary">供应商用量</h2>
          </div>
          {usageHasData(usage) ? (
            <div className="mt-4 divide-y divide-border/60 border-t border-border/60 text-sm">
              <div className="flex items-center justify-between gap-3 py-3">
                <span className="text-text-secondary">调用次数</span>
                <span className="font-mono text-text-primary">{usage!.invocations.toLocaleString("zh-CN")}</span>
              </div>
              <div className="flex items-center justify-between gap-3 py-3">
                <span className="text-text-secondary">估算成本</span>
                <span className="font-mono text-text-primary">{formatMoney(usage!.estimated_cost)}</span>
              </div>
              <div className="flex items-center justify-between gap-3 py-3">
                <span className="text-text-secondary">实际成本</span>
                <span className="font-mono text-text-primary">{usage?.actual_cost ? formatMoney(usage.actual_cost) : "暂无"}</span>
              </div>
            </div>
          ) : (
            <EmptyPanel label="暂无供应商用量" />
          )}
        </section>

        <section className="card p-5">
          <div className="flex items-center gap-2">
            <Tag className="h-5 w-5 text-status-warning" />
            <h2 className="text-xl font-semibold text-text-primary">价格覆盖</h2>
          </div>
          <p className="mt-3 text-sm leading-6 text-text-secondary">
            未定价调用单独统计，不按默认单价估算，避免显示假成本。
          </p>
          <p className="mt-4 font-mono text-3xl font-semibold tabular-nums text-text-primary">
            {(usage?.unpriced_invocation_count ?? 0).toLocaleString("zh-CN")}
          </p>
        </section>
      </aside>
    </div>
  );
}
