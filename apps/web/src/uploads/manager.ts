import {
  ObjectStoreUploadError,
  api,
  putToOss,
  type CompleteUploadResponse,
  type PrepareUploadResponse,
  type ResumeUploadResponse,
  type UploadKind,
  type UploadSession,
} from "../api/client";
import {
  IndexedDbUploadMetadataStore,
  OpfsUploadStagingStore,
  opfsStagingPath,
  type StoredUploadTask,
  type UploadMetadataStore,
  type UploadStagingStore,
} from "./storage";

const REMOTE_PROGRESS_START = 15;
const REMOTE_PROGRESS_END = 90;
const DEFAULT_PART_CONCURRENCY = 3;
const READY_POLL_ATTEMPTS = 180;
const localPreparationTasks = new Set<string>();

if (typeof window !== "undefined") {
  window.addEventListener("beforeunload", (event) => {
    if (localPreparationTasks.size === 0) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

export type UploadFileInput = {
  file: File;
  kind: UploadKind;
  caseId?: string | null;
  metadata?: Record<string, string>;
  stabilize?: boolean;
};

export type UploadManagerStage =
  | "preparing"
  | "uploading"
  | "completing"
  | "completed"
  | "failed";

export type UploadManagerEvent = {
  clientUploadId: string;
  status: UploadManagerStage;
  progress: number;
  session?: UploadSession;
  result?: CompleteUploadResponse;
  error?: Error;
  recovered?: boolean;
};

type PreparePayload = {
  client_upload_id: string;
  kind: UploadKind;
  case_id: string | null;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  stabilize: boolean;
};

type SignedPart = { part_number: number; put_url: string };

export interface UploadTransport {
  prepare(payload: PreparePayload): Promise<PrepareUploadResponse>;
  resume(uploadSessionId: string): Promise<ResumeUploadResponse>;
  signParts(
    uploadSessionId: string,
    partNumbers: number[],
  ): Promise<{ upload_session: UploadSession; parts: SignedPart[] }>;
  objectComplete(
    uploadSessionId: string,
    payload: { size_bytes: number; sha256: string; metadata: Record<string, string> },
  ): Promise<unknown>;
  put(
    url: string,
    body: Blob,
    contentType: string,
    onProgress?: (loaded: number, total: number) => void,
  ): Promise<string | null>;
}

export type UploadManagerDependencies = {
  metadata: UploadMetadataStore;
  staging: UploadStagingStore;
  transport: UploadTransport;
  sleep: (milliseconds: number) => Promise<void>;
  partConcurrency: number;
};

class TerminalUploadError extends Error {}

function productionTransport(): UploadTransport {
  return {
    prepare: (payload) => api.uploads.prepare(payload),
    resume: (uploadSessionId) => api.uploads.resume(uploadSessionId),
    signParts: (uploadSessionId, partNumbers) =>
      api.uploads.signParts(uploadSessionId, { part_numbers: partNumbers }),
    objectComplete: (uploadSessionId, payload) =>
      api.uploads.objectComplete(uploadSessionId, payload),
    put: putToOss,
  };
}

export class UploadManager {
  private readonly dependencies: UploadManagerDependencies;
  private readonly listeners = new Set<(event: UploadManagerEvent) => void>();
  private readonly running = new Map<string, Promise<CompleteUploadResponse>>();
  private readonly taskUsers = new Map<string, string>();
  private readonly recoveredTasks = new Set<string>();
  private readonly latestEvents = new Map<string, UploadManagerEvent>();
  private currentUserId: string | null = null;
  private userGeneration = 0;

  constructor(dependencies: Partial<UploadManagerDependencies> = {}) {
    this.dependencies = {
      metadata: dependencies.metadata ?? new IndexedDbUploadMetadataStore(),
      staging: dependencies.staging ?? new OpfsUploadStagingStore(),
      transport: dependencies.transport ?? productionTransport(),
      sleep: dependencies.sleep ?? ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds))),
      partConcurrency: dependencies.partConcurrency ?? DEFAULT_PART_CONCURRENCY,
    };
  }

  subscribe(listener: (event: UploadManagerEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Subscribe to one user's tasks and replay recoveries that began before mount. */
  subscribeForUser(
    userId: string,
    listener: (event: UploadManagerEvent) => void,
  ): () => void {
    const scoped = (event: UploadManagerEvent) => {
      if (this.taskUsers.get(event.clientUploadId) === userId) listener(event);
    };
    this.listeners.add(scoped);
    this.latestEvents.forEach(scoped);
    return () => this.listeners.delete(scoped);
  }

  /** Select the authenticated namespace and resume only that user's local records. */
  setUser(userId: string | null): void {
    if (this.currentUserId === userId) return;
    this.currentUserId = userId;
    this.userGeneration += 1;
    if (!userId) return;
    const generation = this.userGeneration;
    void this.recoverUser(userId, generation);
  }

  start(userId: string, input: UploadFileInput): {
    clientUploadId: string;
    promise: Promise<CompleteUploadResponse>;
  } {
    if (this.currentUserId !== userId) this.setUser(userId);
    const clientUploadId = createClientUploadId();
    const now = new Date().toISOString();
    const task: StoredUploadTask = {
      clientUploadId,
      userId,
      opfsPath: opfsStagingPath(userId, clientUploadId),
      filename: input.file.name,
      contentType: guessContentType(input.file),
      sizeBytes: input.file.size,
      kind: input.kind,
      caseId: input.caseId ?? null,
      metadata: input.metadata ?? {},
      stabilize: input.stabilize ?? false,
      status: "staging",
      createdAt: now,
      updatedAt: now,
    };
    this.taskUsers.set(clientUploadId, userId);
    this.recoveredTasks.delete(clientUploadId);
    const generation = this.userGeneration;
    const promise = this.track(
      clientUploadId,
      this.stageAndRun(task, input.file, generation),
    );
    return { clientUploadId, promise };
  }

  private async recoverUser(userId: string, generation: number): Promise<void> {
    let tasks: StoredUploadTask[];
    try {
      tasks = await this.dependencies.metadata.listForUser(userId);
    } catch {
      return;
    }
    for (const task of tasks.sort((left, right) => left.createdAt.localeCompare(right.createdAt))) {
      if (!this.isActiveUser(userId, generation)) return;
      if (this.running.has(task.clientUploadId)) continue;
      this.taskUsers.set(task.clientUploadId, task.userId);
      this.recoveredTasks.add(task.clientUploadId);
      const promise = this.track(task.clientUploadId, this.recoverAndRun(task, generation));
      void promise.catch(() => undefined);
    }
  }

  private async stageAndRun(
    task: StoredUploadTask,
    file: File,
    generation: number,
  ): Promise<CompleteUploadResponse> {
    localPreparationTasks.add(task.clientUploadId);
    try {
      await this.dependencies.staging.ensureCapacity(file.size);
      this.assertActiveUser(task, generation);
      await this.dependencies.metadata.put(task);
      this.emit({ clientUploadId: task.clientUploadId, status: "preparing", progress: 0 });
      const staged = await this.dependencies.staging.stage(
        task.userId,
        task.clientUploadId,
        file,
        (loaded, total) =>
          this.emit({
            clientUploadId: task.clientUploadId,
            status: "preparing",
            progress: total ? Math.round((loaded / total) * REMOTE_PROGRESS_START) : 0,
          }),
      );
      if (!staged.complete || staged.sizeBytes !== task.sizeBytes) {
        throw new TerminalUploadError("本地暂存未完整写入，请重新选择文件");
      }
      task = await this.patchTask(task, {
        sha256: staged.sha256,
        status: "staged",
        lastError: undefined,
      });
      localPreparationTasks.delete(task.clientUploadId);
      return await this.runTask(task, generation, false);
    } catch (error) {
      const cannotRecoverLocally = !task.sha256 && !task.sessionId;
      return this.handleFailure(
        task,
        error,
        error instanceof TerminalUploadError || cannotRecoverLocally,
      );
    } finally {
      localPreparationTasks.delete(task.clientUploadId);
    }
  }

  private async recoverAndRun(
    task: StoredUploadTask,
    generation: number,
  ): Promise<CompleteUploadResponse> {
    try {
      this.assertActiveUser(task, generation);
      if (task.status === "staging" || !task.sha256) {
        localPreparationTasks.add(task.clientUploadId);
        const recovered = await this.dependencies.staging.recover(
          task.userId,
          task.clientUploadId,
          task.sizeBytes,
          (loaded, total) =>
            this.emit({
              clientUploadId: task.clientUploadId,
              status: "preparing",
              progress: total ? Math.round((loaded / total) * REMOTE_PROGRESS_START) : 0,
              recovered: true,
            }),
        );
        if (!recovered.complete || recovered.sizeBytes !== task.sizeBytes) {
          throw new TerminalUploadError("崩溃前本地暂存尚未完成，请重新选择原文件");
        }
        task = await this.patchTask(task, {
          sha256: recovered.sha256,
          status: "staged",
          lastError: undefined,
        });
        localPreparationTasks.delete(task.clientUploadId);
      }
      return await this.runTask(task, generation, true);
    } catch (error) {
      const cannotRecoverLocally = !task.sha256 && !task.sessionId;
      return this.handleFailure(
        task,
        error,
        error instanceof TerminalUploadError || cannotRecoverLocally,
      );
    } finally {
      localPreparationTasks.delete(task.clientUploadId);
    }
  }

  private async runTask(
    task: StoredUploadTask,
    generation: number,
    recovered: boolean,
  ): Promise<CompleteUploadResponse> {
    this.assertActiveUser(task, generation);
    if (!task.sha256) throw new TerminalUploadError("上传记录缺少 SHA-256");

    let ticket: PrepareUploadResponse | undefined;
    let resume: ResumeUploadResponse | undefined;
    if (task.sessionId) {
      resume = await this.dependencies.transport.resume(task.sessionId);
      const terminal = await this.handleServerStatus(task, resume, recovered);
      if (terminal) return terminal;
      if (isServerProcessing(resume.upload_session.status)) {
        return this.waitUntilReady(task, recovered, generation);
      }
    }

    task = await this.patchTask(task, { status: "preparing", lastError: undefined });
    ticket = await this.dependencies.transport.prepare({
      client_upload_id: task.clientUploadId,
      kind: task.kind,
      case_id: task.caseId,
      filename: task.filename,
      content_type: task.contentType,
      size_bytes: task.sizeBytes,
      sha256: task.sha256!,
      stabilize: task.stabilize,
    });
    task = await this.patchTask(task, {
      sessionId: ticket.upload_session.id,
      strategy: ticket.upload_strategy,
      partSizeBytes: ticket.part_size_bytes,
      partCount: ticket.part_count,
      status: "uploading",
    });
    this.assertActiveUser(task, generation);

    resume = await this.dependencies.transport.resume(ticket.upload_session.id);
    const terminal = await this.handleServerStatus(task, resume, recovered);
    if (terminal) return terminal;
    if (isServerProcessing(resume.upload_session.status)) {
      return this.waitUntilReady(task, recovered, generation);
    }

    const stagedFile = await this.dependencies.staging.open(task.userId, task.clientUploadId);
    if (stagedFile.size !== task.sizeBytes) {
      throw new TerminalUploadError("OPFS 暂存文件大小与上传登记不一致");
    }
    this.emit({
      clientUploadId: task.clientUploadId,
      status: "uploading",
      progress: REMOTE_PROGRESS_START,
      session: ticket.upload_session,
      recovered,
    });
    if (ticket.upload_strategy === "multipart") {
      await this.uploadMultipart(task, stagedFile, resume, generation, recovered);
    } else {
      await this.uploadSingle(task, stagedFile, ticket, generation, recovered);
    }

    this.assertActiveUser(task, generation);
    task = await this.patchTask(task, { status: "completing" });
    this.emit({
      clientUploadId: task.clientUploadId,
      status: "completing",
      progress: 95,
      session: ticket.upload_session,
      recovered,
    });
    await this.dependencies.transport.objectComplete(task.sessionId!, {
      size_bytes: task.sizeBytes,
      sha256: task.sha256!,
      metadata: task.metadata,
    });
    return this.waitUntilReady(task, recovered, generation);
  }

  private async uploadSingle(
    task: StoredUploadTask,
    file: File,
    initialTicket: PrepareUploadResponse,
    generation: number,
    recovered: boolean,
  ): Promise<void> {
    let ticket = initialTicket;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      if (!ticket.put_url) throw new Error("服务端未返回单文件上传地址");
      try {
        await this.dependencies.transport.put(
          ticket.put_url,
          file,
          ticket.put_content_type,
          (loaded, total) => {
            this.assertActiveUser(task, generation);
            this.emit({
              clientUploadId: task.clientUploadId,
              status: "uploading",
              progress: uploadProgress(loaded, total),
              session: ticket.upload_session,
              recovered,
            });
          },
        );
        return;
      } catch (error) {
        if (attempt === 0 && isExpiredSignature(error)) {
          ticket = await this.dependencies.transport.prepare({
            client_upload_id: task.clientUploadId,
            kind: task.kind,
            case_id: task.caseId,
            filename: task.filename,
            content_type: task.contentType,
            size_bytes: task.sizeBytes,
            sha256: task.sha256!,
            stabilize: task.stabilize,
          });
          continue;
        }
        throw error;
      }
    }
  }

  private async uploadMultipart(
    task: StoredUploadTask,
    file: File,
    resume: ResumeUploadResponse,
    generation: number,
    recovered: boolean,
  ): Promise<void> {
    const partSize = task.partSizeBytes;
    const partCount = task.partCount;
    if (!partSize || !partCount || !task.sessionId) {
      throw new TerminalUploadError("多段上传缺少分片参数");
    }
    const sessionId = task.sessionId;
    // The server's ListParts response is authoritative; no local completion cache is trusted.
    const completed = new Set((resume.completed_parts ?? []).map((part) => part.part_number));
    const confirmedParts: Record<string, string> = Object.fromEntries(
      (resume.completed_parts ?? []).map((part) => [String(part.part_number), part.etag]),
    );
    task = await this.patchTask(task, { completedParts: { ...confirmedParts } });
    const missing = missingPartNumbers(partCount, completed);
    if (missing.length === 0) return;

    const signed = await this.dependencies.transport.signParts(sessionId, missing);
    const urls = new Map(signed.parts.map((part) => [part.part_number, part.put_url]));
    const queue = missing.filter((partNumber) => urls.has(partNumber));
    const baseBytes = Array.from(completed).reduce(
      (total, partNumber) => total + expectedPartSize(task.sizeBytes, partSize, partNumber),
      0,
    );
    const activeProgress = new Map<number, number>();
    let cacheWrite = Promise.resolve();
    const persistPart = (partNumber: number, etag: string | null) => {
      confirmedParts[String(partNumber)] = etag ?? "";
      const snapshot = { ...confirmedParts };
      cacheWrite = cacheWrite.then(async () => {
        const latest =
          (await this.dependencies.metadata.get(task.clientUploadId)) ?? task;
        await this.patchTask(latest, { completedParts: snapshot });
      });
      return cacheWrite;
    };
    const report = () => {
      const loaded = baseBytes + Array.from(activeProgress.values()).reduce((sum, value) => sum + value, 0);
      this.emit({
        clientUploadId: task.clientUploadId,
        status: "uploading",
        progress: uploadProgress(loaded, task.sizeBytes),
        session: signed.upload_session,
        recovered,
      });
    };

    const uploadNext = async () => {
      while (queue.length > 0) {
        const partNumber = queue.shift();
        if (partNumber === undefined) return;
        this.assertActiveUser(task, generation);
        const start = (partNumber - 1) * partSize;
        const end = Math.min(file.size, start + partSize);
        const body = file.slice(start, end, task.contentType);
        let url = urls.get(partNumber);
        if (!url) continue;
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            const etag = await this.dependencies.transport.put(url, body, task.contentType, (loaded) => {
              activeProgress.set(partNumber, loaded);
              report();
            });
            activeProgress.set(partNumber, body.size);
            await persistPart(partNumber, etag);
            report();
            break;
          } catch (error) {
            if (attempt === 0 && isExpiredSignature(error)) {
              const refreshed = await this.dependencies.transport.signParts(sessionId, [partNumber]);
              url = refreshed.parts[0]?.put_url;
              if (!url) {
                // An empty re-sign response means ListParts now sees this part as complete.
                activeProgress.set(partNumber, body.size);
                await persistPart(partNumber, null);
                report();
                break;
              }
              continue;
            }
            throw error;
          }
        }
      }
    };
    const workers = Math.min(Math.max(1, this.dependencies.partConcurrency), queue.length);
    await Promise.all(Array.from({ length: workers }, () => uploadNext()));
    await cacheWrite;
  }

  private async waitUntilReady(
    task: StoredUploadTask,
    recovered: boolean,
    generation: number,
  ): Promise<CompleteUploadResponse> {
    if (!task.sessionId) throw new TerminalUploadError("上传记录缺少服务端会话 ID");
    for (let attempt = 0; attempt < READY_POLL_ATTEMPTS; attempt += 1) {
      this.assertActiveUser(task, generation);
      const resume = await this.dependencies.transport.resume(task.sessionId);
      const terminal = await this.handleServerStatus(task, resume, recovered);
      if (terminal) return terminal;
      this.emit({
        clientUploadId: task.clientUploadId,
        status: "completing",
        progress: Math.min(99, 95 + Math.floor(attempt / 30)),
        session: resume.upload_session,
        recovered,
      });
      await this.dependencies.sleep(Math.min(2_000, 250 + attempt * 50));
    }
    throw new Error("服务端仍在校验上传，稍后会自动继续恢复");
  }

  private async handleServerStatus(
    task: StoredUploadTask,
    resume: ResumeUploadResponse,
    recovered: boolean,
  ): Promise<CompleteUploadResponse | undefined> {
    const status = resume.upload_session.status;
    if (status === "ready") {
      if (!resume.artifact) throw new Error("上传已 ready，但登记产物缺失");
      const result: CompleteUploadResponse = {
        upload_session: resume.upload_session,
        artifact: resume.artifact,
        media_asset: resume.media_asset ?? null,
        publish_package: resume.publish_package ?? null,
        request_id: resume.request_id,
      };
      await this.cleanup(task);
      this.emit({
        clientUploadId: task.clientUploadId,
        status: "completed",
        progress: 100,
        session: resume.upload_session,
        result,
        recovered,
      });
      this.forgetTask(task.clientUploadId);
      return result;
    }
    if (["rejected", "failed", "cancelled", "expired"].includes(status)) {
      throw new TerminalUploadError(
        resume.upload_session.last_error || `上传已终止（${status}）`,
      );
    }
    return undefined;
  }

  private async patchTask(
    task: StoredUploadTask,
    updates: Partial<StoredUploadTask>,
  ): Promise<StoredUploadTask> {
    const updated = { ...task, ...updates, updatedAt: new Date().toISOString() };
    await this.dependencies.metadata.put(updated);
    return updated;
  }

  private async handleFailure(
    task: StoredUploadTask,
    error: unknown,
    terminal: boolean,
  ): Promise<never> {
    const normalized = error instanceof Error ? error : new Error("上传失败");
    const latest = (await this.dependencies.metadata.get(task.clientUploadId).catch(() => undefined)) ?? task;
    if (terminal) {
      await this.cleanup(latest);
    } else {
      await this.patchTask(latest, { lastError: normalized.message }).catch(() => undefined);
    }
    this.emit({
      clientUploadId: task.clientUploadId,
      status: "failed",
      progress: 0,
      error: normalized,
    });
    if (terminal) this.forgetTask(task.clientUploadId);
    throw normalized;
  }

  private async cleanup(task: StoredUploadTask): Promise<void> {
    await Promise.allSettled([
      this.dependencies.staging.remove(task.userId, task.clientUploadId),
      this.dependencies.metadata.remove(task.clientUploadId),
    ]);
  }

  private assertActiveUser(task: StoredUploadTask, generation: number): void {
    if (!this.isActiveUser(task.userId, generation)) {
      throw new Error("登录账号已切换，上传已暂停并保留恢复记录");
    }
  }

  private isActiveUser(userId: string, generation: number): boolean {
    return this.currentUserId === userId && this.userGeneration === generation;
  }

  private track(
    clientUploadId: string,
    promise: Promise<CompleteUploadResponse>,
  ): Promise<CompleteUploadResponse> {
    this.running.set(clientUploadId, promise);
    void promise.then(
      () => this.running.delete(clientUploadId),
      () => this.running.delete(clientUploadId),
    );
    return promise;
  }

  private emit(event: UploadManagerEvent): void {
    const emitted =
      this.recoveredTasks.has(event.clientUploadId) && event.recovered === undefined
        ? { ...event, recovered: true }
        : event;
    this.latestEvents.set(event.clientUploadId, emitted);
    this.listeners.forEach((listener) => listener(emitted));
  }

  private forgetTask(clientUploadId: string): void {
    this.latestEvents.delete(clientUploadId);
    this.recoveredTasks.delete(clientUploadId);
    this.taskUsers.delete(clientUploadId);
  }
}

function createClientUploadId(): string {
  return `client_upload_${crypto.randomUUID()}`;
}

const EXTENSION_CONTENT_TYPES: Record<string, string> = {
  mp4: "video/mp4",
  mov: "video/quicktime",
  webm: "video/webm",
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  mp3: "audio/mpeg",
  wav: "audio/wav",
  m4a: "audio/mp4",
  aac: "audio/aac",
  ttf: "font/ttf",
  otf: "font/otf",
  woff: "font/woff",
  woff2: "font/woff2",
};

export function guessContentType(file: File): string {
  if (file.type) return file.type;
  const extension = file.name.split(".").pop()?.toLowerCase();
  return (extension && EXTENSION_CONTENT_TYPES[extension]) || "application/octet-stream";
}

export function missingPartNumbers(partCount: number, completedPartNumbers: Iterable<number>): number[] {
  const completed = new Set(completedPartNumbers);
  return Array.from({ length: partCount }, (_, index) => index + 1).filter(
    (partNumber) => !completed.has(partNumber),
  );
}

function isServerProcessing(status: string): boolean {
  return ["completing", "object_completed", "verified"].includes(status);
}

function expectedPartSize(totalSize: number, partSize: number, partNumber: number): number {
  return Math.min(partSize, Math.max(0, totalSize - (partNumber - 1) * partSize));
}

function uploadProgress(loaded: number, total: number): number {
  if (!total) return REMOTE_PROGRESS_START;
  const remoteShare = Math.min(1, Math.max(0, loaded / total));
  return Math.round(REMOTE_PROGRESS_START + remoteShare * (REMOTE_PROGRESS_END - REMOTE_PROGRESS_START));
}

function isExpiredSignature(error: unknown): boolean {
  return error instanceof ObjectStoreUploadError && (error.status === 401 || error.status === 403);
}

export const uploadManager = new UploadManager();
