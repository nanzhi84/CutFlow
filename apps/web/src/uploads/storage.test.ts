import { afterEach, describe, expect, it, vi } from "vitest";
import { OpfsUploadStagingStore, opfsStagingPath } from "./storage";

const originalStorage = navigator.storage;

afterEach(() => {
  Object.defineProperty(navigator, "storage", {
    configurable: true,
    value: originalStorage,
  });
});

function installStorage({
  persisted = true,
  quota = 300,
  usage = 0,
}: {
  persisted?: boolean;
  quota?: number;
  usage?: number;
} = {}) {
  const estimate = vi.fn(async () => ({ quota, usage }));
  Object.defineProperty(navigator, "storage", {
    configurable: true,
    value: {
      getDirectory: vi.fn(),
      persist: vi.fn(async () => persisted),
      estimate,
    },
  });
  return { estimate };
}

describe("OpfsUploadStagingStore capacity gate", () => {
  it("requires the browser to grant persistent storage", async () => {
    const { estimate } = installStorage({ persisted: false });

    await expect(new OpfsUploadStagingStore().ensureCapacity(100)).rejects.toThrow(
      "拒绝持久化",
    );
    expect(estimate).not.toHaveBeenCalled();
  });

  it("rejects insufficient quota before staging starts", async () => {
    installStorage({ quota: 100, usage: 40 });

    await expect(new OpfsUploadStagingStore().ensureCapacity(61)).rejects.toThrow(
      "持久存储空间不足",
    );
  });

  it("accepts sufficient persistent quota and records a deterministic path", async () => {
    installStorage({ quota: 100, usage: 40 });

    await expect(new OpfsUploadStagingStore().ensureCapacity(60)).resolves.toBeUndefined();
    expect(opfsStagingPath("user/a", "client 1")).toBe(
      "cutagent-uploads-v1/user%2Fa/client%201.bin",
    );
  });
});
