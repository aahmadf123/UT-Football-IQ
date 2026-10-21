"use client";

/**
 * Film-review transport — the Hudl-style playback controller for Clip Review.
 *
 * Replaces the native `<video controls>` bar with a keyboard-first transport:
 * play/pause, frame stepping, speed shuttle, loop-clip, and prev/next play.
 * All seeks are expressed in *clip-local* seconds and clamped to the clip's
 * bounds; when playback uses the parent video (no rendered clip asset), the
 * clip's `start_time` offset is applied before touching the element.
 *
 * Keyboard map (ignored while typing in a form control):
 *   Space / K        play–pause
 *   J / L            slower / faster (0.25 ·  0.5 · 1 · 1.5 · 2)
 *   ← / →  or , / .  step one frame (pauses first)
 *   Shift+← / →      ±1 second
 *   Home / End       clip start / end
 *   E / Shift+E      next / previous tagged event
 *   [ / ]            previous / next play in the same video
 */

import { useCallback, useEffect, useState, type RefObject } from "react";
import {
  ChevronFirst,
  ChevronLast,
  Pause,
  Play,
  Repeat,
  SkipBack,
  SkipForward,
  StepBack,
  StepForward,
  Volume2,
  VolumeX,
  Maximize,
  Keyboard,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";
import { cn } from "@/lib/utils";

export const PLAYBACK_RATES = [0.25, 0.5, 1, 1.5, 2] as const;

/** Element-timebase seconds for a clip-local time. */
export function toElementTime(localTime: number, clipOffset: number): number {
  return localTime + clipOffset;
}

/**
 * Frame math for stepping: land mid-frame so codec rounding can't slip back
 * to the frame boundary we just left. The current frame is derived with
 * ``floor`` — step targets sit at N+0.5 frames, so rounding would credit the
 * playhead with frame N+1 and make every forward step skip a frame.
 */
export function frameStepTarget(localTime: number, fps: number, delta: number): number {
  const frame = Math.floor(localTime * fps);
  return Math.max(0, (frame + delta + 0.5) / fps);
}

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.closest('[role="slider"]') != null;
}

/**
 * Space/Enter on a focused control activates it natively; handling them in the
 * document keydown too would double-fire (e.g. Space on the play button both
 * clicks it and toggles play again). Other keys stay global.
 */
function isActivationTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.closest('button, a, select, summary, [role="button"]') != null;
}

export function VideoTransport({
  videoRef,
  fps,
  clipOffset,
  duration,
  currentLocalTime,
  eventTimes,
  enforceBounds = false,
  onPrevClip,
  onNextClip,
}: {
  videoRef: RefObject<HTMLVideoElement | null>;
  fps: number;
  /** Clip start in the element's timebase (0 when playing a rendered clip). */
  clipOffset: number;
  /** Clip duration in seconds. */
  duration: number;
  /** Current clip-local playback time in seconds. */
  currentLocalTime: number;
  /** Clip-local times of tagged events, sorted ascending, for E navigation. */
  eventTimes: ReadonlyArray<number>;
  /**
   * Pause at the clip end instead of playing on (parent-video playback would
   * otherwise run into unrelated footage). Loop takes precedence when on.
   */
  enforceBounds?: boolean;
  onPrevClip?: (() => void) | null;
  onNextClip?: (() => void) | null;
}) {
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [loop, setLoop] = useState(false);
  const [muted, setMuted] = useState(false);
  const [fullscreenAvailable, setFullscreenAvailable] = useState(false);
  const readLocalTime = useCallback(() => {
    const el = videoRef.current;
    return el ? Math.max(0, el.currentTime - clipOffset) : currentLocalTime;
  }, [videoRef, clipOffset, currentLocalTime]);

  // Mirror the element's play state (it can change from anywhere).
  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onVolume = () => setMuted(el.muted);
    const onRate = () => setRate(el.playbackRate);
    el.addEventListener("play", onPlay);
    el.addEventListener("pause", onPause);
    el.addEventListener("volumechange", onVolume);
    el.addEventListener("ratechange", onRate);
    el.addEventListener("loadedmetadata", onRate);
    onRate();
    setMuted(el.muted);
    setFullscreenAvailable(Boolean(el.parentElement?.requestFullscreen && document.fullscreenEnabled));
    setPlaying(!el.paused);
    return () => {
      el.removeEventListener("play", onPlay);
      el.removeEventListener("pause", onPause);
      el.removeEventListener("volumechange", onVolume);
      el.removeEventListener("ratechange", onRate);
      el.removeEventListener("loadedmetadata", onRate);
    };
  }, [videoRef]);

  // Clip-end boundary: when playback crosses the end, loop jumps back to the
  // clip start; otherwise enforceBounds pauses at the boundary so parent-video
  // playback doesn't run into unrelated footage.
  useEffect(() => {
    const el = videoRef.current;
    if (!el || duration <= 0 || (!loop && !enforceBounds)) return;
    const boundary = clipOffset + Math.max(0, duration - 0.03);
    const onTime = () => {
      if (el.currentTime < boundary) return;
      if (loop) {
        el.currentTime = clipOffset;
        void el.play().catch(() => undefined);
      } else {
        if (el.paused && Math.abs(el.currentTime - boundary) < 0.001) return;
        el.pause();
        el.currentTime = boundary;
      }
    };
    el.addEventListener("timeupdate", onTime);
    let frameHandle = 0;
    const onFrame = () => {
      onTime();
      frameHandle = el.requestVideoFrameCallback(onFrame);
    };
    if (el.requestVideoFrameCallback) frameHandle = el.requestVideoFrameCallback(onFrame);
    return () => {
      el.removeEventListener("timeupdate", onTime);
      if (frameHandle) el.cancelVideoFrameCallback(frameHandle);
    };
  }, [videoRef, loop, enforceBounds, clipOffset, duration]);

  const seekLocal = useCallback(
    (localTime: number) => {
      const el = videoRef.current;
      if (!el) return;
      const clamped = Math.min(Math.max(localTime, 0), Math.max(duration - 0.01, 0));
      el.currentTime = toElementTime(clamped, clipOffset);
    },
    [videoRef, clipOffset, duration],
  );

  const togglePlay = useCallback(() => {
    const el = videoRef.current;
    if (!el) return;
    if (el.paused) {
      if (el.currentTime >= clipOffset + duration - 0.06) el.currentTime = clipOffset;
      void el.play().catch(() => undefined);
    }
    else el.pause();
  }, [videoRef, clipOffset, duration]);

  const stepFrame = useCallback(
    (delta: number) => {
      const el = videoRef.current;
      if (!el) return;
      el.pause();
      seekLocal(frameStepTarget(readLocalTime(), fps, delta));
    },
    [videoRef, seekLocal, readLocalTime, fps],
  );

  const applyRate = useCallback(
    (next: number) => {
      setRate(next);
      const el = videoRef.current;
      if (el) el.playbackRate = next;
    },
    [videoRef],
  );

  const shuttle = useCallback(
    (direction: 1 | -1) => {
      const index = PLAYBACK_RATES.findIndex((r) => r === rate);
      const nextIndex = Math.min(
        Math.max(index === -1 ? 2 : index + direction, 0),
        PLAYBACK_RATES.length - 1,
      );
      applyRate(PLAYBACK_RATES[nextIndex]);
    },
    [rate, applyRate],
  );

  const jumpEvent = useCallback(
    (direction: 1 | -1) => {
      if (eventTimes.length === 0) return;
      const localTime = readLocalTime();
      // Small epsilon so repeated presses don't get stuck on the same event.
      const next =
        direction === 1
          ? eventTimes.find((t) => t > localTime + 0.05)
          : [...eventTimes].reverse().find((t) => t < localTime - 0.05);
      if (next != null) seekLocal(next);
    },
    [eventTimes, readLocalTime, seekLocal],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
      if ((e.key === " " || e.key === "Enter") && isActivationTarget(e.target)) return;
      switch (e.key) {
        case " ":
        case "k":
        case "K":
          e.preventDefault();
          togglePlay();
          break;
        case "j":
        case "J":
          e.preventDefault();
          shuttle(-1);
          break;
        case "l":
        case "L":
          e.preventDefault();
          shuttle(1);
          break;
        case "ArrowLeft":
          e.preventDefault();
          if (e.shiftKey) seekLocal(readLocalTime() - 1);
          else stepFrame(-1);
          break;
        case "ArrowRight":
          e.preventDefault();
          if (e.shiftKey) seekLocal(readLocalTime() + 1);
          else stepFrame(1);
          break;
        case ",":
          e.preventDefault();
          stepFrame(-1);
          break;
        case ".":
          e.preventDefault();
          stepFrame(1);
          break;
        case "Home":
          e.preventDefault();
          seekLocal(0);
          break;
        case "End":
          e.preventDefault();
          seekLocal(duration);
          break;
        case "e":
          e.preventDefault();
          jumpEvent(e.shiftKey ? -1 : 1);
          break;
        case "E":
          e.preventDefault();
          jumpEvent(-1);
          break;
        case "[":
          e.preventDefault();
          onPrevClip?.();
          break;
        case "]":
          e.preventDefault();
          onNextClip?.();
          break;
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [togglePlay, shuttle, stepFrame, seekLocal, jumpEvent, readLocalTime, duration, onPrevClip, onNextClip]);

  const frame = Math.floor(currentLocalTime * fps);

  return (
    <div
      data-testid="video-transport"
      role="group"
      aria-label="Playback transport"
      className="mt-1 flex flex-wrap items-center gap-1.5"
    >
      <Button
        variant="outline"
        size="icon-sm"
        onClick={() => onPrevClip?.()}
        disabled={!onPrevClip}
        aria-label="Previous play ( [ )"
        title="Previous play  [ "
        data-testid="transport-prev-clip"
      >
        <SkipBack className="size-3.5" />
      </Button>
      <Button
        variant="outline"
        size="icon-sm"
        onClick={() => seekLocal(0)}
        aria-label="Jump to clip start (Home)"
        title="Clip start  Home"
      >
        <ChevronFirst className="size-3.5" />
      </Button>
      <Button
        variant="outline"
        size="icon-sm"
        onClick={() => stepFrame(-1)}
        aria-label="Step back one frame (←)"
        title="Frame back  ← or ,"
        data-testid="transport-frame-back"
      >
        <StepBack className="size-3.5" />
      </Button>
      <Button
        size="icon-sm"
        onClick={togglePlay}
        aria-label={playing ? "Pause (Space)" : "Play (Space)"}
        title="Play/Pause  Space or K"
        data-testid="transport-play"
      >
        {playing ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
      </Button>
      <Button
        variant="outline"
        size="icon-sm"
        onClick={() => stepFrame(1)}
        aria-label="Step forward one frame (→)"
        title="Frame forward  → or ."
        data-testid="transport-frame-forward"
      >
        <StepForward className="size-3.5" />
      </Button>
      <Button
        variant="outline"
        size="icon-sm"
        onClick={() => seekLocal(duration)}
        aria-label="Jump to clip end (End)"
        title="Clip end  End"
      >
        <ChevronLast className="size-3.5" />
      </Button>
      <Button
        variant="outline"
        size="icon-sm"
        onClick={() => onNextClip?.()}
        disabled={!onNextClip}
        aria-label="Next play ( ] )"
        title="Next play  ]"
        data-testid="transport-next-clip"
      >
        <SkipForward className="size-3.5" />
      </Button>

      <span
        data-numeric
        data-testid="transport-readout"
        className="ml-1 font-mono text-xs text-muted-foreground"
      >
        {currentLocalTime.toFixed(2)}s <span className="text-muted-foreground/60">/ {duration.toFixed(1)}s · F{frame}</span>
      </span>

      <div className="ml-auto flex items-center gap-1.5">
        <Button variant="outline" size="icon-sm" aria-label={muted ? "Unmute audio" : "Mute audio"} onClick={() => { const element = videoRef.current; if (element) { element.muted = !element.muted; setMuted(element.muted); } }}>{muted ? <VolumeX className="size-3.5" /> : <Volume2 className="size-3.5" />}</Button>
        <Button variant="outline" size="icon-sm" aria-label="Full screen" disabled={!fullscreenAvailable} title={fullscreenAvailable ? "Full screen" : "Full screen is unavailable in this browser"} onClick={() => { const target = videoRef.current?.parentElement; if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined); else if (target?.requestFullscreen) void target.requestFullscreen().catch(() => undefined); }}><Maximize className="size-3.5" /></Button>
        <NativeSelect
          value={String(rate)}
          onChange={(e) => applyRate(Number(e.target.value))}
          aria-label="Playback speed (J slower, L faster)"
          data-testid="transport-speed"
          className="h-7 text-xs"
        >
          {PLAYBACK_RATES.map((r) => (
            <option key={r} value={String(r)}>
              {r}×
            </option>
          ))}
        </NativeSelect>
        <Button
          variant="outline"
          size="icon-sm"
          onClick={() => setLoop((v) => !v)}
          aria-pressed={loop}
          aria-label="Loop this clip"
          title="Loop clip"
          data-testid="transport-loop"
          className={cn(loop && "border-primary text-primary")}
        >
          <Repeat className="size-3.5" />
        </Button>
        <details className="relative">
          <summary aria-label="Playback keyboard shortcuts" title="Keyboard shortcuts" className="flex size-8 cursor-pointer list-none items-center justify-center rounded-md border border-border-soft text-muted-foreground hover:bg-accent"><Keyboard className="size-3.5" /></summary>
          <div className="absolute right-0 top-10 z-30 w-64 rounded-lg border border-border bg-popover p-3 text-xs shadow-xl"><p className="mb-2 font-semibold">Keyboard shortcuts</p><dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-2 text-muted-foreground"><dt>Play / pause</dt><dd className="font-mono">Space / K</dd><dt>Previous / next frame</dt><dd className="font-mono">← / →</dd><dt>Slower / faster</dt><dd className="font-mono">J / L</dd><dt>Previous / next event</dt><dd className="font-mono">Shift E / E</dd><dt>Previous / next play</dt><dd className="font-mono">[ / ]</dd></dl></div>
        </details>
      </div>
    </div>
  );
}
