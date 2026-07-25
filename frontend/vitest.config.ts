import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import path from "node:path";

const dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    // Full-page renders (shell + Radix primitives) can exceed the 5s default
    // under parallel workers on slower machines; keep headroom so the suite
    // is timing-stable locally and in CI.
    testTimeout: 15_000,
    hookTimeout: 15_000,
  },
});
