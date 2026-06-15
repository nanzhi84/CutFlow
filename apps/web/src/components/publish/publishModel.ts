import type { FinishedVideo, PublishBatch, PublishBatchItem, PublishPackage } from "../../api/client";
import { toDisplayUrl as sanitizeDisplayUrl } from "../../lib/url";

export type PublishDraft = {
  title: string;
  description: string;
  platforms: string[];
  selected: boolean;
  tagsInput: string;
  location: string;
  scheduleMode: "immediate" | "scheduled";
  scheduledAt: string;
  frameTimeSec: number;
};

export type BatchDefaults = {
  platforms: string[];
  scheduleMode: "immediate" | "scheduled";
  scheduledAt: string;
  tagsInput: string;
  location: string;
  titlePrefix: string;
  description: string;
};

export type SourcePoolItem =
  | { id: string; type: "finished"; title: string; video: FinishedVideo }
  | { id: string; type: "upload"; title: string; file: File; package?: PublishPackage };

export const PUBLISH_STEPS = ["选来源", "编辑", "发布"] as const;

export const PLATFORM_OPTIONS = [
  { value: "xiaovmao", label: "小V猫", pending: true },
  { value: "douyin", label: "抖音" },
  { value: "kuaishou", label: "快手" },
  { value: "shipinhao", label: "视频号" },
  { value: "xiaohongshu", label: "小红书" },
  { value: "bilibili", label: "B站" },
] as const;

const TITLE_LIMITS: Record<string, number> = {
  douyin: 16,
  kuaishou: 16,
  shipinhao: 16,
  xiaohongshu: 16,
  xiaovmao: 16,
  bilibili: 30,
};

export const defaultBatchDefaults: BatchDefaults = {
  platforms: ["xiaovmao"],
  scheduleMode: "immediate",
  scheduledAt: "",
  tagsInput: "",
  location: "",
  titlePrefix: "",
  description: "",
};

export function platformLabel(value: string) {
  return PLATFORM_OPTIONS.find((item) => item.value === value)?.label ?? value;
}

export function titleLimitForPlatforms(platforms: string[]) {
  const limits = platforms.map((platform) => TITLE_LIMITS[platform] ?? 16);
  return Math.min(...(limits.length > 0 ? limits : [16]));
}

export function clampTitle(value: string, platforms: string[]) {
  return Array.from(value).slice(0, titleLimitForPlatforms(platforms)).join("");
}

export function titleLength(value: string) {
  return Array.from(value).length;
}

export function buildDraftFromItem(item: PublishBatchItem): PublishDraft {
  const scheduledAtLocal = item.scheduled_at ? toDatetimeLocal(item.scheduled_at) : "";
  return {
    title: item.title,
    description: item.description,
    platforms: [item.platform],
    selected: item.selected,
    tagsInput: (item.tags ?? []).join(" "),
    location: item.location ?? "",
    scheduleMode: scheduledAtLocal ? "scheduled" : "immediate",
    scheduledAt: scheduledAtLocal,
    frameTimeSec: 0,
  };
}

/** Convert a draft into the publish-item PATCH payload, persisting copy + platform
 *  payload fields (tags / location / schedule) instead of discarding them. */
export function itemPatchFromDraft(draft: PublishDraft) {
  return {
    title: draft.title,
    description: draft.description,
    selected: draft.selected,
    tags: parseTags(draft.tagsInput),
    location: draft.location || null,
    scheduled_at:
      draft.scheduleMode === "scheduled" && draft.scheduledAt
        ? new Date(draft.scheduledAt).toISOString()
        : null,
  };
}

function toDatetimeLocal(iso: string): string {
  // Render an ISO timestamp as a <input type="datetime-local"> value (no tz suffix).
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function buildDraftsFromBatch(batch?: PublishBatch | null) {
  const drafts: Record<string, PublishDraft> = {};
  (batch?.items ?? []).forEach((item) => {
    drafts[item.id] = buildDraftFromItem(item);
  });
  return drafts;
}

export function summarizePlatforms(platforms: string[]) {
  if (platforms.length === 0) return "未选择平台";
  return platforms.map(platformLabel).join(" / ");
}

export function formatPublishMode(mode: BatchDefaults["scheduleMode"], scheduledAt: string) {
  if (mode === "scheduled") return scheduledAt ? `定时 ${scheduledAt.replace("T", " ")}` : "定时未设置";
  return "立即发布";
}

export function parseTags(input: string) {
  return input
    .split(/[,，\s#]+/)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

export function isBatchActive(batch?: PublishBatch | null) {
  return batch?.status === "draft" || batch?.status === "processing" || batch?.status === "publishing";
}

export function itemCanPublish(item: PublishBatchItem) {
  return item.selected && !["published", "publishing", "excluded"].includes(item.status);
}

export function itemCanRetry(item: PublishBatchItem) {
  return item.status === "publish_failed";
}

/** 仅 http(s) 或站内相对路径可直接作为浏览器资源 URL；内部 scheme（local:// 等）回退占位。 */
export function toDisplayUrl(url: string | null | undefined): string | null {
  return sanitizeDisplayUrl(url);
}
