"use client";

/**
 * A tiny observable clock for a playing <video>.
 *
 * Playback time changes every frame. Keeping it in React state re-rendered
 * the whole review page 30-60 times a second, which made game clips stutter.
 * The video writes here instead, and only the components that need the time
 * subscribe: the overlay per frame, everything else throttled.
 */

import { useEffect, useState, useSyncExternalStore } from "react";

export interface MediaClock {
  get(): number;
  set(seconds: number): void;
  subscribe(listener: () => void): () => void;
}

export function createMediaClock(initial = 0): MediaClock {
  let time = initial;
  const listeners = new Set<() => void>();
  return {
    get: () => time,
    set(seconds) {
      if (!Number.isFinite(seconds) || seconds === time) return;
      time = seconds;
      for (const listener of listeners) listener();
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

/** The clock's time, re-rendering on every change (use for the overlay only). */
export function useMediaTime(clock: MediaClock): number {
  return useSyncExternalStore(clock.subscribe, clock.get, clock.get);
}

/**
 * The clock's time, re-rendering at most once per ``intervalMs`` (with a
 * trailing update so a pause or seek always lands on the exact time).
 */
export function useThrottledMediaTime(clock: MediaClock, intervalMs: number): number {
  const [time, setTime] = useState(clock.get);
  useEffect(() => {
    let last = 0;
    let trailing: number | undefined;
    const update = () => {
      const now = performance.now();
      const wait = intervalMs - (now - last);
      if (wait <= 0) {
        last = now;
        setTime(clock.get());
        return;
      }
      if (trailing === undefined) {
        trailing = window.setTimeout(() => {
          trailing = undefined;
          last = performance.now();
          setTime(clock.get());
        }, wait);
      }
    };
    setTime(clock.get());
    const unsubscribe = clock.subscribe(update);
    return () => {
      unsubscribe();
      if (trailing !== undefined) window.clearTimeout(trailing);
    };
  }, [clock, intervalMs]);
  return time;
}
