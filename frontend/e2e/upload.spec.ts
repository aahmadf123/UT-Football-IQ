import path from "node:path";
import { expect, test } from "@playwright/test";
import { mockBackend, type Route, sampleInboxItem } from "./helpers";

const FIXTURE_MP4 = path.resolve(__dirname, "fixtures/sample.mp4");

/**
 * End-to-end upload path: a coach opens the upload dialog, picks an MP4,
 * describes the session, the backend issues a presigned upload URL, the file is
 * PUT to that (mocked) URL, the backend registers the resulting video with that
 * session metadata, and the Practice Inbox refreshes to surface the new row.
 *
 * The session metadata is the part worth asserting. Registering a video with a
 * null session_kind and opponent_team is what left the Library filters and the
 * Opponent Prep tab with nothing they could ever match, so this test pins that
 * the dialog actually puts those fields on the wire.
 *
 * Every API call is intercepted; no real backend or object store is required.
 */
test("upload flow drives dialog → upload-url → PUT → register with metadata → inbox refresh", async ({
  page,
}) => {
  let uploadUrlRequested = false;
  let objectPutInvoked = false;
  let videoRegistered = false;

  const objectPutUrl = "http://api.e2e.local/api/v1/videos/upload/raw%2FPR_e2e.mp4";

  // Inbox starts empty, then returns the freshly-registered video after
  // the upload completes. We swap the responder mid-test.
  let inboxRows: ReturnType<typeof sampleInboxItem>[] = [];
  await mockBackend(page, {
    "POST /api/v1/videos/upload-url": () => {
      uploadUrlRequested = true;
      return { uploadUrl: objectPutUrl, key: "raw/PR_e2e.mp4" };
    },
    "PUT /api/v1/videos/upload/raw%2FPR_e2e.mp4": () => {
      objectPutInvoked = true;
      return {
        key: "raw/PR_e2e.mp4",
        size: 32,
        etag: "etag-e2e",
        storageUri: "s3://raw-video/raw/PR_e2e.mp4",
      };
    },
    "GET /api/v1/videos": [],
    "GET /api/v1/jobs": [],
    "GET /api/v1/self-scout/tendencies": { tendencies: [] },
    "GET /api/v1/inbox/status": () => inboxRows,
    "POST /api/v1/videos": (route: Route) => {
      videoRegistered = true;
      const body = JSON.parse(route.request().postData() ?? "{}") as {
        filename: string;
        storage_uri: string;
        session_kind: string | null;
        our_possession: string | null;
        recorded_at: string | null;
      };
      expect(body.filename).toBe("sample.mp4");
      expect(body.storage_uri).toBe("s3://raw-video/raw/PR_e2e.mp4");
      // The whole point of the dialog: these used to be null on every upload.
      expect(body.session_kind).toBe("practice");
      expect(body.our_possession).toBe("offense");
      expect(body.recorded_at).toBeTruthy();
      // After register, populate the inbox so the refresh-after-upload
      // surfaces the new row.
      inboxRows = [
        sampleInboxItem({
          video_id: "v-upload-1",
          filename: "sample.mp4",
          video_status: "uploaded",
          total_jobs: 0,
          succeeded_jobs: 0,
          clip_count: 0,
        }),
      ];
      return {
        id: "v-upload-1",
        filename: "sample.mp4",
        status: "uploaded",
        duration_seconds: null,
        fps: null,
        width: null,
        height: null,
        created_at: "2025-10-01T10:00:00Z",
        storage_uri: "s3://raw-video/raw/PR_e2e.mp4",
      };
    },
  });

  await page.goto("/");

  // Open the dialog; the file input lives inside it and is not in the DOM
  // until it opens.
  await page.getByRole("button", { name: /Upload Film/i }).first().click();
  await expect(page.getByTestId("upload-dialog")).toBeVisible();

  // setInputFiles() drives the picker without scripting the OS file chooser.
  await page.getByTestId("upload-file-input").setInputFiles(FIXTURE_MP4);
  await expect(page.getByTestId("upload-file-list")).toContainText("sample.mp4");

  // Describe the session. session_kind is required; the fixture filename has no
  // DJI timestamp, so the date has to be entered too.
  await page.getByLabel("Session type").selectOption("practice");
  await page.getByLabel("Date recorded").fill("2026-04-16T11:00");
  await page.getByLabel("Our possession").selectOption("offense");

  await page.getByTestId("upload-submit").click();

  // Confirm the upload pipeline executed upload-url → PUT → backend register.
  await expect.poll(() => uploadUrlRequested).toBe(true);
  await expect.poll(() => objectPutInvoked).toBe(true);
  await expect.poll(() => videoRegistered).toBe(true);

  // Toast confirms the UI saw the batch as started.
  await expect(page.getByText(/Uploading \d+ clip/i)).toBeVisible();

  // Practice Inbox refreshes and now shows the registered row.
  const inbox = page.locator("section").filter({ hasText: /Practice Inbox/ });
  await expect(inbox).toContainText("sample.mp4");
});
