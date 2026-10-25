"use client";

import { useEffect, useRef, useState } from "react";
import { Expand, Eye, EyeOff, Move, Shrink } from "lucide-react";
import type { OverlayCalibration, OverlayTracklet } from "@/lib/types";
import { FieldMinimap } from "./field-minimap";
import { cn } from "@/lib/utils";

const CORNERS = ["right-2 top-12", "left-2 top-12", "left-2 bottom-2", "right-2 bottom-2"];
const CORNER_NAMES = ["top right", "top left", "bottom left", "bottom right"];
const CONTROL = "flex size-8 shrink-0 items-center justify-center rounded text-slate-100 hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-white";

/** Video-child HUD: stays beside the action through seeking, resize and fullscreen. */
export function FieldInset({ tracklets, seconds, fps, calibration, playerNamesById }: {
  tracklets: OverlayTracklet[]; seconds: number; fps: number;
  calibration: OverlayCalibration | null; playerNamesById?: ReadonlyMap<string, string>;
}) {
  const [corner, setCorner] = useState(3);
  const [expanded, setExpanded] = useState(false);
  const [visible, setVisible] = useState(true);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const previousVisible = useRef(visible);
  useEffect(() => {
    if (previousVisible.current !== visible) toggleRef.current?.focus();
    previousVisible.current = visible;
  }, [visible]);
  return <section aria-label="Field view inside video" data-testid="field-inset" data-corner={corner}
    data-expanded={expanded} data-visible={visible}
    className={cn("absolute z-10 overflow-hidden rounded-md border border-slate-500 bg-[#0b1520] text-white shadow-md", CORNERS[corner], visible ? expanded ? "w-[65%] max-w-sm" : "w-[24%] min-w-40 max-w-52" : "w-auto")}
    onClick={(event) => event.stopPropagation()}>
    {visible ? <>
      <div className="flex items-center justify-between gap-1 border-b border-slate-600 px-1 text-[10px]">
        <span className="pl-1 font-semibold whitespace-nowrap">Field view</span>
        <div className="flex">
          <button type="button" aria-label="Move field view to next corner" title={`Move field view (currently ${CORNER_NAMES[corner]})`} className={CONTROL} onClick={() => setCorner((value) => (value + 1) % CORNERS.length)}><Move className="size-3.5" aria-hidden="true" /></button>
          <button type="button" aria-label={expanded ? "Shrink field view" : "Expand field view"} title={expanded ? "Shrink field view" : "Expand field view"} aria-pressed={expanded} className={CONTROL} onClick={() => setExpanded((value) => !value)}>{expanded ? <Shrink className="size-3.5" aria-hidden="true" /> : <Expand className="size-3.5" aria-hidden="true" />}</button>
          <button ref={toggleRef} type="button" aria-label="Hide field view" title="Hide field view" className={CONTROL} onClick={() => setVisible(false)}><EyeOff className="size-3.5" aria-hidden="true" /></button>
        </div>
      </div>
      <FieldMinimap tracklets={tracklets} currentFrame={Math.round(seconds * fps)} currentTimeSeconds={seconds} fps={fps} calibration={calibration} playerNamesById={playerNamesById} embedded quiet={!expanded} />
    </> : <button ref={toggleRef} type="button" aria-label="Show field view" className="flex min-h-8 items-center gap-2 rounded px-2 text-xs font-medium hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-white" onClick={() => setVisible(true)}><Eye className="size-3.5" aria-hidden="true" />Field view</button>}
    <span className="sr-only" role="status">Field view {visible ? `at ${CORNER_NAMES[corner]}` : "hidden"}</span>
  </section>;
}
