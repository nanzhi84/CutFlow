import { Film, Search, Sparkles, User } from "lucide-react";
import type { RunDetailResponse } from "../../api/client";
import { shortId } from "../../lib/format";
import type { EditClip } from "./EditTimelinePreview";

export type WindowCandidateView = {
  candidateId: string;
  assetId: string;
  clipId: string;
  retrievalScore: number;
  similarity: number;
  chosen: boolean;
};

export type WindowView = {
  windowId: string;
  kind: "portrait" | "broll";
  start: number;
  end: number;
  narration: string;
  query: string;
  candidates: WindowCandidateView[];
  reason: string;
  confidence?: number;
  matchedKeywords: string[];
  assigned: boolean;
  clipId?: string;
};

export type WindowBoard = {
  engine?: string;
  querySource?: string;
  totalSeconds: number;
  windows: WindowView[];
};

/** 从 run 产物快照组装窗口规划看板：窗口 ⊕ 检索 query ⊕ 候选 ⊕ Agent 指派理由。 */
export function buildWindowBoard(detail: RunDetailResponse | undefined, clips: EditClip[]): WindowBoard | null {
  const payloadFor = (kind: string) => {
    const artifact = detail?.artifacts.find((item) => item.kind === kind);
    return asRecord(artifact ? detail?.artifact_payloads?.[artifact.artifact_id] : undefined);
  };

  const windowsPlan = payloadFor("plan.timeline_windows");
  if (!windowsPlan) return null;
  const fps = readNumber(windowsPlan.fps) ?? 30;
  const totalFrames = readNumber(windowsPlan.total_frames) ?? 0;

  // 旁白文本：broll 窗口自带 text；portrait 窗口经 unit_ids 从 narration.units 取句子拼接。
  const narrationUnits = payloadFor("narration.units");
  const unitTextById = new Map<string, string>();
  for (const unit of readRecordList(narrationUnits?.units)) {
    const unitId = readString(unit.unit_id);
    const text = readString(unit.text);
    if (unitId && text) unitTextById.set(unitId, text);
  }
  const narrationFor = (row: Record<string, unknown>): string => {
    const own = readString(row.text);
    if (own) return own;
    const unitIds = readStringList(row.unit_ids ?? row.host_unit_ids);
    return unitIds
      .map((unitId) => unitTextById.get(unitId) ?? "")
      .filter(Boolean)
      .join(" ");
  };

  const queryPlan = payloadFor("plan.window_queries");
  const queryByWindow = new Map<string, string>();
  for (const item of readRecordList(queryPlan?.window_queries)) {
    const windowId = readString(item.window_id);
    const intent = readString(item.retrieval_intent);
    if (windowId && intent) queryByWindow.set(windowId, intent);
  }

  const retrieval = payloadFor("plan.window_material_retrieval");
  const candidatesByWindow = asRecord(retrieval?.candidates_by_window) ?? {};

  const assignment = payloadFor("plan.media_assignment");
  const assignmentByWindow = new Map<string, Record<string, unknown>>();
  for (const item of [...readRecordList(assignment?.portrait), ...readRecordList(assignment?.broll)]) {
    const windowId = readString(item.window_id);
    if (windowId) assignmentByWindow.set(windowId, item);
  }

  const buildWindows = (rows: unknown, kind: "portrait" | "broll"): WindowView[] =>
    readRecordList(rows).flatMap((row) => {
      const windowId = readString(row.window_id);
      const startFrame = readNumber(row.start_frame);
      const endFrame = readNumber(row.end_frame);
      if (!windowId || startFrame === null || endFrame === null || endFrame <= startFrame || fps <= 0) return [];
      const start = startFrame / fps;
      const end = endFrame / fps;
      const assigned = assignmentByWindow.get(windowId);
      const chosenCandidateId = readString(assigned?.candidate_id);
      const candidates = readRecordList(candidatesByWindow[windowId])
        .flatMap((candidate) => {
          const candidateId = readString(candidate.candidate_id);
          if (!candidateId) return [];
          return [
            {
              candidateId,
              assetId: readString(candidate.asset_id) ?? "",
              clipId: readString(candidate.clip_id) ?? "",
              retrievalScore: readNumber(candidate.retrieval_score) ?? 0,
              similarity: readNumber(candidate.semantic_similarity) ?? 0,
              chosen: candidateId === chosenCandidateId,
            },
          ];
        })
        .sort((a, b) => b.retrievalScore - a.retrievalScore);
      const midpoint = (start + end) / 2;
      const clip = clips.find((item) => item.kind === kind && item.start <= midpoint && midpoint < item.end);
      return [
        {
          windowId,
          kind,
          start,
          end,
          narration: narrationFor(row),
          query: queryByWindow.get(windowId) ?? "",
          candidates,
          reason: readString(assigned?.reason) ?? "",
          confidence: readNumber(assigned?.confidence) ?? undefined,
          matchedKeywords: readStringList(assigned?.matched_keywords),
          assigned: Boolean(assigned),
          clipId: clip?.id,
        },
      ];
    });

  const windows = [...buildWindows(windowsPlan.portrait_windows, "portrait"), ...buildWindows(windowsPlan.broll_windows, "broll")].sort(
    (a, b) => a.start - b.start || a.end - b.end,
  );
  if (windows.length === 0) return null;

  return {
    engine: readString(assignment?.engine) ?? undefined,
    querySource: readString(asRecord(queryPlan?.diagnostics)?.source) ?? undefined,
    totalSeconds: totalFrames > 0 && fps > 0 ? totalFrames / fps : Math.max(...windows.map((window) => window.end)),
    windows,
  };
}

function engineBadge(engine?: string): { label: string; className: string } | null {
  if (engine === "media_selection_agent_llm") return { label: "媒体选择 Agent 指派", className: "badge-info" };
  if (engine === "editing_agent_llm") return { label: "LLM 剪辑 Agent 指派", className: "badge-info" };
  if (engine === "deterministic_default") return { label: "确定性算法指派", className: "badge bg-white/70 text-text-secondary" };
  if (engine === "deterministic_fallback") return { label: "确定性兜底指派（LLM 失败）", className: "badge-warning" };
  return null;
}

function querySourceBadge(source?: string): { label: string; className: string } | null {
  if (source === "llm_window_queries") return { label: "LLM 生成检索 query", className: "badge-info" };
  if (source === "template_fallback") return { label: "模板拼接检索 query（兜底）", className: "badge-warning" };
  return null;
}

function formatClock(seconds: number): string {
  const total = Math.max(0, seconds);
  const minutes = Math.floor(total / 60);
  const rest = total - minutes * 60;
  return `${minutes}:${rest.toFixed(1).padStart(4, "0")}`;
}

const MAX_CANDIDATE_ROWS = 4;

/** 剪辑窗口规划看板：时间轴总览 + 每窗口的检索 query / 候选 / Agent 选择理由。 */
export function WindowPlanBoard({
  board,
  activeClipId,
  onSelect,
}: {
  board: WindowBoard;
  activeClipId?: string | null;
  onSelect?: (clipId: string) => void;
}) {
  const brollCount = board.windows.filter((window) => window.kind === "broll").length;
  const engine = engineBadge(board.engine);
  const querySource = querySourceBadge(board.querySource);
  return (
    <section className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-base font-semibold text-text-primary">剪辑时间线 · 窗口规划</h4>
        <div className="flex flex-wrap items-center gap-2">
          {engine ? <span className={engine.className}>{engine.label}</span> : null}
          {querySource ? <span className={querySource.className}>{querySource.label}</span> : null}
          <span className="badge bg-white/70 text-text-secondary">
            {board.windows.length} 个窗口 · B-roll {brollCount}
          </span>
        </div>
      </div>

      <TimelineLanes board={board} activeClipId={activeClipId} onSelect={onSelect} />

      <div className="grid items-stretch gap-4 lg:grid-cols-2">
        {board.windows.map((window, index) => (
          <WindowCard
            key={window.windowId}
            window={window}
            index={index}
            active={Boolean(window.clipId) && window.clipId === activeClipId}
            onSelect={onSelect}
          />
        ))}
      </div>
    </section>
  );
}

/** 双泳道时间轴：上为数字人主轨窗口、下为 B-roll 窗口，宽度按真实时长比例。 */
function TimelineLanes({
  board,
  activeClipId,
  onSelect,
}: {
  board: WindowBoard;
  activeClipId?: string | null;
  onSelect?: (clipId: string) => void;
}) {
  const total = board.totalSeconds > 0 ? board.totalSeconds : 1;
  const lanes: Array<{ kind: "portrait" | "broll"; label: string }> = [
    { kind: "portrait", label: "数字人" },
    { kind: "broll", label: "B-roll" },
  ];
  return (
    <div className="grid gap-1.5 rounded-2xl border border-border/70 bg-white/60 p-3">
      {lanes.map((lane) => (
        <div className="grid grid-cols-[52px_minmax(0,1fr)] items-center gap-2" key={lane.kind}>
          <span className="text-[11px] font-medium text-text-tertiary">{lane.label}</span>
          <div className="relative h-7 overflow-hidden rounded-lg bg-surface-hover">
            {board.windows
              .filter((window) => window.kind === lane.kind)
              .map((window) => {
                const left = (window.start / total) * 100;
                const width = Math.max(((window.end - window.start) / total) * 100, 0.5);
                const active = Boolean(window.clipId) && window.clipId === activeClipId;
                const tone =
                  lane.kind === "portrait"
                    ? "bg-accent/50 hover:bg-accent/70"
                    : window.assigned
                      ? "bg-brand-cyan/60 hover:bg-brand-cyan/80"
                      : "bg-black/10 hover:bg-black/20";
                return (
                  <button
                    type="button"
                    key={window.windowId}
                    className={`absolute inset-y-0.5 rounded-md border border-white/60 transition-colors ${tone} ${active ? "ring-2 ring-accent" : ""}`}
                    style={{ left: `${left}%`, width: `${width}%` }}
                    title={`${window.windowId} · ${formatClock(window.start)} – ${formatClock(window.end)}${window.assigned ? "" : " · 未选用"}`}
                    onClick={() => window.clipId && onSelect?.(window.clipId)}
                  />
                );
              })}
          </div>
        </div>
      ))}
      <div className="grid grid-cols-[52px_minmax(0,1fr)] gap-2">
        <span />
        <div className="flex justify-between font-mono text-[10px] text-text-tertiary">
          <span>0:00.0</span>
          <span>{formatClock(total / 2)}</span>
          <span>{formatClock(total)}</span>
        </div>
      </div>
    </div>
  );
}

/** 窗口卡片：固定五段结构（头部/旁白/query/候选/理由），让 B-roll 与数字人卡片的同级信息稳定对齐。 */
function WindowCard({
  window,
  index,
  active,
  onSelect,
}: {
  window: WindowView;
  index: number;
  active: boolean;
  onSelect?: (clipId: string) => void;
}) {
  const isBroll = window.kind === "broll";
  const chosen = window.candidates.find((candidate) => candidate.chosen);
  let visibleCandidates = window.candidates.slice(0, MAX_CANDIDATE_ROWS);
  if (chosen && !visibleCandidates.includes(chosen)) {
    visibleCandidates = [...window.candidates.slice(0, MAX_CANDIDATE_ROWS - 1), chosen];
  }
  const hiddenCount = window.candidates.length - visibleCandidates.length;
  const isSelectable = Boolean(window.clipId);
  const selectWindow = () => {
    if (window.clipId) onSelect?.(window.clipId);
  };
  return (
    <article
      className={`flex h-full flex-col gap-3 rounded-2xl border p-4 transition-colors ${
        active ? "border-accent/30 bg-accent/5 ring-1 ring-accent/20" : "border-border/70 bg-white/60"
      } ${isSelectable ? "cursor-pointer hover:bg-white/80 focus:outline-none focus:ring-2 focus:ring-accent/60" : ""}`}
      role={isSelectable ? "button" : undefined}
      tabIndex={isSelectable ? 0 : undefined}
      onClick={isSelectable ? selectWindow : undefined}
      onKeyDown={
        isSelectable
          ? (event) => {
              if (event.key !== "Enter" && event.key !== " ") return;
              event.preventDefault();
              selectWindow();
            }
          : undefined
      }
    >
      <header className="flex min-h-7 flex-wrap items-center gap-2">
        <span
          className={`flex h-7 w-7 items-center justify-center rounded-lg ${isBroll ? "bg-brand-cyan/20 text-text-secondary" : "bg-accent/15 text-accent"}`}
        >
          {isBroll ? <Film className="h-4 w-4" /> : <User className="h-4 w-4" />}
        </span>
        <span className="text-sm font-semibold text-text-primary">#{index + 1}</span>
        <span className="rounded-full bg-surface-hover px-2 py-0.5 text-[11px] font-medium text-text-secondary">{isBroll ? "B-roll" : "数字人"}</span>
        <span className="font-mono text-xs text-text-tertiary">
          {formatClock(window.start)} – {formatClock(window.end)}
        </span>
        <span className="ml-auto font-mono text-[10px] text-text-tertiary">{window.windowId}</span>
        {isBroll && !window.assigned ? <span className="badge bg-black/5 text-text-tertiary">未选用</span> : null}
      </header>

      <p className="h-5 min-w-0 truncate text-xs leading-5 text-text-secondary" title={window.narration || undefined}>
        {window.narration ? `「${window.narration}」` : "（该窗口无对应旁白）"}
      </p>

      <div className="grid h-[5.25rem] content-start gap-1 rounded-xl bg-surface-hover/70 px-3 py-2.5">
        <p className="flex items-center gap-1 text-[11px] font-medium text-text-tertiary">
          <Search className="h-3 w-3" />
          检索 query
        </p>
        <p className="line-clamp-2 text-xs leading-relaxed text-text-secondary" title={window.query || undefined}>
          {window.query || "（无检索 query）"}
        </p>
      </div>

      <div className="grid h-[8.75rem] content-start gap-1.5">
        <p className="text-[11px] font-medium text-text-tertiary">
          检索候选 · 共 {window.candidates.length} 个{hiddenCount > 0 ? `（低分 ${hiddenCount} 个已折叠）` : ""}
        </p>
        {visibleCandidates.length > 0 ? (
          <ol className="grid gap-1">
            {visibleCandidates.map((candidate) => (
              <CandidateRow key={candidate.candidateId} candidate={candidate} />
            ))}
          </ol>
        ) : (
          <p className="rounded-lg bg-white/50 px-2 py-1.5 text-xs text-text-tertiary">无检索候选</p>
        )}
      </div>

      <div className={`mt-3 grid h-[7.75rem] content-start gap-1.5 rounded-xl border px-3 py-2.5 ${window.reason ? "border-accent/15 bg-accent/5" : "border-dashed border-border/70 bg-white/40"}`}>
        <p className={`flex items-center gap-1 text-[11px] font-medium ${window.reason ? "text-accent" : "text-text-tertiary"}`}>
          <Sparkles className="h-3 w-3" />
          选择理由
        </p>
        {window.reason ? (
          <>
            <p className="line-clamp-2 text-xs leading-relaxed text-text-secondary" title={window.reason}>
              {window.reason}
            </p>
            {(window.confidence != null && window.confidence > 0) || window.matchedKeywords.length > 0 ? (
              <div className="flex min-h-5 flex-wrap items-center gap-1.5 overflow-hidden">
                {window.confidence != null && window.confidence > 0 ? (
                  <span className={window.confidence > 0.7 ? "badge-success" : window.confidence >= 0.4 ? "badge-warning" : "badge bg-orange-100 text-orange-700"}>
                    置信度 {Math.round(window.confidence * 100)}%
                  </span>
                ) : null}
                {window.matchedKeywords.map((keyword) => (
                  <span key={keyword} className="badge bg-surface-hover text-text-secondary">
                    {keyword}
                  </span>
                ))}
              </div>
            ) : null}
          </>
        ) : (
          <p className="text-xs leading-relaxed text-text-tertiary">
            {isBroll && !window.assigned ? "Agent 未选用该窗口，不插入 B-roll。" : "（无选择理由记录）"}
          </p>
        )}
      </div>
    </article>
  );
}

function CandidateRow({ candidate }: { candidate: WindowCandidateView }) {
  const percent = Math.round(Math.max(0, Math.min(1, candidate.retrievalScore)) * 100);
  return (
    <li
      className={`grid grid-cols-[minmax(0,1fr)_88px_44px] items-center gap-2 rounded-lg px-2 py-1 ${
        candidate.chosen ? "bg-status-success/10 ring-1 ring-status-success/25" : "bg-white/50"
      }`}
    >
      <span className="flex min-w-0 items-center gap-1.5">
        {candidate.chosen ? <span className="badge-success shrink-0">已选</span> : null}
        <span className="truncate font-mono text-[11px] text-text-secondary" title={`${candidate.assetId} / ${candidate.clipId}`}>
          {shortId(candidate.assetId, 10)}
          {candidate.clipId ? ` · ${candidate.clipId}` : ""}
        </span>
      </span>
      <span className="h-1.5 overflow-hidden rounded-full bg-black/5">
        <span className={`block h-full rounded-full ${candidate.chosen ? "bg-status-success" : "bg-accent/40"}`} style={{ width: `${percent}%` }} />
      </span>
      <span className="text-right font-mono text-[11px] text-text-tertiary">{percent}%</span>
    </li>
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function readRecordList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.flatMap((item) => (asRecord(item) ? [asRecord(item)!] : [])) : [];
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function readStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
}
