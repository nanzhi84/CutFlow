import { AlertTriangle, ArrowRight, Ban, CheckCircle2, CircleDashed, Loader2, XCircle } from "lucide-react";
import { useState, type ReactNode } from "react";
import type { NodeRun } from "../../api/client";
import { TimeText } from "../TimeText";
import { StatusPill } from "../ui/StatusPill";
import { buildNodeTimeline, nodeLabel, severityLabel, warningLabel, type NodeTimelineItem } from "./runModel";

export type NodePipelineBadge = {
  nodeId: string;
  label: string;
  caption?: string;
  detail?: string;
  tone: "success" | "warning" | "info";
  count?: number;
};

function statusIcon(status: string) {
  if (status === "succeeded") return <CheckCircle2 className="h-3.5 w-3.5 text-status-success" />;
  if (status === "degraded") return <AlertTriangle className="h-3.5 w-3.5 text-status-warning" />;
  if (status === "running") return <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" />;
  if (status === "failed") return <XCircle className="h-3.5 w-3.5 text-status-error" />;
  if (status === "cancelled" || status === "skipped") return <Ban className="h-3.5 w-3.5 text-text-tertiary" />;
  return <CircleDashed className="h-3.5 w-3.5 text-text-tertiary" />;
}

function chipClass(status: string, selected: boolean) {
  const base = "grid h-10 w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 rounded-xl border px-2.5 text-left transition-colors";
  const ring = selected ? " ring-2 ring-accent/40" : "";
  if (status === "succeeded") return `${base} border-status-success/25 bg-status-success/10 hover:bg-status-success/15${ring}`;
  if (status === "degraded") return `${base} border-status-warning/30 bg-status-warning/10 hover:bg-status-warning/15${ring}`;
  if (status === "running") return `${base} border-accent/40 bg-accent/10 hover:bg-accent/15${ring}`;
  if (status === "failed") return `${base} border-status-error/30 bg-status-error/10 hover:bg-status-error/15${ring}`;
  if (status === "cancelled" || status === "skipped") return `${base} border-border/60 bg-white/40 opacity-70 hover:bg-white/60${ring}`;
  return `${base} border-dashed border-border/70 bg-white/40 text-text-tertiary hover:bg-white/60${ring}`;
}

function durationText(node?: NodeRun): string | null {
  if (!node?.started_at || !node.finished_at) return null;
  const ms = new Date(node.finished_at).getTime() - new Date(node.started_at).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  return `${Math.floor(seconds / 60)}m${Math.round(seconds % 60)}s`;
}

function issueCount(node?: NodeRun, badges: NodePipelineBadge[] = []): number {
  const nodeIssues = node ? (node.warnings ?? []).length + (node.degradations ?? []).length + (node.error ? 1 : 0) : 0;
  const providerIssues = badges
    .filter((badge) => badge.tone === "warning")
    .reduce((total, badge) => total + (badge.count && badge.count > 0 ? badge.count : 1), 0);
  return nodeIssues + providerIssues;
}

function providerBadgeClass(tone: NodePipelineBadge["tone"]) {
  if (tone === "warning") return "border-status-warning/25 bg-status-warning/15 text-status-warning";
  if (tone === "info") return "border-accent/20 bg-accent/10 text-accent";
  return "border-status-success/20 bg-status-success/15 text-status-success";
}

function visualStatus(status: string, issues: number): string {
  if (status === "succeeded" && issues > 0) return "degraded";
  return status;
}

/** 节点流水线：按模板顺序用固定网格展示每个节点（保留英文节点名），点击查看警告/兜底/错误详情。 */
export function NodePipeline({
  templateId,
  nodes,
  runStatus,
  badges = [],
}: {
  templateId?: string | null;
  nodes: NodeRun[];
  runStatus?: string;
  badges?: NodePipelineBadge[];
}) {
  const items = buildNodeTimeline(templateId, nodes, runStatus);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = items.find((item) => item.nodeId === selectedId) ?? null;
  const badgesByNode = new Map<string, NodePipelineBadge[]>();
  for (const badge of badges) {
    badgesByNode.set(badge.nodeId, [...(badgesByNode.get(badge.nodeId) ?? []), badge]);
  }

  if (items.length === 0) {
    return <p className="rounded-2xl border border-dashed border-border bg-white/55 p-4 text-sm text-text-secondary">暂无节点执行记录。</p>;
  }

  return (
    <div className="grid gap-3">
      <ol className="grid grid-cols-1 gap-2 rounded-2xl border border-border/70 bg-white/60 p-4 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item, index) => {
          const itemBadges = badgesByNode.get(item.nodeId) ?? [];
          const itemIssueCount = issueCount(item.node, itemBadges);
          const itemVisualStatus = visualStatus(item.status, itemIssueCount);
          return (
            <li className="grid min-w-0 grid-cols-[minmax(0,1fr)_1.5rem] items-center gap-1.5" key={item.nodeId}>
              <button
                type="button"
                className={chipClass(itemVisualStatus, selectedId === item.nodeId)}
                title={`${nodeLabel(item.nodeId)} · ${item.status}`}
                onClick={() => setSelectedId((current) => (current === item.nodeId ? null : item.nodeId))}
              >
                {statusIcon(itemVisualStatus)}
                <span className="min-w-0">
                  <span className="block truncate font-mono text-xs font-medium text-text-primary">{item.nodeId}</span>
                  <span className="block truncate text-[10px] text-text-tertiary">{nodeLabel(item.nodeId)}</span>
                </span>
                <span className="flex min-w-0 items-center justify-end gap-1.5">
                  {durationText(item.node) ? <span className="shrink-0 font-mono text-[10px] text-text-tertiary">{durationText(item.node)}</span> : null}
                  {itemIssueCount > 0 ? (
                    <span className="flex h-4 min-w-4 shrink-0 items-center justify-center rounded-full bg-status-warning/20 px-1 text-[10px] font-semibold text-status-warning">
                      {itemIssueCount}
                    </span>
                  ) : null}
                </span>
              </button>
              <span
                aria-hidden="true"
                className={`flex h-6 w-6 shrink-0 items-center justify-center ${
                  index < items.length - 1 ? "text-text-tertiary" : "text-transparent"
                }`}
              >
                <ArrowRight className="h-4 w-4" strokeWidth={3} />
              </span>
            </li>
          );
        })}
      </ol>
      {selected ? <NodeDetailCard item={selected} badges={badgesByNode.get(selected.nodeId) ?? []} /> : null}
    </div>
  );
}

function NodeDetailCard({ item, badges }: { item: NodeTimelineItem; badges: NodePipelineBadge[] }) {
  const node = item.node;
  const error = node?.error;
  const warningBadges = badges.filter((badge) => badge.tone === "warning");
  const infoBadges = badges.filter((badge) => badge.tone !== "warning");
  const hasWarnings = Boolean(node && ((node.warnings ?? []).length > 0 || (node.degradations ?? []).length > 0)) || warningBadges.length > 0;
  const hasError = Boolean(error);
  return (
    <div className="grid h-[13rem] grid-rows-[auto_auto_minmax(0,1fr)] gap-3 overflow-hidden rounded-2xl border border-border/70 bg-white/60 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="font-mono text-sm font-semibold text-text-primary">{item.nodeId}</p>
          <p className="text-xs text-text-secondary">{nodeLabel(item.nodeId)}</p>
        </div>
        <StatusPill status={item.status} />
      </div>
      {node ? (
        <div className="flex min-h-5 flex-wrap gap-x-5 gap-y-1 overflow-hidden text-xs text-text-secondary">
          <Fact label="开始">
            <TimeText value={node.started_at} />
          </Fact>
          <Fact label="结束">
            <TimeText value={node.finished_at} />
          </Fact>
          {durationText(node) ? <Fact label="耗时">{durationText(node)}</Fact> : null}
          {(node.attempt ?? 1) > 1 ? <Fact label="重试次数">{node.attempt}</Fact> : null}
          {node.skipped_reason ? <Fact label="跳过原因">{node.skipped_reason}</Fact> : null}
        </div>
      ) : (
        <p className="text-xs text-text-tertiary">该节点尚未开始执行。</p>
      )}
      <div className="min-h-0 overflow-y-auto pr-1">
        {infoBadges.length > 0 ? (
          <div className="mb-2 flex flex-wrap gap-2">
            {infoBadges.map((badge) => (
              <span
                className={`inline-flex items-center gap-1 rounded-full border px-2 py-1 text-xs font-semibold ${providerBadgeClass(badge.tone)}`}
                key={`${badge.nodeId}-${badge.label}`}
                title={badge.detail}
              >
                {badge.caption ?? badge.label}
                {badge.count && badge.count > 0 ? <span>{badge.count}</span> : null}
              </span>
            ))}
          </div>
        ) : null}
        {hasWarnings ? (
          <div className="grid gap-1 rounded-xl border border-status-warning/20 bg-status-warning/10 p-3 text-sm text-status-warning">
            {warningBadges.map((badge) => (
              <p key={`${badge.nodeId}-${badge.label}`}>
                {badge.caption ?? badge.label}
                {badge.detail ? `：${badge.detail}` : ""}
              </p>
            ))}
            {(node?.warnings ?? []).map((warning) => (
              <p key={warning}>{warningLabel(warning)}</p>
            ))}
            {(node?.degradations ?? []).map((notice) => (
              <p key={`${notice.code}-${notice.node_id ?? ""}`}>{notice.message || warningLabel(notice.code)}</p>
            ))}
          </div>
        ) : null}
        {error ? (
          <div className={`grid gap-1 rounded-xl border border-status-error/25 bg-status-error/10 p-3 text-sm text-status-error ${hasWarnings ? "mt-2" : ""}`}>
            <p className="font-medium">{error.message}</p>
            <p>
              严重级别：{severityLabel(error.severity)} · {error.retryable ? "可重试" : "不可重试"}
            </p>
            {error.request_id ? <p className="font-mono text-xs">request_id: {error.request_id}</p> : null}
          </div>
        ) : null}
        {!hasWarnings && !hasError ? (
          <div className="rounded-xl border border-dashed border-border/70 bg-white/40 p-3 text-sm text-text-tertiary">
            {node ? "该节点没有警告、降级或错误记录。" : "该节点尚未开始执行。"}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className="text-text-tertiary">{label}</span>
      <span className="font-medium text-text-secondary">{children}</span>
    </span>
  );
}
