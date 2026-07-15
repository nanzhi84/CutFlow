import { Captions, Sparkles } from "lucide-react";
import type { RunDetailResponse } from "../../api/client";

type CaptionRunView = {
  runId: string;
  text: string;
  role: string;
  hintId: string | null;
  enterFrame: number;
  exitFrame: number;
  effectId: string;
  tokenIds: string[];
  charSpan: [number, number];
  advancePx: number;
  baselineOffsetPx: number;
};

type CaptionCueView = {
  cueId: string;
  text: string;
  startFrame: number;
  endFrame: number;
  lines: CaptionRunView[][];
};

export type CaptionCompositionView = {
  policyVersion: string;
  fps: number;
  normalEnabled: boolean;
  emphasisEnabled: boolean;
  band: { anchorX: number; baselineY: number; lineHeightRatio: number; maxWidthRatio: number };
  diagnostics: {
    timingSource: string;
    fontMetricsSource: string;
    emphasisFontMetricsSource: string;
    mergedUnits: number;
    splitCues: number;
    unitsUnmatched: number;
    hintsTotal: number;
    hintsApplied: number;
    hintsUnmatched: number;
    hintsTokenUnmatched: number;
    hintsOverlapped: number;
    hintsUnbreakable: number;
    fallbacks: Record<string, unknown>[];
  };
  cues: CaptionCueView[];
};

const CAPTION_COMPOSITION_KIND = "plan.caption_composition";

export function buildCaptionComposition(detail?: RunDetailResponse): CaptionCompositionView | null {
  const artifact = detail?.artifacts.find(
    (item) => (item.kind as string) === CAPTION_COMPOSITION_KIND,
  );
  const payload = asRecord(
    artifact ? detail?.artifact_payloads?.[artifact.artifact_id] : undefined,
  );
  if (!payload) return null;
  const diagnostics = asRecord(payload.diagnostics) ?? {};
  const band = asRecord(payload.band) ?? {};
  return {
    policyVersion: readString(payload.policy_version) ?? "caption_composition_v1",
    fps: readPositiveNumber(payload.fps, 30),
    normalEnabled: payload.normal_enabled === true,
    emphasisEnabled: payload.emphasis_enabled === true,
    band: {
      anchorX: readNumber(band.anchor_x) ?? 0.5,
      baselineY: readNumber(band.baseline_y) ?? 0.84,
      lineHeightRatio: readNumber(band.line_height_ratio) ?? 1.12,
      maxWidthRatio: readNumber(band.max_width_ratio) ?? 0.85,
    },
    diagnostics: {
      timingSource: readString(diagnostics.timing_source) ?? "interpolated",
      fontMetricsSource: readString(diagnostics.font_metrics_source) ?? "hmtx",
      emphasisFontMetricsSource:
        readString(diagnostics.emphasis_font_metrics_source) ?? "hmtx",
      mergedUnits: readCount(diagnostics.merged_units),
      splitCues: readCount(diagnostics.split_cues),
      unitsUnmatched: readCount(diagnostics.units_unmatched),
      hintsTotal: readCount(diagnostics.hints_total),
      hintsApplied: readCount(diagnostics.hints_applied),
      hintsUnmatched: readCount(diagnostics.hints_unmatched),
      hintsTokenUnmatched: readCount(diagnostics.hints_token_unmatched),
      hintsOverlapped: readCount(diagnostics.hints_overlapped),
      hintsUnbreakable: readCount(diagnostics.hints_unbreakable),
      fallbacks: readRecordList(diagnostics.fallbacks),
    },
    cues: readRecordList(payload.cues).map((cue, cueIndex) => ({
      cueId: readString(cue.cue_id) ?? `cue_${cueIndex + 1}`,
      text: typeof cue.text === "string" ? cue.text : "",
      startFrame: readCount(cue.start_frame),
      endFrame: readCount(cue.end_frame),
      lines: readRecordList(cue.lines).map((line) =>
        readRecordList(line.runs).map((run, runIndex) => ({
          runId: readString(run.run_id) ?? `run_${runIndex + 1}`,
          text: typeof run.text === "string" ? run.text : "",
          role: readString(run.role) ?? "normal",
          hintId: readString(run.hint_id),
          enterFrame: readCount(run.enter_frame),
          exitFrame: readCount(run.exit_frame),
          effectId: readString(run.effect_id) ?? "none",
          tokenIds: readStringList(run.token_ids),
          charSpan: readSpan(run.char_span),
          advancePx: readNumber(run.advance_px) ?? 0,
          baselineOffsetPx: readNumber(run.baseline_offset_px) ?? 0,
        })),
      ),
    })),
  };
}

export function CaptionDisplayPanel({ plan }: { plan: CaptionCompositionView | null }) {
  if (!plan) {
    return (
      <section className="grid gap-3">
        <h4 className="text-base font-semibold text-text-primary">字幕合成计划</h4>
        <div className="rounded-2xl border border-dashed border-border/70 bg-white/40 px-4 py-6 text-center text-sm text-text-tertiary">
          本次运行没有字幕合成计划（旧任务，或未开启字幕）
        </div>
      </section>
    );
  }
  const emphasisRuns = plan.cues.flatMap((cue) => cue.lines.flat()).filter(
    (run) => run.role === "emphasis",
  );
  const chips = [
    ["字幕带基线", `${Math.round(plan.band.baselineY * 100)}%`],
    ["安全宽度", `${Math.round(plan.band.maxWidthRatio * 100)}%`],
    ["时间来源", plan.diagnostics.timingSource],
    ["普通字宽", metricsLabel(plan.diagnostics.fontMetricsSource)],
    ["强调字宽", metricsLabel(plan.diagnostics.emphasisFontMetricsSource)],
    ["Cue", `${plan.cues.length} 条`],
    ["强调命中", `${plan.diagnostics.hintsApplied}/${plan.diagnostics.hintsTotal}`],
    ["回退", `${plan.diagnostics.fallbacks.length} 条`],
  ] as const;
  return (
    <section className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-base font-semibold text-text-primary">字幕合成计划</h4>
        <span className="badge bg-white/70 font-mono text-text-secondary">
          {plan.policyVersion}
        </span>
      </div>
      <div className="flex flex-wrap gap-2">
        {chips.map(([label, value]) => (
          <span
            key={label}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border/60 bg-white/60 px-2.5 py-1 text-xs text-text-secondary"
          >
            <span className="text-text-tertiary">{label}</span>
            <span className="font-medium text-text-primary">{value}</span>
          </span>
        ))}
      </div>
      <div className="grid gap-1.5">
        <p className="flex items-center gap-1.5 text-xs font-medium text-text-tertiary">
          <Captions className="h-3.5 w-3.5" />
          固定字幕带 Cue · 共 {plan.cues.length} 条
        </p>
        <div className="grid gap-2 rounded-xl border border-border/60 bg-white/60 p-3">
          {plan.cues.map((cue) => (
            <div key={cue.cueId} className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
              <span className="min-w-0 text-text-primary">
                {cue.lines.map((line, lineIndex) => (
                  <span key={`${cue.cueId}-${lineIndex}`} className="block text-center">
                    {line.map((run) => (
                      <span key={run.runId} className="inline-grid gap-0.5 align-baseline">
                        <span className={run.role === "emphasis" ? "font-semibold text-accent" : ""}>
                          {run.text}
                        </span>
                        <span className="font-mono text-[10px] text-text-tertiary" data-testid="caption-run-detail">
                          {run.role} · f{run.enterFrame}–f{run.exitFrame} · char[{run.charSpan.join(",")}]
                          {run.tokenIds.length ? ` · token ${run.tokenIds.join(",")}` : " · token —"}
                          {run.hintId ? ` · ${run.hintId}` : ""} · {run.effectId} · {run.advancePx}px / baseline {run.baselineOffsetPx}px
                        </span>
                      </span>
                    ))}
                  </span>
                ))}
              </span>
              <span className="shrink-0 font-mono text-text-tertiary">
                f{cue.startFrame}–f{cue.endFrame}
              </span>
            </div>
          ))}
        </div>
      </div>
      <p className="flex items-center gap-1.5 text-xs text-text-tertiary">
        <Sparkles className="h-3.5 w-3.5" />
        字幕内强调 Run {emphasisRuns.length} 个；合并 {plan.diagnostics.mergedUnits} 段，拆分 {plan.diagnostics.splitCues} 条，文本未命中 {plan.diagnostics.hintsUnmatched} 条，Token 未命中 {plan.diagnostics.hintsTokenUnmatched} 条，重叠取舍 {plan.diagnostics.hintsOverlapped} 条。
      </p>
      {plan.diagnostics.fallbacks.length ? (
        <div className="grid gap-1 rounded-xl border border-warning/30 bg-warning/5 p-3 text-xs" data-testid="caption-fallbacks">
          <span className="font-medium text-text-primary">确定性回退明细</span>
          {plan.diagnostics.fallbacks.map((item, index) => (
            <code key={index} className="break-all text-text-secondary">
              {JSON.stringify(item)}
            </code>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function metricsLabel(value: string) {
  return value === "hmtx" ? "hmtx 精确" : "EAW 估算";
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function readRecordList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.flatMap((item) => (asRecord(item) ? [asRecord(item)!] : []))
    : [];
}

function readStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && Boolean(item))
    : [];
}

function readSpan(value: unknown): [number, number] {
  return Array.isArray(value) && value.length === 2
    ? [readCount(value[0]), readCount(value[1])]
    : [0, 0];
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readPositiveNumber(value: unknown, fallback: number): number {
  const parsed = readNumber(value);
  return parsed !== null && parsed > 0 ? parsed : fallback;
}

function readCount(value: unknown): number {
  const parsed = readNumber(value);
  return parsed !== null && parsed >= 0 ? Math.trunc(parsed) : 0;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}
