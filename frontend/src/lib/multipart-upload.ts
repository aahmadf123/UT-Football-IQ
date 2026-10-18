/**
 * Large film uploads through the edge Worker's R2 multipart path.
 *
 * A single PUT can't carry full-game film: a Cloudflare Worker rejects request
 * bodies over ~100 MB, and a dropped connection 90% through a 4 GB game would
 * mean starting over. This splits the file into parts uploaded four at a time
 * straight to R2 (`/api/v1/uploads/multipart/*`), retries only a failed part,
 * and re-reads the access token for every request so a long upload survives a
 * token refresh.
 */

import { apiBase } from "./endpoints";
import { ApiError, apiErrorFromResponse, offlineError } from "./api-error";
import type { ObjectUploadResult } from "./api";

/** Files at least this large use multipart. */
export const MULTIPART_THRESHOLD = 50 * 1024 * 1024;
export const PART_SIZE = 32 * 1024 * 1024;
const CONCURRENCY = 4;
const PART_ATTEMPTS = 4;

interface MultipartOptions {
  getToken: () => string | undefined | Promise<string | undefined>;
  refreshToken?: () => string | undefined | Promise<string | undefined>;
  onProgress?: (loaded: number, total: number) => void;
  signal?: AbortSignal;
  partSize?: number;
  /** Test hook: milliseconds to wait before retry ``attempt`` (1-based). */
  backoffMs?: (attempt: number) => number;
}

async function authHeaders(
  getToken: MultipartOptions["getToken"],
  json = false,
  tokenOverride?: string,
): Promise<Record<string, string>> {
  const token = tokenOverride ?? await getToken();
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(json ? { "Content-Type": "application/json" } : {}),
  };
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Upload cancelled", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    if (signal?.aborted) onAbort();
    else signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Upload ``file`` in parts. Returns ``null`` when this server has no multipart
 * route (local development API) so the caller can fall back to a single PUT.
 */
export async function uploadFilmMultipart(file: File, options: MultipartOptions): Promise<ObjectUploadResult | null> {
  const base = `${apiBase()}/api/v1/uploads/multipart`;
  const partSize = options.partSize ?? PART_SIZE;
  const backoff = options.backoffMs ?? ((attempt: number) => 1000 * 2 ** (attempt - 1));
  const action = "upload the film";

  let created: Response;
  try {
    created = await fetch(`${base}/create`, {
      method: "POST",
      headers: await authHeaders(options.getToken, true),
      body: JSON.stringify({ filename: file.name, contentType: file.type || "video/mp4" }),
      signal: options.signal,
    });
  } catch (cause) {
    if (options.signal?.aborted) throw cause;
    throw offlineError(action, cause);
  }
  if (created.status === 404 || created.status === 405) return null;
  if (!created.ok) throw await apiErrorFromResponse(created, action);
  const { key, uploadId } = (await created.json()) as { key: string; uploadId: string };

  const total = file.size;
  const count = Math.max(1, Math.ceil(total / partSize));
  const done: { partNumber: number; etag: string }[] = [];
  let uploaded = 0;
  let refreshedToken: string | undefined;
  let refreshPromise: Promise<string | undefined> | undefined;
  const uploadController = new AbortController();
  const abortFromCaller = () => uploadController.abort(options.signal?.reason);
  if (options.signal?.aborted) abortFromCaller();
  else options.signal?.addEventListener("abort", abortFromCaller, { once: true });
  options.onProgress?.(0, total);

  const refreshExpiredToken = (failedToken: string | undefined) => {
    if (!options.refreshToken) return Promise.resolve(undefined);
    if (refreshedToken && failedToken !== refreshedToken) return Promise.resolve(refreshedToken);
    refreshPromise ??= Promise.resolve()
      .then(() => options.refreshToken!())
      .then((token) => {
        if (token) refreshedToken = token;
        return token;
      })
      .catch(() => undefined);
    return refreshPromise;
  };

  const sendPart = async (partNumber: number) => {
    const blob = file.slice((partNumber - 1) * partSize, Math.min(partNumber * partSize, total));
    for (let attempt = 1; ; attempt++) {
      try {
        const query = new URLSearchParams({ key, uploadId, partNumber: String(partNumber) });
        const token = refreshedToken ?? await options.getToken();
        const response = await fetch(`${base}/part?${query.toString()}`, {
          method: "PUT",
          headers: token ? { Authorization: "Bearer " + token } : {},
          body: blob,
          signal: uploadController.signal,
        });
        if (!response.ok) {
          const error = await apiErrorFromResponse(response, action);
          if (response.status === 401 && options.refreshToken) {
            const nextToken = await refreshExpiredToken(token);
            if (nextToken && nextToken !== token) {
              if (attempt >= PART_ATTEMPTS) throw error;
              continue;
            }
            throw error;
          }
          // Client errors (bad token after refresh, invalid part) won't fix
          // themselves; server/network errors are worth retrying.
          if (response.status < 500 && response.status !== 401 && response.status !== 429) throw error;
          if (attempt >= PART_ATTEMPTS) throw error;
        } else {
          const part = (await response.json()) as { partNumber: number; etag: string };
          done.push({ partNumber: part.partNumber, etag: part.etag });
          uploaded += blob.size;
          options.onProgress?.(uploaded, total);
          return;
        }
      } catch (cause) {
        if (uploadController.signal.aborted || cause instanceof ApiError || attempt >= PART_ATTEMPTS) {
          throw cause instanceof ApiError || uploadController.signal.aborted ? cause : offlineError(action, cause);
        }
      }
      await sleep(backoff(attempt), uploadController.signal);
    }
  };

  try {
    let next = 1;
    let firstError: unknown;
    let hasError = false;
    const workers = Array.from({ length: Math.min(CONCURRENCY, count) }, async () => {
      while (!uploadController.signal.aborted && next <= count) {
        const partNumber = next++;
        try {
          await sendPart(partNumber);
        } catch (error) {
          if (!hasError) {
            firstError = error;
            hasError = true;
          }
          if (!uploadController.signal.aborted) uploadController.abort(error);
          throw error;
        }
      }
    });
    await Promise.allSettled(workers);
    if (hasError) throw firstError;
    if (uploadController.signal.aborted) {
      throw uploadController.signal.reason ?? new DOMException("Upload cancelled", "AbortError");
    }

    const completed = await fetch(`${base}/complete`, {
      method: "POST",
      headers: await authHeaders(options.getToken, true, refreshedToken),
      body: JSON.stringify({ key, uploadId, parts: done.sort((a, b) => a.partNumber - b.partNumber) }),
      signal: uploadController.signal,
    });
    if (!completed.ok) throw await apiErrorFromResponse(completed, action);
    const result = (await completed.json()) as { key: string; size: number; etag: string; storageUri: string };
    return { key: result.key, size: result.size, etag: result.etag, storageUri: result.storageUri };
  } catch (error) {
    // Free the incomplete upload's parts only after all part requests have stopped.
    const query = new URLSearchParams({ key, uploadId });
    await fetch(`${base}/abort?${query.toString()}`, {
      method: "DELETE",
      headers: await authHeaders(options.getToken, false, refreshedToken).catch(() => ({})),
    }).catch(() => undefined);
    throw error;
  } finally {
    options.signal?.removeEventListener("abort", abortFromCaller);
  }
}
