import { CheckCircle2, Film, FolderUp, Loader2, RefreshCw, Tag, Trash2, Upload, Video } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type MediaAssetRecord, type UploadKind } from "../../api/client";
import { AnnotationEditorModal } from "../../components/annotation/AnnotationEditorModal";
import { TemplateAssetCard } from "../../components/library/TemplateAssetCard";
import { TemplateGridSkeleton } from "../../components/library/TemplateGridSkeleton";
import { UploadPlaceholderCard } from "../../components/library/UploadPlaceholderCard";
import { templateKindLabels, type TemplateKind, type UploadPlaceholder, uploadStageLabel, toDisplayUrl } from "../../components/library/libraryModel";
import { DropZone } from "../../components/ui/DropZone";
import { Modal } from "../../components/ui/Modal";
import { SearchInput } from "../../components/ui/SearchInput";
import { useToast } from "../../components/ui/Toast";
import { InfiniteScrollSentinel } from "../../components/ui/InfiniteScrollSentinel";
import { usePageVisible } from "../../hooks/usePageVisible";
import { useUpload } from "../../hooks/useUpload";
import { shortId } from "../../lib/format";

export function TemplatesTab() {
  const toast = useToast();
  const pageVisible = usePageVisible();
  const queryClient = useQueryClient();
  const [caseSearch, setCaseSearch] = useState("");
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [kind, setKind] = useState<TemplateKind>("portrait");
  const [assetLimit, setAssetLimit] = useState(50);
  const [assetSearch, setAssetSearch] = useState("");
  const [sceneFilter, setSceneFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState<"all" | MediaAssetRecord["annotation_status"]>("all");
  const [batchMode, setBatchMode] = useState(false);
  const [selectedAssetIds, setSelectedAssetIds] = useState<string[]>([]);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [annotationAssetId, setAnnotationAssetId] = useState<string | null>(null);
  const [placeholders, setPlaceholders] = useState<UploadPlaceholder[]>([]);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});

  const casesQuery = useQuery({
    queryKey: ["library", "cases", caseSearch],
    queryFn: () => api.cases.list({ limit: 80, search: caseSearch.trim() || null }),
  });

  const cases = casesQuery.data?.items ?? [];

  useEffect(() => {
    if (!selectedCaseId && cases[0]?.id) setSelectedCaseId(cases[0].id);
  }, [cases, selectedCaseId]);
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

  const activeQuery = kind === "portrait" ? portraitQuery : brollQuery;
  const activeItems = activeQuery.data?.items ?? [];
  const hasMoreAssets = Boolean(activeQuery.data && activeItems.length >= assetLimit);
  const selectedCase = cases.find((item) => item.id === selectedCaseId) ?? null;
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
      toast.success("分析任务已提交", response.run_id ? `运行 ID：${shortId(response.run_id)}` : "沙箱环境已完成标注状态更新");
    },
    onError: (error) => toast.error("分析失败", error),
  });

  async function ensurePreview(assetId: string) {
    if (previewUrls[assetId]) return previewUrls[assetId];
    try {
      const response = await api.mediaAssets.previewUrl(assetId);
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

  return (
    <section className="grid gap-4 xl:grid-cols-[290px_minmax(0,1fr)]">
      <aside className="card grid content-start gap-4">
        <div>
          <h2 className="text-lg font-semibold text-text-primary">案例</h2>
          <p className="mt-1 text-sm text-text-secondary">模板与 B-roll 按案例归档。</p>
        </div>
        <SearchInput value={caseSearch} onChange={setCaseSearch} placeholder="搜索案例" />
        <div className="grid max-h-[620px] gap-2 overflow-y-auto pr-1">
          {casesQuery.isLoading ? <p className="text-sm text-text-secondary">案例加载中...</p> : null}
          {cases.map((item) => (
            <button
              key={item.id}
              className={`rounded-2xl border p-3 text-left transition-all ${
                selectedCaseId === item.id ? "border-accent/25 bg-accent/10 text-accent" : "border-border/75 bg-white/55 text-text-primary hover:bg-white/80"
              }`}
              type="button"
              onClick={() => {
                setSelectedCaseId(item.id);
                setSelectedAssetIds([]);
              }}
            >
              <span className="block truncate text-sm font-semibold">{item.name}</span>
              <span className="mt-1 block truncate text-xs text-text-secondary">
                {item.owner_user_id ? `负责人 ${shortId(item.owner_user_id)}` : `${item.active_memory_count} 条记忆`}
              </span>
            </button>
          ))}
          {!casesQuery.isLoading && cases.length === 0 ? <p className="text-sm text-text-secondary">暂无案例。</p> : null}
        </div>
      </aside>

      <div className="card grid gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-xl font-semibold text-text-primary">{selectedCase?.name ?? "选择案例"}</h2>
            <p className="mt-1 text-sm text-text-secondary">人像模板与 B-roll 共用上传与标注流程。</p>
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

        {batchMode ? <BatchActionBar selectedCount={selectedAssetIds.length} onClear={() => setSelectedAssetIds([])} /> : null}

        {activeQuery.isLoading ? <TemplateGridSkeleton /> : null}
        {activeQuery.error ? (
          <p className="rounded-2xl border border-status-error/30 bg-status-error/10 p-4 text-sm text-status-error">
            素材加载失败：{String(activeQuery.error)}
          </p>
        ) : null}

        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {visiblePlaceholders.map((item) => (
            <UploadPlaceholderCard key={item.id} item={item} />
          ))}
          {filteredItems.map((card) => (
            <TemplateAssetCard
              key={card.asset.id}
              card={card}
              previewUrl={toDisplayUrl(previewUrls[card.asset.id] ?? card.preview_url)}
              batchMode={batchMode}
              selected={selectedAssetIds.includes(card.asset.id)}
              isAnalyzing={rerunMutation.isPending && rerunMutation.variables === card.asset.id}
              onToggleSelected={() =>
                setSelectedAssetIds((current) =>
                  current.includes(card.asset.id) ? current.filter((id) => id !== card.asset.id) : [...current, card.asset.id],
                )
              }
              onPreview={() => void ensurePreview(card.asset.id)}
              onAnalyze={() => rerunMutation.mutate(card.asset.id)}
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
          <div className="rounded-[24px] border border-dashed border-border bg-white/55 p-8 text-center">
            <Video className="mx-auto h-8 w-8 text-text-tertiary" />
            <p className="mt-3 text-sm font-medium text-text-primary">暂无{templateKindLabels[kind]}</p>
            <p className="mt-1 text-xs text-text-secondary">上传素材后会进入标注队列。</p>
          </div>
        ) : null}
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
      />
      <AnnotationEditorModal assetId={annotationAssetId} caseId={selectedCaseId} onClose={() => setAnnotationAssetId(null)} />
    </section>
  );
}

function BatchActionBar({ selectedCount, onClear }: { selectedCount: number; onClear: () => void }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border/80 bg-white/65 p-3">
      <span className="text-sm text-text-secondary">已选择 {selectedCount} 个素材</span>
      <div className="flex flex-wrap gap-2">
        <button className="btn-secondary min-h-9 px-3" type="button" disabled title="待接入（依赖 M6b/M6d）">
          <RefreshCw className="h-4 w-4" />
          <span>批量分析</span>
        </button>
        <button className="btn-secondary min-h-9 px-3" type="button" disabled title="待接入（依赖 M6b/M6d）">
          <Tag className="h-4 w-4" />
          <span>改场景/标签</span>
        </button>
        <button className="btn-secondary min-h-9 px-3" type="button" disabled title="后端暂无素材删除 API">
          <Trash2 className="h-4 w-4" />
          <span>批量删除</span>
        </button>
        <button className="btn-ghost min-h-9 px-3" type="button" onClick={onClear}>
          清空选择
        </button>
      </div>
    </div>
  );
}

type TemplateUploadModalProps = {
  isOpen: boolean;
  onClose: () => void;
  caseId: string | null;
  kind: TemplateKind;
  onPlaceholder: (placeholder: UploadPlaceholder) => void;
  onSuccess: (placeholderId: string) => Promise<void>;
};

function TemplateUploadModal({ isOpen, onClose, caseId, kind, onPlaceholder, onSuccess }: TemplateUploadModalProps) {
  const toast = useToast();
  const upload = useUpload();
  const [files, setFiles] = useState<File[]>([]);
  const [scene, setScene] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const accept = kind === "portrait" ? ".mp4,.mov,.m4v,.webm" : ".mp4,.mov,.m4v,.webm,.avi,.mkv";

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!caseId) {
      setError("请先选择案例");
      return;
    }
    if (files.length === 0) {
      setError("请上传至少一个文件");
      return;
    }
    setError(null);
    setIsSubmitting(true);
    for (const file of files) {
      const placeholderId = `${file.name}_${file.lastModified}_${Math.random().toString(16).slice(2)}`;
      onPlaceholder({ id: placeholderId, name: file.name, kind, status: "uploading", progress: 30 });
      try {
        await upload.uploadFile({
          file,
          kind: kind as UploadKind,
          caseId,
          metadata: {
            title: file.name,
            scene: scene.trim(),
          },
        });
        onPlaceholder({ id: placeholderId, name: file.name, kind, status: "uploading", progress: 100 });
        await onSuccess(placeholderId);
      } catch (err) {
        const message = err instanceof Error ? err.message : "上传失败";
        onPlaceholder({ id: placeholderId, name: file.name, kind, status: "failed", progress: 100, error: message });
      }
    }
    setIsSubmitting(false);
    toast.success("上传处理完成", "成功素材会进入当前案例网格，失败卡片会保留错误。");
    setFiles([]);
    upload.reset();
    onClose();
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={`上传${templateKindLabels[kind]}`} size="lg">
      <form className="grid gap-4" onSubmit={handleSubmit}>
        <DropZone accept={accept} maxSize={500} multiple onFilesDrop={(nextFiles) => setFiles(nextFiles)} label={`上传${templateKindLabels[kind]}文件`} />
        <label>
          <span>统一场景标签</span>
          <input value={scene} onChange={(event) => setScene(event.target.value)} placeholder="例如：办公室、产品特写、生活方式" />
        </label>
        <div className="rounded-2xl border border-status-info/20 bg-status-info/10 p-3 text-xs leading-5 text-status-info">
          批量文件夹解析依赖浏览器目录能力和 M6b 素材处理增强；当前支持多文件批量上传，全部走 UploadSession。
        </div>
        {upload.status !== "idle" ? (
          <div className="rounded-2xl border border-border/80 bg-white/65 p-3">
            <div className="flex items-center justify-between gap-3 text-sm text-text-secondary">
              <span>当前文件：{uploadStageLabel(upload.status)}</span>
              <span>{upload.progress}%</span>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded-full bg-border/70">
              <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${upload.progress}%` }} />
            </div>
          </div>
        ) : null}
        {error ? <p className="text-sm text-status-error">{error}</p> : null}
        <div className="flex justify-end gap-3 border-t border-border/70 pt-4">
          <button className="btn-secondary" type="button" onClick={onClose} disabled={isSubmitting}>
            取消
          </button>
          <button className="btn-primary" type="submit" disabled={isSubmitting || files.length === 0 || !caseId}>
            {isSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            <span>{isSubmitting ? "上传中" : "开始上传"}</span>
          </button>
        </div>
      </form>
    </Modal>
  );
}
