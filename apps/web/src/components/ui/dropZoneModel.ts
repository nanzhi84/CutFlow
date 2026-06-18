export type DropZoneFileLike = {
  name: string;
  type?: string;
  size: number;
};

type ResolveAcceptedDropFilesOptions<T extends DropZoneFileLike> = {
  accept?: string;
  multiple: boolean;
  maxSizeMb: number;
  currentFiles: T[];
};

export function matchesAccept(file: DropZoneFileLike, accept?: string) {
  if (!accept) return true;
  const tokens = accept
    .split(",")
    .map((token) => token.trim().toLowerCase())
    .filter(Boolean);
  const extension = `.${file.name.split(".").pop()?.toLowerCase() ?? ""}`;
  const mime = (file.type || "").toLowerCase();
  return tokens.some((token) => {
    if (token.startsWith(".")) return extension === token;
    if (token.endsWith("/*")) return mime.startsWith(token.slice(0, -1));
    return mime === token;
  });
}

export function resolveAcceptedDropFiles<T extends DropZoneFileLike>(
  incomingFiles: T[],
  { accept, multiple, maxSizeMb, currentFiles }: ResolveAcceptedDropFilesOptions<T>,
) {
  let error: string | null = null;
  const acceptedFiles = incomingFiles.filter((file) => {
    if (maxSizeMb && file.size > maxSizeMb * 1024 * 1024) {
      error = error ?? `文件大小不能超过 ${maxSizeMb}MB`;
      return false;
    }
    if (!matchesAccept(file, accept)) {
      error = error ?? `不支持的文件类型，请上传 ${accept} 格式的文件`;
      return false;
    }
    return true;
  });

  if (acceptedFiles.length === 0) {
    return { files: currentFiles, acceptedFiles, error };
  }

  return {
    files: multiple ? [...currentFiles, ...acceptedFiles] : [acceptedFiles[0]],
    acceptedFiles,
    error,
  };
}
