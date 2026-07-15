import { describe, expect, it } from "vitest";
import { IncrementalSha256 } from "./sha256";

const encode = (value: string) => new TextEncoder().encode(value);

describe("IncrementalSha256", () => {
  it("matches the standard empty and abc vectors", () => {
    expect(new IncrementalSha256().hex()).toBe(
      "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    );
    expect(new IncrementalSha256().update(encode("abc")).hex()).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });

  it("is independent of chunk boundaries", () => {
    const hasher = new IncrementalSha256();
    hasher.update(encode("a"));
    hasher.update(encode("bc"));
    expect(hasher.hex()).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });

  it("hashes one million bytes incrementally", () => {
    const hasher = new IncrementalSha256();
    const chunk = encode("a".repeat(1_000));
    for (let index = 0; index < 1_000; index += 1) hasher.update(chunk);
    expect(hasher.hex()).toBe("cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");
  });
});
