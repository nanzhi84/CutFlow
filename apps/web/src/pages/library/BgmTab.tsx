import { Headphones, ListMusic, Upload } from "lucide-react";
import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type MediaAssetRecord } from "../../api/client";
import { AnnotationEditorModal } from "../../components/annotation/AnnotationEditorModal";
import { BgmAssetCard } from "../../components/library/BgmAssetCard";
import { LibraryAssetUploadModal } from "../../components/library/LibraryAssetUploadModal";
import { TemplateGridSkeleton } from "../../components/library/TemplateGridSkeleton";
import { UsageRankingPanel } from "../../components/library/UsageRankingPanel";
import { collectUsefulTags } from "../../components/library/libraryModel";
import { toDisplayUrl } from "../../lib/url";
import { SearchInput } from "../../components/ui/SearchInput";
import { useToast } from "../../components/ui/Toast";
import { InfiniteScrollSentinel } from "../../components/ui/InfiniteScrollSentinel";
import { EmptyState, ErrorState } from "../../components/ui/State";
import { shortId } from "../../lib/format";

export function BgmTab() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [styleFilter, setStyleFilter] = useState("all");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [annotationAssetId, setAnnotationAssetId] = useState<string | null>(null);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [playing, setPlaying] = useState<{ asset: MediaAssetRecord; url: string } | null>(null);
  const [limit, setLimit] = useState(50);
  const [highlightAssetId, setHighlightAssetId] = useState<string | null>(null);

  const bgmQuery = useQuery({
    queryKey: ["library", "media", "bgm", limit],
    queryFn: () => api.mediaAssets.list({ limit, kind: "bgm" }),
  });
  const usageQuery = useQuery({
    queryKey: ["library", "usage-ranking", "bgm"],
    queryFn: () => api.mediaAssets.usageRanking("bgm", { top_n: 20 }),
  });

  const items = bgmQuery.data?.items ?? [];
  const hasMore = Boolean(bgmQuery.data && items.length >= limit);
  const usageByAssetId = useMemo(
    () => new Map((usageQuery.data?.items ?? []).map((item) => [item.asset_id, item])),
    [usageQuery.data],
  );
  const styles = useMemo(() => collectUsefulTags(items, ["bgm", "upload"]), [items]);
  const filteredItems = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    return items.filter((card) => {
      const asset = card.asset;
      const matchesKeyword =
        !keyword ||
        asset.title.toLowerCase().includes(keyword) ||
        asset.id.toLowerCase().includes(keyword) ||
        (asset.tags ?? []).some((tag) => tag.toLowerCase().includes(keyword));
      const matchesStyle = styleFilter === "all" || (asset.tags ?? []).includes(styleFilter);
      return matchesKeyword && matchesStyle;
    });
  }, [items, search, styleFilter]);

  async function ensurePreview(asset: MediaAssetRecord) {
    if (previewUrls[asset.id]) return previewUrls[asset.id];
    try {
      const response = await api.mediaAssets.previewUrl(asset.id);
      const displayUrl = toDisplayUrl(response.url);
      if (!displayUrl) {
        toast.info("BGM 预览暂不可用（待真实媒体接入）");
        return null;
      }
      setPreviewUrls((current) => ({ ...current, [asset.id]: displayUrl }));
      return displayUrl;
    } catch (error) {
      toast.error("BGM 预览加载失败", error);
      return null;
    }
  }

  async function handlePlay(asset: MediaAssetRecord) {
    const url = await ensurePreview(asset);
    if (url) setPlaying({ asset, url });
  }

  function jumpToAsset(assetId: string) {
    if (!items.some((card) => card.asset.id === assetId)) {
      toast.info("该素材不在当前列表");
      return;
    }
    setSearch("");
    setStyleFilter("all");
    setHighlightAssetId(assetId);
    window.setTimeout(() => {
      document.getElementById(`asset-${assetId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 60);
    window.setTimeout(() => setHighlightAssetId((current) => (current === assetId ? null : current)), 2600);
  }

  return (
    <section className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
      <div className="card grid content-start gap-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-xl font-semibold text-text-primary">BGM 库</h2>
            <p className="mt-1 text-sm text-text-secondary">管理配乐、在线试听并查看标注状态。</p>
          </div>
          <button className="btn-primary" type="button" onClick={() => setUploadOpen(true)}>
            <Upload className="h-4 w-4" />
            <span>上传 BGM</span>
          </button>
        </div>

        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_220px]">
          <SearchInput value={search} onChange={setSearch} placeholder="搜索 BGM 名称、ID 或风格" />
          <select value={styleFilter} onChange={(event) => setStyleFilter(event.target.value)}>
            <option value="all">全部风格</option>
            {styles.map((style) => (
              <option key={style} value={style}>
                {style}
              </option>
            ))}
          </select>
        </div>

        {bgmQuery.isLoading ? <TemplateGridSkeleton /> : null}
        {bgmQuery.error ? <ErrorState error={bgmQuery.error} /> : null}

        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {filteredItems.map((card) => (
            <BgmAssetCard
              key={card.asset.id}
              domId={`asset-${card.asset.id}`}
              highlighted={highlightAssetId === card.asset.id}
              asset={card.asset}
              usage={usageByAssetId.get(card.asset.id)}
              isPlaying={playing?.asset.id === card.asset.id}
              onPlay={() => void handlePlay(card.asset)}
              onAnnotation={() => setAnnotationAssetId(card.asset.id)}
            />
          ))}
        </div>

        {!bgmQuery.isLoading && filteredItems.length === 0 ? (
          <EmptyState icon={Headphones} title="暂无 BGM 素材" detail="上传音频后可在线试听并进入标注流程。" />
        ) : null}

        <InfiniteScrollSentinel
          enabled={hasMore && !bgmQuery.isFetching}
          onVisible={() => setLimit((current) => current + 50)}
          label={`继续加载 BGM（已显示 ${filteredItems.length} 条）`}
        />

        {playing ? (
        <div className="sticky bottom-4 z-20 rounded-[24px] border border-border/80 bg-white/92 p-4 shadow-glow backdrop-blur-xl">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-3">
              <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-accent/10 text-accent">
                <ListMusic className="h-5 w-5" />
              </span>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-text-primary">{playing.asset.title}</p>
                <p className="font-mono text-xs text-text-tertiary">{shortId(playing.asset.id, 12)}</p>
              </div>
            </div>
            <audio src={playing.url} controls autoPlay className="min-w-[260px] flex-1" />
          </div>
        </div>
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

      <LibraryAssetUploadModal
        isOpen={uploadOpen}
        onClose={() => setUploadOpen(false)}
        kind="bgm"
        onUploaded={() => queryClient.invalidateQueries({ queryKey: ["library", "media", "bgm"] })}
      />
      <AnnotationEditorModal assetId={annotationAssetId} caseId={null} onClose={() => setAnnotationAssetId(null)} />
    </section>
  );
}
