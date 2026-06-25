import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Loader2, Scissors } from "lucide-react";
import { useEffect, useState } from "react";
import { editorHandoffApi, type JianyingDraftResult } from "../../api/r6";
import { toDisplayUrl } from "../../lib/url";
import { useToast } from "../ui/Toast";
import { ConfirmDialog } from "../ui/ConfirmDialog";

type Props = {
  finishedVideoId?: string | null;
  videoDownloadUrl?: string | null;
  compact?: boolean;
};

export function EditorHandoffActions({ finishedVideoId, videoDownloadUrl, compact = false }: Props) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [result, setResult] = useState<JianyingDraftResult | null>(null);
  const queryKey = ["finished-video", finishedVideoId, "jianying-draft-latest"];
  const latest = useQuery({
    queryKey,
    queryFn: () => editorHandoffApi.latestJianyingDraft(finishedVideoId!),
    enabled: Boolean(finishedVideoId),
    staleTime: 30_000,
  });
  const packageResult = result ?? latest.data?.package ?? null;
  const disabled = !finishedVideoId || Boolean(isRunning) || latest.isLoading;

  useEffect(() => {
    setResult(null);
  }, [finishedVideoId]);

  async function runAction() {
    if (!finishedVideoId) return;
    setIsRunning(true);
    try {
      const value = await editorHandoffApi.createJianyingDraft(finishedVideoId, { template_id: null });
      setResult(value);
      queryClient.setQueryData(queryKey, { package: value, request_id: value.package_artifact.artifact_id });
      const downloaded = triggerDownload(value.download_url, downloadFilename(value));
      toast.success("剪映工程包已生成", downloaded ? "下载已开始" : value.package_artifact.artifact_id);
    } catch (error) {
      toast.error("生成剪映工程包失败", error);
    } finally {
      setIsRunning(false);
      setConfirmOpen(false);
    }
  }

  function handleJianyingClick() {
    if (packageResult?.download_url) {
      const downloaded = triggerDownload(packageResult.download_url, downloadFilename(packageResult));
      if (!downloaded) {
        toast.error("剪映工程包下载地址不可用");
      }
      return;
    }
    setConfirmOpen(true);
  }

  if (!finishedVideoId && !videoDownloadUrl) return null;

  return (
    <div className={compact ? "flex flex-wrap items-center gap-2" : "flex flex-wrap items-center gap-2"}>
      {videoDownloadUrl ? (
        <a className="btn-secondary compactButton text-sm no-underline" href={videoDownloadUrl} target="_blank" rel="noopener noreferrer" download>
          <Download className="h-4 w-4" />
          <span>下载 MP4</span>
        </a>
      ) : null}
      <button
        className={`${packageResult ? "btn-secondary" : "btn-primary"} compactButton`}
        type="button"
        disabled={disabled}
        onClick={handleJianyingClick}
      >
        {isRunning || latest.isLoading ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : packageResult ? (
          <Download className="h-4 w-4" />
        ) : (
          <Scissors className="h-4 w-4" />
        )}
        <span>{packageResult ? "下载剪映工程包" : "生成剪映工程包"}</span>
      </button>
      <ConfirmDialog
        isOpen={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        onConfirm={runAction}
        isLoading={isRunning}
        type="info"
        title="确认生成剪映工程包"
        message="系统会基于当前成片和原始素材创建可导入剪映的多轨工程包，不会修改或覆盖原成片文件。"
        consequences={["会新增一个 artifact 记录", "产物会写入对象存储并返回 package URI", "剪映桌面端兼容性需要导入草稿后最终确认"]}
        confirmText="生成工程包"
      />
    </div>
  );
}

type ManifestMap = Record<string, unknown>;

function asRecord(value: unknown): ManifestMap {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as ManifestMap) : {};
}

function readString(record: ManifestMap, key: string): string | null {
  const value = record[key];
  return typeof value === "string" && value ? value : null;
}

function downloadFilename(result: JianyingDraftResult): string {
  const manifest = asRecord(result.draft_manifest);
  const packageUri = readString(manifest, "package_uri") ?? result.package_artifact.uri;
  const lastSegment = packageUri.split(/[\\/]/).pop();
  if (lastSegment?.endsWith(".zip")) return lastSegment;
  const draftName = readString(manifest, "draft_name") ?? result.package_artifact.artifact_id;
  return `${draftName}.zip`;
}

function triggerDownload(url: string | null | undefined, filename: string): boolean {
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
