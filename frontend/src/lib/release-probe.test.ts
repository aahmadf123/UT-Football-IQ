import { describe, expect, it } from "vitest";
import { readReleaseIdentity } from "./release-probe";

describe("release identity polling", () => {
  it("survives a dropped rollout request and can observe the next serving build", async () => {
    let attempts = 0;
    const get = async () => {
      if (++attempts === 1) throw new Error("connection reset during rollout");
      return { ok: () => true, json: async () => ({ git_sha: "a".repeat(40) }) };
    };
    expect(await readReleaseIdentity(get)).toBeNull();
    expect(await readReleaseIdentity(get)).toBe("a".repeat(40));
  });

  it("rejects process error responses and malformed build identities", async () => {
    for (const body of [null, {}, { git_sha: "unknown" }, { git_sha: true }]) {
      expect(await readReleaseIdentity(async () => ({ ok: () => true, json: async () => body }))).toBeNull();
    }
    expect(await readReleaseIdentity(async () => ({ ok: () => false, json: async () => ({ git_sha: "a".repeat(40) }) }))).toBeNull();
  });
});
