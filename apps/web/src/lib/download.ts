import { toDisplayUrl } from "./url";

/**
 * Start a browser download via a synthetic anchor click. Unlike `window.open`,
 * this is not treated as a popup, so it survives popup blockers even inside a
 * loop of downloads. `url` is sanitized through {@link toDisplayUrl}; internal
 * schemes (`local://` 等) 或空值返回 `false` 而不触发下载。
 */
export function triggerDownload(url: string | null | undefined, filename: string): boolean {
  const safeUrl = toDisplayUrl(url);
  if (!safeUrl || typeof document === "undefined") return false;
  const link = document.createElement("a");
  link.href = safeUrl;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  return true;
}
