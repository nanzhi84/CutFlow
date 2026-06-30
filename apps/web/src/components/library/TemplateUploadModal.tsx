import { Loader2, Upload } from "lucide-react";
import { useState, type FormEvent } from "react";
import type { UploadKind } from "../../api/client";
import { useUpload } from "../../hooks/useUpload";
import { DropZone } from "../ui/DropZone";
import { Modal } from "../ui/Modal";
import { useToast } from "../ui/Toast";
import { templateKindLabels, type TemplateKind, type UploadPlaceholder, uploadStageLabel } from "./libraryModel";
import { buildUploadPlaceholders } from "./libraryInteractionModel";

type TemplateUploadModalProps = {
  isOpen: boolean;
  onClose: () => void;
  caseId: string | null;
  kind: TemplateKind;
  onPlaceholder: (placeholder: UploadPlaceholder) => void;
  onSuccess: (placeholderId: string) => Promise<void>;
  onAutoReplace: (uploadSessionIds: string[]) => Promise<void>;
};

type UploadMode = "create" | "replace";

export function TemplateUploadModal({
  isOpen,
  onClose,
  caseId,
  kind,
  onPlaceholder,
  onSuccess,
  onAutoReplace,
}: TemplateUploadModalProps) {
  const toast = useToast();
  const upload = useUpload();
  const [files, setFiles] = useState<File[]>([]);
  const [scene, setScene] = useState("");
  const [stabilize, setStabilize] = useState(false);
  const [mode, setMode] = useState<UploadMode>("create");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  // Issue #99: the unified `video` bucket is the only visual upload kind now, so a
  // single broad video accept list (no portrait-specific narrowing).
  const accept = ".mp4,.mov,.m4v,.webm,.avi,.mkv";

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!caseId) {
      setError("请先选择案例");
      return;
    }
    if (files.length === 0) {
      setError("请上传至少一个文件");
      return;
    }
    setError(null);
    setIsSubmitting(true);
    const submittedFiles = [...files];
    const submittedScene = scene.trim();
    const submittedStabilize = stabilize;
    const submittedMode = mode;
    const submittedPlaceholders = buildUploadPlaceholders(submittedFiles, kind);
    submittedPlaceholders.forEach(onPlaceholder);
    setFiles([]);
    setStabilize(false);
    upload.reset();
    onClose();
    setIsSubmitting(false);

    void processUploads(submittedFiles, submittedPlaceholders, {
      scene: submittedScene,
      stabilize: submittedStabilize,
      mode: submittedMode,
      caseId,
    });
  }

  async function processUploads(
    submittedFiles: File[],
    submittedPlaceholders: UploadPlaceholder[],
    options: { scene: string; stabilize: boolean; mode: UploadMode; caseId: string },
  ) {
    const replaceUploadIds: string[] = [];
    const successfulPlaceholders: string[] = [];
    for (let index = 0; index < submittedFiles.length; index += 1) {
      const file = submittedFiles[index];
      const placeholder = submittedPlaceholders[index];
      try {
        const result = await upload.uploadFile({
          file,
          kind: kind as UploadKind,
          caseId: options.caseId,
          stabilize: options.stabilize,
          metadata: {
            title: file.name,
            scene: options.scene,
            ...(options.mode === "replace" ? { template_mode: "replace" } : {}),
          },
        });
        onPlaceholder({ ...placeholder, progress: 100 });
        if (options.mode === "replace") {
          replaceUploadIds.push(result.upload_session.id);
          successfulPlaceholders.push(placeholder.id);
        } else {
          await onSuccess(placeholder.id);
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : "上传失败";
        onPlaceholder({ ...placeholder, status: "failed", progress: 100, error: message });
      }
    }
    if (options.mode === "replace" && replaceUploadIds.length > 0) {
      try {
        await onAutoReplace(replaceUploadIds);
        await Promise.all(successfulPlaceholders.map((placeholderId) => onSuccess(placeholderId)));
      } catch (err) {
        toast.error("自动匹配替换失败", err);
        return;
      }
    }
    toast.success(options.mode === "replace" ? "替换处理完成" : "上传处理完成", "成功素材会进入当前案例网格，失败卡片会保留错误。");
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={`上传${templateKindLabels[kind]}`} size="lg">
      <form className="grid gap-4" onSubmit={handleSubmit}>
        <div className="grid grid-cols-2 gap-2 rounded-2xl border border-border/80 bg-white/65 p-1">
          {(["create", "replace"] as UploadMode[]).map((item) => (
            <button
              key={item}
              type="button"
              className={`rounded-xl px-3 py-2 text-sm font-medium transition ${
                mode === item ? "bg-accent text-white shadow-sm" : "text-text-secondary hover:bg-white"
              }`}
              onClick={() => setMode(item)}
            >
              {item === "create" ? "新增素材" : "自动匹配替换"}
            </button>
          ))}
        </div>
        <DropZone accept={accept} maxSize={100} multiple onFilesDrop={(nextFiles) => setFiles(nextFiles)} label={`上传${templateKindLabels[kind]}文件`} />
        <label>
          <span>统一场景标签</span>
          <input value={scene} onChange={(event) => setScene(event.target.value)} placeholder="例如：办公室、产品特写、生活方式" />
        </label>
        <label className="flex items-center gap-3 rounded-2xl border border-border/80 bg-white/65 p-3">
          <input type="checkbox" checked={stabilize} onChange={(event) => setStabilize(event.target.checked)} />
          <span className="text-sm font-medium text-text-primary">轻微抖动启用防抖</span>
        </label>
        {upload.status !== "idle" ? (
          <div className="rounded-2xl border border-border/80 bg-white/65 p-3">
            <div className="flex items-center justify-between gap-3 text-sm text-text-secondary">
              <span>当前文件：{uploadStageLabel(upload.status)}</span>
              <span>{upload.progress}%</span>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded-full bg-border/70">
              <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${upload.progress}%` }} />
            </div>
          </div>
        ) : null}
        {error ? <p className="text-sm text-status-error">{error}</p> : null}
        <div className="flex justify-end gap-3 border-t border-border/70 pt-4">
          <button className="btn-secondary" type="button" onClick={onClose} disabled={isSubmitting}>
            取消
          </button>
          <button className="btn-primary" type="submit" disabled={isSubmitting || files.length === 0 || !caseId}>
            {isSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            <span>{isSubmitting ? "上传中" : mode === "replace" ? "上传并匹配" : "开始上传"}</span>
          </button>
        </div>
      </form>
    </Modal>
  );
}
