import { Mic2, Upload, Wand2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type VoiceProfile } from "../../api/client";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { SearchInput } from "../../components/ui/SearchInput";
import { useToast } from "../../components/ui/Toast";
import { VoiceCard } from "../../components/library/VoiceCard";
import { VoiceGeneratorPanel } from "../../components/library/VoiceGeneratorPanel";
import { VoiceGridSkeleton } from "../../components/library/VoiceGridSkeleton";
import { CloneVoiceModal, DesignVoiceModal, EditVoiceModal } from "../../components/library/VoiceModals";
import { type VoiceSourceFilter } from "../../components/library/libraryModel";
import { InfiniteScrollSentinel } from "../../components/ui/InfiniteScrollSentinel";

export function VoicesTab() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState<VoiceSourceFilter>("all");
  const [limit, setLimit] = useState(50);
  const [cloneOpen, setCloneOpen] = useState(false);
  const [designOpen, setDesignOpen] = useState(false);
  const [editVoice, setEditVoice] = useState<VoiceProfile | null>(null);
  const [deleteVoice, setDeleteVoice] = useState<VoiceProfile | null>(null);
  const [previewText, setPreviewText] = useState("这是树影音色库的试听文本。");
  const [playingVoiceId, setPlayingVoiceId] = useState<string | null>(null);
  const [previewUri, setPreviewUri] = useState<string | null>(null);
  const [previewDuration, setPreviewDuration] = useState<number | null>(null);

  const voicesQuery = useQuery({
    queryKey: ["library", "voices", sourceFilter, limit],
    queryFn: () =>
      api.voices.list({
        limit,
        source: sourceFilter === "all" ? null : sourceFilter,
      }),
  });

  const voices = voicesQuery.data?.items ?? [];
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
  }, [search, sourceFilter]);

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
    mutationFn: ({ voice, displayName, enabled }: { voice: VoiceProfile; displayName: string; enabled: boolean }) =>
      api.voices.patch(voice.id, { display_name: displayName, enabled }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["library", "voices"] });
      await queryClient.invalidateQueries({ queryKey: ["voices"] });
      toast.success("音色已更新");
      setEditVoice(null);
    },
    onError: (error) => toast.error("更新失败", error),
  });

  const deleteMutation = useMutation({
    mutationFn: (voiceId: string) => api.voices.delete(voiceId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["library", "voices"] });
      await queryClient.invalidateQueries({ queryKey: ["voices"] });
      toast.success("音色已删除", "创作页将不再展示该音色");
      setDeleteVoice(null);
    },
    onError: (error) => toast.error("删除失败", error),
  });

  return (
    <section className="grid gap-4">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="card grid gap-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-xl font-semibold text-text-primary">音色库</h2>
              <p className="mt-1 text-sm text-text-secondary">搜索、试听、克隆和设计 TTS 音色。</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button className="btn-secondary" type="button" onClick={() => setCloneOpen(true)}>
                <Upload className="h-4 w-4" />
                <span>克隆音色</span>
              </button>
              <button className="btn-primary" type="button" onClick={() => setDesignOpen(true)}>
                <Wand2 className="h-4 w-4" />
                <span>设计音色</span>
              </button>
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_220px]">
            <SearchInput value={search} onChange={setSearch} placeholder="搜索音色名称、ID 或 provider profile" />
            <select value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value as VoiceSourceFilter)}>
              <option value="all">全部类型</option>
              <option value="builtin">系统音色</option>
              <option value="cloned">克隆音色</option>
              <option value="designed">设计音色</option>
            </select>
          </div>

          {voicesQuery.isLoading ? <VoiceGridSkeleton /> : null}
          {voicesQuery.error ? (
            <p className="rounded-2xl border border-status-error/30 bg-status-error/10 p-4 text-sm text-status-error">
              音色加载失败：{String(voicesQuery.error)}
            </p>
          ) : null}

          {!voicesQuery.isLoading && filteredVoices.length === 0 ? (
            <div className="rounded-[24px] border border-dashed border-border bg-white/55 p-8 text-center">
              <Mic2 className="mx-auto h-8 w-8 text-text-tertiary" />
              <p className="mt-3 text-sm font-medium text-text-primary">没有匹配的音色</p>
              <p className="mt-1 text-xs text-text-secondary">调整搜索词或类型筛选后重试。</p>
            </div>
          ) : null}

          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {filteredVoices.map((voice) => (
              <VoiceCard
                key={voice.id}
                voice={voice}
                isPreviewing={previewMutation.isPending && previewMutation.variables?.id === voice.id}
                isPlaying={playingVoiceId === voice.id}
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

      <CloneVoiceModal isOpen={cloneOpen} onClose={() => setCloneOpen(false)} />
      <DesignVoiceModal isOpen={designOpen} onClose={() => setDesignOpen(false)} />
      {editVoice ? (
        <EditVoiceModal
          voice={editVoice}
          isOpen={Boolean(editVoice)}
          isLoading={patchMutation.isPending}
          onClose={() => setEditVoice(null)}
          onSubmit={(displayName, enabled) => patchMutation.mutate({ voice: editVoice, displayName, enabled })}
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
