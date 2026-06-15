import { ImagePlus, Loader2, Sparkles, Trash2, Video } from "lucide-react";
import { useRef, useState } from "react";
import type { PublishBatchItem, PublishPackage } from "../../api/client";
import { useUpload } from "../../hooks/useUpload";
import { useToast } from "../ui/Toast";
import { toDisplayUrl } from "../../lib/url";
import type { PublishDraft } from "./publishModel";

type CoverPanelProps = {
  item: PublishBatchItem;
  draft: PublishDraft;
  publishPackage?: PublishPackage;
  onDraftChange: (patch: Partial<PublishDraft>) => void;
  onCoverArtifact: (packageId: string, artifactId: string | null) => Promise<void>;
};

export function CoverPanel({ item, draft, publishPackage, onDraftChange, onCoverArtifact }: CoverPanelProps) {
  const toast = useToast();
  const upload = useUpload();
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [framePreview, setFramePreview] = useState<string | null>(null);
  const [isCapturing, setIsCapturing] = useState(false);

  const videoUrl = toDisplayUrl(publishPackage?.video_artifact.uri);
  const coverUrl = toDisplayUrl(publishPackage?.cover_artifact?.uri);
  const packageId = publishPackage?.id ?? item.publish_package_id;

  async function seekToFrame() {
    const video = videoRef.current;
    if (!video) throw new Error("视频预览尚未加载");
    const target = Math.max(0, draft.frameTimeSec || 0);
    if (Math.abs(video.currentTime - target) < 0.05) return video;
    await new Promise<void>((resolve, reject) => {
      const cleanup = () => {
        video.removeEventListener("seeked", onSeeked);
        video.removeEventListener("error", onError);
      };
      const onSeeked = () => {
        cleanup();
        resolve();
      };
      const onError = () => {
        cleanup();
        reject(new Error("视频选帧失败"));
      };
      video.addEventListener("seeked", onSeeked, { once: true });
      video.addEventListener("error", onError, { once: true });
      video.currentTime = target;
    });
    return video;
  }

  function drawFrame(video: HTMLVideoElement) {
    const width = video.videoWidth || 720;
    const height = video.videoHeight || 1280;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("浏览器无法创建画布");
    context.drawImage(video, 0, 0, width, height);
    return canvas;
  }

  async function previewFrame() {
    if (!videoUrl) return;
    try {
      setIsCapturing(true);
      const video = await seekToFrame();
      setFramePreview(drawFrame(video).toDataURL("image/jpeg", 0.86));
    } catch (error) {
      toast.error("选帧失败", error);
    } finally {
      setIsCapturing(false);
    }
  }

  async function uploadCoverFile(file: File) {
    if (!publishPackage) {
      toast.error("缺少发布包", "请刷新批次后重试。");
      return;
    }
    const result = await upload.uploadFile({
      file,
      kind: "cover_template",
      caseId: publishPackage.case_id,
      metadata: { title: `${draft.title || "发布封面"}.jpg` },
    });
    await onCoverArtifact(packageId, result.artifact.artifact_id);
    toast.success("封面已上传", draft.title || item.id.slice(0, 8));
  }

  async function useCurrentFrame() {
    if (!videoUrl) return;
    try {
      setIsCapturing(true);
      const video = await seekToFrame();
      const canvas = drawFrame(video);
      setFramePreview(canvas.toDataURL("image/jpeg", 0.86));
      const blob = await new Promise<Blob>((resolve, reject) =>
        canvas.toBlob((value) => (value ? resolve(value) : reject(new Error("封面编码失败"))), "image/jpeg", 0.9),
      );
      await uploadCoverFile(new File([blob], `${item.id}-cover.jpg`, { type: "image/jpeg" }));
    } catch (error) {
      toast.error("封面上传失败", error);
    } finally {
      setIsCapturing(false);
    }
  }

  return (
    <details className="group rounded-2xl border border-dashed border-border bg-white/55 p-4" open>
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-semibold text-text-primary [&::-webkit-details-marker]:hidden">
        <span>封面{coverUrl ? "已设置" : "未设置"}</span>
        <span className="text-xs text-text-tertiary">视频抓帧 / 上传封面</span>
      </summary>
      <div className="mt-4 grid gap-4 lg:grid-cols-[160px_minmax(0,1fr)]">
        <div className="overflow-hidden rounded-2xl border border-border bg-surface">
          {framePreview || coverUrl ? (
            <img src={framePreview ?? coverUrl ?? undefined} alt="封面预览" className="aspect-[3/4] w-full object-cover" />
          ) : (
            <div className="flex aspect-[3/4] items-center justify-center text-text-tertiary">
              <ImagePlus className="h-8 w-8" />
            </div>
          )}
        </div>
        <div className="grid gap-3">
          {videoUrl ? (
            <video ref={videoRef} src={videoUrl} controls preload="metadata" playsInline className="aspect-video w-full rounded-2xl border border-border bg-black object-contain" />
          ) : (
            <div className="flex min-h-28 items-center justify-center rounded-2xl border border-border bg-surface-hover/40 px-4 text-center text-sm text-text-secondary">
              内部视频 URI 已净化，当前环境不能直接预览；可上传本地封面。
            </div>
          )}
          <div className="grid gap-2 md:grid-cols-[160px_auto_auto]">
            <label>
              <span>选帧秒数</span>
              <input
                type="number"
                min="0"
                step="0.1"
                value={draft.frameTimeSec}
                onChange={(event) => onDraftChange({ frameTimeSec: Number(event.target.value) })}
              />
            </label>
            <button className="btn-secondary self-end" type="button" disabled={!videoUrl || isCapturing} onClick={() => void previewFrame()}>
              {isCapturing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Video className="h-4 w-4" />}
              预览帧
            </button>
            <button className="btn-primary self-end" type="button" disabled={!videoUrl || isCapturing || upload.status === "uploading"} onClick={() => void useCurrentFrame()}>
              {isCapturing || upload.status === "uploading" ? <Loader2 className="h-4 w-4 animate-spin" /> : <ImagePlus className="h-4 w-4" />}
              用当前画面
            </button>
          </div>
          <div className="flex flex-wrap gap-2">
            <label className="btn-secondary cursor-pointer">
              <ImagePlus className="h-4 w-4" />
              上传封面
              <input
                type="file"
                className="hidden"
                accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) void uploadCoverFile(file).catch((error) => toast.error("封面上传失败", error));
                  event.currentTarget.value = "";
                }}
              />
            </label>
            <button className="btn-secondary" type="button" disabled={!publishPackage?.cover_artifact} onClick={() => void onCoverArtifact(packageId, null)}>
              <Trash2 className="h-4 w-4" />
              清除封面
            </button>
            <button className="btn-secondary" type="button" disabled title="待接入（依赖 M6c/M6d）">
              <Sparkles className="h-4 w-4" />
              AI 生成封面
            </button>
          </div>
          <p className="rounded-2xl border border-status-warning/25 bg-status-warning/10 p-3 text-xs leading-5 text-status-warning">
            AI 封面待接入（依赖 M6c/M6d）；当前仅支持浏览器选帧或上传封面图。
          </p>
        </div>
      </div>
    </details>
  );
}
