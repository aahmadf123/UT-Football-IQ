interface VersionResponse {
  ok(): boolean;
  json(): Promise<unknown>;
}

/** Rollout failures are polling observations, so the next request can retry. */
export async function readReleaseIdentity(get: () => Promise<VersionResponse>): Promise<string | null> {
  try {
    const response = await get();
    if (!response.ok()) return null;
    const body: unknown = await response.json();
    if (!body || typeof body !== "object" || !("git_sha" in body)) return null;
    return typeof body.git_sha === "string" && /^[a-f0-9]{40}$/.test(body.git_sha) ? body.git_sha : null;
  } catch {
    return null;
  }
}
