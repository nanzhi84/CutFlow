import { IncrementalSha256 } from "./sha256";

const CHUNK_SIZE_BYTES = 4 * 1024 * 1024;
const ROOT_DIRECTORY = "cutagent-uploads-v1";

type StageRequest = {
  type: "stage";
  requestId: string;
  userId: string;
  clientUploadId: string;
  file: File;
};

type RecoverRequest = {
  type: "recover";
  requestId: string;
  userId: string;
  clientUploadId: string;
  expectedSizeBytes: number;
};

type WorkerRequest = StageRequest | RecoverRequest;

type WorkerResponse =
  | { type: "progress"; requestId: string; loaded: number; total: number }
  | { type: "completed"; requestId: string; sha256: string; sizeBytes: number }
  | { type: "incomplete"; requestId: string; sizeBytes: number }
  | { type: "error"; requestId: string; message: string };

function safeSegment(value: string): string {
  return encodeURIComponent(value);
}

async function uploadDirectory(userId: string): Promise<FileSystemDirectoryHandle> {
  const root = await navigator.storage.getDirectory();
  const uploads = await root.getDirectoryHandle(ROOT_DIRECTORY, { create: true });
  return uploads.getDirectoryHandle(safeSegment(userId), { create: true });
}

async function hashFile(
  file: Blob,
  requestId: string,
  onChunk?: (chunk: Uint8Array, offset: number) => Promise<void>,
): Promise<string> {
  const hasher = new IncrementalSha256();
  for (let offset = 0; offset < file.size; offset += CHUNK_SIZE_BYTES) {
    const chunk = new Uint8Array(
      await file.slice(offset, Math.min(file.size, offset + CHUNK_SIZE_BYTES)).arrayBuffer(),
    );
    hasher.update(chunk);
    await onChunk?.(chunk, offset);
    post({ type: "progress", requestId, loaded: offset + chunk.byteLength, total: file.size });
  }
  return hasher.hex();
}

async function stage(request: StageRequest): Promise<void> {
  const directory = await uploadDirectory(request.userId);
  const handle = await directory.getFileHandle(`${safeSegment(request.clientUploadId)}.bin`, {
    create: true,
  });
  const writable = await handle.createWritable({ keepExistingData: false });
  try {
    const sha256 = await hashFile(request.file, request.requestId, async (chunk, offset) => {
      const data = new ArrayBuffer(chunk.byteLength);
      new Uint8Array(data).set(chunk);
      await writable.write({ type: "write", position: offset, data });
    });
    await writable.close();
    post({ type: "completed", requestId: request.requestId, sha256, sizeBytes: request.file.size });
  } catch (error) {
    await writable.abort().catch(() => undefined);
    throw error;
  }
}

async function recover(request: RecoverRequest): Promise<void> {
  const directory = await uploadDirectory(request.userId);
  let file: File;
  try {
    const handle = await directory.getFileHandle(`${safeSegment(request.clientUploadId)}.bin`);
    file = await handle.getFile();
  } catch (error) {
    if (error instanceof DOMException && error.name === "NotFoundError") {
      post({ type: "incomplete", requestId: request.requestId, sizeBytes: 0 });
      return;
    }
    throw error;
  }
  if (file.size !== request.expectedSizeBytes) {
    post({ type: "incomplete", requestId: request.requestId, sizeBytes: file.size });
    return;
  }
  const sha256 = await hashFile(file, request.requestId);
  post({ type: "completed", requestId: request.requestId, sha256, sizeBytes: file.size });
}

function post(message: WorkerResponse): void {
  self.postMessage(message);
}

self.onmessage = (event: MessageEvent<WorkerRequest>) => {
  const request = event.data;
  const operation = request.type === "stage" ? stage(request) : recover(request);
  void operation.catch((error: unknown) => {
    post({
      type: "error",
      requestId: request.requestId,
      message: error instanceof Error ? error.message : "本地暂存失败",
    });
  });
};
