import { Clock, Download, Eye, Loader2, Maximize2, Play, RefreshCw, Trash2 } from "lucide-react";
import type { MaterialUsageRankingItem, MediaAssetCard } from "../../api/client";
import { formatDuration, formatRelativeTime, shortId } from "../../lib/format";
import { annotationStatusLabels, annotationTone, readAssetDurationSec } from "./libraryModel";
import { readCardThumbnailUrl } from "./libraryInteractionModel";

type TemplateAssetCardProps = {
  card: MediaAssetCard;
  previewUrl: string | null;
  batchMode: boolean;
  selected: boolean;
  isAnalyzing: boolean;
  isDownloading: boolean;
  isPreviewLoading: boolean;
  usage?: MaterialUsageRankingItem;
  domId?: string;
  highlighted?: boolean;
  onToggleSelected: () => void;
  onPreview: () => void;
  onAnalyze: () => void;
  onOpenAnnotation: () => void;
  onDownload: () => void;
  onDelete: () => void;
};

export function TemplateAssetCard({
  card,
  previewUrl,
  batchMode,
  selected,
  isAnalyzing,
  isDownloading,
  isPreviewLoading,
  usage,
  domId,
  highlighted,
  onToggleSelected,
  onPreview,
  onAnalyze,
  onOpenAnnotation,
  onDownload,
  onDelete,
}: TemplateAssetCardProps) {
  const asset = card.asset;
  const thumbnailUrl = readCardThumbnailUrl(card);
  const durationSec = readAssetDurationSec(asset);
  return (
    <article
      id={domId}
      className={`group rounded-[24px] border bg-white/65 p-3 shadow-glow transition-all hover:-translate-y-0.5 ${
        highlighted
          ? "border-accent ring-2 ring-accent/60"
          : selected
            ? "border-accent/40"
            : "border-border/80 hover:border-accent/25"
      }`}
    >
      <div className="relative overflow-hidden rounded-2xl bg-[#151913]">
        {batchMode ? (
          <label className="absolute left-2 top-2 z-10 flex h-8 w-8 items-center justify-center rounded-xl bg-white/90">
            <input type="checkbox" checked={selected} onChange={onToggleSelected} aria-label="选择素材" />
          </label>
        ) : null}
        {thumbnailUrl ? (
          <button type="button" onClick={onPreview} className="relative flex aspect-video w-full items-center justify-center" aria-label="放大预览">
            <img src={thumbnailUrl} alt={asset.title} className="aspect-video w-full object-cover opacity-90 transition-opacity group-hover:opacity-100" />
            <span className="absolute grid h-12 w-12 place-items-center rounded-full bg-black/45 text-white/90">
              <Play className="h-6 w-6 translate-x-0.5" />
            </span>
          </button>
        ) : previewUrl ? (
          <video
            src={previewUrl}
            muted
            loop
            playsInline
            preload="metadata"
            className="aspect-video w-full object-cover opacity-90 transition-opacity group-hover:opacity-100"
            onMouseEnter={(event) => void event.currentTarget.play().catch(() => undefined)}
            onMouseLeave={(event) => event.currentTarget.pause()}
          />
        ) : (
          <button type="button" onClick={onPreview} className="flex aspect-video w-full items-center justify-center text-white/75" aria-label="放大预览">
            <Play className="h-9 w-9" />
          </button>
        )}
        {durationSec !== undefined ? (
          <span className="absolute left-2 bottom-2 inline-flex items-center gap-1 rounded-full bg-black/70 px-2 py-1 text-[11px] font-medium text-white">
            <Clock className="h-3 w-3" />
            {formatDuration(durationSec)}
          </span>
        ) : null}
        <button
          type="button"
          onClick={onPreview}
          disabled={isPreviewLoading}
          className="absolute bottom-2 right-2 inline-flex items-center gap-1 rounded-full bg-black/70 px-2 py-1 text-xs text-white transition-colors hover:bg-black/85 disabled:opacity-70"
          title="放大预览"
        >
          {isPreviewLoading ? <Loader2 className="h-3 w-3 animate-spin" /> : <Maximize2 className="h-3 w-3" />}
          <span>{isPreviewLoading ? "加载中" : "预览"}</span>
        </button>
      </div>
      <div className="mt-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-text-primary">{asset.title}</h3>
          <p className="mt-1 font-mono text-xs text-text-tertiary">{shortId(asset.id, 12)}</p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <span className={`badge ${annotationTone(asset.annotation_status)}`}>
            {annotationStatusLabels[asset.annotation_status]}
          </span>
          {usage && usage.task_use_count > 0 ? (
            <span className="badge bg-accent/10 text-accent" title={`最近 ${formatRelativeTime(usage.last_used_at)}`}>
              使用 {usage.task_use_count}
            </span>
          ) : null}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {(asset.tags ?? []).slice(0, 4).map((tag) => (
          <span key={tag} className="badge bg-surface-hover text-text-secondary">
            {tag}
          </span>
        ))}
      </div>
      <div className="mt-4 grid grid-cols-4 gap-2">
        <button
          className="icon-button w-full"
          type="button"
          onClick={onAnalyze}
          disabled={isAnalyzing}
          title={isAnalyzing ? "处理中…" : "重新标注并重建嵌入"}
          aria-label={isAnalyzing ? "处理中" : "重新标注并重建嵌入"}
        >
          {isAnalyzing ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </button>
        <button className="icon-button w-full" type="button" onClick={onOpenAnnotation} title="查看标注" aria-label="查看标注">
          <Eye className="h-4 w-4" />
        </button>
        <button
          className="icon-button w-full"
          type="button"
          onClick={onDownload}
          disabled={isDownloading}
          title={isDownloading ? "准备下载…" : previewUrl ? "下载原视频" : "获取下载地址并下载"}
          aria-label={isDownloading ? "准备下载" : "下载原视频"}
        >
          {isDownloading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
        </button>
        <button
          className="icon-button w-full text-status-error hover:border-status-error/40 hover:bg-status-error/10"
          type="button"
          onClick={onDelete}
          title="删除素材"
          aria-label="删除素材"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
    </article>
  );
}
