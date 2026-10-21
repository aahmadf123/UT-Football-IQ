import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { FieldInset } from "./field-inset";
import type { OverlayTracklet } from "@/lib/types";

afterEach(cleanup);
const track: OverlayTracklet = { id: "one", player_id: null, start_frame: 0, end_frame: 300, team_label: "home", side_of_ball: null, position_group: null, track_confidence: null, track_points: [0, 10].map((timestamp_seconds) => ({ timestamp_seconds, frame_number: timestamp_seconds * 30, field_x: 50, field_y: 0, bbox: [0, 0, 10, 20], detection_confidence: 0.9 })) };
const calibration = { analytics_safe: true, confidence: 0.9, reason: null, reason_codes: [] };
test("video inset stays calibrated and suppresses long-gap player positions", () => {
  const { rerender } = render(<FieldInset tracklets={[track]} seconds={0} fps={30} calibration={calibration} />);
  expect(screen.getByTestId("field-diagram").querySelectorAll("circle")).toHaveLength(1);
  rerender(<FieldInset tracklets={[track]} seconds={5} fps={30} calibration={calibration} />);
  expect(screen.getByTestId("field-diagram").querySelectorAll("circle")).toHaveLength(0);
});
test("move and expand change the in-video field without changing playback", () => {
  render(<FieldInset tracklets={[track]} seconds={0} fps={30} calibration={calibration} />);
  fireEvent.click(screen.getByRole("button", { name: "Move field view to next corner" }));
  expect(screen.getByTestId("field-inset").getAttribute("data-corner")).toBe("0");
  fireEvent.click(screen.getByRole("button", { name: "Expand field view" }));
  expect(screen.getByRole("button", { name: "Shrink field view" }).getAttribute("aria-pressed")).toBe("true");
});
test("unqualified calibration never draws player or field positions", () => {
  render(<FieldInset tracklets={[track]} seconds={0} fps={30} calibration={{ ...calibration, analytics_safe: false, reason: "Camera calibration missing" }} />);
  expect(screen.queryByTestId("field-diagram")).toBeNull();
  expect(screen.getByText("Camera calibration missing")).toBeTruthy();
});
