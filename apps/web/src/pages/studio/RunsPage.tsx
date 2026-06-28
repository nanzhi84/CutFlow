import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, Image as ImageIcon, OctagonX, Play, RotateCw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api, type ApiError, type RunCard } from "../../api/client";
import { EmptyState, ErrorState, LoadingState } from "../../components/ui/State";
import { StatusPill } from "../../components/ui/StatusPill";
import { StudioTabs } from "../../components/StudioTabs";
import { TimeText } from "../../components/TimeText";
import { RunDetailModal } from "../../components/runs/RunDetailModal";
import {
  confirmButtonText,
  confirmConsequences,
  confirmMessage,
  confirmTitle,
  connectionLabel,
  type PendingAction,
} from "../../components/runs/runModel";
import { useToast } from "../../components/ui/Toast";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { InfiniteScrollSentinel } from "../../components/ui/InfiniteScrollSentinel";
import { useRunEvents } from "../../hooks/useRunEvents";
import { usePageVisible } from "../../hooks/usePageVisible";
import { shortId } from "../../lib/format";
import { toDisplayUrl } from "../../lib/url";

export default function RunsPage() {
  const { caseId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const highlightedRunId = searchParams.get("run");
  const queryClient = useQueryClient();
  const toast = useToast();
  const pageVisible = usePageVisible();
  const [runLimit, setRunLimit] = useState(50);
  const finishedVideoLimit = 50;
  const caseDetail = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => api.cases.detail(caseId),
    enabled: Boolean(caseId),
  });
  const runs = useQuery({
    queryKey: ["case-runs", caseId, runLimit],
    queryFn: () => api.cases.runs(caseId, { limit: runLimit }),
    enabled: Boolean(caseId),
    refetchInterval: pageVisible ? 10000 : false,
  });
  const finishedVideos = useQuery({
    queryKey: ["finished-videos", caseId, finishedVideoLimit],
    queryFn: () => api.finishedVideos.list(caseId, { limit: finishedVideoLimit }),
    enabled: Boolean(caseId),
    refetchInterval: pageVisible ? 10000 : false,
  });
  const [selectedRunId, setSelectedRunId] = useState<string | null>(highlightedRunId);
  const [detailOpen, setDetailOpen] = useState(false);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const selectedCard = useMemo(
    () => runs.data?.items.find((run) => run.runId === selectedRunId) ?? runs.data?.items[0],
    [runs.data?.items, selectedRunId],
  );
  const runDetail = useQuery({
    queryKey: ["run-detail", selectedCard?.runId],
    queryFn: () => api.runs.detail(selectedCard!.runId),
    enabled: Boolean(selectedCard?.runId),
    refetchInterval: pageVisible ? 10000 : false,
  });
  const runEvents = useRunEvents(selectedCard?.runId, Boolean(selectedCard?.runId));
  const lastEventKey = runEvents.events.at(-1)?.event_id ?? runEvents.events.length;

  useEffect(() => {
    if (!selectedRunId && runs.data?.items[0]) {
      setSelectedRunId(runs.data.items[0].runId);
    }
  }, [runs.data?.items, selectedRunId]);

  useEffect(() => {
    if (!lastEventKey) return;
    void queryClient.invalidateQueries({ queryKey: ["case-runs", caseId] });
    if (selectedCard?.runId) {
      void queryClient.invalidateQueries({ queryKey: ["run-detail", selectedCard.runId] });
    }
  }, [caseId, lastEventKey, queryClient, selectedCard?.runId]);

  function selectRun(run: RunCard) {
    setSelectedRunId(run.runId);
    setSearchParams({ run: run.runId });
  }

  function openRunDetail(run: RunCard) {
    selectRun(run);
    setDetailOpen(true);
  }

  const cancelRun = useMutation({
    mutationFn: ({ runId, force }: { runId: string; force: boolean }) =>
      api.runs.cancel(runId, { reason: force ? "用户在前端强制终止" : "用户在前端取消", force }),
    onSuccess: async (_, variables) => {
      toast.success(variables.force ? "已发送强制终止请求" : "已发送中断请求");
      await queryClient.invalidateQueries({ queryKey: ["case-runs", caseId] });
      if (selectedCard?.runId) await queryClient.invalidateQueries({ queryKey: ["run-detail", selectedCard.runId] });
    },
  });
  const retryRun = useMutation({
    mutationFn: (runId: string) => api.runs.retry(runId, { reason: "前端重试" }),
    onSuccess: async (data) => {
      toast.success("已复制配置并重新提交", shortId(data.run.id));
      setSelectedRunId(data.run.id);
      setSearchParams({ run: data.run.id });
      await queryClient.invalidateQueries({ queryKey: ["case-runs", caseId] });
    },
    onError: (error: ApiError) => error,
  });
  const resumeRun = useMutation({
    mutationFn: (runId: string) => api.runs.resume(runId, { reason: "前端恢复", reuse_valid_artifacts: true }),
    onSuccess: async (data) => {
      toast.success("已从失败阶段续跑", "将复用已完成节点的有效产物");
      setSelectedRunId(data.run.id);
      setSearchParams({ run: data.run.id });
      await queryClient.invalidateQueries({ queryKey: ["case-runs", caseId] });
    },
  });
  const deleteRun = useMutation({
    mutationFn: (runId: string) => api.runs.delete(runId),
    onSuccess: async () => {
      toast.success("任务记录已删除", "成片文件未删除");
      setSelectedRunId(null);
      setSearchParams({});
      await queryClient.invalidateQueries({ queryKey: ["case-runs", caseId] });
      await queryClient.invalidateQueries({ queryKey: ["finished-videos", caseId] });
    },
    onError: (error: ApiError) => toast.error("删除任务记录失败", error),
  });

  const items = runs.data?.items ?? [];
  const hasMoreRuns = Boolean(runs.data && items.length >= runLimit);
  const confirmLoading =
    ((pendingAction?.type === "cancel" || pendingAction?.type === "forceCancel") && cancelRun.isPending) ||
    (pendingAction?.type === "retry" && retryRun.isPending) ||
    (pendingAction?.type === "resume" && resumeRun.isPending) ||
    (pendingAction?.type === "delete" && deleteRun.isPending);

  async function confirmRunAction() {
    if (!pendingAction) return;
    if (pendingAction.type === "cancel" || pendingAction.type === "forceCancel") {
      await cancelRun.mutateAsync({ runId: pendingAction.run.runId, force: pendingAction.type === "forceCancel" });
    } else if (pendingAction.type === "retry") {
      await retryRun.mutateAsync(pendingAction.run.runId);
    } else if (pendingAction.type === "resume") {
      await resumeRun.mutateAsync(pendingAction.run.runId);
    } else {
      await deleteRun.mutateAsync(pendingAction.run.runId);
    }
    setPendingAction(null);
  }

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>{caseDetail.data?.name ?? "成片"}</h1>
          <p>{connectionLabel(runEvents.state)} · {items.length} 个生成任务</p>
        </div>
      </header>
      <StudioTabs caseId={caseId} />
      {caseDetail.error ? <ErrorState error={caseDetail.error} /> : null}
      {runs.isLoading ? <LoadingState label="加载生成任务" /> : null}
      {runs.error ? <ErrorState error={runs.error} /> : null}
      {items.length === 0 && !runs.isLoading ? <EmptyState title="暂无生成任务" detail="从创作页提交任务后会实时出现在这里。" /> : null}

      {items.length > 0 ? (
        <div>
          <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(340px,1fr))]">
            {items.map((run) => (
              <article
                className={`group flex cursor-pointer gap-4 self-start rounded-[24px] border bg-[linear-gradient(180deg,rgba(255,255,252,0.9),rgba(249,250,244,0.96))] p-3 shadow-glow transition-all duration-200 hover:-translate-y-0.5 hover:border-accent/25 ${
                  run.runId === selectedCard?.runId ? "border-accent/25" : "border-border/80"
                } ${run.runId === highlightedRunId ? "ring-2 ring-accent/20" : ""}`}
                onClick={() => openRunDetail(run)}
                key={run.runId}
              >
                <div className="w-[84px] flex-none sm:w-[96px]">
                  <RunThumbnail run={run} />
                </div>

                <div className="flex min-w-0 flex-1 flex-col py-1">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 space-y-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <code className="rounded-full bg-accent/10 px-2.5 py-1 text-xs font-semibold text-accent">
                          {shortId(run.runId)}
                        </code>
                        <StatusPill status={run.status} />
                      </div>
                      <h2 className="line-clamp-2 text-base font-semibold text-text-primary">{run.title}</h2>
                      <p className="truncate text-sm text-text-secondary">{run.currentNodeLabel || "等待节点推进"}</p>
                    </div>
                  </div>

                  <div className="mt-3">
                    <div className="mb-1.5 flex items-center justify-between gap-3 text-xs text-text-tertiary">
                      <span>生产进度</span>
                      <span className="font-mono font-medium text-text-secondary">{Math.round(run.progress * 100)}%</span>
                    </div>
                    <div className="h-2 overflow-hidden rounded-full bg-surface-hover">
                      <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${Math.round(run.progress * 100)}%` }} />
                    </div>
                  </div>

                  <div className="mt-3 grid grid-cols-1 gap-2 text-xs text-text-secondary sm:grid-cols-2">
                    <div className="min-w-0">
                      <p className="text-[11px] text-text-tertiary">开始时间</p>
                      <p className="truncate">
                        <TimeText value={run.startedAt} />
                      </p>
                    </div>
                    <div className="min-w-0">
                      <p className="text-[11px] text-text-tertiary">更新时间</p>
                      <p className="truncate">
                        <TimeText value={run.updatedAt} />
                      </p>
                    </div>
                  </div>

                  <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-border/60 pt-3">
                    <span className="badge-info">{run.canPublish ? "可创建发布包" : "等待成片"}</span>
                    <div className="flex items-center gap-1.5" onClick={(event) => event.stopPropagation()}>
                      <button className="rounded-lg p-2 text-text-tertiary hover:bg-surface hover:text-text-primary" type="button" onClick={() => openRunDetail(run)} title="查看详情">
                        <Eye className="h-4 w-4" />
                      </button>
                      <button
                        className="rounded-lg p-2 text-text-tertiary hover:bg-status-error/10 hover:text-status-error"
                        type="button"
                        disabled={!isProcessingStatus(run.status)}
                        onClick={() => setPendingAction({ type: "forceCancel", run })}
                        title="强制终止"
                      >
                        <OctagonX className="h-4 w-4" />
                      </button>
                      <button
                        className="rounded-lg p-2 text-text-tertiary hover:bg-surface hover:text-text-primary"
                        type="button"
                        disabled={!run.canRetry}
                        onClick={() => setPendingAction({ type: "retry", run })}
                        title="重试"
                      >
                        <RotateCw className="h-4 w-4" />
                      </button>
                      <button
                        className="rounded-lg p-2 text-text-tertiary hover:bg-surface hover:text-text-primary"
                        type="button"
                        disabled={!run.canResume}
                        onClick={() => setPendingAction({ type: "resume", run })}
                        title="续跑"
                      >
                        <Play className="h-4 w-4" />
                      </button>
                      <button
                        className="rounded-lg p-2 text-text-tertiary hover:bg-status-error/10 hover:text-status-error"
                        type="button"
                        disabled={isProcessingStatus(run.status)}
                        onClick={() => setPendingAction({ type: "delete", run })}
                        title="删除任务记录"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                </div>
              </article>
            ))}
            <div className="col-span-full">
              <InfiniteScrollSentinel
                enabled={hasMoreRuns && !runs.isFetching}
                onVisible={() => setRunLimit((current) => current + 50)}
                label="继续加载任务记录"
              />
            </div>
          </div>
        </div>
      ) : null}

      <RunDetailModal
        isOpen={detailOpen}
        onClose={() => setDetailOpen(false)}
        card={selectedCard}
        detail={runDetail.data}
        isLoading={runDetail.isLoading}
        error={runDetail.error}
        finishedVideo={finishedVideos.data?.items.find((video) => video.run_id === selectedCard?.runId) ?? null}
        onAction={(type, run) => setPendingAction({ type, run })}
      />

      <ConfirmDialog
        isOpen={Boolean(pendingAction)}
        onClose={() => setPendingAction(null)}
        onConfirm={confirmRunAction}
        isLoading={confirmLoading}
        type={["cancel", "forceCancel", "delete"].includes(pendingAction?.type ?? "") ? "danger" : "warning"}
        title={confirmTitle(pendingAction)}
        message={confirmMessage(pendingAction)}
        consequences={confirmConsequences(pendingAction)}
        confirmText={confirmButtonText(pendingAction)}
      />
    </section>
  );
}

function isProcessingStatus(status: RunCard["status"]) {
  return status === "created" || status === "admitted" || status === "running" || status === "cancelling";
}

function RunThumbnail({ run }: { run: RunCard }) {
  const previewUrl = toDisplayUrl(run.previewUrl);
  return (
    <div className="group relative aspect-[3/4] w-full overflow-hidden rounded-[18px] border border-border/70 bg-[#20231f] shadow-sm">
      {previewUrl ? (
        <img
          src={previewUrl}
          alt={run.title}
          className="h-full w-full object-cover opacity-90 transition-opacity group-hover:opacity-100"
          loading="lazy"
          decoding="async"
        />
      ) : (
        <div className="flex h-full w-full flex-col items-center justify-center gap-1 bg-surface-hover text-text-tertiary">
          <ImageIcon className="h-6 w-6" />
          <span className="text-[10px]">{run.status === "running" || run.status === "admitted" ? "生成中" : "待出片"}</span>
        </div>
      )}
      <div className="absolute inset-x-0 bottom-0 h-20 bg-gradient-to-t from-black/60 to-transparent" />
      {run.status === "running" || run.status === "admitted" ? (
        <div className="absolute inset-0 flex items-center justify-center bg-black/20">
          <span className="flex h-10 w-10 items-center justify-center rounded-full bg-white/88 text-[#1b1d1a] shadow-lg">
            <Play className="h-5 w-5" />
          </span>
        </div>
      ) : null}
    </div>
  );
}
