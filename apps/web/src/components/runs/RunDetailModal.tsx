import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Download, OctagonX, Play, RotateCw, Trash2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { api, type FinishedVideo, type NodeRun, type RunCard, type RunDetailResponse } from "../../api/client";
import { EmptyState, ErrorState, LoadingState } from "../State";
import { StatusPill } from "../Status";
import { TimeText } from "../TimeText";
import { EditorHandoffActions } from "../editor-handoff/EditorHandoffActions";
import { Modal } from "../ui/Modal";
import { VideoPlayer } from "../ui/VideoPlayer";
import { EditTimelinePreview, buildEditClips } from "./EditTimelinePreview";
import { RunConfigPanel } from "./RunConfigPanel";
import { StageProgress } from "./StageProgress";
import { shortId } from "../../lib/format";
import { toDisplayUrl } from "../../lib/url";
import { artifactLabel, buildStages, lipsyncProviderLabel, nodeLabel, severityLabel, warningLabel, type RunAction } from "./runModel";

function qcLabel(status: string) {
  if (status === "passed") return "质检通过";
  if (status === "failed") return "质检未通过";
  if (status === "warning") return "质检告警";
  return "待质检";
}

export function RunDetailModal({
  isOpen,
  onClose,
  card,
  detail,
  isLoading,
  error,
  finishedVideo,
  onAction,
}: {
  isOpen: boolean;
  onClose: () => void;
  card?: RunCard;
  detail?: RunDetailResponse;
  isLoading: boolean;
  error: unknown;
  finishedVideo?: FinishedVideo | null;
  onAction: (type: RunAction, run: RunCard) => void;
}) {
  const nodes = detail?.node_runs ?? [];
  const artifacts = detail?.artifacts ?? [];
  const stages = buildStages(nodes);
  const editClips = buildEditClips(detail);
  const [activeClipId, setActiveClipId] = useState<string | null>(null);

  const videoPreview = useQuery({
    queryKey: ["finished-video-preview", finishedVideo?.id],
    queryFn: () => api.finishedVideos.previewUrl(finishedVideo!.id),
    enabled: Boolean(finishedVideo?.id) && isOpen,
  });
  const videoUrl = toDisplayUrl(videoPreview.data?.url);

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={card ? `运行详情 ${shortId(card.runId)}` : "运行详情"} size="2xl">
      {!card ? <EmptyState title="暂无任务" /> : null}
      {isLoading ? <LoadingState label="加载运行详情" /> : null}
      {error ? <ErrorState error={error} /> : null}
      {card ? (
        <div className="grid gap-5">
          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-start">
            <div>
              <h3 className="text-xl font-semibold text-text-primary">{card.title}</h3>
              <p className="mt-1 text-sm text-text-secondary">当前阶段：{card.currentNodeLabel || "等待节点推进"}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button className="btn-secondary compactButton" type="button" disabled={!isProcessingStatus(card.status)} onClick={() => onAction("forceCancel", card)}>
                <OctagonX className="h-4 w-4" />
                <span>强制终止</span>
              </button>
              <button className="btn-secondary compactButton" type="button" disabled={!card.canRetry} onClick={() => onAction("retry", card)}>
                <RotateCw className="h-4 w-4" />
                <span>重试</span>
              </button>
              <button className="btn-secondary compactButton" type="button" disabled={!card.canResume} onClick={() => onAction("resume", card)}>
                <Play className="h-4 w-4" />
                <span>续跑</span>
              </button>
              <button className="btn-secondary compactButton" type="button" disabled={isProcessingStatus(card.status)} onClick={() => onAction("delete", card)}>
                <Trash2 className="h-4 w-4" />
                <span>删记录</span>
              </button>
            </div>
          </div>

          {/* 成片预览（优先展示） */}
          {finishedVideo ? (
            <section className="grid gap-2">
              {videoUrl ? (
                <VideoPlayer
                  src={videoUrl}
                  poster={toDisplayUrl(card.previewUrl) ?? undefined}
                  className="mx-auto aspect-[9/16] w-full max-w-[320px]"
                  durationHint={finishedVideo.duration_sec}
                  segments={editClips.map((clip) => ({ id: clip.id, start: clip.start, end: clip.end, label: clip.label, role: clip.playerRole }))}
                  activeSegmentId={activeClipId}
                  onSegmentClick={(segment) => setActiveClipId(segment.id ?? null)}
                />
              ) : (
                <div className="mx-auto flex aspect-[9/16] w-full max-w-[320px] items-center justify-center rounded-2xl border border-border/70 bg-surface-hover text-sm text-text-tertiary">
                  {videoPreview.isLoading ? "加载成片预览…" : "成片暂不可预览"}
                </div>
              )}
              <div className="mx-auto flex w-full max-w-[320px] items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="badge-info">{qcLabel(finishedVideo.qc_status)}</span>
                  {lipsyncProviderLabel(finishedVideo.lipsync_provider_id, finishedVideo.lipsync_fallback_used) ? (
                    <span
                      className={finishedVideo.lipsync_fallback_used ? "badge-warning" : "badge-info"}
                      title={finishedVideo.lipsync_fallback_used ? finishedVideo.lipsync_fallback_reason ?? undefined : undefined}
                    >
                      {lipsyncProviderLabel(finishedVideo.lipsync_provider_id, finishedVideo.lipsync_fallback_used)}
                    </span>
                  ) : null}
                </div>
                {videoUrl ? (
                  <a className="btn-secondary text-sm no-underline" href={videoUrl} target="_blank" rel="noopener noreferrer">
                    <Download className="h-4 w-4" />
                    <span>下载 / 全屏</span>
                  </a>
                ) : null}
              </div>
              {finishedVideo.lipsync_fallback_used && finishedVideo.lipsync_fallback_reason ? (
                <p className="mx-auto w-full max-w-[320px] rounded-xl border border-status-warning/20 bg-status-warning/10 px-3 py-2 text-xs text-status-warning">
                  口型兜底原因：{finishedVideo.lipsync_fallback_reason}
                </p>
              ) : null}
            </section>
          ) : null}

          <div className="grid gap-3 md:grid-cols-4">
            <DetailMetric label="状态" value={<StatusPill status={card.status} />} />
            <DetailMetric label="进度" value={`${Math.round(card.progress * 100)}%`} />
            <DetailMetric label="开始" value={<TimeText value={card.startedAt} />} />
            <DetailMetric label="更新" value={<TimeText value={card.updatedAt} />} />
          </div>

          {/* 生成配置（任务输入快照） */}
          <RunConfigPanel config={detail?.config} runId={card.runId} />

          {/* 生产阶段（友好聚合） */}
          <section className="grid gap-3">
            <h4 className="text-base font-semibold text-text-primary">生产阶段</h4>
            <StageProgress stages={stages} />
          </section>

          <EditTimelinePreview clips={editClips} activeClipId={activeClipId} onSelect={setActiveClipId} />

          {/* 高级（开发者）：原始节点时间线 + 产物清单 + 交接包 */}
          <details className="overflow-hidden rounded-2xl border border-border/70">
            <summary className="flex cursor-pointer items-center gap-2 px-4 py-3 text-sm font-semibold text-text-primary transition-colors hover:bg-surface-hover">
              <ChevronDown className="h-4 w-4 text-accent" />
              高级（开发者）：节点时间线 · 产物清单 · 交接包
            </summary>
            <div className="grid gap-5 border-t border-border/70 p-4">
              {detail?.config?.workflow_template_id ? (
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="text-text-tertiary">工作流模板</span>
                  <span className="font-mono text-xs text-text-secondary">{detail.config.workflow_template_id}</span>
                </div>
              ) : null}

              <section className="grid gap-3">
                <h5 className="text-sm font-semibold text-text-secondary">节点时间线</h5>
                {nodes.length === 0 && !isLoading ? <EmptyState title="暂无节点" /> : null}
                <div className="grid gap-3">
                  {nodes.map((node) => (
                    <NodeDetail key={node.id} node={node} />
                  ))}
                </div>
              </section>

              <section className="grid gap-3">
                <h5 className="text-sm font-semibold text-text-secondary">产物清单</h5>
                {artifacts.length === 0 ? <EmptyState title="暂无产物" detail="节点完成后会显示可下载产物。" /> : null}
                <div className="grid gap-2">
                  {artifacts.map((artifact) => {
                    const safeUrl = toDisplayUrl(artifact.uri);
                    const content = (
                      <>
                        <div className="min-w-0">
                          <p className="truncate font-medium text-text-primary">{artifactLabel(artifact.kind)}</p>
                          <p className="truncate font-mono text-xs text-text-tertiary">
                            {shortId(artifact.artifact_id, 12)} · {artifact.schema_version}
                          </p>
                        </div>
                        {safeUrl ? <Download className="h-4 w-4 text-accent" /> : <span className="text-xs text-text-tertiary">内部产物 URI</span>}
                      </>
                    );
                    if (!safeUrl) {
                      return (
                        <div className="flex items-center justify-between gap-3 rounded-2xl border border-border/70 bg-white/60 p-3" key={artifact.artifact_id}>
                          {content}
                        </div>
                      );
                    }
                    return (
                      <a
                        className="flex items-center justify-between gap-3 rounded-2xl border border-border/70 bg-white/60 p-3 no-underline hover:bg-white/80"
                        href={safeUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        key={artifact.artifact_id}
                      >
                        {content}
                      </a>
                    );
                  })}
                </div>
              </section>

              <section className="grid gap-3">
                <h5 className="text-sm font-semibold text-text-secondary">剪映草稿 / 交接包</h5>
                <EditorHandoffActions finishedVideoId={finishedVideo?.id} />
              </section>
            </div>
          </details>
        </div>
      ) : null}
    </Modal>
  );
}

function isProcessingStatus(status: RunCard["status"]) {
  return status === "created" || status === "admitted" || status === "running" || status === "cancelling";
}

function DetailMetric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-2xl border border-border/70 bg-white/60 p-3">
      <p className="text-xs text-text-tertiary">{label}</p>
      <div className="mt-1 text-sm font-medium text-text-primary">{value}</div>
    </div>
  );
}

function NodeDetail({ node }: { node: NodeRun }) {
  const warnings = [...(node.warnings ?? []), ...(node.degradations ?? []).map((item) => item.code)];
  return (
    <div className="grid gap-3 rounded-[20px] border border-border/70 bg-white/60 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="font-semibold text-text-primary">{nodeLabel(node.node_id)}</p>
          <p className="font-mono text-[11px] text-text-tertiary">{node.node_id}</p>
          <p className="text-xs text-text-secondary">
            <TimeText value={node.started_at} /> - <TimeText value={node.finished_at} />
          </p>
        </div>
        <StatusPill status={node.status} />
      </div>
      {warnings.length > 0 ? (
        <div className="grid gap-1 rounded-2xl border border-status-warning/20 bg-status-warning/10 p-3 text-sm text-status-warning">
          {warnings.map((warning) => (
            <p key={warning}>{warningLabel(warning)}</p>
          ))}
          {(node.degradations ?? []).map((notice) => (
            <p key={`${notice.code}-${notice.node_id ?? ""}`}>{notice.message || warningLabel(notice.code)}</p>
          ))}
        </div>
      ) : null}
      {node.error ? (
        <div className="grid gap-1 rounded-2xl border border-status-error/25 bg-status-error/10 p-3 text-sm text-status-error">
          <p className="font-medium">{node.error.message}</p>
          <p>
            严重级别：{severityLabel(node.error.severity)} · {node.error.retryable ? "可重试" : "不可重试"}
          </p>
          {node.error.request_id ? <p className="font-mono text-xs">request_id: {node.error.request_id}</p> : null}
        </div>
      ) : null}
    </div>
  );
}
