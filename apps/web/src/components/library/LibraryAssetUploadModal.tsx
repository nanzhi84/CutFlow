import { Loader2, Upload } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useUpload } from "../../hooks/useUpload";
import { DropZone } from "../ui/DropZone";
import { Modal } from "../ui/Modal";
import { useToast } from "../ui/Toast";
import { libraryAssetLabels, type LibraryAssetKind, uploadStageLabel } from "./libraryModel";

type LibraryAssetUploadModalProps = {
  isOpen: boolean;
  onClose: () => void;
  kind: LibraryAssetKind;
  onUploaded: () => Promise<unknown>;
};

export function LibraryAssetUploadModal({ isOpen, onClose, kind, onUploaded }: LibraryAssetUploadModalProps) {
  const toast = useToast();
  const upload = useUpload();
  const [files, setFiles] = useState<File[]>([]);
  const [tag, setTag] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const accept = kind === "font" ? ".ttf,.otf,.woff,.woff2" : ".mp3,.wav,.m4a,.aac,.ogg,.flac";

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (files.length === 0) {
      setError(`请上传${libraryAssetLabels[kind]}文件`);
      return;
    }
    setError(null);
    setIsSubmitting(true);
    try {
      for (const file of files) {
        await upload.uploadFile({
          file,
          kind,
          metadata: {
            title: file.name,
            style: tag.trim(),
          },
        });
      }
      await onUploaded();
      toast.success("上传完成", `${libraryAssetLabels[kind]}已进入素材库`);
      setFiles([]);
      setTag("");
      upload.reset();
      onClose();
    } catch (err) {
      const message = err instanceof Error ? err.message : "上传失败";
      setError(message);
      toast.error("上传失败", message);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={`上传${libraryAssetLabels[kind]}`} size="lg">
      <form className="grid gap-4" onSubmit={handleSubmit}>
        <DropZone
          accept={accept}
          maxSize={kind === "font" ? 40 : 200}
          multiple={kind === "bgm"}
          onFilesDrop={(nextFiles) => setFiles(nextFiles)}
          label={`上传${libraryAssetLabels[kind]}文件`}
        />
        <label>
          <span>{kind === "font" ? "字体备注" : "风格备注"}</span>
          <input value={tag} onChange={(event) => setTag(event.target.value)} placeholder={kind === "font" ? "例如：标题、字幕、手写" : "例如：轻快、科技、温暖"} />
        </label>
        {upload.status !== "idle" ? (
          <div className="rounded-2xl border border-border/80 bg-white/65 p-3">
            <div className="flex items-center justify-between gap-3 text-sm text-text-secondary">
              <span>{uploadStageLabel(upload.status)}</span>
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
          <button className="btn-primary" type="submit" disabled={isSubmitting || files.length === 0}>
            {isSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            <span>{isSubmitting ? "上传中" : "开始上传"}</span>
          </button>
        </div>
      </form>
    </Modal>
  );
}
