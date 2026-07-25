import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { UploadDialog } from "./upload-dialog";
import type { UploadMetadataDraft } from "@/lib/upload-metadata";

function videoFile(name: string, size = 1024): File {
  const file = new File([new Uint8Array(8)], name, { type: "video/mp4" });
  // File size is read-only; override it so the size summary can be asserted.
  Object.defineProperty(file, "size", { value: size });
  return file;
}

function dropFiles(files: File[]): void {
  const dropzone = screen.getByTestId("upload-dropzone");
  fireEvent.drop(dropzone, { dataTransfer: { files } });
}

function renderDialog(onSubmit = vi.fn()) {
  render(<UploadDialog open onOpenChange={vi.fn()} onSubmit={onSubmit} />);
  return onSubmit;
}

describe("UploadDialog", () => {
  it("accepts dropped video files", () => {
    renderDialog();
    dropFiles([videoFile("Dji 20260416110000 0257 D.mp4")]);
    expect(screen.getByTestId("upload-file-list").textContent).toContain("Dji 20260416110000 0257 D.mp4");
  });

  it("pre-fills the date from the DJI filename", () => {
    renderDialog();
    dropFiles([videoFile("Dji 20260416110000 0257 D.mp4")]);
    const input = screen.getByLabelText("Date recorded") as HTMLInputElement;
    expect(input.value).toBe("2026-04-16T11:00");
  });

  it("de-duplicates a folder dropped twice", () => {
    // A common slip that would otherwise upload every clip a second time.
    renderDialog();
    const file = videoFile("Dji 20260416110000 0257 D.mp4");
    dropFiles([file]);
    dropFiles([videoFile("Dji 20260416110000 0257 D.mp4")]);
    expect(screen.getByTestId("upload-file-list").textContent).toContain("1 file");
  });

  it("skips non-video files and says so", () => {
    renderDialog();
    dropFiles([videoFile("practice.mp4"), new File(["x"], "notes.txt", { type: "text/plain" })]);
    expect(screen.getByTestId("upload-file-list").textContent).toContain("1 file");
    expect(screen.getByRole("status").textContent).toContain("notes.txt");
  });

  it("will not submit without a session type", async () => {
    const onSubmit = renderDialog();
    dropFiles([videoFile("Dji 20260416110000 0257 D.mp4")]);
    fireEvent.click(screen.getByTestId("upload-submit"));
    await waitFor(() => {
      expect(screen.getByText(/Pick practice, scrimmage, or game/)).toBeTruthy();
    });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("requires an opponent on game film", async () => {
    // Without it the film never reaches Opponent Prep, which derives its team
    // list from videos.opponent_team.
    const onSubmit = renderDialog();
    dropFiles([videoFile("Dji 20260416110000 0257 D.mp4")]);
    fireEvent.change(screen.getByLabelText("Session type"), { target: { value: "game" } });
    fireEvent.click(screen.getByTestId("upload-submit"));
    await waitFor(() => {
      expect(screen.getByText(/needs an opponent/)).toBeTruthy();
    });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("does not ask for an opponent on practice film", () => {
    renderDialog();
    dropFiles([videoFile("Dji 20260416110000 0257 D.mp4")]);
    fireEvent.change(screen.getByLabelText("Session type"), { target: { value: "practice" } });
    expect(screen.queryByLabelText("Opponent")).toBeNull();
  });

  it("submits the batch with the metadata that makes it findable", async () => {
    const onSubmit = renderDialog();
    dropFiles([
      videoFile("Dji 20260416110000 0257 D.mp4"),
      videoFile("Dji 20260416110028 0258 D.mp4"),
    ]);
    fireEvent.change(screen.getByLabelText("Session type"), { target: { value: "practice" } });
    fireEvent.change(screen.getByLabelText("Our possession"), { target: { value: "offense" } });
    fireEvent.change(screen.getByLabelText("Tags"), { target: { value: "Red Zone, red zone" } });
    fireEvent.click(screen.getByTestId("upload-submit"));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const [files, draft] = onSubmit.mock.calls[0] as [File[], UploadMetadataDraft];
    expect(files).toHaveLength(2);
    expect(draft.sessionKind).toBe("practice");
    expect(draft.ourPossession).toBe("offense");
    expect(draft.sourceType).toBe("drone");
    expect(draft.recordedAt).not.toBeNull();
    // Tags are normalised, so one label cannot become two browsing buckets.
    expect(draft.tags).toEqual(["red zone"]);
  });

  it("cannot submit with no files", () => {
    renderDialog();
    expect((screen.getByTestId("upload-submit") as HTMLButtonElement).disabled).toBe(true);
  });

  it("clears the selected files", () => {
    renderDialog();
    dropFiles([videoFile("a.mp4")]);
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.queryByTestId("upload-file-list")).toBeNull();
  });

  it("offers known opponents as suggestions", () => {
    render(
      <UploadDialog
        open
        onOpenChange={vi.fn()}
        onSubmit={vi.fn()}
        opponentSuggestions={["Ball State", "Northern Illinois"]}
      />,
    );
    dropFiles([videoFile("a.mp4")]);
    fireEvent.change(screen.getByLabelText("Session type"), { target: { value: "game" } });
    const list = document.getElementById("upload-opponent-options");
    expect(list?.querySelectorAll("option")).toHaveLength(2);
  });
});
