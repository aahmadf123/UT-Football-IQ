import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { OverlayLayerKey, OverlayTracklet } from "@/lib/types";
import { OverlayCanvas } from "./overlay-canvas";

afterEach(cleanup);

function track(id: string, x: number): OverlayTracklet {
  return {
    id,
    player_id: null,
    start_frame: 0,
    end_frame: 2,
    team_label: "home",
    track_confidence: 0.8,
    track_points: [0, 1, 2].map((frame) => ({
      frame_number: frame,
      timestamp_seconds: frame / 30,
      field_x: null,
      field_y: null,
      bbox: [x + frame, 100, x + frame + 40, 200] as [number, number, number, number],
    })),
  } as OverlayTracklet;
}

const LAYERS: ReadonlySet<OverlayLayerKey> = new Set(["tracks", "labels", "metrics", "events"]);

function canvas(props: Partial<Parameters<typeof OverlayCanvas>[0]> = {}) {
  return render(
    <OverlayCanvas
      tracklets={[track("a", 100), track("b", 400)]}
      events={[]}
      currentTimeSeconds={1 / 30}
      fps={30}
      videoWidth={1280}
      videoHeight={720}
      activeLayers={LAYERS}
      metricTextByTrackletId={new Map([["a", "8.1 yd/s"]])}
      {...props}
    />,
  );
}

describe("OverlayCanvas styles", () => {
  it("analyst style draws every box, tag and callout", () => {
    canvas();
    expect(screen.getByTestId("overlay-tracklet-tag-a")).toBeTruthy();
    expect(screen.getByTestId("overlay-tracklet-tag-b")).toBeTruthy();
    expect(screen.getByTestId("overlay-tracklet-metric-a")).toBeTruthy();
    expect(screen.queryByTestId("overlay-tracklet-ring-a")).toBeNull();
  });

  it("clean style shows only a ring at each player's feet", () => {
    canvas({ overlayStyle: "clean" });
    const ring = screen.getByTestId("overlay-tracklet-ring-a");
    // Bottom-centre of the box at frame 1: x 101..141, y 100..200.
    expect(Number(ring.getAttribute("cx"))).toBeCloseTo(121);
    expect(Number(ring.getAttribute("cy"))).toBeCloseTo(200);
    expect(screen.getByTestId("overlay-tracklet-ring-b")).toBeTruthy();
    expect(screen.queryByTestId("overlay-tracklet-tag-a")).toBeNull();
    expect(screen.queryByTestId("overlay-tracklet-metric-a")).toBeNull();
  });

  it("clean style gives the pointed-at player full detail, and still selects", () => {
    const onSelect = vi.fn();
    canvas({ overlayStyle: "clean", onSelectTracklet: onSelect });
    const player = screen.getByTestId("overlay-tracklet-a");
    fireEvent.mouseEnter(player);
    expect(screen.getByTestId("overlay-tracklet-tag-a")).toBeTruthy();
    expect(screen.getByTestId("overlay-tracklet-metric-a")).toBeTruthy();
    expect(screen.queryByTestId("overlay-tracklet-tag-b")).toBeNull();
    fireEvent.mouseLeave(player);
    expect(screen.queryByTestId("overlay-tracklet-tag-a")).toBeNull();
    fireEvent.click(player);
    expect(onSelect).toHaveBeenCalledWith("a");
  });

  it("clean style keeps the selected player detailed", () => {
    canvas({ overlayStyle: "clean", selectedTrackletId: "b" });
    expect(screen.getByTestId("overlay-tracklet-tag-b")).toBeTruthy();
    expect(screen.queryByTestId("overlay-tracklet-tag-a")).toBeNull();
  });
});
