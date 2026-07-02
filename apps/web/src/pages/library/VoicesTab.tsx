import { BriefcaseBusiness, Mic2, RefreshCw, Upload } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type CaseListItem, type VoiceProfile } from "../../api/client";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { SearchInput } from "../../components/ui/SearchInput";
import { useToast } from "../../components/ui/Toast";
import { VoiceCard } from "../../components/library/VoiceCard";
import { VoiceGeneratorPanel } from "../../components/library/VoiceGeneratorPanel";
import { VoiceGridSkeleton } from "../../components/library/VoiceGridSkeleton";
import { CloneVoiceModal, EditVoiceModal } from "../../components/library/VoiceModals";
import {
  vendorLabels,
  type VoiceSourceFilter,
  type VoiceVendorFilter,
} from "../../components/library/libraryModel";
import { InfiniteScrollSentinel } from "../../components/ui/InfiniteScrollSentinel";
import { EmptyState, ErrorState } from "../../components/ui/State";

export function VoicesTab() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState<VoiceSourceFilter>("all");
  const [vendorFilter, setVendorFilter] = useState<VoiceVendorFilter>("all");
  const [caseFilter, setCaseFilter] = useState("all");
  const [limit, setLimit] = useState(50);
  const [cloneOpen, setCloneOpen] = useState(false);
  const [editVoice, setEditVoice] = useState<VoiceProfile | null>(null);
  const [deleteVoice, setDeleteVoice] = useState<VoiceProfile | null>(null);
  const [previewText, setPreviewText] = useState("这是树影音色库的试听文本。");
  const [playingVoiceId, setPlayingVoiceId] = useState<string | null>(null);
  const [previewUri, setPreviewUri] = useState<string | null>(null);
  const [previewDuration, setPreviewDuration] = useState<number | null>(null);

  const voicesQuery = useQuery({
    queryKey: ["library", "voices", sourceFilter, vendorFilter, caseFilter, limit],
    queryFn: () =>
      api.voices.list({
        limit,
        source: sourceFilter === "all" ? null : sourceFilter,
        vendor: vendorFilter === "all" ? null : vendorFilter,
        case_id: caseFilter === "all" ? null : caseFilter,
      }),
  });
  const casesQuery = useQuery({
    queryKey: ["cases", "voice-bindings"],
    queryFn: () => api.cases.list({ limit: 200 }),
  });

  const voices = voicesQuery.data?.items ?? [];
  const cases = casesQuery.data?.items ?? [];
  const casesById = useMemo(() => new Map(cases.map((item) => [item.id, item])), [cases]);
  const filteredVoices = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    if (!keyword) return voices;
    return voices.filter((voice) => {
      return (
        voice.display_name.toLowerCase().includes(keyword) ||
        voice.id.toLowerCase().includes(keyword) ||
        (voice.provider_profile_id ?? "").toLowerCase().includes(keyword)
      );
    });
  }, [search, voices]);

  const hasMore = Boolean(voicesQuery.data && voices.length >= limit);

  useEffect(() => {
    setLimit(50);
  }, [search, sourceFilter, vendorFilter, caseFilter]);

  // Poll any training (Volcengine clone) voices until they flip to ready/failed.
  useEffect(() => {
    const trainingIds = voices.filter((voice) => voice.status === "training").map((voice) => voice.id);
    if (trainingIds.length === 0) return;
    const timer = window.setInterval(() => {
      void Promise.all(trainingIds.map((id) => api.voices.refreshStatus(id).catch(() => null))).then(() =>
        queryClient.invalidateQueries({ queryKey: ["library", "voices"] }),
      );
    }, 6000);
    return () => window.clearInterval(timer);
  }, [voices, queryClient]);

  const previewMutation = useMutation({
    mutationFn: (voice: VoiceProfile) => api.voices.preview(voice.id, { text: previewText.trim() || "这是试听文本。" }),
    onSuccess: (response) => {
      setPlayingVoiceId(response.voice_id);
      setPreviewUri(response.audio_artifact.uri);
      setPreviewDuration(response.duration_sec);
      toast.success("试听已生成", `时长约 ${Math.round(response.duration_sec)} 秒`);
    },
    onError: (error) => toast.error("试听生成失败", error),
  });

  const patchMutation = useMutation({
    mutationFn: ({
      voice,
      displayName,
      enabled,
      caseIds,
    }: {
      voice: VoiceProfile;
      displayName: string;
      enabled: boolean;
      caseIds: string[];
    }) => api.voices.patch(voice.id, { display_name: displayName, enabled, case_ids: caseIds }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["library", "voices"] });
      await queryClient.invalidateQueries({ queryKey: ["voices"] });
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      toast.success("音色已更新");
      setEditVoice(null);
    },
    onError: (error) => toast.error("更新失败", error),
  });

  const syncMutation = useMutation({
    mutationFn: () => api.voices.sync({}),
    onSuccess: async (response) => {
      await queryClient.invalidateQueries({ queryKey: ["library", "voices"] });
      await queryClient.invalidateQueries({ queryKey: ["voices"] });
      toast.success(
        "音色已同步",
        `新增 ${response.imported} 个 · 更新 ${response.updated} 个（共 ${response.total} 个远程音色）`,
      );
    },
    onError: (error) => toast.error("同步失败", error),
  });

  const deleteMutation = useMutation({
    mutationFn: (voiceId: string) => api.voices.delete(voiceId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["library", "voices"] });
      await queryClient.invalidateQueries({ queryKey: ["voices"] });
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      toast.success("音色已删除", "创作页将不再展示该音色");
      setDeleteVoice(null);
    },
    onError: (error) => toast.error("删除失败", error),
  });

  return (
    <section className="grid gap-4">
      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="card grid min-w-0 gap-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-xl font-semibold text-text-primary">音色库</h2>
              <p className="mt-1 text-sm text-text-secondary">搜索、试听、克隆，并按案例归纳 TTS 音色。</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                className="btn-secondary"
                type="button"
                onClick={() => syncMutation.mutate()}
                disabled={syncMutation.isPending}
                title="从已配置的 TTS 供应商账号拉取已克隆 / 设计的音色"
              >
                <RefreshCw className={`h-4 w-4 ${syncMutation.isPending ? "animate-spin" : ""}`} />
                <span>{syncMutation.isPending ? "同步中…" : "同步音色"}</span>
              </button>
              <button className="btn-primary" type="button" onClick={() => setCloneOpen(true)}>
                <Upload className="h-4 w-4" />
                <span>克隆音色</span>
              </button>
            </div>
          </div>

          <div className="flex flex-wrap gap-2 border-b border-border/60 pb-3">
            {(
              [
                ["all", "全部"],
                ["minimax", vendorLabels.minimax],
                ["volcengine", vendorLabels.volcengine],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={`rounded-full px-4 py-1.5 text-sm font-medium transition ${
                  vendorFilter === key
                    ? "bg-accent text-white shadow-glow"
                    : "bg-white/65 text-text-secondary hover:text-text-primary"
                }`}
                onClick={() => setVendorFilter(key)}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="grid min-w-0 gap-3 md:grid-cols-[minmax(180px,1fr)_minmax(150px,190px)] xl:grid-cols-[minmax(220px,1fr)_180px_minmax(220px,280px)]">
            <SearchInput
              className="min-w-0"
              value={search}
              onChange={setSearch}
              placeholder="搜索音色名称、ID 或 provider profile"
            />
            <label className="min-w-0">
              <span className="sr-only">音色类型</span>
              <select
                value={sourceFilter}
                onChange={(event) => setSourceFilter(event.target.value as VoiceSourceFilter)}
              >
                <option value="all">全部类型</option>
                <option value="builtin">系统音色</option>
                <option value="cloned">克隆音色</option>
                <option value="designed">设计音色</option>
              </select>
            </label>
            <label className="min-w-0" htmlFor="voice-case-filter">
              <span className="sr-only">案例归纳</span>
              <select id="voice-case-filter" value={caseFilter} onChange={(event) => setCaseFilter(event.target.value)}>
                <option value="all">全部案例归纳</option>
                {cases.map((item) => (
                  <option key={item.id} value={item.id}>
                    {caseLabel(item)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {voicesQuery.isLoading ? <VoiceGridSkeleton /> : null}
          {voicesQuery.error ? <ErrorState error={voicesQuery.error} /> : null}

          {!voicesQuery.isLoading && filteredVoices.length === 0 ? (
            <EmptyState
              icon={caseFilter === "all" ? Mic2 : BriefcaseBusiness}
              title="没有匹配的音色"
              detail={caseFilter === "all" ? "调整搜索词或类型筛选后重试。" : "编辑音色并绑定到当前案例后会显示在这里。"}
            />
          ) : null}

          <div className="grid min-w-0 gap-3 md:grid-cols-2 2xl:grid-cols-3">
            {filteredVoices.map((voice) => (
              <VoiceCard
                key={voice.id}
                voice={voice}
                isPreviewing={previewMutation.isPending && previewMutation.variables?.id === voice.id}
                isPlaying={playingVoiceId === voice.id}
                caseNames={(voice.case_ids ?? []).map((caseId) => casesById.get(caseId)?.name ?? caseId)}
                onPreview={() => previewMutation.mutate(voice)}
                onEdit={() => setEditVoice(voice)}
                onDelete={() => setDeleteVoice(voice)}
              />
            ))}
          </div>

          <InfiniteScrollSentinel
            enabled={hasMore && !voicesQuery.isFetching}
            onVisible={() => setLimit((current) => current + 50)}
            label={`继续加载音色（已显示 ${filteredVoices.length} 个）`}
          />
        </div>

        <VoiceGeneratorPanel
          voices={voices}
          selectedVoiceId={playingVoiceId ?? voices[0]?.id ?? ""}
          previewText={previewText}
          previewUri={previewUri}
          previewDuration={previewDuration}
          isPreviewing={previewMutation.isPending}
          onTextChange={setPreviewText}
          onPreview={(voice) => previewMutation.mutate(voice)}
        />
      </div>

      <CloneVoiceModal isOpen={cloneOpen} onClose={() => setCloneOpen(false)} cases={cases} />
      {editVoice ? (
        <EditVoiceModal
          voice={editVoice}
          isOpen={Boolean(editVoice)}
          isLoading={patchMutation.isPending}
          cases={cases}
          onClose={() => setEditVoice(null)}
          onSubmit={(displayName, enabled, caseIds) =>
            patchMutation.mutate({ voice: editVoice, displayName, enabled, caseIds })
          }
        />
      ) : null}
      <ConfirmDialog
        isOpen={Boolean(deleteVoice)}
        onClose={() => setDeleteVoice(null)}
        title="删除音色"
        message={deleteVoice ? `确认删除「${deleteVoice.display_name}」吗？` : ""}
        consequences={[
          "删除后创作页无法再选择该音色。",
          "已创建的历史任务不会被修改，但后续复用该 voice_id 会失败。",
          "系统音色删除需要管理员权限，失败时会保留原音色。",
        ]}
        confirmText="删除音色"
        type="danger"
        isLoading={deleteMutation.isPending}
        onConfirm={() => {
          if (deleteVoice) deleteMutation.mutate(deleteVoice.id);
        }}
      />
    </section>
  );
}

function caseLabel(item: CaseListItem) {
  return item.industry ? `${item.name} · ${item.industry}` : item.name;
}
