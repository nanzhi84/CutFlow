import { describe, expect, it, vi } from "vitest";
import {
  ObjectStoreUploadError,
  type CompleteUploadResponse,
  type PrepareUploadResponse,
  type ResumeUploadResponse,
  type UploadSession,
} from "../api/client";
import {
  UploadManager,
  missingPartNumbers,
  type UploadManagerEvent,
  type UploadTransport,
} from "./manager";
import type {
  StagingResult,
  StoredUploadTask,
  UploadMetadataStore,
  UploadStagingStore,
} from "./storage";

class MemoryMetadata implements UploadMetadataStore {
  readonly tasks = new Map<string, StoredUploadTask>();
  readonly history: StoredUploadTask[] = [];

  constructor(task?: StoredUploadTask) {
    if (task) this.tasks.set(task.clientUploadId, task);
  }

  async put(task: StoredUploadTask) {
    const copy = structuredClone(task);
    this.tasks.set(task.clientUploadId, copy);
    this.history.push(copy);
  }

  async get(clientUploadId: string) {
    return this.tasks.get(clientUploadId);
  }

  async listForUser(userId: string) {
    return Array.from(this.tasks.values()).filter((task) => task.userId === userId);
  }

  async remove(clientUploadId: string) {
    this.tasks.delete(clientUploadId);
  }
}

class MemoryStaging implements UploadStagingStore {
  removed = false;

  constructor(private readonly file: File) {}

  async ensureCapacity() {}

  async stage(): Promise<StagingResult> {
    throw new Error("unexpected stage");
  }

  async recover(): Promise<StagingResult> {
    throw new Error("unexpected recover");
  }

  async open() {
    return this.file;
  }

  async remove() {
    this.removed = true;
  }
}

function session(status: UploadSession["status"]): UploadSession {
  return {
    id: "upl_server",
    client_upload_id: "client_upload_recovery",
    owner_user_id: "user_a",
    kind: "video",
    filename: "recovery.mp4",
    content_type: "video/mp4",
    size_bytes: 20,
    status,
    upload_strategy: "multipart",
    part_size_bytes: 8,
    part_count: 3,
    stabilize: false,
    stabilized: false,
    normalized: false,
    completion_metadata: {},
    retry_count: 0,
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    schema_version: "1.0",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  } as UploadSession;
}

function resume(
  status: UploadSession["status"],
  completedParts: number[] = [],
): ResumeUploadResponse {
  return {
    upload_session: session(status),
    completed_parts: completedParts.map((partNumber) => ({
      part_number: partNumber,
      etag: `etag-${partNumber}`,
      size_bytes: partNumber === 3 ? 4 : 8,
    })),
    artifact:
      status === "ready"
        ? {
            artifact_id: "art_ready",
            kind: "video",
            uri: "s3://materials/recovery.mp4",
            schema_version: "1.0",
          }
        : null,
    media_asset: null,
    publish_package: null,
    request_id: "req_resume",
  } as ResumeUploadResponse;
}

describe("missingPartNumbers", () => {
  it("uses the server part set as the source of truth", () => {
    expect(missingPartNumbers(5, [2, 4])).toEqual([1, 3, 5]);
  });
});

describe("UploadManager recovery", () => {
  it("blocks page unload only while the original file is being durably staged", async () => {
    let markStageStarted!: () => void;
    let finishStage!: (result: StagingResult) => void;
    let markPrepareStarted!: () => void;
    let failPrepare!: (error: Error) => void;
    const stageStarted = new Promise<void>((resolve) => {
      markStageStarted = resolve;
    });
    const staged = new Promise<StagingResult>((resolve) => {
      finishStage = resolve;
    });
    const prepareStarted = new Promise<void>((resolve) => {
      markPrepareStarted = resolve;
    });
    const prepared = new Promise<PrepareUploadResponse>((_resolve, reject) => {
      failPrepare = reject;
    });
    const metadata = new MemoryMetadata();
    const staging: UploadStagingStore = {
      ensureCapacity: async () => undefined,
      stage: async () => {
        markStageStarted();
        return staged;
      },
      recover: async () => ({ complete: false }),
      open: async () => new File([], "unused.bin"),
      remove: async () => undefined,
    };
    const manager = new UploadManager({
      metadata,
      staging,
      transport: {
        prepare: async () => {
          markPrepareStarted();
          return prepared;
        },
        resume: async () => resume("uploading"),
        signParts: async () => ({ upload_session: session("uploading"), parts: [] }),
        objectComplete: async () => undefined,
        put: async () => null,
      },
      sleep: async () => undefined,
    });

    manager.setUser("user_a");
    const upload = manager.start("user_a", {
      file: new File(["payload"], "staging.mp4", { type: "video/mp4" }),
      kind: "video",
    });
    await stageStarted;

    const duringStaging = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(duringStaging);
    expect(duringStaging.defaultPrevented).toBe(true);

    finishStage({ complete: true, sha256: "abc123", sizeBytes: 7 });
    await prepareStarted;
    const duringRemotePrepare = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(duringRemotePrepare);
    expect(duringRemotePrepare.defaultPrevented).toBe(false);

    failPrepare(new Error("network unavailable"));
    await expect(upload.promise).rejects.toThrow("network unavailable");
    await vi.waitFor(() => expect(metadata.tasks.size).toBe(1));
  });

  it("resumes only missing parts, refreshes one expired URL, and cleans terminal local state", async () => {
    const task: StoredUploadTask = {
      clientUploadId: "client_upload_recovery",
      userId: "user_a",
      opfsPath: "cutagent-uploads-v1/user_a/client_upload_recovery.bin",
      filename: "recovery.mp4",
      contentType: "video/mp4",
      sizeBytes: 20,
      sha256: "abc123",
      kind: "video",
      caseId: "case_1",
      metadata: { title: "recovery.mp4" },
      stabilize: false,
      status: "uploading",
      sessionId: "upl_server",
      strategy: "multipart",
      partSizeBytes: 8,
      partCount: 3,
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
    };
    const metadata = new MemoryMetadata(task);
    const staging = new MemoryStaging(new File([new Uint8Array(20)], task.filename));
    const signCalls: number[][] = [];
    const uploadedSizes: number[] = [];
    let resumeCall = 0;
    let firstPartAttempt = true;
    const transport: UploadTransport = {
      prepare: async (payload) => {
        expect(payload.client_upload_id).toBe(task.clientUploadId);
        return {
          upload_session: session("uploading"),
          upload_strategy: "multipart",
          part_size_bytes: 8,
          part_count: 3,
          put_url: null,
          put_content_type: "video/mp4",
          expires_at: null,
        } as PrepareUploadResponse;
      },
      resume: async () => {
        resumeCall += 1;
        return resumeCall < 3 ? resume("uploading", [2]) : resume("ready", [1, 2, 3]);
      },
      signParts: async (_uploadSessionId, partNumbers) => {
        signCalls.push(partNumbers);
        return {
          upload_session: session("uploading"),
          parts: partNumbers.map((partNumber) => ({
            part_number: partNumber,
            put_url: `https://objects.test/part-${partNumber}-${signCalls.length}`,
          })),
        };
      },
      objectComplete: async (_uploadSessionId, payload) => {
        expect(payload).toEqual({
          size_bytes: 20,
          sha256: "abc123",
          metadata: { title: "recovery.mp4" },
        });
      },
      put: async (url, body, _contentType, onProgress) => {
        if (url.includes("part-1-1") && firstPartAttempt) {
          firstPartAttempt = false;
          throw new ObjectStoreUploadError(403, "expired");
        }
        uploadedSizes.push(body.size);
        onProgress?.(body.size, body.size);
        return `etag-${url}`;
      },
    };
    const manager = new UploadManager({
      metadata,
      staging,
      transport,
      sleep: async () => undefined,
      partConcurrency: 3,
    });
    const completed = new Promise<CompleteUploadResponse>((resolve, reject) => {
      manager.subscribe((event: UploadManagerEvent) => {
        if (event.status === "completed" && event.result) resolve(event.result);
        if (event.status === "failed" && event.error) reject(event.error);
      });
    });

    manager.setUser("user_a");
    const result = await completed;

    expect(result.artifact.artifact_id).toBe("art_ready");
    expect(signCalls).toContainEqual([1, 3]);
    expect(signCalls).toContainEqual([1]);
    expect(uploadedSizes.sort((left, right) => left - right)).toEqual([4, 8]);
    expect(
      metadata.history.some(
        (record) => record.completedParts?.["1"] !== undefined && record.completedParts?.["3"] !== undefined,
      ),
    ).toBe(true);
    expect(metadata.tasks.size).toBe(0);
    expect(staging.removed).toBe(true);
  });

  it("does not scan another authenticated user's records", async () => {
    const otherTask = {
      clientUploadId: "client_upload_other",
      userId: "user_b",
      opfsPath: "cutagent-uploads-v1/user_b/client_upload_other.bin",
      filename: "other.mp4",
      contentType: "video/mp4",
      sizeBytes: 1,
      sha256: "x",
      kind: "video",
      caseId: null,
      metadata: {},
      stabilize: false,
      status: "uploading",
      sessionId: "upl_other",
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
    } satisfies StoredUploadTask;
    const metadata = new MemoryMetadata(otherTask);
    let resumed = false;
    const manager = new UploadManager({
      metadata,
      staging: new MemoryStaging(new File(["x"], "other.mp4")),
      transport: {
        prepare: async () => {
          throw new Error("unexpected prepare");
        },
        resume: async () => {
          resumed = true;
          return resume("uploading");
        },
        signParts: async () => ({ upload_session: session("uploading"), parts: [] }),
        objectComplete: async () => undefined,
        put: async () => null,
      },
      sleep: async () => undefined,
    });
    manager.setUser("user_a");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(resumed).toBe(false);
    expect(metadata.tasks.has(otherTask.clientUploadId)).toBe(true);
  });

  it("replays a recovered task to late subscribers for the same user only", async () => {
    const task = {
      clientUploadId: "client_upload_recovery",
      userId: "user_a",
      opfsPath: "cutagent-uploads-v1/user_a/client_upload_recovery.bin",
      filename: "recovery.mp4",
      contentType: "video/mp4",
      sizeBytes: 20,
      sha256: "abc123",
      kind: "video",
      caseId: null,
      metadata: {},
      stabilize: false,
      status: "uploading",
      sessionId: "upl_server",
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
    } satisfies StoredUploadTask;
    const metadata = new MemoryMetadata(task);
    const manager = new UploadManager({
      metadata,
      staging: new MemoryStaging(new File([new Uint8Array(20)], task.filename)),
      transport: {
        prepare: async () => {
          throw new Error("unexpected prepare");
        },
        resume: async () => {
          throw new Error("network unavailable");
        },
        signParts: async () => ({ upload_session: session("uploading"), parts: [] }),
        objectComplete: async () => undefined,
        put: async () => null,
      },
      sleep: async () => undefined,
    });

    manager.setUser("user_a");
    await vi.waitFor(() =>
      expect(metadata.history.at(-1)?.lastError).toBe("network unavailable"),
    );

    const sameUserEvents: UploadManagerEvent[] = [];
    const otherUserEvents: UploadManagerEvent[] = [];
    const unsubscribeOther = manager.subscribeForUser("user_b", (event) =>
      otherUserEvents.push(event),
    );
    const unsubscribeSame = manager.subscribeForUser("user_a", (event) =>
      sameUserEvents.push(event),
    );

    expect(otherUserEvents).toEqual([]);
    expect(sameUserEvents).toHaveLength(1);
    expect(sameUserEvents[0]).toMatchObject({
      clientUploadId: task.clientUploadId,
      status: "failed",
      recovered: true,
    });
    unsubscribeOther();
    unsubscribeSame();
  });

  it("fails before prepare when durable local staging cannot be guaranteed", async () => {
    const metadata = new MemoryMetadata();
    let prepared = false;
    const staging: UploadStagingStore = {
      ensureCapacity: async () => {
        throw new Error("浏览器拒绝持久化站点存储");
      },
      stage: async () => {
        throw new Error("unexpected stage");
      },
      recover: async () => ({ complete: false }),
      open: async () => new File([], "never.bin"),
      remove: async () => undefined,
    };
    const manager = new UploadManager({
      metadata,
      staging,
      transport: {
        prepare: async () => {
          prepared = true;
          throw new Error("unexpected prepare");
        },
        resume: async () => resume("uploading"),
        signParts: async () => ({ upload_session: session("uploading"), parts: [] }),
        objectComplete: async () => undefined,
        put: async () => null,
      },
      sleep: async () => undefined,
    });

    manager.setUser("user_a");
    const upload = manager.start("user_a", {
      file: new File(["payload"], "blocked.mp4", { type: "video/mp4" }),
      kind: "video",
    });

    await expect(upload.promise).rejects.toThrow("浏览器拒绝持久化站点存储");
    expect(prepared).toBe(false);
    expect(metadata.tasks.size).toBe(0);
  });
});
