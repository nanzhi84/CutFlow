import { Type, Upload } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type MediaAssetRecord } from "../../api/client";
import { FontAssetCard } from "../../components/library/FontAssetCard";
import { FontDetailModal } from "../../components/library/FontDetailModal";
import { LibraryAssetUploadModal } from "../../components/library/LibraryAssetUploadModal";
import { TemplateGridSkeleton } from "../../components/library/TemplateGridSkeleton";
import { UsageRankingPanel } from "../../components/library/UsageRankingPanel";
import { collectUsefulTags } from "../../components/library/libraryModel";
import { toDisplayUrl } from "../../lib/url";
import { SearchInput } from "../../components/ui/SearchInput";
import { useToast } from "../../components/ui/Toast";
import { EmptyState, ErrorState } from "../../components/ui/State";

export function FontsTab() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("all");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [detailAsset, setDetailAsset] = useState<MediaAssetRecord | null>(null);
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const [highlightAssetId, setHighlightAssetId] = useState<string | null>(null);

  const fontsQuery = useQuery({
    queryKey: ["library", "media", "font"],
    queryFn: () => api.mediaAssets.list({ limit: 200, kind: "font" }),
  });
  const usageQuery = useQuery({
    queryKey: ["library", "usage-ranking", "font"],
    queryFn: () => api.mediaAssets.usageRanking("font", { top_n: 20 }),
  });

  const items = fontsQuery.data?.items ?? [];
  const usageByAssetId = useMemo(
    () => new Map((usageQuery.data?.items ?? []).map((item) => [item.asset_id, item])),
    [usageQuery.data],
  );
  const categories = useMemo(() => collectUsefulTags(items, ["font", "upload"]), [items]);
  const filteredItems = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    return items.filter((card) => {
      const asset = card.asset;
      const matchesKeyword =
        !keyword ||
        asset.title.toLowerCase().includes(keyword) ||
        asset.id.toLowerCase().includes(keyword) ||
        (asset.tags ?? []).some((tag) => tag.toLowerCase().includes(keyword));
      const matchesCategory = category === "all" || (asset.tags ?? []).includes(category);
      return matchesKeyword && matchesCategory;
    });
  }, [category, items, search]);

  async function ensurePreview(asset: MediaAssetRecord) {
    if (previewUrls[asset.id]) return previewUrls[asset.id];
    try {
      const response = await api.mediaAssets.previewUrl(asset.id);
      const displayUrl = toDisplayUrl(response.url);
      if (!displayUrl) {
        toast.info("字体预览暂不可用（待真实媒体接入）");
        return null;
      }
      setPreviewUrls((current) => ({ ...current, [asset.id]: displayUrl }));
      return displayUrl;
    } catch (error) {
      toast.error("字体预览加载失败", error);
      return null;
    }
  }

  useEffect(() => {
    if (!items.length) return;
    let cancelled = false;
    const missingAssets = items.map((card) => card.asset).filter((asset) => !previewUrls[asset.id]);
    if (!missingAssets.length) return;

    void Promise.all(
      missingAssets.map(async (asset) => {
        try {
          const response = await api.mediaAssets.previewUrl(asset.id);
          const displayUrl = toDisplayUrl(response.url);
          return displayUrl ? ([asset.id, displayUrl] as const) : null;
        } catch {
          return null;
        }
      }),
    ).then((entries) => {
      if (cancelled) return;
      const resolvedEntries = entries.filter((entry): entry is readonly [string, string] => entry !== null);
      if (!resolvedEntries.length) return;
      setPreviewUrls((current) => {
        let changed = false;
        const next = { ...current };
        resolvedEntries.forEach(([assetId, url]) => {
          if (!next[assetId]) {
            next[assetId] = url;
            changed = true;
          }
        });
        return changed ? next : current;
      });
    });

    return () => {
      cancelled = true;
    };
  }, [items, previewUrls]);

  function jumpToAsset(assetId: string) {
    if (!items.some((card) => card.asset.id === assetId)) {
      toast.info("该素材不在当前列表");
      return;
    }
    setSearch("");
    setCategory("all");
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
            <h2 className="text-xl font-semibold text-text-primary">字体库</h2>
            <p className="mt-1 text-sm text-text-secondary">上传字体文件并实时预览字幕样式。</p>
          </div>
          <button className="btn-primary" type="button" onClick={() => setUploadOpen(true)}>
            <Upload className="h-4 w-4" />
            <span>上传字体</span>
          </button>
        </div>

        <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_220px]">
          <SearchInput value={search} onChange={setSearch} placeholder="搜索字体名称、ID 或标签" />
          <select value={category} onChange={(event) => setCategory(event.target.value)}>
            <option value="all">全部分类</option>
            {categories.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </div>

        {fontsQuery.isLoading ? <TemplateGridSkeleton /> : null}
        {fontsQuery.error ? <ErrorState error={fontsQuery.error} /> : null}

        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {filteredItems.map((card) => (
            <FontAssetCard
              key={card.asset.id}
              domId={`asset-${card.asset.id}`}
              highlighted={highlightAssetId === card.asset.id}
              asset={card.asset}
              usage={usageByAssetId.get(card.asset.id)}
              previewUrl={previewUrls[card.asset.id] ?? null}
              onLoadPreview={() => void ensurePreview(card.asset)}
              onDetail={async () => {
                await ensurePreview(card.asset);
                setDetailAsset(card.asset);
              }}
            />
          ))}
        </div>

        {!fontsQuery.isLoading && filteredItems.length === 0 ? (
          <EmptyState icon={Type} title="暂无字体素材" detail="上传 ttf、otf、woff 或 woff2 后可预览。" />
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
        kind="font"
        onUploaded={() => queryClient.invalidateQueries({ queryKey: ["library", "media", "font"] })}
      />
      <FontDetailModal asset={detailAsset} previewUrl={detailAsset ? previewUrls[detailAsset.id] ?? null : null} onClose={() => setDetailAsset(null)} />
    </section>
  );
}
