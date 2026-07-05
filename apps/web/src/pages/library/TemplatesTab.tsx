import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  Database,
  Fingerprint,
  FolderUp,
  Loader2,
  Video,
  Wand2,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type MediaAssetRecord } from "../../api/client";
import { AnnotationEditorModal } from "../../components/annotation/AnnotationEditorModal";
import { TemplateBatchActionBar } from "../../components/library/TemplateBatchActionBar";
import { TemplateAssetCard } from "../../components/library/TemplateAssetCard";
import { TemplateGridSkeleton } from "../../components/library/TemplateGridSkeleton";
import { TemplateUploadModal } from "../../components/library/TemplateUploadModal";
import { UploadPlaceholderCard } from "../../components/library/UploadPlaceholderCard";
import { VideoPreviewModal } from "../../components/library/VideoPreviewModal";
import { UsageRankingPanel } from "../../components/library/UsageRankingPanel";
import {
  type UploadPlaceholder,
  readPreviewUrlMeta,
} from "../../components/library/libraryModel";
import { addPendingIds, removePendingId, removePendingIds } from "../../components/library/libraryInteractionModel";
import { toDisplayUrl } from "../../lib/url";
import { SearchInput } from "../../components/ui/SearchInput";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { useToast } from "../../components/ui/Toast";
import { InfiniteScrollSentinel } from "../../components/ui/InfiniteScrollSentinel";
import { EmptyState, ErrorState, LoadingState } from "../../components/ui/State";
import { usePageVisible } from "../../hooks/usePageVisible";
import { formatRelativeTime, shortId } from "../../lib/format";

const EMBEDDING_BATCH_LIMIT = 500;

type ClipEmbeddingStatusVm = Awaited<ReturnType<typeof api.mediaAssets.clipEmbeddingStatus>>;
type ClipEmbeddingJobVm = Awaited<ReturnType<typeof api.mediaAssets.clipEmbeddingJobStatus>>;
type AnnotationStatusVm = Awaited<ReturnType<typeof api.mediaAssets.annotationStatus>>;

function isClipEmbeddingJobActive(status: string | undefined): boolean {
  return status === "queued" || status === "running";
}

function clampPercent(done: number, total: number) {
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}

function safeDownloadFilename(title: string | undefined, fallback: string) {
  const base = (title?.trim() || fallback).replace(/[\\/:*?"<>|]+/g, "_").slice(0, 120) || fallback;
  return /\.[a-z0-9]{2,5}$/i.test(base) ? base : `${base}.mp4`;
}

function clipEmbeddingJobLabel(status: string | undefined) {
  if (status === "queued") return "排队中";
  if (status === "running") return "运行中";
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "有失败";
  return "待启动";
}

export function TemplatesTab() {
  const toast = useToast();
  const pageVisible = usePageVisible();
  const queryClient = useQueryClient();
  const [caseSearch, setCaseSearch] = useState("");
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [assetLimit, setAssetLimit] = useState(50);
  const [assetSearch, setAssetSearch] = useState("");
  const [sceneFilter, setSceneFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState<"all" | MediaAssetRecord["annotation_status"]>("all");
  const [batchMode, setBatchMode] = useState(false);
  const [selectedAssetIds, setSelectedAssetIds] = useState<string[]>([]);
  // Asset ids queued for the annotation confirm dialog (null = closed). Fed by
  // both the batch bar (selected ids) and the header 智能标注 (auto-collected
  // unannotated ids).
  const [annotateTargetIds, setAnnotateTargetIds] = useState<string[] | null>(null);
  const [embeddingConfirmOpen, setEmbeddingConfirmOpen] = useState(false);
  const [embeddingJobId, setEmbeddingJobId] = useState<string | null>(null);
  const [lastEmbeddingJob, setLastEmbeddingJob] = useState<ClipEmbeddingJobVm | null>(null);
  const [deleteTargetIds, setDeleteTargetIds] = useState<string[] | null>(null);
  // Asset highlighted (ring) after a usage-ranking click jumped to it.
  const [highlightAssetId, setHighlightAssetId] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [annotationAssetId, setAnnotationAssetId] = useState<string | null>(null);
  const [placeholders, setPlaceholders] = useState<UploadPlaceholder[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  // Per-asset playability flag from the preview-url response (true/false; absent => unknown).
  const [previewPlayable, setPreviewPlayable] = useState<Record<string, boolean>>({});
  const [previewAssetId, setPreviewAssetId] = useState<string | null>(null);
  const [previewLoadingId, setPreviewLoadingId] = useState<string | null>(null);
  const [downloadingAssetId, setDownloadingAssetId] = useState<string | null>(null);
  const [analyzingAssetIds, setAnalyzingAssetIds] = useState<Set<string>>(() => new Set());
  const [batchAnnotationPending, setBatchAnnotationPending] = useState(false);

  const casesQuery = useQuery({
    queryKey: ["library", "cases", caseSearch],
    queryFn: () => api.cases.list({ limit: 80, search: caseSearch.trim() || null }),
  });

  const cases = casesQuery.data?.items ?? [];

  // No auto-select: the case grid is the preface page; the materials view only
  // renders after the user picks a case.
  useEffect(() => {
    setAssetLimit(50);
  }, [selectedCaseId]);

  // Visual assets are one unified ``video`` bucket (#133): A-roll (口播) vs B-roll
  // (空镜) is a per-clip annotation decision, not an asset kind, so a single
  // ``kind="video"`` query covers the whole grid. The A-roll/B-roll split survives
  // only as the usage-ranking *medium* below.
  const videoQuery = useQuery({
    queryKey: ["library", "media", selectedCaseId, "video", assetLimit],
    queryFn: () => api.mediaAssets.list({ limit: assetLimit, case_id: selectedCaseId, kind: "video" }),
    enabled: Boolean(selectedCaseId),
    refetchInterval: pageVisible ? 10_000 : false,
  });

  const portraitUsageQuery = useQuery({
    queryKey: ["library", "usage-ranking", selectedCaseId, "portrait"],
    queryFn: () => api.mediaAssets.usageRanking("portrait", { case_id: selectedCaseId, top_n: 20 }),
    enabled: Boolean(selectedCaseId),
    refetchInterval: pageVisible ? 10_000 : false,
  });

  const brollUsageQuery = useQuery({
    queryKey: ["library", "usage-ranking", selectedCaseId, "broll"],
    queryFn: () => api.mediaAssets.usageRanking("broll", { case_id: selectedCaseId, top_n: 20 }),
    enabled: Boolean(selectedCaseId),
    refetchInterval: pageVisible ? 10_000 : false,
  });

  const annotationStatusQuery = useQuery({
    queryKey: ["library", "annotation-status", selectedCaseId, "video"],
    queryFn: () => api.mediaAssets.annotationStatus({ case_id: selectedCaseId!, kind: "video" }),
    enabled: Boolean(selectedCaseId),
    refetchInterval: pageVisible ? (batchAnnotationPending || analyzingAssetIds.size > 0 ? 1_500 : 10_000) : false,
  });

  const embeddingStatusQuery = useQuery({
    queryKey: ["library", "clip-embeddings", selectedCaseId, "all"],
    queryFn: () => api.mediaAssets.clipEmbeddingStatus({ case_id: selectedCaseId!, namespace: "all" }),
    enabled: Boolean(selectedCaseId),
    refetchInterval: pageVisible ? (embeddingJobId ? 1_500 : 10_000) : false,
  });

  const embeddingJobQuery = useQuery({
    queryKey: ["library", "clip-embedding-job", embeddingJobId],
    queryFn: () => api.mediaAssets.clipEmbeddingJobStatus(embeddingJobId!),
    enabled: Boolean(embeddingJobId),
    refetchInterval: pageVisible && embeddingJobId ? 1_500 : false,
  });

  const activeItems = useMemo(() => {
    const merged = [...(videoQuery.data?.items ?? [])];
    const byId = new Map<string, { card: (typeof merged)[number]; index: number }>();
    merged.forEach((card, index) => {
      if (!byId.has(card.asset.id)) byId.set(card.asset.id, { card, index });
    });
    return Array.from(byId.values())
      .sort((left, right) => {
        const leftTime = Date.parse(left.card.asset.created_at ?? "");
        const rightTime = Date.parse(right.card.asset.created_at ?? "");
        if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) return rightTime - leftTime;
        if (Number.isFinite(leftTime) !== Number.isFinite(rightTime)) return Number.isFinite(rightTime) ? 1 : -1;
        const titleCompare = left.card.asset.title.localeCompare(right.card.asset.title, "zh-Hans-CN");
        return titleCompare || left.index - right.index;
      })
      .map((entry) => entry.card);
  }, [videoQuery.data]);
  const assetQueries = [videoQuery];
  const hasMoreAssets = assetQueries.some((query) => Boolean(query.data && query.data.items.length >= assetLimit));
  const isAssetsLoading = assetQueries.some((query) => query.isLoading);
  const isAssetsFetching = assetQueries.some((query) => query.isFetching);
  const assetError = assetQueries.find((query) => query.error)?.error;
  const selectedCase = cases.find((item) => item.id === selectedCaseId) ?? null;
  const usageByAssetId = useMemo(
    () => new Map([...(portraitUsageQuery.data?.items ?? []), ...(brollUsageQuery.data?.items ?? [])].map((item) => [item.asset_id, item])),
    [portraitUsageQuery.data, brollUsageQuery.data],
  );
  const previewCard = useMemo(() => {
    if (!previewAssetId) return null;
    const pool = [...(videoQuery.data?.items ?? [])];
    return pool.find((card) => card.asset.id === previewAssetId) ?? null;
  }, [previewAssetId, videoQuery.data]);
  const scenes = useMemo(() => {
    const values = new Set<string>();
    activeItems.forEach((card) => card.asset.tags?.forEach((tag) => values.add(tag)));
    return Array.from(values).sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
  }, [activeItems]);

  const filteredItems = useMemo(() => {
    const keyword = assetSearch.trim().toLowerCase();
    return activeItems.filter((card) => {
      const asset = card.asset;
      const matchesKeyword =
        !keyword ||
        asset.title.toLowerCase().includes(keyword) ||
        asset.id.toLowerCase().includes(keyword) ||
        (asset.tags ?? []).some((tag) => tag.toLowerCase().includes(keyword));
      const matchesScene = sceneFilter === "all" || (asset.tags ?? []).includes(sceneFilter);
      const matchesStatus = statusFilter === "all" || asset.annotation_status === statusFilter;
      return matchesKeyword && matchesScene && matchesStatus;
    });
  }, [activeItems, assetSearch, sceneFilter, statusFilter]);

  const visiblePlaceholders = placeholders.filter((item) => item.kind === "video");

  const stabilizeMutation = useMutation({
    mutationFn: (assetIds: string[]) => api.mediaAssets.batchStabilize({ asset_ids: assetIds }),
    onSuccess: async (response) => {
      await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
      const failed = response.results.filter((item) => item.status === "failed");
      if (failed.length > 0) {
        toast.warning("部分素材增稳失败", failed.map((item) => item.message || item.error_code).join("；"));
      } else {
        toast.success("批量增稳完成", `已处理 ${response.results.length} 个素材`);
      }
      setSelectedAssetIds([]);
    },
    onError: (error) => toast.error("批量增稳失败", error),
  });

  // All loaded assets in the current view that are not yet annotated — the
  // 智能标注 target and the count force=false will actually bill the VLM for.
  const unannotatedAssetIds = useMemo(
    () =>
      activeItems
        .filter((card) => card.asset.annotation_status !== "annotated" && !analyzingAssetIds.has(card.asset.id))
        .map((card) => card.asset.id),
    [activeItems, analyzingAssetIds],
  );
  // Of the ids queued for the confirm dialog, how many are unannotated (the ones
  // that will really hit the VLM; already-annotated ones are skipped server-side).
  const annotateTargetUnannotatedCount = useMemo(
    () =>
      activeItems.filter(
        (card) =>
          annotateTargetIds?.includes(card.asset.id) &&
          card.asset.annotation_status !== "annotated" &&
          !analyzingAssetIds.has(card.asset.id),
      ).length,
    [activeItems, annotateTargetIds, analyzingAssetIds],
  );
  const embeddingPendingCount = embeddingStatusQuery.data?.pending_count ?? 0;
  const embeddingCandidateCount = embeddingStatusQuery.data?.candidate_count ?? 0;
  const embeddingBatchSize = Math.max(1, Math.min(embeddingPendingCount || EMBEDDING_BATCH_LIMIT, EMBEDDING_BATCH_LIMIT));
  const embeddingJobStatus = embeddingJobQuery.data?.status;
  const embeddingJobActive = Boolean(isClipEmbeddingJobActive(embeddingJobStatus) || (embeddingJobId && !embeddingJobQuery.data));

  const embeddingMutation = useMutation({
    mutationFn: () => {
      if (!selectedCaseId) throw new Error("请选择案例");
      return api.mediaAssets.indexClipEmbeddings({
        schema_version: "clip_embedding_index_request.v1",
        case_id: selectedCaseId,
        namespace: "all",
        provider_profile_id: "dashscope.multimodal_embedding.prod",
        limit: embeddingBatchSize,
        force: false,
      });
    },
    onSuccess: async (response) => {
      setEmbeddingConfirmOpen(false);
      setLastEmbeddingJob(null);
      setEmbeddingJobId(response.job_id);
      await queryClient.invalidateQueries({ queryKey: ["library", "clip-embeddings", selectedCaseId] });
      toast.success("视频嵌入任务已开始", `任务 ${shortId(response.job_id)} · 本次排队 ${response.queued_count} 条`);
    },
    onError: (error) => {
      setEmbeddingConfirmOpen(false);
      toast.error("生成视频嵌入失败", error);
    },
  });

  useEffect(() => {
    const job = embeddingJobQuery.data;
    if (!job || !embeddingJobId || !["succeeded", "failed"].includes(job.status)) return;
    setLastEmbeddingJob(job);
    void queryClient.invalidateQueries({ queryKey: ["library", "clip-embeddings", job.case_id] });
    void queryClient.invalidateQueries({ queryKey: ["library", "media", job.case_id] });
    if (job.status === "failed") {
      toast.warning(
        "视频嵌入任务结束，有失败项",
        `新增 ${job.indexed_now_count} 条，失败 ${job.failed_count} 条，剩余 ${job.remaining_count} 条`,
      );
    } else if (job.indexed_now_count > 0) {
      toast.success("视频嵌入已完成", `新增 ${job.indexed_now_count} 条，剩余 ${job.remaining_count} 条`);
    } else {
      toast.info("无需生成", "当前可用片段已有 clip 视频嵌入。");
    }
    setEmbeddingJobId(null);
  }, [embeddingJobId, embeddingJobQuery.data, queryClient, toast]);

  const isEmbeddingBusy = embeddingMutation.isPending || embeddingJobActive;
  const displayedEmbeddingJob = embeddingJobQuery.data ?? lastEmbeddingJob;

  // Batch delete: the backend exposes per-asset DELETE, so fan out one call per
  // selected asset.
  const deleteMutation = useMutation({
    mutationFn: async (assetIds: string[]) => {
      await Promise.all(assetIds.map((id) => api.mediaAssets.delete(id)));
      return assetIds;
    },
    onSuccess: async (assetIds) => {
      await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
      await queryClient.invalidateQueries({ queryKey: ["library", "annotation-status", selectedCaseId] });
      await queryClient.invalidateQueries({ queryKey: ["library", "clip-embeddings", selectedCaseId] });
      await queryClient.invalidateQueries({ queryKey: ["library", "usage-ranking", selectedCaseId] });
      toast.success(assetIds.length === 1 ? "素材已删除" : "批量删除完成", `已删除 ${assetIds.length} 个素材`);
      setSelectedAssetIds((current) => current.filter((id) => !assetIds.includes(id)));
      setDeleteTargetIds(null);
    },
    onError: (error) => {
      toast.error("删除失败", error);
      setDeleteTargetIds(null);
    },
  });

  // Jump from a usage-ranking item to its asset card: clear filters that might
  // hide it, scroll it into view, and flash a highlight ring.
  function jumpToAsset(assetId: string) {
    if (!activeItems.some((card) => card.asset.id === assetId)) {
      toast.info("该素材不在当前列表", "可能尚未加载或不属于当前案例。");
      return;
    }
    setAssetSearch("");
    setSceneFilter("all");
    setStatusFilter("all");
    setHighlightAssetId(assetId);
    window.setTimeout(() => {
      document.getElementById(`asset-${assetId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 60);
    window.setTimeout(() => setHighlightAssetId((current) => (current === assetId ? null : current)), 2600);
  }

  function runAnnotation(assetId: string) {
    if (analyzingAssetIds.has(assetId)) return;
    if (!selectedCaseId) {
      toast.error("重新标注失败", "请选择案例后再操作。");
      return;
    }
    setAnalyzingAssetIds((current) => addPendingIds(current, [assetId]));
    void (async () => {
      const response = await api.annotations.rerun(assetId, { force: true });
      await queryClient.invalidateQueries({ queryKey: ["library", "annotation", assetId] });
      await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
      await queryClient.invalidateQueries({ queryKey: ["library", "annotation-status", selectedCaseId] });
      try {
        const job = await api.mediaAssets.indexClipEmbeddings({
          schema_version: "clip_embedding_index_request.v1",
          case_id: selectedCaseId,
          asset_ids: [assetId],
          namespace: "all",
          provider_profile_id: "dashscope.multimodal_embedding.prod",
          limit: EMBEDDING_BATCH_LIMIT,
          force: true,
        });
        setLastEmbeddingJob(null);
        setEmbeddingJobId(job.job_id);
        await queryClient.invalidateQueries({ queryKey: ["library", "clip-embeddings", selectedCaseId] });
        const annotationText = response.run_id ? `标注 ${shortId(response.run_id)} · ` : "";
        toast.success(
          "重新标注与嵌入已提交",
          `${annotationText}嵌入任务 ${shortId(job.job_id)} · 排队 ${job.queued_count} 条`,
        );
      } catch (error) {
        toast.error("嵌入重建失败", error);
      }
    })()
      .catch((error) => toast.error("重新标注失败", error))
      .finally(() => {
        setAnalyzingAssetIds((current) => removePendingId(current, assetId));
      });
  }

  function runAnnotationBatch(assetIds: string[]) {
    if (batchAnnotationPending) return;
    const requestedIds = new Set(assetIds);
    const targetIds = activeItems
      .filter(
        (card) =>
          requestedIds.has(card.asset.id) &&
          card.asset.annotation_status !== "annotated" &&
          !analyzingAssetIds.has(card.asset.id),
      )
      .map((card) => card.asset.id);
    setAnnotateTargetIds(null);
    setSelectedAssetIds([]);
    if (targetIds.length === 0) {
      toast.info("无需标注", "选中的素材已经标注。");
      return;
    }
    setBatchAnnotationPending(true);
    setAnalyzingAssetIds((current) => addPendingIds(current, targetIds));
    void api.annotations
      .batch({ schema_version: "annotation_batch_request.v1", asset_ids: targetIds, force: false })
      .then(async (response) => {
        await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
        await queryClient.invalidateQueries({ queryKey: ["library", "annotation-status", selectedCaseId] });
        toast.success(
          "批量标注已提交",
          `新标注 ${response.completed_count} 个 · 跳过 ${response.skipped_count} 个已标注 · 失败 ${response.failed_count} 个`,
        );
      })
      .catch((error) => toast.error("批量标注失败", error))
      .finally(() => {
        setBatchAnnotationPending(false);
        setAnalyzingAssetIds((current) => removePendingIds(current, targetIds));
      });
  }

  async function autoReplaceUploads(uploadSessionIds: string[]) {
    const response = await api.mediaAssets.autoMatchReplace({
      case_id: selectedCaseId,
      kind: "video",
      upload_session_ids: uploadSessionIds,
    });
    await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
    await queryClient.invalidateQueries({ queryKey: ["library", "annotation-status", selectedCaseId] });
    const matched = response.results.filter((item) => item.status === "matched").length;
    const pending = response.results.length - matched;
    if (pending > 0) {
      toast.warning("自动匹配替换完成", `已替换 ${matched} 个，${pending} 个需手动处理。`);
    } else {
      toast.success("自动匹配替换完成", `已替换 ${matched} 个素材。`);
    }
  }

  async function ensurePreview(assetId: string) {
    if (previewUrls[assetId]) return previewUrls[assetId];
    try {
      const response = await api.mediaAssets.previewUrl(assetId);
      const meta = readPreviewUrlMeta(response);
      if (meta.playable !== undefined) {
        setPreviewPlayable((current) => ({ ...current, [assetId]: meta.playable! }));
      }
      const displayUrl = toDisplayUrl(response.url);
      if (!displayUrl) {
        toast.info("素材预览暂不可用（待真实媒体接入）");
        return null;
      }
      setPreviewUrls((current) => ({ ...current, [assetId]: displayUrl }));
      return displayUrl;
    } catch (error) {
      toast.error("预览地址获取失败", error);
      return null;
    }
  }

  // Open the enlarged preview modal: ensure a playable URL first (with per-card loading feedback),
  // then surface the modal even if the URL is unavailable (the modal renders a placeholder state).
  async function openPreview(assetId: string) {
    setPreviewLoadingId(assetId);
    try {
      await ensurePreview(assetId);
    } finally {
      setPreviewLoadingId((current) => (current === assetId ? null : current));
    }
    setPreviewAssetId(assetId);
  }

  async function downloadAsset(assetId: string) {
    if (downloadingAssetId) return;
    setDownloadingAssetId(assetId);
    try {
      const url = await ensurePreview(assetId);
      if (!url) return;
      const card = activeItems.find((item) => item.asset.id === assetId);
      const link = document.createElement("a");
      link.href = url;
      link.download = safeDownloadFilename(card?.asset.title, assetId);
      link.rel = "noopener";
      document.body.appendChild(link);
      link.click();
      link.remove();
    } finally {
      setDownloadingAssetId((current) => (current === assetId ? null : current));
    }
  }

  function setPlaceholder(update: UploadPlaceholder) {
    setPlaceholders((current) => {
      const exists = current.some((item) => item.id === update.id);
      return exists ? current.map((item) => (item.id === update.id ? update : item)) : [update, ...current];
    });
  }

  function clearSuccessfulPlaceholder(id: string) {
    setPlaceholders((current) => current.filter((item) => item.id !== id));
  }

  if (!selectedCaseId) {
    return (
      <section className="grid gap-4">
        <div className="card grid gap-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-xl font-semibold text-text-primary">选择案例</h2>
              <p className="mt-1 text-sm text-text-secondary">点击案例卡片进入其素材库。</p>
            </div>
          </div>
          <SearchInput value={caseSearch} onChange={setCaseSearch} placeholder="搜索案例" />
          {casesQuery.isLoading ? <LoadingState label="加载案例" /> : null}
          {casesQuery.error ? <ErrorState error={casesQuery.error} /> : null}
          {!casesQuery.isLoading && !casesQuery.error && cases.length === 0 ? (
            <EmptyState title="暂无案例" detail="先在案例中心创建案例。" />
          ) : null}
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {cases.map((item) => (
            <button
              key={item.id}
              type="button"
              className="group rounded-[24px] border border-border/80 bg-white/65 p-4 text-left shadow-glow transition-all hover:-translate-y-0.5 hover:border-accent/25"
              onClick={() => {
                setSelectedCaseId(item.id);
                setSelectedAssetIds([]);
                setBatchMode(false);
              }}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <span className="badge bg-accent/10 text-accent">案例</span>
                  <h3 className="mt-3 truncate text-lg font-semibold text-text-primary">{item.name}</h3>
                  <p className="mt-1 font-mono text-xs text-text-tertiary">{shortId(item.id, 12)}</p>
                </div>
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-accent/10 text-accent transition-transform group-hover:translate-x-0.5">
                  <ArrowRight className="h-5 w-5" />
                </span>
              </div>
              <dl className="mt-4 grid gap-2 text-xs text-text-secondary">
                <div className="flex justify-between gap-2">
                  <dt>素材</dt>
                  <dd>{item.material_count} 个</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>脚本</dt>
                  <dd>{item.script_count} 个</dd>
                </div>
                <div className="flex justify-between gap-2">
                  <dt>更新时间</dt>
                  <dd>{formatRelativeTime(item.updated_at ?? item.created_at)}</dd>
                </div>
              </dl>
            </button>
            ))}
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
      <div className="card grid content-start gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <button
              className="icon-button mt-0.5"
              type="button"
              aria-label="返回案例"
              title="返回案例列表"
              onClick={() => {
                setSelectedCaseId(null);
                setSelectedAssetIds([]);
                setBatchMode(false);
              }}
            >
              <ArrowLeft className="h-4 w-4" />
            </button>
            <div>
              <h2 className="text-xl font-semibold text-text-primary">{selectedCase?.name ?? "素材库"}</h2>
              <p className="mt-1 text-sm text-text-secondary">视频素材共用上传、标注与替换流程。</p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <button className="btn-secondary" type="button" onClick={() => setBatchMode((value) => !value)}>
              <CheckCircle2 className="h-4 w-4" />
              <span>{batchMode ? "退出批量" : "批量操作"}</span>
            </button>
            <button className="btn-primary" type="button" onClick={() => setUploadOpen(true)} disabled={!selectedCaseId}>
              <FolderUp className="h-4 w-4" />
              <span>上传素材</span>
            </button>
          </div>
        </div>

        <AnnotationProgressPanel
          status={annotationStatusQuery.data}
          loading={annotationStatusQuery.isFetching}
          active={batchAnnotationPending || analyzingAssetIds.size > 0}
          loadedPendingCount={unannotatedAssetIds.length}
          disabled={!selectedCaseId || unannotatedAssetIds.length === 0 || batchAnnotationPending}
          onStart={() => setAnnotateTargetIds(unannotatedAssetIds)}
        />

        <ClipEmbeddingProgressPanel
          status={embeddingStatusQuery.data}
          job={displayedEmbeddingJob}
          loading={embeddingStatusQuery.isFetching || embeddingJobQuery.isFetching}
          error={embeddingStatusQuery.error || embeddingJobQuery.error}
          active={embeddingJobActive}
          starting={embeddingMutation.isPending}
          batchSize={embeddingBatchSize}
          disabled={
            !selectedCaseId ||
            isEmbeddingBusy ||
            Boolean(embeddingStatusQuery.data && embeddingPendingCount === 0)
          }
          onStart={() => setEmbeddingConfirmOpen(true)}
        />

        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="flex items-center gap-2 text-base font-semibold text-text-primary">
            <Video className="h-4 w-4 text-accent" />
            <span>视频素材</span>
          </h3>
          <span className="badge bg-white/70 text-text-secondary">{activeItems.length} 个</span>
        </div>

        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_180px_190px]">
          <SearchInput value={assetSearch} onChange={setAssetSearch} placeholder="搜索标题、ID 或标签" />
          <select value={sceneFilter} onChange={(event) => setSceneFilter(event.target.value)}>
            <option value="all">全部场景</option>
            {scenes.map((scene) => (
              <option key={scene} value={scene}>
                {scene}
              </option>
            ))}
          </select>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)}>
            <option value="all">全部标注状态</option>
            <option value="pending">待标注</option>
            <option value="annotated">已标注</option>
            <option value="annotation_failed">标注失败</option>
          </select>
        </div>

        {batchMode ? (
          <TemplateBatchActionBar
            selectedCount={selectedAssetIds.length}
            totalCount={filteredItems.length}
            isStabilizing={stabilizeMutation.isPending}
            isAnnotating={batchAnnotationPending || analyzingAssetIds.size > 0}
            isDeleting={deleteMutation.isPending}
            onSelectAll={() => setSelectedAssetIds(filteredItems.map((card) => card.asset.id))}
            onStabilize={() => stabilizeMutation.mutate(selectedAssetIds)}
            onAnnotate={() => setAnnotateTargetIds(selectedAssetIds)}
            onDelete={() => setDeleteTargetIds(selectedAssetIds)}
            onClear={() => setSelectedAssetIds([])}
          />
        ) : null}

        {isAssetsLoading ? <TemplateGridSkeleton /> : null}
        {assetError ? <ErrorState error={assetError} /> : null}

        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {visiblePlaceholders.map((item) => (
            <UploadPlaceholderCard key={item.id} item={item} />
          ))}
          {filteredItems.map((card) => (
            <TemplateAssetCard
              key={card.asset.id}
              domId={`asset-${card.asset.id}`}
              highlighted={highlightAssetId === card.asset.id}
              card={card}
              previewUrl={toDisplayUrl(previewUrls[card.asset.id] ?? card.preview_url)}
              batchMode={batchMode}
              selected={selectedAssetIds.includes(card.asset.id)}
              isAnalyzing={analyzingAssetIds.has(card.asset.id)}
              isDownloading={downloadingAssetId === card.asset.id}
              isPreviewLoading={previewLoadingId === card.asset.id}
              usage={usageByAssetId.get(card.asset.id)}
              onToggleSelected={() =>
                setSelectedAssetIds((current) =>
                  current.includes(card.asset.id) ? current.filter((id) => id !== card.asset.id) : [...current, card.asset.id],
                )
              }
              onPreview={() => void openPreview(card.asset.id)}
              onAnalyze={() => runAnnotation(card.asset.id)}
              onOpenAnnotation={() => setAnnotationAssetId(card.asset.id)}
              onDownload={() => void downloadAsset(card.asset.id)}
              onDelete={() => setDeleteTargetIds([card.asset.id])}
            />
          ))}
        </div>
        <InfiniteScrollSentinel
          enabled={hasMoreAssets && !isAssetsFetching}
          onVisible={() => setAssetLimit((current) => current + 50)}
          label="继续加载视频素材"
        />

        {!isAssetsLoading && visiblePlaceholders.length === 0 && filteredItems.length === 0 ? (
          <EmptyState icon={Video} title="暂无视频素材" detail="上传素材后会进入标注队列。" />
        ) : null}
      </div>

      {/* Ranking sidebar is its OWN scroll region (pinned to the viewport, scrolls
          internally) so the A-roll/B-roll panels are fully reachable independently of
          the material grid's page scroll — no shared sticky binding. */}
      <div className="grid content-start gap-4 xl:sticky xl:top-4 xl:max-h-[calc(100vh-2rem)] xl:self-start xl:overflow-y-auto xl:pr-1">
        <UsageRankingPanel
          title="A-roll（口播）"
          report={portraitUsageQuery.data}
          isLoading={portraitUsageQuery.isLoading}
          error={portraitUsageQuery.error}
          onItemClick={jumpToAsset}
          embedded
        />
        <UsageRankingPanel
          title="B-roll（空镜）"
          report={brollUsageQuery.data}
          isLoading={brollUsageQuery.isLoading}
          error={brollUsageQuery.error}
          onItemClick={jumpToAsset}
          embedded
        />
      </div>

      <TemplateUploadModal
        isOpen={uploadOpen}
        onClose={() => setUploadOpen(false)}
        caseId={selectedCaseId}
        kind="video"
        onPlaceholder={setPlaceholder}
        onSuccess={async (placeholderId) => {
          await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
          await queryClient.invalidateQueries({ queryKey: ["library", "annotation-status", selectedCaseId] });
          clearSuccessfulPlaceholder(placeholderId);
        }}
        onAutoReplace={autoReplaceUploads}
      />
      <ConfirmDialog
        isOpen={annotateTargetIds !== null}
        onClose={() => setAnnotateTargetIds(null)}
        onConfirm={() => runAnnotationBatch(annotateTargetIds ?? [])}
        title="批量标注素材"
        message={`将对 ${annotateTargetIds?.length ?? 0} 个素材中未标注的部分调用 VLM 视觉模型标注；已标注的会自动跳过。`}
        consequences={[
          `预计约 ${annotateTargetUnannotatedCount} 个素材会真实调用 VLM（产生费用）`,
          "已标注素材会被跳过，不重复计费",
          "标注为异步任务，状态会在素材卡片上更新",
        ]}
        confirmText="开始标注"
        type="warning"
        isLoading={batchAnnotationPending}
      />
      <ConfirmDialog
        isOpen={embeddingConfirmOpen}
        onClose={() => setEmbeddingConfirmOpen(false)}
        onConfirm={() => embeddingMutation.mutate()}
        title="生成 clip 视频嵌入"
        message="将为当前案例已标注片段裁出 clip，上传 OSS 后调用百炼视频 embedding；已有新版本索引会跳过。"
        consequences={[
          `当前可用片段 ${embeddingCandidateCount} 条，待补 ${embeddingPendingCount} 条`,
          `本次最多排队 ${embeddingBatchSize} 条，可继续点击补齐剩余片段`,
          "会调用百炼 qwen3-vl-embedding 的 video 输入",
        ]}
        confirmText="生成视频嵌入"
        type="warning"
        isLoading={isEmbeddingBusy}
      />
      <ConfirmDialog
        isOpen={deleteTargetIds !== null}
        onClose={() => setDeleteTargetIds(null)}
        onConfirm={() => deleteMutation.mutate(deleteTargetIds ?? [])}
        title={(deleteTargetIds?.length ?? 0) === 1 ? "删除素材" : "批量删除素材"}
        message={`确定删除 ${deleteTargetIds?.length ?? 0} 个素材吗？`}
        consequences={[
          "素材记录与其标注会被删除，操作不可撤销",
          "已用于历史成片的产物不受影响",
        ]}
        confirmText="删除"
        type="danger"
        isLoading={deleteMutation.isPending}
      />
      <AnnotationEditorModal assetId={annotationAssetId} caseId={selectedCaseId} onClose={() => setAnnotationAssetId(null)} />
      <VideoPreviewModal
        card={previewCard}
        previewUrl={previewCard ? toDisplayUrl(previewUrls[previewCard.asset.id] ?? previewCard.preview_url) : null}
        playable={previewCard ? previewPlayable[previewCard.asset.id] : undefined}
        onClose={() => setPreviewAssetId(null)}
        onOpenAnnotation={
          previewCard
            ? () => {
                const id = previewCard.asset.id;
                setPreviewAssetId(null);
                setAnnotationAssetId(id);
              }
            : undefined
        }
      />
    </section>
  );
}

function AnnotationProgressPanel({
  status,
  loading,
  active,
  loadedPendingCount,
  disabled,
  onStart,
}: {
  status?: AnnotationStatusVm;
  loading: boolean;
  active: boolean;
  loadedPendingCount: number;
  disabled: boolean;
  onStart: () => void;
}) {
  const totalCount = status?.total_count ?? 0;
  const annotatedCount = status?.annotated_count ?? 0;
  const pendingCount = status?.pending_count ?? 0;
  const failedCount = status?.failed_count ?? 0;
  const percent = clampPercent(annotatedCount, totalCount);
  const complete = totalCount > 0 && pendingCount === 0 && failedCount === 0 && !active;
  const label = active ? "标注中" : complete ? "已齐全" : pendingCount > 0 ? "待标注" : failedCount > 0 ? "有失败" : "无素材";
  const chipClass = active
    ? "bg-status-info/12 text-status-info"
    : complete
      ? "bg-status-success/15 text-status-success"
      : failedCount > 0
        ? "bg-status-error/12 text-status-error"
        : pendingCount > 0
          ? "bg-status-warning/12 text-status-warning"
          : "bg-white/70 text-text-secondary";
  const updatedText = status?.last_annotated_at ? formatRelativeTime(status.last_annotated_at) : "尚无标注";
  const buttonText = active
    ? "标注中"
    : complete
      ? "已齐全"
      : loadedPendingCount > 0
        ? `智能标注 (${loadedPendingCount})`
        : pendingCount > 0
          ? "加载待标注素材"
          : "已齐全";

  return (
    <section className="grid gap-3 rounded-2xl border border-border/80 bg-white/60 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-accent/10 text-accent">
            <Wand2 className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-text-primary">素材标注进度</h3>
              <span className={`badge ${chipClass}`}>{label}</span>
              {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin text-text-tertiary" /> : null}
            </div>
            <p className="mt-1 truncate text-xs text-text-tertiary">VLM 结构化文字标注</p>
          </div>
        </div>
        <button className={complete ? "btn-secondary" : "btn-primary"} type="button" onClick={onStart} disabled={disabled}>
          {active ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
          <span>{buttonText}</span>
        </button>
      </div>

      <div className="grid gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-text-secondary">
          <span>总覆盖 {annotatedCount}/{totalCount} 个素材</span>
          <span className="font-mono text-text-primary">{percent}%</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-surface-hover">
          <div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${percent}%` }} />
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-4">
        <div className="flex min-w-0 items-center gap-2 rounded-xl border border-border/70 bg-white/55 px-3 py-2">
          <Database className="h-4 w-4 shrink-0 text-text-tertiary" />
          <div className="min-w-0">
            <p className="text-[11px] text-text-tertiary">已标注</p>
            <p className="truncate text-sm font-semibold text-text-primary">{annotatedCount}</p>
          </div>
        </div>
        <div className="flex min-w-0 items-center gap-2 rounded-xl border border-border/70 bg-white/55 px-3 py-2">
          <Activity className="h-4 w-4 shrink-0 text-text-tertiary" />
          <div className="min-w-0">
            <p className="text-[11px] text-text-tertiary">待标注</p>
            <p className="truncate text-sm font-semibold text-text-primary">{pendingCount}</p>
          </div>
        </div>
        <div className="flex min-w-0 items-center gap-2 rounded-xl border border-border/70 bg-white/55 px-3 py-2">
          <AlertTriangle className={`h-4 w-4 shrink-0 ${failedCount > 0 ? "text-status-error" : "text-text-tertiary"}`} />
          <div className="min-w-0">
            <p className="text-[11px] text-text-tertiary">失败</p>
            <p className={`truncate text-sm font-semibold ${failedCount > 0 ? "text-status-error" : "text-text-primary"}`}>{failedCount}</p>
          </div>
        </div>
        <div className="flex min-w-0 items-center gap-2 rounded-xl border border-border/70 bg-white/55 px-3 py-2">
          <Loader2 className={`h-4 w-4 shrink-0 ${loading || active ? "animate-spin text-status-info" : "text-text-tertiary"}`} />
          <div className="min-w-0">
            <p className="text-[11px] text-text-tertiary">更新</p>
            <p className="truncate text-sm font-semibold text-text-primary">{updatedText}</p>
          </div>
        </div>
      </div>
    </section>
  );
}

function ClipEmbeddingProgressPanel({
  status,
  job,
  loading,
  error,
  active,
  starting,
  batchSize,
  disabled,
  onStart,
}: {
  status?: ClipEmbeddingStatusVm;
  job?: ClipEmbeddingJobVm | null;
  loading: boolean;
  error: unknown;
  active: boolean;
  starting: boolean;
  batchSize: number;
  disabled: boolean;
  onStart: () => void;
}) {
  const candidateCount = status?.candidate_count ?? job?.candidate_count ?? 0;
  const indexedCount = status?.indexed_count ?? Math.max(0, candidateCount - (job?.pending_count ?? candidateCount));
  const pendingCount = status?.pending_count ?? job?.pending_count ?? 0;
  const coveragePercent = clampPercent(indexedCount, candidateCount);
  const queuedCount = job?.queued_count ?? 0;
  const processedCount = job?.processed_count ?? 0;
  const batchTotal = Math.max(queuedCount, processedCount, 0);
  const batchDone = Math.min(processedCount, batchTotal);
  const batchPercent = clampPercent(batchDone, batchTotal);
  const batchRemaining = Math.max(0, queuedCount - processedCount);
  const failedCount = job?.failed_count ?? 0;
  const hasError = Boolean(error) || job?.status === "failed";
  const complete = Boolean(status && candidateCount > 0 && pendingCount === 0 && !active && !hasError);
  const empty = Boolean(status && candidateCount === 0 && !active && !hasError);
  const label = hasError
    ? "有失败"
    : active || starting
      ? clipEmbeddingJobLabel(job?.status ?? "running")
      : complete
        ? "已齐全"
        : empty
          ? "无片段"
          : "待补齐";
  const chipClass = hasError
    ? "bg-status-error/12 text-status-error"
    : active || starting
      ? "bg-status-info/12 text-status-info"
      : complete
        ? "bg-status-success/15 text-status-success"
        : empty
          ? "bg-white/70 text-text-secondary"
          : "bg-status-warning/12 text-status-warning";
  const buttonText = starting
    ? "启动中"
    : active
      ? "嵌入中"
      : complete
        ? "已齐全"
        : `生成嵌入${pendingCount > 0 ? ` (${Math.min(pendingCount, batchSize)})` : ""}`;
  const modelText = status ? `${status.embedding_model} · ${status.embedding_dimension} 维` : "qwen3-vl-embedding · 1024 维";
  const updatedText = status?.last_indexed_at ? formatRelativeTime(status.last_indexed_at) : "尚无嵌入";

  return (
    <section className="grid gap-3 rounded-2xl border border-border/80 bg-white/60 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-accent/10 text-accent">
            <Fingerprint className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-text-primary">Clip 视频嵌入索引</h3>
              <span className={`badge ${chipClass}`}>{label}</span>
              {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin text-text-tertiary" /> : null}
            </div>
            <p className="mt-1 truncate text-xs text-text-tertiary">
              {modelText}
              {job?.job_id ? ` · ${shortId(job.job_id)}` : ""}
            </p>
          </div>
        </div>
        <button className={complete ? "btn-secondary" : "btn-primary"} type="button" onClick={onStart} disabled={disabled}>
          {starting || active ? <Loader2 className="h-4 w-4 animate-spin" /> : <Fingerprint className="h-4 w-4" />}
          <span>{buttonText}</span>
        </button>
      </div>

      <div className="grid gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-text-secondary">
          <span>总覆盖 {indexedCount}/{candidateCount} clip</span>
          <span className="font-mono text-text-primary">{coveragePercent}%</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-surface-hover">
          <div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${coveragePercent}%` }} />
        </div>
      </div>

      {active || starting || job ? (
        <div className="grid gap-2 rounded-xl border border-border/70 bg-white/55 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-text-secondary">
            <span>本批 {processedCount}/{queuedCount || batchTotal} clip</span>
            <span>{active || starting ? `剩余 ${batchRemaining} 个` : clipEmbeddingJobLabel(job?.status)}</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-surface-hover">
            <div className="h-full rounded-full bg-status-info transition-all duration-500" style={{ width: `${batchPercent}%` }} />
          </div>
        </div>
      ) : null}

      <div className="grid gap-2 sm:grid-cols-4">
        <div className="flex min-w-0 items-center gap-2 rounded-xl border border-border/70 bg-white/55 px-3 py-2">
          <Database className="h-4 w-4 shrink-0 text-text-tertiary" />
          <div className="min-w-0">
            <p className="text-[11px] text-text-tertiary">已入库</p>
            <p className="truncate text-sm font-semibold text-text-primary">{indexedCount}</p>
          </div>
        </div>
        <div className="flex min-w-0 items-center gap-2 rounded-xl border border-border/70 bg-white/55 px-3 py-2">
          <Activity className="h-4 w-4 shrink-0 text-text-tertiary" />
          <div className="min-w-0">
            <p className="text-[11px] text-text-tertiary">待补</p>
            <p className="truncate text-sm font-semibold text-text-primary">{pendingCount}</p>
          </div>
        </div>
        <div className="flex min-w-0 items-center gap-2 rounded-xl border border-border/70 bg-white/55 px-3 py-2">
          <AlertTriangle className={`h-4 w-4 shrink-0 ${failedCount > 0 || hasError ? "text-status-error" : "text-text-tertiary"}`} />
          <div className="min-w-0">
            <p className="text-[11px] text-text-tertiary">失败</p>
            <p className={`truncate text-sm font-semibold ${failedCount > 0 || hasError ? "text-status-error" : "text-text-primary"}`}>
              {failedCount}
            </p>
          </div>
        </div>
        <div className="flex min-w-0 items-center gap-2 rounded-xl border border-border/70 bg-white/55 px-3 py-2">
          <Loader2 className={`h-4 w-4 shrink-0 ${loading || active || starting ? "animate-spin text-status-info" : "text-text-tertiary"}`} />
          <div className="min-w-0">
            <p className="text-[11px] text-text-tertiary">更新</p>
            <p className="truncate text-sm font-semibold text-text-primary">{updatedText}</p>
          </div>
        </div>
      </div>
    </section>
  );
}
