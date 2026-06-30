import { FileAudio, Library, Mic2, Music4, Sparkles } from "lucide-react";
import type { MediaAssetCard, MediaAssetRecord, SignedUrlResponse, VoiceProfile } from "../../api/client";
import { toDisplayUrl } from "../../lib/url";

export const VOICE_UPLOAD_ACCEPT = ".mp3,.wav,.m4a,.aac,.ogg,.flac";

export type LibraryTab = "voices" | "templates" | "fonts" | "bgm" | "ai_source";
export type VoiceSourceFilter = "all" | VoiceProfile["source"];
// Issue #99: visual asset kinds converge to the unified `video` bucket — the
// create/upload path no longer exposes portrait/broll. Legacy portrait/broll
// assets are still merged in for display (see TemplatesTab), but no new visual
// asset is ever created with those kinds.
export type TemplateKind = "video";
export type LibraryAssetKind = "font" | "bgm";

export type UploadPlaceholder = {
  id: string;
  name: string;
  kind: TemplateKind;
  status: "uploading" | "failed";
  progress: number;
  error?: string;
};

export const libraryTabs: Array<{ id: LibraryTab; label: string; to: string; icon: typeof Mic2 }> = [
  { id: "voices", label: "音色", to: "/library/voices", icon: Mic2 },
  { id: "templates", label: "视频模板", to: "/library/templates", icon: Library },
  { id: "ai_source", label: "AI素材", to: "/library/ai-source", icon: Sparkles },
  { id: "fonts", label: "字体", to: "/library/fonts", icon: FileAudio },
  { id: "bgm", label: "BGM", to: "/library/bgm", icon: Music4 },
];

export const voiceSourceLabels: Record<VoiceProfile["source"], string> = {
  builtin: "系统音色",
  cloned: "克隆音色",
  designed: "设计音色",
};

export const templateKindLabels: Record<TemplateKind, string> = {
  video: "视频素材",
};

export const annotationStatusLabels: Record<MediaAssetRecord["annotation_status"], string> = {
  pending: "待标注",
  annotated: "已标注",
  annotation_failed: "标注失败",
};

export const libraryAssetLabels: Record<LibraryAssetKind, string> = {
  font: "字体",
  bgm: "BGM",
};

export function readTab(pathname: string): LibraryTab | null {
  const segment = pathname.split("/").filter(Boolean).at(-1);
  if (segment === "voices" || segment === "templates" || segment === "fonts" || segment === "bgm") return segment;
  if (segment === "ai-source") return "ai_source";
  return null;
}

export function sourceTone(source: VoiceProfile["source"]) {
  if (source === "builtin") return "badge-info";
  if (source === "cloned") return "badge-success";
  return "badge-warning";
}

export type VoiceVendorFilter = "all" | string;

export const vendorLabels: Record<string, string> = {
  minimax: "MiniMax",
  volcengine: "火山豆包",
};

/** Human label for a vendor tag; empty/unknown vendors bucket under 未指定厂商. */
export function vendorLabel(vendor: string): string {
  if (vendorLabels[vendor]) return vendorLabels[vendor];
  return vendor || "未指定厂商";
}

function vendorKeyFromProviderProfileId(providerProfileId?: string | null): string {
  const value = providerProfileId?.trim() ?? "";
  if (!value) return "";
  const head = value.split(".", 1)[0];
  return head === "sandbox" ? "" : head;
}

function voiceVendorSuffix(voice: Pick<VoiceProfile, "vendor" | "provider_profile_id">): string {
  const vendor = voice.vendor.trim() || vendorKeyFromProviderProfileId(voice.provider_profile_id);
  return vendor ? vendorLabel(vendor) : "";
}

export function voiceDisplayLabel(voice: Pick<VoiceProfile, "display_name" | "vendor" | "provider_profile_id">): string {
  const suffix = voiceVendorSuffix(voice);
  return suffix ? `${voice.display_name}（${suffix}）` : voice.display_name;
}

export function vendorTone(vendor: string) {
  if (vendor === "volcengine") return "badge-info";
  if (vendor === "minimax") return "badge-success";
  return "badge-warning";
}

export const voiceStatusLabels: Record<string, string> = {
  ready: "就绪",
  training: "训练中",
  failed: "失败",
};

export function voiceStatusTone(status: string) {
  if (status === "training") return "badge-warning";
  if (status === "failed") return "badge-error";
  return "badge-success";
}

export function annotationTone(status: MediaAssetRecord["annotation_status"]) {
  if (status === "annotated") return "badge-success";
  if (status === "annotation_failed") return "badge-error";
  return "badge-warning";
}

export function collectUsefulTags(items: MediaAssetCard[], excluded: string[]) {
  const excludedSet = new Set(excluded);
  const tags = new Set<string>();
  items.forEach((card) => {
    card.asset.tags?.forEach((tag) => {
      if (!excludedSet.has(tag)) tags.add(tag);
    });
  });
  return Array.from(tags).sort((a, b) => a.localeCompare(b, "zh-Hans-CN"));
}

export function fontFamilyName(assetId: string) {
  return `cutagent-font-${assetId.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

export function uploadStageLabel(status: string) {
  if (status === "preparing") return "准备上传";
  if (status === "uploading") return "传输文件";
  if (status === "completing") return "写入素材";
  if (status === "completed") return "已完成";
  if (status === "failed") return "失败";
  return "等待";
}

// ─────────────────────────────────────────────────────────────────────────────
// Media/preview metadata accessors backed by the generated OpenAPI types:
//   - MediaAssetRecord.thumbnail_url / duration_sec
//   - SignedUrlResponse.content_type / playable (from the preview-url endpoint)
// These wrap the raw fields with sanitization / narrowing for UI consumption.
// ─────────────────────────────────────────────────────────────────────────────

/** Browser-displayable poster/thumbnail for an asset (sanitized), or null. */
export function readAssetThumbnailUrl(asset: MediaAssetRecord): string | null {
  return toDisplayUrl(asset.thumbnail_url);
}

/** Asset media duration in seconds (available even for un-annotated assets), or undefined. */
export function readAssetDurationSec(asset: MediaAssetRecord): number | undefined {
  const raw = asset.duration_sec;
  return typeof raw === "number" && Number.isFinite(raw) ? raw : undefined;
}

/** Preview-url metadata projected from SignedUrlResponse for UI consumption. */
export type PreviewUrlMeta = {
  contentType?: string;
  /**
   * Whether the resolved URL is browser-playable. Tri-state:
   *   true  => backend asserts playable;
   *   false => backend asserts not playable (degrade to placeholder/download);
   *   undefined => backend did not include the field — fall back to URL heuristic.
   */
  playable?: boolean;
};

/**
 * Read content_type / playable from a preview-url (SignedUrlResponse) payload.
 * `playable` is a non-optional boolean in the schema, but we narrow defensively so
 * missing values collapse to `undefined` -> URL heuristic.
 */
export function readPreviewUrlMeta(response: SignedUrlResponse): PreviewUrlMeta {
  const contentType = typeof response.content_type === "string" ? response.content_type : undefined;
  const playable = typeof response.playable === "boolean" ? response.playable : undefined;
  return { contentType, playable };
}
