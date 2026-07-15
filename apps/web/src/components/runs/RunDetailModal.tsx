import { useQuery } from "@tanstack/react-query";
import { OctagonX, Play, RotateCw, Trash2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { api, type FinishedVideo, type RunCard, type RunDetailResponse } from "../../api/client";
import { EmptyState, ErrorState, LoadingState } from "../ui/State";
import { StatusPill } from "../ui/StatusPill";
import { TimeText } from "../TimeText";
import { EditorHandoffActions } from "../editor-handoff/EditorHandoffActions";
import { Modal } from "../ui/Modal";
import { VideoPlayer } from "../ui/VideoPlayer";
import { CaptionDisplayPanel, buildCaptionComposition } from "./CaptionDisplayPanel";
import { EditTimelinePreview, buildEditClips } from "./EditTimelinePreview";
import { NodePipeline, type NodePipelineBadge } from "./NodePipeline";
import { RunConfigPanel } from "./RunConfigPanel";
import { StageProgress } from "./StageProgress";
import { WindowPlanBoard, buildWindowBoard } from "./WindowPlanBoard";
import { toDisplayUrl } from "../../lib/url";
import { buildStages, canResumeRun, type RunAction } from "./runModel";

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
  const stages = buildStages(nodes);
  const editClips = buildEditClips(detail);
  const windowBoard = buildWindowBoard(detail, editClips);
  const captionPlan = buildCaptionComposition(detail);
  // 字幕计划区块只在数字人链混过字幕后出现：有 artifact 就展示明细，否则若 SubtitleAndBgmMix
  // 已完成（老 run 无该产物）给兼容空态；Seedance / 未到该节点则整块不渲染，避免噪音。
  const subtitleMixNode = nodes.find((node) => node.node_id === "SubtitleAndBgmMix");
  const subtitleMixSettled = subtitleMixNode ? ["succeeded", "degraded"].includes(subtitleMixNode.status) : false;
  const showCaptionSection = Boolean(captionPlan) || subtitleMixSettled;
  const coverSource = coverSourceInfo(detail, card);
  const nodeBadges = buildNodeProviderBadges(coverSource, finishedVideo);
  const [activeClipId, setActiveClipId] = useState<string | null>(null);

  const videoPreview = useQuery({
    queryKey: ["finished-video-preview", finishedVideo?.id],
    queryFn: () => api.finishedVideos.previewUrl(finishedVideo!.id),
    enabled: Boolean(finishedVideo?.id) && isOpen,
  });
  const videoUrl = toDisplayUrl(videoPreview.data?.url);

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="运行详情" size="2xl">
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
              {canResumeRun(card) ? (
                <button className="btn-secondary compactButton" type="button" onClick={() => onAction("resume", card)}>
                  <Play className="h-4 w-4" />
                  <span>续跑</span>
                </button>
              ) : null}
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
              <div className="mx-auto flex w-full max-w-[320px] flex-wrap items-center justify-center gap-2">
                <EditorHandoffActions finishedVideoId={finishedVideo?.id} compact />
              </div>
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

          {/* 剪辑时间线：优先窗口规划看板（窗口 + 检索 query + 候选 + 选择理由），老 run 回退片段列表 */}
          {windowBoard ? (
            <WindowPlanBoard board={windowBoard} activeClipId={activeClipId} onSelect={setActiveClipId} />
          ) : (
            <EditTimelinePreview clips={editClips} activeClipId={activeClipId} onSelect={setActiveClipId} />
          )}

          {/* 固定字幕带与字幕内强调 Run 的权威合成计划 */}
          {showCaptionSection ? <CaptionDisplayPanel plan={captionPlan} /> : null}

          {/* 节点时间线：按工作流模板顺序平铺全部节点（保留英文节点名） */}
          <section className="grid gap-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h4 className="text-base font-semibold text-text-primary">节点时间线</h4>
              {detail?.config?.workflow_template_id ? (
                <span className="flex items-center gap-1.5 text-xs text-text-tertiary">
                  工作流模板
                  <span className="badge bg-white/70 font-mono text-text-secondary">{detail.config.workflow_template_id}</span>
                </span>
              ) : null}
            </div>
            {nodes.length === 0 && !detail && !isLoading ? (
              <EmptyState title="暂无节点" />
            ) : (
              <NodePipeline templateId={detail?.config?.workflow_template_id} nodes={nodes} runStatus={card.status} badges={nodeBadges} />
            )}
          </section>
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

type CoverSourceInfo = {
  label: string;
  providerLabel: string;
  detail?: string;
  tone: "info" | "warning";
};

function buildNodeProviderBadges(coverSource: CoverSourceInfo | null, finishedVideo?: FinishedVideo | null): NodePipelineBadge[] {
  const badges: NodePipelineBadge[] = [];
  if (coverSource) {
    badges.push({
      nodeId: "ExportFinishedVideo",
      label: coverSource.providerLabel,
      caption: coverSource.label,
      detail: coverSource.detail,
      tone: coverSource.tone === "warning" ? "warning" : "success",
      count: coverSource.tone === "warning" ? 1 : undefined,
    });
  }
  const lipsyncBadge = lipsyncProviderBadge(finishedVideo);
  if (lipsyncBadge) badges.push(lipsyncBadge);
  return badges;
}

function lipsyncProviderBadge(finishedVideo?: FinishedVideo | null): NodePipelineBadge | null {
  const providerName = lipsyncProviderName(finishedVideo?.lipsync_provider_id);
  if (!providerName) return null;
  const fallbackUsed = Boolean(finishedVideo?.lipsync_fallback_used) || providerName === "VideoReTalk";
  if (!fallbackUsed) return null;
  return {
    nodeId: "LipSync",
    label: providerName,
    caption: `${providerName} 兜底生成`,
    detail: finishedVideo?.lipsync_fallback_reason ?? undefined,
    tone: "warning",
    count: 1,
  };
}

function lipsyncProviderName(providerId: string | null | undefined): string | null {
  if (!providerId) return null;
  if (providerId.startsWith("runninghub.heygem")) return "HeyGem";
  if (providerId.startsWith("dashscope.videoretalk")) return "VideoReTalk";
  return providerId;
}

function coverSourceInfo(detail?: RunDetailResponse, card?: RunCard): CoverSourceInfo | null {
  const cover = detail?.artifacts.find((artifact) => artifact.kind === "cover.image");
  const payload = asRecord(cover ? detail?.artifact_payloads?.[cover.artifact_id] : undefined);
  const degradedToFrame =
    card?.warnings?.includes("cover.frame_fallback") ||
    detail?.node_runs.some((node) =>
      (node.degradations ?? []).some((notice) => notice.code === "cover.frame_fallback"),
    );
  if (payload) {
    const source = asString(payload.source);
    const reason = asString(payload.reason);
    if (source === "ai") {
      const providerId = asString(payload.provider_id);
      const providerLabel = coverProviderName(providerId, asString(payload.provider_label));
      const fallbackFrom = asStringArray(payload.fallback_from_provider_profile_ids);
      const fallbackUsed = fallbackFrom.length > 0;
      return {
        label: fallbackUsed ? `${providerLabel} 兜底封面` : `${providerLabel} 生成封面`,
        providerLabel,
        detail: fallbackUsed ? `原封面生成不可用，已改用 ${providerLabel} 生成封面。` : undefined,
        tone: fallbackUsed ? "warning" : "info",
      };
    }
    if (source === "frame") {
      if (reason === "ai_failed") {
        return { label: "帧封面（AI 失败）", providerLabel: "帧封面", detail: "AI 封面生成失败后回退到视频帧。", tone: "warning" };
      }
      if (reason === "ai_unavailable") {
        return { label: "帧封面（AI 未启用）", providerLabel: "帧封面", detail: "没有可用的真实图片生成供应商或密钥。", tone: "info" };
      }
      return { label: "帧封面", providerLabel: "帧封面", detail: "封面来自视频帧。", tone: "info" };
    }
  }
  if (degradedToFrame) {
    return { label: "帧封面（AI 失败）", providerLabel: "帧封面", detail: "旧运行没有封面来源快照；根据降级记录判断。", tone: "warning" };
  }
  return imageRequestCoverSourceInfo(detail);
}

function imageRequestCoverSourceInfo(detail?: RunDetailResponse): CoverSourceInfo | null {
  const requestSnapshots = Object.values(detail?.artifact_payloads ?? {})
    .filter((payload): payload is Record<string, unknown> => Boolean(payload))
    .filter((payload) => asString(payload.capability_id) === "image.generate");
  const payload = requestSnapshots[requestSnapshots.length - 1];
  if (!payload) return null;
  const providerId = asString(payload.provider_id);
  const providerLabel = coverProviderName(providerId, undefined);
  return {
    label: `${providerLabel} 生成封面`,
    providerLabel,
    detail: undefined,
    tone: "info",
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
}

function coverProviderName(providerId: string | undefined, providerLabel: string | undefined): string {
  if (providerLabel === "image2" || providerId === "openai.image") return "image2";
  if (providerLabel === "seedream" || providerId === "volcengine.seedream") return "Seedream";
  return providerId || "AI";
}
