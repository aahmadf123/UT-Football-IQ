import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { OverlayTracklet } from "@/lib/types";
import type { ActorBindingReview } from "@/lib/api";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), update: vi.fn(), remove: vi.fn(), role: "analyst" }));
vi.mock("@/lib/app-state", () => ({ useAppState: () => ({ authToken: "staff-token", currentRole: mocks.role }) }));
vi.mock("@/lib/api", () => ({ fetchActorBindingReview: mocks.fetch, updateReviewedEvent: mocks.update, removeInvalidReviewedEvent: mocks.remove }));
import { ActorBindingReviewNotice } from "./actor-binding-review";

const tracks = ["current-one", "current-two"].map((id) => ({ id, player_id: null, start_frame: 0, end_frame: 10, track_confidence: 1, team_label: "offense", position_group: null, side_of_ball: "offense", track_points: [{ frame_number: 0, timestamp_seconds: 0, shot_id: 0 as unknown as string, visibility_state: "observed", field_x: 50, field_y: 0, bbox: [0, 0, 2, 4], detection_confidence: 1 }, { frame_number: 1, timestamp_seconds: 0.1, shot_id: " unknown ", field_x: null, field_y: null, bbox: null, detection_confidence: null }] })) as OverlayTracklet[];
function review(reason = "actor_binding_observations_missing_or_changed"): ActorBindingReview {
  return { annotation_revision: 4, observations: [{ event: { id: "saved-event", event_type: "position_participation", timestamp_seconds: 0, attributes: { actor_id: "old-one", opponent_id: "old-two", opportunity_kind: "routes", timebase: "clip", end_seconds: 1, interaction_id: "opaque-operation" } }, issues: [{ reason }] }] };
}
afterEach(() => { cleanup(); vi.clearAllMocks(); mocks.role = "analyst"; });

test("shows a film-readable reason and updates the SAME saved event with explicit staff mappings and auth", async () => {
  mocks.fetch.mockResolvedValueOnce(review()).mockResolvedValue({ annotation_revision: 5, observations: [] });
  mocks.update.mockResolvedValue({ id: "saved-event" });
  const onSaved = vi.fn();
  render(<ActorBindingReviewNotice clipId="clip" annotationRevision={4} tracks={tracks} clipStart={0} clipEnd={1} onSaved={onSaved} />);
  expect(await screen.findByText("The player observations changed or are no longer available.")).toBeTruthy();
  expect(mocks.fetch).toHaveBeenCalledWith("clip", "staff-token");
  expect(screen.queryByText("old-one")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Review observation 1" }));
  fireEvent.change(screen.getByLabelText("Observed player reference 1"), { target: { value: "current-one" } });
  fireEvent.change(screen.getByLabelText("Comparison player reference 2"), { target: { value: "current-two" } });
  fireEvent.change(screen.getByLabelText("Reviewed source shot"), { target: { value: "0" } });
  expect(screen.getByLabelText("Reviewed source shot").querySelectorAll("option")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "Update saved observation" }));
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(mocks.update).toHaveBeenCalledWith("saved-event", expect.objectContaining({ actor_id: "current-one", opponent_id: "current-two", shot_id: "0", interaction_id: "opaque-operation", start_seconds: 0, end_seconds: 1, interval_timebase: "clip" }), "staff-token", 4, 0);
  expect(screen.queryByRole("heading", { name: "Review saved player observations" })).toBeNull();
});

test("clip changes discard the old editor and late responses cannot show an old observation", async () => {
  mocks.fetch.mockResolvedValueOnce(review());
  const view = render(<ActorBindingReviewNotice clipId="first" tracks={tracks} clipStart={0} clipEnd={1} />);
  fireEvent.click(await screen.findByRole("button", { name: "Review observation 1" }));
  expect(screen.getByLabelText("Observed player reference 1")).toBeTruthy();
  let finish: (value: ActorBindingReview) => void = () => {};
  mocks.fetch.mockImplementationOnce(() => new Promise<ActorBindingReview>((resolve) => { finish = resolve; }));
  view.rerender(<ActorBindingReviewNotice clipId="second" tracks={[]} clipStart={10} clipEnd={12} />);
  expect(screen.queryByLabelText("Observed player reference 1")).toBeNull();
  mocks.fetch.mockResolvedValueOnce({ annotation_revision: 0, observations: [] });
  view.rerender(<ActorBindingReviewNotice clipId="third" tracks={[]} clipStart={20} clipEnd={22} />);
  await act(async () => finish(review()));
  expect(screen.queryByRole("button", { name: "Review observation 1" })).toBeNull();
  expect(mocks.update).not.toHaveBeenCalled();
});

test("malformed saved fields offer a revision-checked removal and the actual structured entry workflow", async () => {
  mocks.fetch.mockResolvedValueOnce(review("actor_reference_container_invalid")).mockResolvedValue({ annotation_revision: 5, observations: [] });
  mocks.remove.mockResolvedValue(undefined);
  render(<ActorBindingReviewNotice clipId="clip" tracks={tracks} clipStart={0} clipEnd={1} />);
  fireEvent.click(await screen.findByRole("button", { name: "Review observation 1" }));
  expect(screen.getByText(/Fix something → Record position observation/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Remove invalid observation" }));
  expect(mocks.remove).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm remove observation" }));
  await waitFor(() => expect(mocks.remove).toHaveBeenCalledWith("saved-event", "staff-token", 4));
  expect(mocks.update).not.toHaveBeenCalled();
});

test("duplicate clock observations explain ambiguity and remove only the explicitly confirmed existing event", async () => {
  const duplicate = review("prediction_clock_ambiguous");
  duplicate.observations[0].event.event_type = "snap";
  mocks.fetch.mockResolvedValueOnce(duplicate).mockResolvedValue({ annotation_revision: 5, observations: [] });
  mocks.remove.mockResolvedValue(undefined);
  render(<ActorBindingReviewNotice clipId="clip" tracks={[]} clipStart={0} clipEnd={1} />);
  expect(await screen.findByText(/More than one reviewed event defines this prediction clock/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Review observation 1" }));
  expect(screen.getByText(/Keep every valid observation/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Remove saved observation" }));
  expect(mocks.remove).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Keep observation" }));
  expect(mocks.remove).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Remove saved observation" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm remove observation" }));
  await waitFor(() => expect(mocks.remove).toHaveBeenCalledWith("saved-event", "staff-token", 4));
  expect(mocks.update).not.toHaveBeenCalled();
});

test("unresolved replay of a preserved removal has a separate read-only history notice", async () => {
  mocks.fetch.mockResolvedValue({ annotation_revision: 4, observations: [], removed_observations: [review().observations[0]] });
  render(<ActorBindingReviewNotice clipId="clip" tracks={[]} clipStart={0} clipEnd={1} />);
  expect(await screen.findByText("Removal history needs source review")).toBeTruthy();
  expect(screen.getByText(/The original tags remain removed/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /Review observation/ })).toBeNull();
  expect(mocks.update).not.toHaveBeenCalled();
});

test("viewer accounts neither fetch staff diagnostics nor show editing actions", () => {
  mocks.role = "viewer";
  render(<ActorBindingReviewNotice clipId="clip" tracks={tracks} clipStart={0} clipEnd={1} />);
  expect(mocks.fetch).not.toHaveBeenCalled();
  expect(screen.queryByText("Review saved player observations")).toBeNull();
});

test("a concurrent same-play revision invalidates the old editor instead of upgrading its stale CAS", async () => {
  const newer = review(); newer.annotation_revision = 5;
  newer.observations[0].event.attributes!.opportunity_kind = "targets";
  mocks.fetch.mockResolvedValueOnce(review()).mockResolvedValueOnce(newer);
  mocks.update.mockResolvedValue({ id: "saved-event" });
  const view = render(<ActorBindingReviewNotice clipId="clip" annotationRevision={4} tracks={tracks} clipStart={0} clipEnd={1} />);
  fireEvent.click(await screen.findByRole("button", { name: "Review observation 1" }));
  expect(screen.getByLabelText("Observed player reference 1")).toBeTruthy();
  view.rerender(<ActorBindingReviewNotice clipId="clip" annotationRevision={5} tracks={tracks} clipStart={0} clipEnd={1} />);
  await waitFor(() => expect(screen.queryByLabelText("Observed player reference 1")).toBeNull());
  expect(mocks.update).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Review observation 1" }));
  fireEvent.change(screen.getByLabelText("Observed player reference 1"), { target: { value: "current-one" } });
  fireEvent.change(screen.getByLabelText("Comparison player reference 2"), { target: { value: "current-two" } });
  fireEvent.change(screen.getByLabelText("Reviewed source shot"), { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "Update saved observation" }));
  await waitFor(() => expect(mocks.update).toHaveBeenCalledWith("saved-event", expect.objectContaining({ opportunity_kind: "targets" }), "staff-token", 5, 0));
});

test("late save completion from the old play does not cancel or reload the new play", async () => {
  mocks.fetch.mockResolvedValueOnce(review());
  let finishSave: (value: object) => void = () => {};
  mocks.update.mockImplementationOnce(() => new Promise((resolve) => { finishSave = resolve; }));
  const saved = vi.fn();
  const view = render(<ActorBindingReviewNotice clipId="first" tracks={tracks} clipStart={0} clipEnd={1} onSaved={saved} />);
  fireEvent.click(await screen.findByRole("button", { name: "Review observation 1" }));
  fireEvent.change(screen.getByLabelText("Observed player reference 1"), { target: { value: "current-one" } });
  fireEvent.change(screen.getByLabelText("Comparison player reference 2"), { target: { value: "current-two" } });
  fireEvent.change(screen.getByLabelText("Reviewed source shot"), { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "Update saved observation" }));
  let finishLoad: (value: ActorBindingReview) => void = () => {};
  mocks.fetch.mockImplementationOnce(() => new Promise<ActorBindingReview>((resolve) => { finishLoad = resolve; }));
  view.rerender(<ActorBindingReviewNotice clipId="second" tracks={[]} clipStart={10} clipEnd={12} onSaved={saved} />);
  await act(async () => finishSave({ id: "saved-event" }));
  expect(mocks.fetch).toHaveBeenCalledTimes(2);
  expect(saved).not.toHaveBeenCalled();
  await act(async () => finishLoad({ annotation_revision: 0, observations: [] }));
  expect(screen.queryByText("Review saved player observations")).toBeNull();
});

test.each(["track", "shot"])("a same-play replacement of the selected %s requires explicit reselection", async (kind) => {
  mocks.fetch.mockResolvedValue(review());
  const view = render(<ActorBindingReviewNotice clipId="clip" tracks={tracks} clipStart={0} clipEnd={1} />);
  fireEvent.click(await screen.findByRole("button", { name: "Review observation 1" }));
  fireEvent.change(screen.getByLabelText("Observed player reference 1"), { target: { value: "current-one" } });
  fireEvent.change(screen.getByLabelText("Comparison player reference 2"), { target: { value: "current-two" } });
  fireEvent.change(screen.getByLabelText("Reviewed source shot"), { target: { value: "0" } });
  expect((screen.getByRole("button", { name: "Update saved observation" }) as HTMLButtonElement).disabled).toBe(false);
  const replacement = tracks.map((track, index) => ({ ...track, id: kind === "track" ? `replacement-${index}` : track.id, track_points: track.track_points.map((point) => ({ ...point, shot_id: kind === "shot" ? "new-shot" : point.shot_id })) }));
  view.rerender(<ActorBindingReviewNotice clipId="clip" tracks={replacement} clipStart={0} clipEnd={1} />);
  expect((screen.getByRole("button", { name: "Update saved observation" }) as HTMLButtonElement).disabled).toBe(true);
  expect(mocks.update).not.toHaveBeenCalled();
});
