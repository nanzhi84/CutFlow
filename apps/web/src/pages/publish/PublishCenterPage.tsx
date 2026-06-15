import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RadioTower } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api, type FinishedVideo, type PublishBatch, type PublishBatchItem, type PublishPackage } from "../../api/client";
import { EmptyState, LoadingState } from "../../components/ui/State";
import { DraftEditorStep } from "../../components/publish/DraftEditorStep";
import { PublishReviewStep } from "../../components/publish/PublishReviewStep";
import { RecentBatchesSidebar } from "../../components/publish/RecentBatchesSidebar";
import { SourceStep } from "../../components/publish/SourceStep";
import {
  PUBLISH_STEPS,
  type BatchDefaults,
  type PublishDraft,
  type SourcePoolItem,
  buildDraftFromItem,
  buildDraftsFromBatch,
  defaultBatchDefaults,
  isBatchActive,
  itemPatchFromDraft,
} from "../../components/publish/publishModel";
import { StudioTabs } from "../../components/StudioTabs";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { FlowStepper } from "../../components/ui/FlowStepper";
import { useToast } from "../../components/ui/Toast";
import { usePageVisible } from "../../hooks/usePageVisible";
type ConfirmState = {
  title: string;
  message: string;
  consequences: string[];
  confirmText: string;
  type?: "danger" | "warning" | "info";
  onConfirm: () => void | Promise<void>;
};
export default function PublishCenterPage() {
  const { caseId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeBatchId = searchParams.get("batchId") || "";
  const queryClient = useQueryClient();
  const toast = useToast();
  const pageVisible = usePageVisible();
  const selectedCaseId = caseId || null;
  const [activeStep, setActiveStep] = useState(activeBatchId ? 1 : 0);
  const [sourcePool, setSourcePool] = useState<SourcePoolItem[]>([]);
  const [defaults, setDefaults] = useState<BatchDefaults>(defaultBatchDefaults);
  const [drafts, setDrafts] = useState<Record<string, PublishDraft>>({});
  const [activeItemId, setActiveItemId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState | null>(null);
  const videosQuery = useQuery({
    queryKey: ["publish-center", "finished-videos", selectedCaseId],
    queryFn: () => api.finishedVideos.list(selectedCaseId ?? ""),
    enabled: Boolean(selectedCaseId),
  });
  const batchesQuery = useQuery({
    queryKey: ["publish-center", "batches", selectedCaseId],
    queryFn: () => api.publishing.batches({ limit: 80, case_id: selectedCaseId }),
    enabled: Boolean(selectedCaseId),
    refetchInterval: pageVisible ? 8_000 : false,
  });
  const batchQuery = useQuery({
    queryKey: ["publish-center", "batch", activeBatchId],
    queryFn: () => api.publishing.batch(activeBatchId),
    enabled: Boolean(activeBatchId),
    refetchInterval: (query) => (pageVisible ? (isBatchActive(query.state.data as PublishBatch | undefined) ? 3_000 : 8_000) : false),
  });
  const packagesQuery = useQuery({
    queryKey: ["publish-center", "packages"],
    queryFn: () => api.publishing.packages({ limit: 200 }),
    refetchInterval: pageVisible ? 12_000 : false,
  });

  const attemptsQuery = useQuery({
    queryKey: ["publish-center", "attempts", activeBatchId],
    queryFn: () => api.publishing.attempts(activeBatchId, { limit: 80 }),
    enabled: Boolean(activeBatchId),
    refetchInterval: pageVisible ? 8_000 : false,
  });

  const batch = batchQuery.data;
  const batchItemIds = useMemo(() => (batch?.items ?? []).map((item) => item.id).join("|"), [batch?.items]);
  const packagesById = useMemo(() => {
    const lookup = new Map<string, PublishPackage>();
    packagesQuery.data?.items.forEach((item) => lookup.set(item.id, item));
    return lookup;
  }, [packagesQuery.data?.items]);

  useEffect(() => {
    if (!batch) return;
    const serverDrafts = buildDraftsFromBatch(batch);
    setDrafts((current) => {
      const next: Record<string, PublishDraft> = {};
      Object.keys(serverDrafts).forEach((itemId) => {
        next[itemId] = current[itemId] ?? serverDrafts[itemId];
      });
      return next;
    });
    setActiveItemId((current) => (batch.items?.some((item) => item.id === current) ? current : (batch.items?.[0]?.id ?? null)));
    setActiveStep((step) => (step === 0 ? 1 : step));
  }, [batch, batchItemIds]);

  function routeToBatch(id: string) {
    setSearchParams({ batchId: id });
    setActiveStep(1);
  }

  function resetToSource() {
    setSearchParams({});
    setActiveStep(0);
    setDrafts({});
    setActiveItemId(null);
  }

  function addFinished(video: FinishedVideo) {
    const id = `finished:${video.id}`;
    setSourcePool((current) => (current.some((item) => item.id === id) ? current : [...current, { id, type: "finished", title: video.title, video }]));
  }

  function addUpload(file: File, publishPackage: PublishPackage) {
    const id = `upload:${publishPackage.id}`;
    setSourcePool((current) => (current.some((item) => item.id === id) ? current : [...current, { id, type: "upload", title: file.name, file, package: publishPackage }]));
    void queryClient.invalidateQueries({ queryKey: ["publish-center", "packages"] });
  }

  const createBatch = useMutation({
    mutationFn: async () => {
      const packageIds: string[] = [];
      for (const source of sourcePool) {
        if (source.type === "upload") {
          if (!source.package) throw new Error("外部视频缺少发布包，请重新上传。");
          packageIds.push(source.package.id);
        } else {
          const created = await api.publishing.createPackage({
            source_finished_video_id: source.video.id,
            title: source.video.title,
            description: "",
          });
          packageIds.push(created.id);
        }
      }
      return api.publishing.createBatch({ publish_package_ids: packageIds, platform_targets: defaults.platforms });
    },
    onSuccess: async (created) => {
      setSourcePool([]);
      toast.success("批次已创建", `进入编辑：${created.id.slice(0, 8)}`);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["publish-center", "batches"] }),
        queryClient.invalidateQueries({ queryKey: ["publish-center", "packages"] }),
      ]);
      routeToBatch(created.id);
    },
    onError: (error) => toast.error("批次创建失败", error),
  });

  const saveItem = useMutation({
    mutationFn: (item: PublishBatchItem) => {
      const draft = drafts[item.id] ?? buildDraftFromItem(item);
      return api.publishing.patchItem(item.id, itemPatchFromDraft(draft));
    },
    onSuccess: async () => {
      toast.success("草稿已保存");
      await queryClient.invalidateQueries({ queryKey: ["publish-center", "batch", activeBatchId] });
    },
    onError: (error) => toast.error("草稿保存失败", error),
  });

  const deleteItem = useMutation({
    mutationFn: (itemId: string) => api.publishing.deleteItem(itemId),
    onSuccess: async () => {
      toast.success("条目已删除", "只移除当前批次条目，源成片和上传文件仍保留。");
      await queryClient.invalidateQueries({ queryKey: ["publish-center", "batch", activeBatchId] });
    },
    onError: (error) => toast.error("删除条目失败", error),
  });

  const patchCover = useMutation({
    mutationFn: ({ packageId, artifactId }: { packageId: string; artifactId: string | null }) =>
      api.publishing.patchPackage(packageId, { cover_artifact_id: artifactId }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["publish-center", "packages"] });
    },
    onError: (error) => toast.error("封面更新失败", error),
  });

  const deleteBatch = useMutation({
    mutationFn: (id: string) => api.publishing.deleteBatch(id),
    onSuccess: async (_, id) => {
      toast.success("批次已删除");
      if (id === activeBatchId) resetToSource();
      await queryClient.invalidateQueries({ queryKey: ["publish-center", "batches"] });
    },
    onError: (error) => toast.error("删除批次失败", error),
  });

  async function syncDrafts() {
    for (const item of batch?.items ?? []) {
      const draft = drafts[item.id] ?? buildDraftFromItem(item);
      await api.publishing.patchItem(item.id, itemPatchFromDraft(draft));
    }
  }

  const submitBatch = useMutation({
    mutationFn: async (mode: "manual" | "auto") => {
      if (!batch) throw new Error("缺少发布批次");
      await syncDrafts();
      // Resolve schedule from the batch's first selected draft (defaults carry it
      // for the whole batch). Asia/Shanghai validation happens server-side.
      const scheduled = (batch.items ?? [])
        .map((item) => drafts[item.id] ?? buildDraftFromItem(item))
        .find((draft) => draft.scheduleMode === "scheduled" && draft.scheduledAt);
      const scheduleFields =
        scheduled && scheduled.scheduledAt
          ? { mode: "scheduled" as const, scheduled_at: new Date(scheduled.scheduledAt).toISOString() }
          : { mode: "immediate" as const };
      return api.publishing.submitBatch(batch.id, {
        dry_run: mode === "manual",
        simulate_publish_failure: false,
        ...scheduleFields,
      });
    },
    onSuccess: async (_, mode) => {
      toast.success(mode === "manual" ? "半自动准备已完成" : "全自动发布已完成", "结果已写入 PublishAttempt。");
      setActiveStep(2);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["publish-center", "batch", activeBatchId] }),
        queryClient.invalidateQueries({ queryKey: ["publish-center", "attempts", activeBatchId] }),
      ]);
    },
    onError: (error) => toast.error("发布提交失败", error),
  });

  const retryItem = useMutation({
    mutationFn: (itemId: string) => api.publishing.retryItem(activeBatchId, itemId),
    onSuccess: async () => {
      toast.success("失败条目已重试");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["publish-center", "batch", activeBatchId] }),
        queryClient.invalidateQueries({ queryKey: ["publish-center", "attempts", activeBatchId] }),
      ]);
    },
    onError: (error) => toast.error("重试失败", error),
  });

  function updateDraft(itemId: string, patch: Partial<PublishDraft>) {
    setDrafts((current) => {
      const item = batch?.items?.find((entry) => entry.id === itemId);
      const base = current[itemId] ?? (item ? buildDraftFromItem(item) : null);
      if (!base) return current;
      return { ...current, [itemId]: { ...base, ...patch } };
    });
  }

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>发布</h1>
          <p className="mt-2 text-sm text-text-secondary">选来源、编辑文案与封面，生成内部发布结果。</p>
        </div>
      </header>
      <StudioTabs caseId={caseId} />
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-status-warning/25 bg-status-warning/10 px-4 py-3 text-sm text-status-warning">
        <span className="inline-flex items-center gap-2">
          <RadioTower className="h-4 w-4" />
          小V猫真实发布需在发布主机启用 CUTAGENT_PUBLISH_ADAPTER 适配器（未对真实平台验证）。
        </span>
        <span>当前默认走沙盒适配器：发布文案、封面（截帧回退）与定时/标签/地点均已生效。</span>
      </div>
      <FlowStepper steps={PUBLISH_STEPS} activeStep={activeStep} ariaLabel="发布流程" onStepClick={(step) => (step === 0 || batch ? setActiveStep(step) : undefined)} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_310px]">
        <div>
          {activeStep === 0 ? (
            <SourceStep
              embedded={true}
              cases={[]}
              selectedCaseId={selectedCaseId}
              onCaseChange={() => undefined}
              videos={videosQuery.data?.items ?? []}
              isVideosLoading={videosQuery.isLoading}
              pool={sourcePool}
              defaults={defaults}
              onDefaultsChange={setDefaults}
              onAddFinished={addFinished}
              onAddUpload={addUpload}
              onRemove={(itemId) => setSourcePool((current) => current.filter((item) => item.id !== itemId))}
              onClear={() => setSourcePool([])}
              onCreateBatch={() => createBatch.mutate()}
              isCreating={createBatch.isPending}
            />
          ) : null}

          {activeStep === 1 && batch ? (
            <DraftEditorStep
              batch={batch}
              packagesById={packagesById}
              drafts={drafts}
              defaults={defaults}
              activeItemId={activeItemId}
              isSavingItem={saveItem.isPending}
              onDefaultsChange={setDefaults}
              onDraftChange={updateDraft}
              onResetDraft={(item) => updateDraft(item.id, buildDraftFromItem(item))}
              onSaveItem={(item) => saveItem.mutate(item)}
              onDeleteItem={(item) =>
                setConfirm({
                  title: "删除发布条目",
                  message: `确认从当前批次删除「${item.title}」吗？`,
                  consequences: ["只会移除此批次里的平台草稿。", "不会删除源成片、上传文件或其他批次。", "删除后需要重新创建批次条目才能发布该平台。"],
                  confirmText: "删除条目",
                  type: "danger",
                  onConfirm: () => deleteItem.mutate(item.id),
                })
              }
              onActiveItemChange={setActiveItemId}
              onCoverArtifact={async (packageId, artifactId) => {
                await patchCover.mutateAsync({ packageId, artifactId });
              }}
              onNext={() => setActiveStep(2)}
            />
          ) : null}

          {activeStep > 0 && !batch ? (
            <div className="card">
              {batchQuery.isLoading ? (
                <LoadingState block label="加载发布批次" />
              ) : (
                <EmptyState title="请选择或创建发布批次" detail="在来源步骤选择成片并生成批次后继续。" />
              )}
            </div>
          ) : null}

          {activeStep === 2 && batch ? (
            <PublishReviewStep
              batch={batch}
              drafts={drafts}
              attempts={attemptsQuery.data?.items ?? []}
              isSubmitting={submitBatch.isPending}
              isRetrying={retryItem.isPending}
              onDraftChange={updateDraft}
              onSubmit={(mode) =>
                setConfirm({
                  title: mode === "manual" ? "半自动发布" : "全自动发布",
                  message: mode === "manual" ? "确认生成待人工发布结果吗？" : "确认执行全自动发布吗？",
                  consequences: ["会先保存当前草稿标题、正文和选中状态。", "发布仅生成内部发布记录，不会触达真实外部平台。", "发布结果会写入 PublishAttempt 和发布记录。"],
                  confirmText: mode === "manual" ? "半自动发布" : "全自动发布",
                  type: "warning",
                  onConfirm: () => submitBatch.mutate(mode),
                })
              }
              onRetry={(itemId) => retryItem.mutate(itemId)}
              onBack={() => setActiveStep(1)}
            />
          ) : null}
        </div>
        <RecentBatchesSidebar
          batches={batchesQuery.data?.items ?? []}
          activeBatchId={activeBatchId}
          isLoading={batchesQuery.isLoading}
          onSelect={routeToBatch}
          onNew={resetToSource}
          onDelete={(item) =>
            setConfirm({
              title: "删除发布批次",
              message: `确认删除批次 ${item.id.slice(0, 8)} 吗？`,
              consequences: ["会移除该批次和批次内条目。", "已生成的源成片、上传文件和发布包不会被删除。", "删除后最近批次列表不再显示此批次。"],
              confirmText: "删除批次",
              type: "danger",
              onConfirm: () => deleteBatch.mutate(item.id),
            })
          }
        />
      </div>

      <ConfirmDialog
        isOpen={Boolean(confirm)}
        onClose={() => setConfirm(null)}
        title={confirm?.title ?? ""}
        message={confirm?.message ?? ""}
        consequences={confirm?.consequences ?? []}
        confirmText={confirm?.confirmText}
        type={confirm?.type}
        isLoading={deleteBatch.isPending || deleteItem.isPending || submitBatch.isPending}
        onConfirm={async () => {
          await confirm?.onConfirm();
          setConfirm(null);
        }}
      />
    </section>
  );
}
