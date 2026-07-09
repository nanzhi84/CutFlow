import { Captions, Sparkles } from "lucide-react";
import type { RunDetailResponse } from "../../api/client";

// 花字动画白名单 → 中文文案（D1 的 7 种枚举）；未知值回落原始串，不当失败处理。
const ANIMATION_LABELS: Record<string, string> = {
  none: "无",
  fade_in: "淡入",
  pop_in: "弹入",
  slide_up: "上滑入",
  slide_left: "左滑入",
  slide_right: "右滑入",
  punch: "冲击",
};

export type CaptionEmphasisEventView = {
  eventId: string;
  text: string;
  start: number;
  end: number;
  layoutBoxId: string;
  animationId: string;
  priority: number;
  reason: string;
};

export type CaptionSuppressedCueView = {
  start: number;
  end: number;
  firstLine: string;
  suppressedBy: string;
};

export type CaptionDisplayPlanView = {
  policyVersion: string;
  diagnostics: {
    mergedUnits: number;
    splitCues: number;
    suppressedDuplicates: number;
    droppedFragments: number;
    animationFallbacks: number;
    fontMetricsSource: string;
  };
  emphasisEvents: CaptionEmphasisEventView[];
  suppressedCues: CaptionSuppressedCueView[];
};

// 新 artifact kind；schema.d.ts 的 ArtifactKind 联合类型由协调者统一重生成后才会包含它，
// 这里用字符串比较避免生成物落后时字面量不重叠的类型报错。
const CAPTION_DISPLAY_KIND = "plan.caption_display";

/** 从 run 产物快照解析 `plan.caption_display`（Caption Display v2 诊断）；无该 artifact 返回 null。 */
export function buildCaptionDisplay(detail?: RunDetailResponse): CaptionDisplayPlanView | null {
  const artifact = detail?.artifacts.find((item) => (item.kind as string) === CAPTION_DISPLAY_KIND);
  const payload = asRecord(artifact ? detail?.artifact_payloads?.[artifact.artifact_id] : undefined);
  if (!payload) return null;
  const diagnostics = asRecord(payload.diagnostics) ?? {};
  return {
    policyVersion: readString(payload.policy_version) ?? "caption_display_v2",
    diagnostics: {
      mergedUnits: readCount(diagnostics.merged_units),
      splitCues: readCount(diagnostics.split_cues),
      suppressedDuplicates: readCount(diagnostics.suppressed_duplicates),
      droppedFragments: readCount(diagnostics.dropped_fragments),
      animationFallbacks: readCount(diagnostics.animation_fallbacks),
      fontMetricsSource: readString(diagnostics.font_metrics_source) ?? "hmtx",
    },
    emphasisEvents: readRecordList(payload.emphasis_events).flatMap((event, index) => {
      const start = readNumber(event.start);
      const end = readNumber(event.end);
      if (start === null || end === null) return [];
      return [
        {
          eventId: readString(event.event_id) ?? `event_${index + 1}`,
          text: readString(event.text) ?? "",
          start,
          end,
          layoutBoxId: readString(event.layout_box_id) ?? "",
          animationId: readString(event.animation_id) ?? "none",
          priority: readNumber(event.priority) ?? 0,
          reason: readString(event.reason) ?? "",
        },
      ];
    }),
    suppressedCues: readRecordList(payload.suppressed_cues).flatMap((cue) => {
      const start = readNumber(cue.start);
      const end = readNumber(cue.end);
      if (start === null || end === null) return [];
      return [
        {
          start,
          end,
          firstLine: readStringList(cue.lines)[0] ?? "",
          suppressedBy: readString(cue.suppressed_by) ?? "",
        },
      ];
    }),
  };
}

function animationLabel(id: string): string {
  return ANIMATION_LABELS[id] ?? id;
}

function fontMetricsLabel(source: string): string {
  return source === "hmtx" ? "hmtx 精确" : "EAW 估算";
}

function formatSpan(start: number, end: number): string {
  return `${start.toFixed(2)}s – ${end.toFixed(2)}s`;
}

/** 运行详情里的"字幕显示计划"区块；plan 为 null 时展示旧版本兼容空态（非失败）。 */
export function CaptionDisplayPanel({ plan }: { plan: CaptionDisplayPlanView | null }) {
  if (!plan) {
    return (
      <section className="grid gap-3">
        <h4 className="text-base font-semibold text-text-primary">字幕显示计划</h4>
        <div className="rounded-2xl border border-dashed border-border/70 bg-white/40 px-4 py-6 text-center text-sm text-text-tertiary">
          旧版本无详细字幕计划
        </div>
      </section>
    );
  }
  const { diagnostics } = plan;
  const chips: Array<{ label: string; value: string }> = [
    { label: "合并", value: `${diagnostics.mergedUnits} 段` },
    { label: "拆分", value: `${diagnostics.splitCues} 条` },
    { label: "去重抑制", value: `${diagnostics.suppressedDuplicates} 条` },
    { label: "丢弃碎片", value: `${diagnostics.droppedFragments} 个` },
    { label: "动画降级", value: `${diagnostics.animationFallbacks} 次` },
    { label: "字宽来源", value: fontMetricsLabel(diagnostics.fontMetricsSource) },
  ];
  return (
    <section className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-base font-semibold text-text-primary">字幕显示计划</h4>
        <span className="badge bg-white/70 font-mono text-text-secondary">{plan.policyVersion}</span>
      </div>

      <div className="flex flex-wrap gap-2">
        {chips.map((chip) => (
          <span
            key={chip.label}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border/60 bg-white/60 px-2.5 py-1 text-xs text-text-secondary"
          >
            <span className="text-text-tertiary">{chip.label}</span>
            <span className="font-medium text-text-primary">{chip.value}</span>
          </span>
        ))}
      </div>

      <div className="grid gap-1.5">
        <p className="flex items-center gap-1.5 text-xs font-medium text-text-tertiary">
          <Sparkles className="h-3.5 w-3.5" />
          花字事件 · 共 {plan.emphasisEvents.length} 条
        </p>
        {plan.emphasisEvents.length > 0 ? (
          <div className="overflow-x-auto rounded-xl border border-border/60 bg-white/60">
            <table className="w-full min-w-[640px] border-collapse text-xs">
              <thead>
                <tr className="border-b border-border/60 text-left text-text-tertiary">
                  <th className="px-3 py-2 font-medium">文本</th>
                  <th className="px-3 py-2 font-medium">时段</th>
                  <th className="px-3 py-2 font-medium">位置框</th>
                  <th className="px-3 py-2 font-medium">动画</th>
                  <th className="px-3 py-2 font-medium">优先级</th>
                  <th className="px-3 py-2 font-medium">理由</th>
                </tr>
              </thead>
              <tbody>
                {plan.emphasisEvents.map((event) => (
                  <tr key={event.eventId} className="border-b border-border/40 last:border-b-0">
                    <td className="px-3 py-2 font-medium text-text-primary">{event.text || "—"}</td>
                    <td className="whitespace-nowrap px-3 py-2 font-mono text-text-secondary">
                      {formatSpan(event.start, event.end)}
                    </td>
                    <td className="px-3 py-2 font-mono text-text-tertiary">{event.layoutBoxId || "—"}</td>
                    <td className="whitespace-nowrap px-3 py-2 text-text-secondary">{animationLabel(event.animationId)}</td>
                    <td className="px-3 py-2 font-mono text-text-secondary">{event.priority}</td>
                    <td className="px-3 py-2 text-text-tertiary" title={event.reason || undefined}>
                      <span className="line-clamp-2">{event.reason || "—"}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="rounded-lg bg-surface-hover/60 px-3 py-2 text-xs text-text-tertiary">本次无花字事件。</p>
        )}
      </div>

      {plan.suppressedCues.length > 0 ? (
        <div className="grid gap-1.5">
          <p className="flex items-center gap-1.5 text-xs font-medium text-text-tertiary">
            <Captions className="h-3.5 w-3.5" />
            被抑制字幕 · 共 {plan.suppressedCues.length} 条
          </p>
          <ul className="grid gap-1">
            {plan.suppressedCues.map((cue, index) => (
              <li
                key={`${cue.start}_${index}`}
                className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 rounded-lg border border-border/50 bg-white/50 px-3 py-1.5 text-xs"
              >
                <span className="whitespace-nowrap font-mono text-text-tertiary">{formatSpan(cue.start, cue.end)}</span>
                <span className="min-w-0 truncate text-text-secondary" title={cue.firstLine || undefined}>
                  {cue.firstLine ? `「${cue.firstLine}」` : "（空行）"}
                </span>
                <span className="whitespace-nowrap font-mono text-text-tertiary">
                  {cue.suppressedBy ? `被 ${cue.suppressedBy} 抑制` : "被抑制"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
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

function readCount(value: unknown): number {
  const parsed = readNumber(value);
  return parsed !== null && parsed >= 0 ? Math.trunc(parsed) : 0;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function readStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
}
