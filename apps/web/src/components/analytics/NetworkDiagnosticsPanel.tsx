import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { api, type NetworkHop } from "../../api/client";
import { usePageVisible } from "../../hooks/usePageVisible";
import { ErrorState, LoadingState } from "../ui/State";

const HOP_LABELS: Record<string, string> = {
  postgres: "Postgres 数据库",
  redis: "Redis 协调",
  oss: "对象存储 (OSS)",
  temporal: "Temporal 工作流",
};

function hopTone(status: string): string {
  if (status === "ok") return "border-emerald-200 bg-emerald-50 text-emerald-700";
  if (status === "failed") return "border-red-200 bg-red-50 text-red-700";
  // skipped / not_configured: neutral, not an alarm.
  return "border-border bg-white/60 text-text-secondary";
}

function hopStatusLabel(status: string): string {
  if (status === "ok") return "正常";
  if (status === "failed") return "失败";
  if (status === "skipped") return "未启用";
  if (status === "not_configured") return "未配置";
  return status;
}

function HopCard({ name, hop }: { name: string; hop: NetworkHop }) {
  return (
    <div className="rounded-2xl border border-border bg-white/65 p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-text-primary">{HOP_LABELS[name] ?? name}</span>
        <span className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${hopTone(hop.status)}`}>
          {hopStatusLabel(hop.status)}
        </span>
      </div>
      <dl className="mt-2 space-y-1 text-xs text-text-secondary">
        {typeof hop.latency_ms === "number" ? (
          <div className="flex justify-between gap-2">
            <dt>延迟</dt>
            <dd className="font-mono text-text-primary">{hop.latency_ms} ms</dd>
          </div>
        ) : null}
        {hop.backend ? (
          <div className="flex justify-between gap-2">
            <dt>后端</dt>
            <dd>{hop.backend}</dd>
          </div>
        ) : null}
        {hop.runtime ? (
          <div className="flex justify-between gap-2">
            <dt>运行时</dt>
            <dd>{hop.runtime}</dd>
          </div>
        ) : null}
        {hop.address ? (
          <div className="flex justify-between gap-2">
            <dt>地址</dt>
            <dd className="truncate font-mono">{hop.address}</dd>
          </div>
        ) : null}
        {hop.error ? (
          <div className="mt-1 rounded-lg bg-red-50 px-2 py-1 text-red-700">{hop.error}</div>
        ) : null}
      </dl>
    </div>
  );
}

export function NetworkDiagnosticsPanel() {
  const pageVisible = usePageVisible();
  const diagnostics = useQuery({
    queryKey: ["health", "network"],
    queryFn: () => api.health.network(),
    refetchInterval: pageVisible ? 10000 : false,
  });

  if (diagnostics.isLoading) return <LoadingState label="探测网络链路" block />;
  if (diagnostics.error || !diagnostics.data) return <ErrorState error={diagnostics.error} />;

  const data = diagnostics.data;
  const hops = Object.entries(data.hops);
  const overallOk = data.status === "ok";

  return (
    <section className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="font-semibold text-text-primary">网络链路诊断</h2>
          <p className="mt-1 text-sm text-text-secondary">
            Web → VPS → Mac → 对象存储各段的实时探测，每 10 秒刷新（探测受超时保护）。
          </p>
        </div>
        <span
          className={`inline-flex items-center gap-1.5 self-start rounded-full border px-3 py-1 text-sm font-medium ${
            overallOk
              ? "border-emerald-200 bg-emerald-50 text-emerald-700"
              : "border-red-200 bg-red-50 text-red-700"
          }`}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${diagnostics.isFetching ? "animate-spin" : ""}`} />
          {overallOk ? "整体正常" : "链路降级"}
        </span>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        {hops.map(([name, hop]) => (
          <HopCard key={name} name={name} hop={hop} />
        ))}
      </div>
    </section>
  );
}
