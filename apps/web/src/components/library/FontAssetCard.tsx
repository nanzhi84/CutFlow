import { Eye, Info, Trash2, Type } from "lucide-react";
import type { MaterialUsageRankingItem, MediaAssetRecord } from "../../api/client";
import { formatRelativeTime, shortId } from "../../lib/format";
import { annotationStatusLabels, annotationTone, fontFamilyName } from "./libraryModel";
import { FontFaceStyle } from "./FontFaceStyle";

type FontAssetCardProps = {
  asset: MediaAssetRecord;
  usage?: MaterialUsageRankingItem;
  previewUrl: string | null;
  domId?: string;
  highlighted?: boolean;
  onLoadPreview: () => void;
  onDetail: () => void;
};

export function FontAssetCard({ asset, usage, previewUrl, domId, highlighted, onLoadPreview, onDetail }: FontAssetCardProps) {
  const family = fontFamilyName(asset.id);
  return (
    <article
      id={domId}
      className={`rounded-[24px] border bg-white/65 p-4 shadow-glow transition-all ${
        highlighted ? "border-accent ring-2 ring-accent/60" : "border-border/80"
      }`}
    >
      {previewUrl ? <FontFaceStyle assetId={asset.id} url={previewUrl} /> : null}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <span className={`badge ${annotationTone(asset.annotation_status)}`}>{annotationStatusLabels[asset.annotation_status]}</span>
          {usage && usage.task_use_count > 0 ? (
            <span className="mt-2 inline-flex badge bg-accent/10 text-accent" title={`最近 ${formatRelativeTime(usage.last_used_at)}`}>
              使用 {usage.task_use_count}
            </span>
          ) : null}
          <h3 className="mt-3 truncate text-lg font-semibold text-text-primary">{asset.title}</h3>
          <p className="mt-1 font-mono text-xs text-text-tertiary">{shortId(asset.id, 12)}</p>
        </div>
        <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-accent/10 text-accent">
          <Type className="h-5 w-5" />
        </span>
      </div>
      <div className="mt-4 rounded-2xl border border-border/70 bg-surface p-4">
        <p className="text-2xl leading-snug text-text-primary" style={previewUrl ? { fontFamily: family } : undefined}>
          树影字幕 Aa 123
        </p>
        <p className="mt-2 text-xs text-text-secondary">实时字体预览</p>
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {(asset.tags ?? []).map((tag) => (
          <span key={tag} className="badge bg-surface-hover text-text-secondary">
            {tag}
          </span>
        ))}
      </div>
      <div className="mt-4 grid grid-cols-3 gap-2">
        <button className="icon-button w-full" type="button" onClick={onLoadPreview} title="加载预览">
          <Eye className="h-4 w-4" />
        </button>
        <button className="icon-button w-full" type="button" onClick={onDetail} title="字体详情">
          <Info className="h-4 w-4" />
        </button>
        <button className="icon-button w-full" type="button" disabled title="暂不支持删除素材">
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
    </article>
  );
}
