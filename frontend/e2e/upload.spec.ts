import path from "node:path";
import { expect, test } from "@playwright/test";
import { mockBackend, type Route, sampleInboxItem } from "./helpers";

const FIXTURE_MP4 = path.resolve(__dirname, "fixtures/sample.mp4");

/**
 * End-to-end upload path: a coach selects an MP4, the backend issues a
 * presigned upload URL, the file is PUT to that (mocked) URL, the backend
 * registers the resulting video, and the Practice Inbox refreshes to
 * surface the new row.
 *
 * Every API call is intercepted; no real backend or object store is required.
 */
test("upload flow drives upload-url → PUT → backend register → inbox refresh", async ({
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
      };
      expect(body.filename).toBe("sample.mp4");
      expect(body.storage_uri).toBe("s3://raw-video/raw/PR_e2e.mp4");
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

  // The hidden file input is rendered by PageRenderer at the top of every
  // shell page. Use setInputFiles() to drive the upload pipeline without
  // having to script the OS file chooser.
  await page.locator('input[type="file"][accept="video/*"]').setInputFiles(
    FIXTURE_MP4,
  );

  // Confirm the upload pipeline executed upload-url → PUT → backend register.
  await expect.poll(() => uploadUrlRequested).toBe(true);
  await expect.poll(() => objectPutInvoked).toBe(true);
  await expect.poll(() => videoRegistered).toBe(true);

  // Upload toast confirms the UI saw the upload as complete.
  await expect(page.getByText(/Uploaded \d+ clip/i)).toBeVisible();

  // Practice Inbox refreshes and now shows the registered row.
  const inbox = page.locator("section").filter({ hasText: /Practice Inbox/ });
  await expect(inbox).toContainText("sample.mp4");
});
