import { describe, expect, test } from "vitest";
import { describeWorkspace } from "./workspace-context";

describe("workspace destination", () => {
  test("distinguishes a connected team API from this browser's computer", () => {
    expect(describeWorkspace("https://football-api.example.workers.dev")).toMatchObject({ local: false, libraryLabel: "Team library", settingsLabel: "Workspace settings", host: "football-api.example.workers.dev" });
  });

  test.each(["", "http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"])("recognizes local API destination %s", (destination) => {
    expect(describeWorkspace(destination)).toMatchObject({ local: true, libraryLabel: "Local library", settingsLabel: "This computer" });
  });
});
