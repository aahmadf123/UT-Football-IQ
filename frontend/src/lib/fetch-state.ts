"use client";

/**
 * Canonical fetch-state union for backend-backed views.
 *
 * Every view that loads data from the API models its request lifecycle with
 * this union (previously copy-pasted per file):
 *
 *   - `loading` — request in flight (or initial mount)
 *   - `offline` — NEXT_PUBLIC_API_URL is not configured; no request was made
 *   - `error`   — the request failed (network / non-2xx)
 *   - `empty`   — the request succeeded but returned no data
 *   - `ready`   — real data, carried in `data`
 *
 * The union is deliberately explicit so views never render fabricated values:
 * an unwired or empty surface must say so instead of showing sample numbers
 * (repo rule #96 — no mock data masquerading as real).
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type FetchState<T> =
  | { kind: "loading" }
  | { kind: "offline" }
  | { kind: "error"; message: string }
  | { kind: "empty" }
  | { kind: "ready"; data: T };

/**
 * Drive a {@link FetchState} from an async fetcher.
 *
 * The `fetcher` must be referentially stable (wrap it in `useCallback`) — it
 * is the effect dependency, so a new identity re-runs the fetch. `offline` is
 * derived from `NEXT_PUBLIC_API_URL` being unset. `empty` defaults to
 * "resolved to an empty array"; pass `isEmpty` for other payload shapes (or
 * `() => false` to disable the empty state entirely).
 *
 * Stale resolutions are ignored: only the most recent invocation may update
 * state, so a slow earlier request can never clobber a newer result.
 */
export function useFetchState<T>(
  fetcher: () => Promise<T>,
  options?: { isEmpty?: (data: T) => boolean },
): { state: FetchState<T>; reload: () => Promise<void> } {
  const [state, setState] = useState<FetchState<T>>({ kind: "loading" });
  const isEmptyRef = useRef(options?.isEmpty);
  isEmptyRef.current = options?.isEmpty;
  const runRef = useRef(0);

  const reload = useCallback(async () => {
    const run = ++runRef.current;
    if (!process.env.NEXT_PUBLIC_API_URL) {
      setState({ kind: "offline" });
      return;
    }
    setState({ kind: "loading" });
    try {
      const data = await fetcher();
      if (run !== runRef.current) return;
      const empty = isEmptyRef.current
        ? isEmptyRef.current(data)
        : Array.isArray(data) && data.length === 0;
      setState(empty ? { kind: "empty" } : { kind: "ready", data });
    } catch (err) {
      if (run !== runRef.current) return;
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [fetcher]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { state, reload };
}
