"use client";

import { useState } from "react";
import { Expand, Move, Shrink } from "lucide-react";
import type { OverlayCalibration, OverlayTracklet } from "@/lib/types";
import { FieldMinimap } from "./field-minimap";
import { cn } from "@/lib/utils";

const CORNERS = ["right-2 top-12", "left-2 top-12", "left-2 bottom-2", "right-2 bottom-2"];

/** Video-child HUD: stays beside the action through seeking, resize and fullscreen. */
export function FieldInset({ tracklets, seconds, fps, calibration, playerNamesById }: {
  tracklets: OverlayTracklet[]; seconds: number; fps: number;
  calibration: OverlayCalibration | null; playerNamesById?: ReadonlyMap<string, string>;
}) {
  const [corner, setCorner] = useState(3);
  const [expanded, setExpanded] = useState(false);
  return <section aria-label="Field view inside video" data-testid="field-inset" data-corner={corner}
    data-expanded={expanded}
    className={cn("group absolute z-10 overflow-hidden rounded border text-white transition-opacity duration-200", CORNERS[corner], expanded ? "w-[42%] min-w-48 max-w-md border-white/30 bg-black/85" : "w-[22%] min-w-28 max-w-52 border-white/10 bg-black/30 opacity-65 hover:opacity-100 focus-within:opacity-100")}
    onClick={(event) => event.stopPropagation()}>
    <div className={cn("flex items-center justify-between gap-1 bg-black/85 px-1.5 text-[10px]", !expanded && "absolute inset-x-0 top-0 z-10 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100")}><span className="font-medium text-white">Field view</span><div className="flex gap-0.5"><button type="button" aria-label="Move field view to next corner" className="flex size-6 items-center justify-center rounded hover:bg-white/15 focus-visible:outline-2 focus-visible:outline-white" onClick={() => setCorner((value) => (value + 1) % CORNERS.length)}><Move className="size-3" /></button><button type="button" aria-label={expanded ? "Shrink field view" : "Expand field view"} aria-pressed={expanded} className="flex size-6 items-center justify-center rounded hover:bg-white/15 focus-visible:outline-2 focus-visible:outline-white" onClick={() => setExpanded((value) => !value)}>{expanded ? <Shrink className="size-3" /> : <Expand className="size-3" />}</button></div></div>
    <FieldMinimap tracklets={tracklets} currentFrame={Math.round(seconds * fps)} currentTimeSeconds={seconds} fps={fps} calibration={calibration} playerNamesById={playerNamesById} embedded quiet={!expanded} />
  </section>;
}
