import { Eye, Music4, PauseCircle, Play, Trash2 } from "lucide-react";
import type { MaterialUsageRankingItem, MediaAssetRecord } from "../../api/client";
import { formatRelativeTime, shortId } from "../../lib/format";
import { annotationStatusLabels, annotationTone } from "./libraryModel";

type BgmAssetCardProps = {
  asset: MediaAssetRecord;
  usage?: MaterialUsageRankingItem;
  isPlaying: boolean;
  domId?: string;
  highlighted?: boolean;
  onPlay: () => void;
  onAnnotation: () => void;
};

export function BgmAssetCard({ asset, usage, isPlaying, domId, highlighted, onPlay, onAnnotation }: BgmAssetCardProps) {
  return (
    <article
      id={domId}
      className={`rounded-[24px] border bg-white/65 p-4 shadow-glow transition-all ${
        highlighted ? "border-accent ring-2 ring-accent/60" : "border-border/80"
      }`}
    >
      <div className="flex aspect-video items-center justify-center rounded-2xl bg-[linear-gradient(135deg,rgba(94,109,81,0.14),rgba(214,255,72,0.16))] text-accent">
        {isPlaying ? <PauseCircle className="h-10 w-10" /> : <Music4 className="h-10 w-10" />}
      </div>
      <div className="mt-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold text-text-primary">{asset.title}</h3>
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
        {(asset.tags ?? []).map((tag) => (
          <span key={tag} className="badge bg-surface-hover text-text-secondary">
            {tag}
          </span>
        ))}
      </div>
      <div className="mt-4 grid grid-cols-4 gap-2">
        <button className="icon-button col-span-2 w-full" type="button" onClick={onPlay} title={isPlaying ? "暂停试听" : "在线试听"}>
          {isPlaying ? <PauseCircle className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          <span className="text-xs">{isPlaying ? "播放中" : "试听"}</span>
        </button>
        <button className="icon-button w-full" type="button" onClick={onAnnotation} title="查看标注">
          <Eye className="h-4 w-4" />
        </button>
        <button className="icon-button w-full" type="button" disabled title="后端暂无素材删除 API">
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
    </article>
  );
}
