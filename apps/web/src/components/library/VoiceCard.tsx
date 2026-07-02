import { BriefcaseBusiness, CheckCircle2, Edit3, Loader2, PauseCircle, Play, Trash2 } from "lucide-react";
import type { VoiceProfile } from "../../api/client";
import { formatRelativeTime, shortId } from "../../lib/format";
import {
  sourceTone,
  vendorLabel,
  vendorTone,
  voiceSourceLabels,
  voiceStatusLabels,
  voiceStatusTone,
} from "./libraryModel";

type VoiceCardProps = {
  voice: VoiceProfile;
  isPreviewing: boolean;
  isPlaying: boolean;
  caseNames: string[];
  onPreview: () => void;
  onEdit: () => void;
  onDelete: () => void;
};

export function VoiceCard({ voice, isPreviewing, isPlaying, caseNames, onPreview, onEdit, onDelete }: VoiceCardProps) {
  return (
    <article className="min-w-0 rounded-[24px] border border-border/80 bg-white/65 p-4 shadow-glow transition-all hover:-translate-y-0.5 hover:border-accent/25">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className={`badge ${vendorTone(voice.vendor)}`}>{vendorLabel(voice.vendor)}</span>
            <span className={`badge ${sourceTone(voice.source)}`}>{voiceSourceLabels[voice.source]}</span>
            {voice.status !== "ready" ? (
              <span className={`badge ${voiceStatusTone(voice.status)}`}>
                {voiceStatusLabels[voice.status] ?? voice.status}
              </span>
            ) : null}
          </div>
          <h3 className="mt-3 truncate text-lg font-semibold text-text-primary">{voice.display_name}</h3>
          <p className="mt-1 font-mono text-xs text-text-tertiary">{shortId(voice.id, 12)}</p>
        </div>
        <span
          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl ${
            voice.enabled ? "bg-accent/10 text-accent" : "bg-white text-text-tertiary"
          }`}
        >
          {voice.enabled ? <CheckCircle2 className="h-5 w-5" /> : <PauseCircle className="h-5 w-5" />}
        </span>
      </div>

      <div className="mt-3 flex min-h-7 min-w-0 flex-wrap items-center gap-1.5 text-xs text-text-secondary">
        <BriefcaseBusiness className="h-3.5 w-3.5 shrink-0" />
        {caseNames.length > 0 ? (
          <>
            <span className="shrink-0 text-text-tertiary">已绑 {caseNames.length} 个</span>
            {caseNames.slice(0, 2).map((name) => (
              <span key={name} className="badge max-w-[9rem] truncate bg-surface-hover/80 text-text-secondary" title={name}>
                {name}
              </span>
            ))}
          </>
        ) : (
          <span>未绑定案例</span>
        )}
        {caseNames.length > 2 ? <span className="shrink-0 text-text-tertiary">+{caseNames.length - 2}</span> : null}
      </div>

      <dl className="mt-4 grid gap-2 text-xs text-text-secondary">
        <div className="flex justify-between gap-2">
          <dt>状态</dt>
          <dd>{voice.enabled ? "可用" : "已停用"}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt>更新时间</dt>
          <dd>{formatRelativeTime(voice.updated_at ?? voice.created_at)}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt>厂商</dt>
          <dd className="truncate">{vendorLabel(voice.vendor)}</dd>
        </div>
      </dl>

      <div className="mt-4 grid grid-cols-4 gap-2">
        <button
          className="icon-button col-span-2 w-full min-w-0"
          type="button"
          onClick={onPreview}
          disabled={isPreviewing || voice.status === "training"}
          title={
            voice.status === "training"
              ? "复刻训练中，暂不可试听"
              : isPreviewing
                ? "生成试听中…"
                : "生成试听"
          }
        >
          {isPreviewing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
          <span className="min-w-0 truncate text-xs">
            {voice.status === "training" ? "训练中" : isPreviewing ? "生成中" : isPlaying ? "已试听" : "试听"}
          </span>
        </button>
        <button className="icon-button w-full" type="button" onClick={onEdit} title="编辑音色">
          <Edit3 className="h-4 w-4" />
        </button>
        <button className="icon-button w-full hover:border-status-error/30 hover:text-status-error" type="button" onClick={onDelete} title="删除音色">
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
    </article>
  );
}
