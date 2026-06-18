import type { MediaAssetCard } from "../../api/client";
import type { TemplateKind, UploadPlaceholder } from "./libraryModel";
import { toDisplayUrl } from "../../lib/url";

export type UploadFileLike = {
  name: string;
  lastModified?: number;
};

export type PlaceholderIdFactory = (file: UploadFileLike, index: number) => string;

function defaultPlaceholderId(file: UploadFileLike, index: number) {
  const timestamp = file.lastModified ?? Date.now();
  return `${file.name}_${timestamp}_${index}_${Math.random().toString(16).slice(2)}`;
}

export function buildUploadPlaceholders(
  files: readonly UploadFileLike[],
  kind: TemplateKind,
  makeId: PlaceholderIdFactory = defaultPlaceholderId,
): UploadPlaceholder[] {
  return files.map((file, index) => ({
    id: makeId(file, index),
    name: file.name,
    kind,
    status: "uploading",
    progress: 5,
  }));
}

export function addPendingIds(current: ReadonlySet<string>, ids: readonly string[]) {
  const next = new Set(current);
  ids.forEach((id) => {
    if (id) next.add(id);
  });
  return next;
}

export function removePendingId(current: ReadonlySet<string>, id: string) {
  const next = new Set(current);
  next.delete(id);
  return next;
}

export function removePendingIds(current: ReadonlySet<string>, ids: readonly string[]) {
  const next = new Set(current);
  ids.forEach((id) => next.delete(id));
  return next;
}

export function readCardThumbnailUrl(card: Pick<MediaAssetCard, "thumbnail_url" | "asset">) {
  return toDisplayUrl(card.thumbnail_url) ?? toDisplayUrl(card.asset.thumbnail_url);
}
