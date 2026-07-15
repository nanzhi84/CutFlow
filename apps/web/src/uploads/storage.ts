import type { UploadKind } from "../api/client";

const DATABASE_NAME = "cutagent-upload-recovery";
const DATABASE_VERSION = 1;
const TASK_STORE = "upload_tasks";
const USER_INDEX = "by_user";
const ROOT_DIRECTORY = "cutagent-uploads-v1";

export type StoredUploadStatus =
  | "staging"
  | "staged"
  | "preparing"
  | "uploading"
  | "completing";

/** IndexedDB metadata record. File bytes and Blob values are deliberately absent. */
export type StoredUploadTask = {
  clientUploadId: string;
  userId: string;
  /** Persisted for diagnostics; legacy v1 records can derive the same path. */
  opfsPath?: string;
  filename: string;
  contentType: string;
  sizeBytes: number;
  sha256?: string;
  kind: UploadKind;
  caseId: string | null;
  metadata: Record<string, string>;
  stabilize: boolean;
  status: StoredUploadStatus;
  sessionId?: string;
  strategy?: "single" | "multipart";
  partSizeBytes?: number | null;
  partCount?: number;
  completedParts?: Record<string, string>;
  lastError?: string;
  createdAt: string;
  updatedAt: string;
};

export interface UploadMetadataStore {
  put(task: StoredUploadTask): Promise<void>;
  get(clientUploadId: string): Promise<StoredUploadTask | undefined>;
  listForUser(userId: string): Promise<StoredUploadTask[]>;
  remove(clientUploadId: string): Promise<void>;
}

export class IndexedDbUploadMetadataStore implements UploadMetadataStore {
  private database?: Promise<IDBDatabase>;

  async put(task: StoredUploadTask): Promise<void> {
    const database = await this.open();
    await transactionPromise(database, "readwrite", (store) => store.put(task));
  }

  async get(clientUploadId: string): Promise<StoredUploadTask | undefined> {
    const database = await this.open();
    return transactionPromise(database, "readonly", (store) => store.get(clientUploadId));
  }

  async listForUser(userId: string): Promise<StoredUploadTask[]> {
    const database = await this.open();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(TASK_STORE, "readonly");
      const request = transaction.objectStore(TASK_STORE).index(USER_INDEX).getAll(userId);
      let result: StoredUploadTask[] = [];
      request.onsuccess = () => {
        result = request.result as StoredUploadTask[];
      };
      request.onerror = () => reject(request.error ?? new Error("读取上传恢复记录失败"));
      transaction.onabort = () => reject(transaction.error ?? new Error("读取上传恢复记录失败"));
      transaction.oncomplete = () => resolve(result);
    });
  }

  async remove(clientUploadId: string): Promise<void> {
    const database = await this.open();
    await transactionPromise(database, "readwrite", (store) => store.delete(clientUploadId));
  }

  private open(): Promise<IDBDatabase> {
    this.database ??= new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = () => {
        const database = request.result;
        const store = database.objectStoreNames.contains(TASK_STORE)
          ? request.transaction?.objectStore(TASK_STORE)
          : database.createObjectStore(TASK_STORE, { keyPath: "clientUploadId" });
        if (store && !store.indexNames.contains(USER_INDEX)) {
          store.createIndex(USER_INDEX, "userId", { unique: false });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error ?? new Error("打开上传恢复数据库失败"));
    });
    return this.database;
  }
}

function transactionPromise<T>(
  database: IDBDatabase,
  mode: IDBTransactionMode,
  operation: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(TASK_STORE, mode);
    const request = operation(transaction.objectStore(TASK_STORE));
    let result: T;
    request.onsuccess = () => {
      result = request.result;
    };
    request.onerror = () => reject(request.error ?? new Error("上传恢复记录写入失败"));
    transaction.onabort = () => reject(transaction.error ?? new Error("上传恢复记录写入失败"));
    transaction.oncomplete = () => resolve(result);
  });
}

type StagingWorkerResponse =
  | { type: "progress"; requestId: string; loaded: number; total: number }
  | { type: "completed"; requestId: string; sha256: string; sizeBytes: number }
  | { type: "incomplete"; requestId: string; sizeBytes: number }
  | { type: "error"; requestId: string; message: string };

export type StagingResult = { complete: true; sha256: string; sizeBytes: number } | { complete: false };

export interface UploadStagingStore {
  ensureCapacity(sizeBytes: number): Promise<void>;
  stage(
    userId: string,
    clientUploadId: string,
    file: File,
    onProgress: (loaded: number, total: number) => void,
  ): Promise<StagingResult>;
  recover(
    userId: string,
    clientUploadId: string,
    expectedSizeBytes: number,
    onProgress: (loaded: number, total: number) => void,
  ): Promise<StagingResult>;
  open(userId: string, clientUploadId: string): Promise<File>;
  remove(userId: string, clientUploadId: string): Promise<void>;
}

export class OpfsUploadStagingStore implements UploadStagingStore {
  async ensureCapacity(sizeBytes: number): Promise<void> {
    if (
      !("storage" in navigator) ||
      typeof navigator.storage.getDirectory !== "function" ||
      typeof navigator.storage.persist !== "function" ||
      typeof navigator.storage.estimate !== "function"
    ) {
      throw new Error("当前浏览器不支持 OPFS，无法提供可恢复上传");
    }
    const persisted = await navigator.storage.persist();
    if (!persisted) {
      throw new Error("浏览器拒绝持久化站点存储，无法保证刷新后恢复上传");
    }
    const estimate = await navigator.storage.estimate();
    if (estimate.quota === undefined) {
      throw new Error("浏览器无法确认持久存储配额，上传尚未开始");
    }
    const available = estimate.quota - (estimate.usage ?? 0);
    if (available < sizeBytes) {
      throw new Error(
        `浏览器持久存储空间不足：还需 ${formatMiB(sizeBytes)}，可用 ${formatMiB(available)}`,
      );
    }
  }

  stage(
    userId: string,
    clientUploadId: string,
    file: File,
    onProgress: (loaded: number, total: number) => void,
  ): Promise<StagingResult> {
    return this.runWorker(
      { type: "stage", userId, clientUploadId, file },
      onProgress,
    );
  }

  recover(
    userId: string,
    clientUploadId: string,
    expectedSizeBytes: number,
    onProgress: (loaded: number, total: number) => void,
  ): Promise<StagingResult> {
    return this.runWorker(
      { type: "recover", userId, clientUploadId, expectedSizeBytes },
      onProgress,
    );
  }

  async open(userId: string, clientUploadId: string): Promise<File> {
    const directory = await this.directory(userId, false);
    const handle = await directory.getFileHandle(`${safeSegment(clientUploadId)}.bin`);
    return handle.getFile();
  }

  async remove(userId: string, clientUploadId: string): Promise<void> {
    try {
      const directory = await this.directory(userId, false);
      await directory.removeEntry(`${safeSegment(clientUploadId)}.bin`);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "NotFoundError")) throw error;
    }
  }

  private runWorker(
    payload: Record<string, unknown>,
    onProgress: (loaded: number, total: number) => void,
  ): Promise<StagingResult> {
    const requestId = crypto.randomUUID();
    const worker = new Worker(new URL("./staging.worker.ts", import.meta.url), { type: "module" });
    return new Promise((resolve, reject) => {
      const close = () => worker.terminate();
      worker.onmessage = (event: MessageEvent<StagingWorkerResponse>) => {
        const message = event.data;
        if (message.requestId !== requestId) return;
        if (message.type === "progress") {
          onProgress(message.loaded, message.total);
          return;
        }
        close();
        if (message.type === "completed") {
          resolve({ complete: true, sha256: message.sha256, sizeBytes: message.sizeBytes });
        } else if (message.type === "incomplete") {
          resolve({ complete: false });
        } else {
          reject(new Error(message.message));
        }
      };
      worker.onerror = (event) => {
        close();
        reject(new Error(event.message || "本地暂存 Worker 失败"));
      };
      worker.postMessage({ ...payload, requestId });
    });
  }

  private async directory(userId: string, create: boolean): Promise<FileSystemDirectoryHandle> {
    const root = await navigator.storage.getDirectory();
    const uploads = await root.getDirectoryHandle(ROOT_DIRECTORY, { create });
    return uploads.getDirectoryHandle(safeSegment(userId), { create });
  }
}

function safeSegment(value: string): string {
  return encodeURIComponent(value);
}

export function opfsStagingPath(userId: string, clientUploadId: string): string {
  return `${ROOT_DIRECTORY}/${safeSegment(userId)}/${safeSegment(clientUploadId)}.bin`;
}

function formatMiB(value: number): string {
  return `${Math.max(0, value) / 1024 / 1024}`.replace(/(\.\d{1})\d+$/, "$1") + " MiB";
}
