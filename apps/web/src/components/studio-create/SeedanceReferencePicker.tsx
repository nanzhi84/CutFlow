import { useQuery } from "@tanstack/react-query";
import { Check, ImageOff } from "lucide-react";
import { api } from "../../api/client";
import { readCardThumbnailUrl } from "../library/libraryInteractionModel";

// Still-image asset kinds usable as a Seedance reference image. (Video/audio
// references are a later extension; v1 feeds reference_image only.)
const IMAGE_KINDS = new Set(["image", "portrait", "cover_template"]);

export function SeedanceReferencePicker({
  caseId,
  selectedIds,
  onChange,
}: {
  caseId: string;
  selectedIds: string[];
  onChange: (ids: string[]) => void;
}) {
  const assets = useQuery({
    queryKey: ["media-assets", "seedance-ref", caseId],
    queryFn: () => api.mediaAssets.list({ case_id: caseId, limit: 100 }),
    enabled: Boolean(caseId),
  });
  const cards = (assets.data?.items ?? []).filter((card) => IMAGE_KINDS.has(card.asset.kind));

  function toggle(assetId: string) {
    onChange(
      selectedIds.includes(assetId)
        ? selectedIds.filter((id) => id !== assetId)
        : [...selectedIds, assetId],
    );
  }

  return (
    <div className="grid gap-3 border-y border-border/60 py-4">
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-text-primary">参考图素材</span>
        <span className="text-xs text-text-tertiary">已选 {selectedIds.length} 张</span>
      </div>
      <p className="text-xs text-text-secondary">
        选择门头、产品、人物等图片，Seedance 会按参考图保持画面一致性。至少选一张。
      </p>
      {assets.isLoading ? (
        <div className="stateBox muted">
          <span>正在加载素材…</span>
        </div>
      ) : cards.length === 0 ? (
        <div className="stateBox muted flex items-center gap-2">
          <ImageOff className="h-4 w-4 shrink-0" />
          <span>该案例下暂无图片素材，请先到「素材库」上传图片后再来选择。</span>
        </div>
      ) : (
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-4">
          {cards.map((card) => {
            const assetId = card.asset.id;
            const selected = selectedIds.includes(assetId);
            const thumb = readCardThumbnailUrl(card);
            return (
              <button
                type="button"
                key={assetId}
                onClick={() => toggle(assetId)}
                aria-pressed={selected}
                className={`relative aspect-square overflow-hidden rounded-xl border-2 transition-colors ${
                  selected ? "border-accent" : "border-border/60 hover:border-accent/50"
                }`}
              >
                {thumb ? (
                  <img src={thumb} alt={card.asset.kind} className="h-full w-full object-cover" />
                ) : (
                  <span className="flex h-full w-full items-center justify-center bg-surface-hover text-xs text-text-tertiary">
                    {card.asset.kind}
                  </span>
                )}
                {selected ? (
                  <span className="absolute right-1 top-1 flex h-5 w-5 items-center justify-center rounded-full bg-accent text-white">
                    <Check className="h-3.5 w-3.5" />
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
