import { ArrowLeft, ArrowRight, CheckCircle2, Film, FolderUp, Loader2, Video, Wand2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type MediaAssetRecord, type UploadKind } from "../../api/client";
import { AnnotationEditorModal } from "../../components/annotation/AnnotationEditorModal";
import { TemplateBatchActionBar } from "../../components/library/TemplateBatchActionBar";
import { TemplateAssetCard } from "../../components/library/TemplateAssetCard";
import { TemplateGridSkeleton } from "../../components/library/TemplateGridSkeleton";
import { TemplateUploadModal } from "../../components/library/TemplateUploadModal";
import { UploadPlaceholderCard } from "../../components/library/UploadPlaceholderCard";
import { VideoPreviewModal } from "../../components/library/VideoPreviewModal";
import { UsageRankingPanel } from "../../components/library/UsageRankingPanel";
import {
  templateKindLabels,
  type TemplateKind,
  type UploadPlaceholder,
  readPreviewUrlMeta,
} from "../../components/library/libraryModel";
import { toDisplayUrl } from "../../lib/url";
import { SearchInput } from "../../components/ui/SearchInput";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { useToast } from "../../components/ui/Toast";
import { InfiniteScrollSentinel } from "../../components/ui/InfiniteScrollSentinel";
import { EmptyState, ErrorState, LoadingState } from "../../components/ui/State";
import { usePageVisible } from "../../hooks/usePageVisible";
import { useUpload } from "../../hooks/useUpload";
import { formatRelativeTime, shortId } from "../../lib/format";

export function TemplatesTab() {
  const toast = useToast();
  const pageVisible = usePageVisible();
  const queryClient = useQueryClient();
  const replaceUpload = useUpload();
  const replaceInputRef = useRef<HTMLInputElement>(null);
  const [caseSearch, setCaseSearch] = useState("");
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [kind, setKind] = useState<TemplateKind>("portrait");
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
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  // Asset highlighted (ring) after a usage-ranking click jumped to it.
  const [highlightAssetId, setHighlightAssetId] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [annotationAssetId, setAnnotationAssetId] = useState<string | null>(null);
  const [replaceTargetAssetId, setReplaceTargetAssetId] = useState<string | null>(null);
  const [placeholders, setPlaceholders] = useState<UploadPlaceholder[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  // Per-asset playability flag from the preview-url response (true/false; absent => unknown).
  const [previewPlayable, setPreviewPlayable] = useState<Record<string, boolean>>({});
  const [previewAssetId, setPreviewAssetId] = useState<string | null>(null);
  const [previewLoadingId, setPreviewLoadingId] = useState<string | null>(null);

  const casesQuery = useQuery({
    queryKey: ["library", "cases", caseSearch],
    queryFn: () => api.cases.list({ limit: 80, search: caseSearch.trim() || null }),
  });

  const cases = casesQuery.data?.items ?? [];

  // No auto-select: the case grid is the preface page; the materials view only
  // renders after the user picks a case.
  useEffect(() => {
    setAssetLimit(50);
  }, [kind, selectedCaseId]);

  const portraitQuery = useQuery({
    queryKey: ["library", "media", selectedCaseId, "portrait", assetLimit],
    queryFn: () => api.mediaAssets.list({ limit: assetLimit, case_id: selectedCaseId, kind: "portrait" }),
    enabled: Boolean(selectedCaseId),
    refetchInterval: pageVisible ? 10_000 : false,
  });

  const brollQuery = useQuery({
    queryKey: ["library", "media", selectedCaseId, "broll", assetLimit],
    queryFn: () => api.mediaAssets.list({ limit: assetLimit, case_id: selectedCaseId, kind: "broll" }),
    enabled: Boolean(selectedCaseId),
    refetchInterval: pageVisible ? 10_000 : false,
  });

  const usageQuery = useQuery({
    queryKey: ["library", "usage-ranking", selectedCaseId, kind],
    queryFn: () => api.mediaAssets.usageRanking(kind, { case_id: selectedCaseId, top_n: 20 }),
    enabled: Boolean(selectedCaseId),
    refetchInterval: pageVisible ? 10_000 : false,
  });

  const activeQuery = kind === "portrait" ? portraitQuery : brollQuery;
  const activeItems = activeQuery.data?.items ?? [];
  const hasMoreAssets = Boolean(activeQuery.data && activeItems.length >= assetLimit);
  const selectedCase = cases.find((item) => item.id === selectedCaseId) ?? null;
  const usageByAssetId = useMemo(
    () => new Map((usageQuery.data?.items ?? []).map((item) => [item.asset_id, item])),
    [usageQuery.data],
  );
  const previewCard = useMemo(() => {
    if (!previewAssetId) return null;
    const pool = [...(portraitQuery.data?.items ?? []), ...(brollQuery.data?.items ?? [])];
    return pool.find((card) => card.asset.id === previewAssetId) ?? null;
  }, [previewAssetId, portraitQuery.data, brollQuery.data]);
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

  const visiblePlaceholders = placeholders.filter((item) => item.kind === kind);

  const rerunMutation = useMutation({
    mutationFn: (assetId: string) => api.annotations.rerun(assetId, { force: false }),
    onSuccess: async (response) => {
      await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
      toast.success("分析任务已提交", response.run_id ? `运行 ID：${shortId(response.run_id)}` : "已更新标注状态");
    },
    onError: (error) => toast.error("分析失败", error),
  });

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

  // Batch annotation (force=false): VLM-analyzes the selected assets, skipping
  // any that are already annotated so it never re-bills annotated material.
  const annotateMutation = useMutation({
    mutationFn: (assetIds: string[]) =>
      api.annotations.batch({ schema_version: "annotation_batch_request.v1", asset_ids: assetIds, force: false }),
    onSuccess: async (response) => {
      await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
      toast.success(
        "批量标注已提交",
        `新标注 ${response.completed_count} 个 · 跳过 ${response.skipped_count} 个已标注 · 失败 ${response.failed_count} 个`,
      );
      setSelectedAssetIds([]);
      setAnnotateTargetIds(null);
    },
    onError: (error) => {
      toast.error("批量标注失败", error);
      setAnnotateTargetIds(null);
    },
  });

  // All loaded assets in the current view that are not yet annotated — the
  // 智能标注 target and the count force=false will actually bill the VLM for.
  const unannotatedAssetIds = useMemo(
    () => activeItems.filter((card) => card.asset.annotation_status !== "annotated").map((card) => card.asset.id),
    [activeItems],
  );
  // Of the ids queued for the confirm dialog, how many are unannotated (the ones
  // that will really hit the VLM; already-annotated ones are skipped server-side).
  const annotateTargetUnannotatedCount = useMemo(
    () =>
      activeItems.filter(
        (card) => annotateTargetIds?.includes(card.asset.id) && card.asset.annotation_status !== "annotated",
      ).length,
    [activeItems, annotateTargetIds],
  );

  // Batch delete: the backend exposes per-asset DELETE, so fan out one call per
  // selected asset.
  const deleteMutation = useMutation({
    mutationFn: async (assetIds: string[]) => {
      await Promise.all(assetIds.map((id) => api.mediaAssets.delete(id)));
      return assetIds.length;
    },
    onSuccess: async (count) => {
      await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
      await queryClient.invalidateQueries({ queryKey: ["library", "usage-ranking", selectedCaseId] });
      toast.success("批量删除完成", `已删除 ${count} 个素材`);
      setSelectedAssetIds([]);
      setDeleteConfirmOpen(false);
    },
    onError: (error) => {
      toast.error("批量删除失败", error);
      setDeleteConfirmOpen(false);
    },
  });

  // Jump from a usage-ranking item to its asset card: clear filters that might
  // hide it, scroll it into view, and flash a highlight ring.
  function jumpToAsset(assetId: string) {
    if (!activeItems.some((card) => card.asset.id === assetId)) {
      toast.info("该素材不在当前列表", "可能属于另一个标签页或尚未加载。");
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

  const replaceMutation = useMutation({
    mutationFn: async ({ assetId, file }: { assetId: string; file: File }) => {
      const result = await replaceUpload.uploadFile({
        file,
        kind: kind as UploadKind,
        caseId: selectedCaseId,
        metadata: { title: file.name, template_mode: "replace" },
      });
      return api.mediaAssets.replaceSource(assetId, { upload_session_id: result.upload_session.id });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
      toast.success("素材原视频已替换", "标注和卡片位置已保留。");
    },
    onError: (error) => toast.error("替换失败", error),
    onSettled: () => {
      setReplaceTargetAssetId(null);
      if (replaceInputRef.current) replaceInputRef.current.value = "";
      replaceUpload.reset();
    },
  });

  async function autoReplaceUploads(uploadSessionIds: string[]) {
    const response = await api.mediaAssets.autoMatchReplace({
      case_id: selectedCaseId,
      kind,
      upload_session_ids: uploadSessionIds,
    });
    await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
    const matched = response.results.filter((item) => item.status === "matched").length;
    const pending = response.results.length - matched;
    if (pending > 0) {
      toast.warning("自动匹配替换完成", `已替换 ${matched} 个，${pending} 个需手动处理。`);
    } else {
      toast.success("自动匹配替换完成", `已替换 ${matched} 个素材。`);
    }
  }

  function openReplacePicker(assetId: string) {
    setReplaceTargetAssetId(assetId);
    replaceInputRef.current?.click();
  }

  function handleReplaceFile(file: File | undefined) {
    if (!file || !replaceTargetAssetId) return;
    replaceMutation.mutate({ assetId: replaceTargetAssetId, file });
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

  function setPlaceholder(update: UploadPlaceholder) {
    setPlaceholders((current) => {
      const exists = current.some((item) => item.id === update.id);
      return exists ? current.map((item) => (item.id === update.id ? update : item)) : [update, ...current];
    });
  }

  function clearSuccessfulPlaceholder(id: string) {
    window.setTimeout(() => {
      setPlaceholders((current) => current.filter((item) => item.id !== id));
    }, 900);
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
              <p className="mt-1 text-sm text-text-secondary">人像模板与 B-roll 共用上传与标注流程。</p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              className="btn-secondary"
              type="button"
              disabled={!selectedCaseId || unannotatedAssetIds.length === 0 || annotateMutation.isPending}
              onClick={() => setAnnotateTargetIds(unannotatedAssetIds)}
              title="自动选择未标注的素材并发起 VLM 标注"
            >
              {annotateMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
              <span>智能标注{unannotatedAssetIds.length > 0 ? ` (${unannotatedAssetIds.length})` : ""}</span>
            </button>
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

        <div className="tabs">
          {(["portrait", "broll"] as TemplateKind[]).map((item) => (
            <button key={item} className={`tabLink ${kind === item ? "active" : ""}`} type="button" onClick={() => setKind(item)}>
              {item === "portrait" ? <Video className="h-4 w-4" /> : <Film className="h-4 w-4" />}
              <span>{templateKindLabels[item]}</span>
              <span className="badge bg-white/70 text-text-secondary">
                {item === "portrait" ? (portraitQuery.data?.items.length ?? 0) : (brollQuery.data?.items.length ?? 0)}
              </span>
            </button>
          ))}
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
            isAnnotating={annotateMutation.isPending}
            isDeleting={deleteMutation.isPending}
            onSelectAll={() => setSelectedAssetIds(filteredItems.map((card) => card.asset.id))}
            onStabilize={() => stabilizeMutation.mutate(selectedAssetIds)}
            onAnnotate={() => setAnnotateTargetIds(selectedAssetIds)}
            onDelete={() => setDeleteConfirmOpen(true)}
            onClear={() => setSelectedAssetIds([])}
          />
        ) : null}

        {activeQuery.isLoading ? <TemplateGridSkeleton /> : null}
        {activeQuery.error ? <ErrorState error={activeQuery.error} /> : null}

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
              isAnalyzing={rerunMutation.isPending && rerunMutation.variables === card.asset.id}
              isReplacing={replaceMutation.isPending && replaceMutation.variables?.assetId === card.asset.id}
              isPreviewLoading={previewLoadingId === card.asset.id}
              usage={usageByAssetId.get(card.asset.id)}
              onToggleSelected={() =>
                setSelectedAssetIds((current) =>
                  current.includes(card.asset.id) ? current.filter((id) => id !== card.asset.id) : [...current, card.asset.id],
                )
              }
              onPreview={() => void openPreview(card.asset.id)}
              onAnalyze={() => rerunMutation.mutate(card.asset.id)}
              onReplaceSource={() => openReplacePicker(card.asset.id)}
              onOpenAnnotation={() => setAnnotationAssetId(card.asset.id)}
            />
          ))}
        </div>
        <InfiniteScrollSentinel
          enabled={hasMoreAssets && !activeQuery.isFetching}
          onVisible={() => setAssetLimit((current) => current + 50)}
          label={`继续加载${templateKindLabels[kind]}`}
        />

        {!activeQuery.isLoading && visiblePlaceholders.length === 0 && filteredItems.length === 0 ? (
          <EmptyState icon={Video} title={`暂无${templateKindLabels[kind]}`} detail="上传素材后会进入标注队列。" />
        ) : null}
      </div>

      <div className="xl:sticky xl:top-4 xl:self-start">
        <UsageRankingPanel
          report={usageQuery.data}
          isLoading={usageQuery.isLoading}
          error={usageQuery.error}
          onItemClick={jumpToAsset}
        />
      </div>

      <TemplateUploadModal
        isOpen={uploadOpen}
        onClose={() => setUploadOpen(false)}
        caseId={selectedCaseId}
        kind={kind}
        onPlaceholder={setPlaceholder}
        onSuccess={async (placeholderId) => {
          clearSuccessfulPlaceholder(placeholderId);
          await queryClient.invalidateQueries({ queryKey: ["library", "media", selectedCaseId] });
        }}
        onAutoReplace={autoReplaceUploads}
      />
      <input
        ref={replaceInputRef}
        className="hidden"
        type="file"
        accept=".mp4,.mov,.m4v,.webm,.avi,.mkv"
        onChange={(event) => handleReplaceFile(event.currentTarget.files?.[0])}
      />
      <ConfirmDialog
        isOpen={annotateTargetIds !== null}
        onClose={() => setAnnotateTargetIds(null)}
        onConfirm={() => annotateMutation.mutate(annotateTargetIds ?? [])}
        title="批量标注素材"
        message={`将对 ${annotateTargetIds?.length ?? 0} 个素材中未标注的部分调用 VLM 视觉模型标注；已标注的会自动跳过。`}
        consequences={[
          `预计约 ${annotateTargetUnannotatedCount} 个素材会真实调用 VLM（产生费用）`,
          "已标注素材会被跳过，不重复计费",
          "标注为异步任务，状态会在素材卡片上更新",
        ]}
        confirmText="开始标注"
        type="warning"
        isLoading={annotateMutation.isPending}
      />
      <ConfirmDialog
        isOpen={deleteConfirmOpen}
        onClose={() => setDeleteConfirmOpen(false)}
        onConfirm={() => deleteMutation.mutate(selectedAssetIds)}
        title="批量删除素材"
        message={`确定删除选中的 ${selectedAssetIds.length} 个素材吗？`}
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
