import { File, Upload, X } from "lucide-react";
import { useCallback, useState } from "react";
import { resolveAcceptedDropFiles } from "./dropZoneModel";

type DropZoneProps = {
  onFilesDrop: (files: File[], details: { acceptedFiles: File[] }) => void;
  accept?: string;
  multiple?: boolean;
  maxSize?: number;
  className?: string;
  label?: string;
  children?: React.ReactNode;
};

export function DropZone({
  onFilesDrop,
  accept,
  multiple = false,
  maxSize = 200,
  className = "",
  label = "拖拽文件到此处或点击上传",
  children,
}: DropZoneProps) {
  const [isDragging, setIsDragging] = useState(false);
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [error, setError] = useState<string | null>(null);

  const commitFiles = useCallback(
    (files: File[]) => {
      const result = resolveAcceptedDropFiles(files, {
        accept,
        multiple,
        maxSizeMb: maxSize,
        currentFiles: selectedFiles,
      });
      setError(result.error);
      if (result.acceptedFiles.length === 0) return;
      setSelectedFiles(result.files);
      onFilesDrop(result.files, { acceptedFiles: result.acceptedFiles });
    },
    [accept, maxSize, multiple, onFilesDrop, selectedFiles],
  );

  return (
    <div className={className}>
      <label
        onDrop={(event) => {
          event.preventDefault();
          setIsDragging(false);
          commitFiles(Array.from(event.dataTransfer.files));
        }}
        onDragOver={(event) => {
          event.preventDefault();
          setIsDragging(true);
        }}
        onDragLeave={(event) => {
          event.preventDefault();
          setIsDragging(false);
        }}
        className={`flex min-h-[140px] w-full cursor-pointer flex-col items-center justify-center rounded-[24px] border-2 border-dashed py-6 transition-colors ${
          isDragging ? "border-accent bg-accent/5" : "border-border hover:border-accent/50 hover:bg-surface-hover"
        }`}
      >
        {children ?? (
          <div className="flex flex-col items-center justify-center px-4 py-5 text-center">
            <Upload className={`mb-3 h-8 w-8 ${isDragging ? "text-accent" : "text-text-secondary"}`} />
            <p className="text-sm font-medium text-text-primary">{label}</p>
            <p className="mt-1 text-xs text-text-secondary">
              {accept ? `支持格式：${accept}` : "支持所有格式"}，最大 {maxSize}MB
            </p>
          </div>
        )}
        <input
          type="file"
          className="hidden"
          accept={accept}
          multiple={multiple}
          onChange={(event) => {
            commitFiles(Array.from(event.target.files ?? []));
            event.currentTarget.value = "";
          }}
        />
      </label>
      {error ? <p className="mt-2 text-sm text-status-error">{error}</p> : null}
      {selectedFiles.length > 0 ? (
        <div className="mt-4 grid gap-2">
          {selectedFiles.map((file, index) => (
            <div className="flex items-center gap-3 rounded-xl bg-surface-hover p-2" key={`${file.name}-${index}`}>
              <File className="h-4 w-4 text-accent" />
              <span className="flex-1 truncate text-sm text-text-primary">{file.name}</span>
              <span className="text-xs text-text-tertiary">{(file.size / 1024 / 1024).toFixed(2)} MB</span>
              <button
                type="button"
                onClick={() => {
                  const next = selectedFiles.filter((_, itemIndex) => itemIndex !== index);
                  setSelectedFiles(next);
                  onFilesDrop(next, { acceptedFiles: [] });
                }}
                className="rounded-lg p-1 text-text-tertiary hover:bg-surface hover:text-text-primary"
                aria-label="移除文件"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
