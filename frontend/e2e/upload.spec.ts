import path from "node:path";
import { expect, test } from "@playwright/test";
import { mockBackend, mockWorker, type Route, sampleInboxItem } from "./helpers";

const FIXTURE_MP4 = path.resolve(__dirname, "fixtures/sample.mp4");

/**
 * End-to-end upload path: a coach selects an MP4, the Worker issues an
 * upload URL, the file is PUT to a (mocked) R2 endpoint, the backend
 * registers the resulting video, and the Practice Inbox refreshes to
 * surface the new row.
 *
 * Every Worker/API call is intercepted; no real R2 or Fly.io is required.
 */
test("upload flow drives Worker → R2 → backend register → inbox refresh", async ({
  page,
}) => {
  let uploadUrlRequested = false;
  let r2PutInvoked = false;
  let videoRegistered = false;

  const r2PutUrl = "http://worker.e2e.local/r2/put/PR_e2e.mp4";

  await mockWorker(page, {
    "POST /api/v1/videos/upload-url": () => {
      uploadUrlRequested = true;
      return { uploadUrl: r2PutUrl, key: "raw/PR_e2e.mp4" };
    },
    "PUT /r2/put/PR_e2e.mp4": () => {
      r2PutInvoked = true;
      return {
        key: "raw/PR_e2e.mp4",
        size: 32,
        etag: "etag-e2e",
        storageUri: "r2://raw-video/raw/PR_e2e.mp4",
      };
    },
  });

  // Inbox starts empty, then returns the freshly-registered video after
  // the upload completes. We swap the responder mid-test.
  let inboxRows: ReturnType<typeof sampleInboxItem>[] = [];
  await mockBackend(page, {
    "GET /api/v1/videos": [],
    "GET /api/v1/jobs": [],
    "GET /api/v1/self-scout/tendencies": { tendencies: [] },
    "GET /api/v1/inbox/status": () => inboxRows,
    "POST /api/v1/videos": (route: Route) => {
      videoRegistered = true;
      const body = JSON.parse(route.request().postData() ?? "{}") as {
        filename: string;
        storage_uri: string;
      };
      expect(body.filename).toBe("sample.mp4");
      expect(body.storage_uri).toBe("r2://raw-video/raw/PR_e2e.mp4");
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
        storage_uri: "r2://raw-video/raw/PR_e2e.mp4",
      };
    },
  });

  await page.goto("/");

  // The hidden file input is rendered by PageRenderer at the top of every
  // shell page. Use setInputFiles() to drive the upload pipeline without
  // having to script the OS file chooser.
  await page.locator('input[type="file"][accept="video/*"]').setInputFiles(
    FIXTURE_MP4,
  );

  // Confirm the upload pipeline executed Worker → R2 PUT → backend register.
  await expect.poll(() => uploadUrlRequested).toBe(true);
  await expect.poll(() => r2PutInvoked).toBe(true);
  await expect.poll(() => videoRegistered).toBe(true);

  // Upload toast confirms the UI saw the upload as complete.
  await expect(page.getByText(/Uploaded \d+ clip/i)).toBeVisible();

  // Practice Inbox refreshes and now shows the registered row.
  const inbox = page.locator("section").filter({ hasText: /Practice Inbox/ });
  await expect(inbox).toContainText("sample.mp4");
});
