import { describe, expect, test } from "vitest";
import { ApiError, apiErrorFromResponse, friendlyMessage, processingErrorMessage, serverSentence } from "./api-error";

describe("ApiError", () => {
  test("never shows URLs, status codes or JSON to people", async () => {
    const response = new Response('{"detail":{"error_code":"internal","trace":"x"}}', { status: 500 });
    const error = await apiErrorFromResponse(response, "load the latest data");
    expect(error.message).toBe("Couldn't load the latest data. Something went wrong on our side. Try again in a moment.");
    expect(error.message).not.toMatch(/\d{3}|[{}]|https?:/);
    expect(error.status).toBe(500);
    expect(error.technical).toContain("internal");
  });

  test("keeps a human sentence the server wrote", async () => {
    const error = await apiErrorFromResponse(
      new Response('{"detail":"This recording is already in the library"}', { status: 409 }),
      "add the film to the library",
    );
    expect(error.message).toBe("Couldn't add the film to the library. This recording is already in the library.");
  });

  test("maps statuses to plain reasons", () => {
    expect(new ApiError({ status: 401, action: "x" }).kind).toBe("auth");
    expect(new ApiError({ status: 503, action: "x" }).message).toContain("busy");
    expect(new ApiError({ status: 0, action: "x" }).kind).toBe("offline");
  });

  test("ignores machine codes and validation lists", () => {
    expect(serverSentence('{"detail":"workload_gated"}')).toBeNull();
    expect(serverSentence('{"detail":[{"msg":"field required"}]}')).toBeNull();
    expect(serverSentence("<html>Bad gateway</html>")).toBeNull();
  });
});

describe("friendlyMessage", () => {
  test("cleans browser network errors and legacy raw strings", () => {
    expect(friendlyMessage(new TypeError("Failed to fetch"), "load film")).toBe(
      "Couldn't load film. The server can't be reached right now. Check your connection and try again.",
    );
    expect(friendlyMessage(new Error('GET https://x/api failed (403): {"detail":"no"}'))).toContain("doesn't have access");
    expect(friendlyMessage(new Error("Upload was cancelled."))).toBe("Upload was cancelled.");
  });
});

describe("processingErrorMessage", () => {
  test("never shows the worker's raw exception text", () => {
    const raw = "Client error '422 Unprocessable Entity' for url 'https://api.example/api/v1/videos/x/clips'";
    const text = processingErrorMessage("segment", raw);
    expect(text).toBe("Processing stopped while finding the plays. Retry it; if it fails again, let an admin know.");
    expect(text).not.toMatch(/422|https?:|Client error/);
  });

  test("explains an offline processing computer", () => {
    expect(processingErrorMessage(null, "lease expired; attempts exhausted")).toContain("went offline");
  });

  test("recovers a recognized stage prefix when the structured stage is missing", () => {
    const text = processingErrorMessage(null, "segment: GET https://api.example failed (422)");
    expect(text).toBe("Processing stopped while finding the plays. Retry it; if it fails again, let an admin know.");
    expect(text).not.toMatch(/segment:|422|https?:/);
  });
});
