"use client";

import { useSyncExternalStore } from "react";
import { apiBase } from "@/lib/endpoints";

function subscribe(listener: () => void) {
  window.addEventListener("storage", listener);
  return () => window.removeEventListener("storage", listener);
}

function snapshot() {
  return apiBase() || window.location.origin;
}

/** Describe the API destination, rather than the browser's computer. */
export function describeWorkspace(destination: string) {
  let host = "";
  try { host = new URL(destination).hostname; } catch { /* Same-origin local install. */ }
  const local = !host || host === "localhost" || host === "::1" || host === "[::1]" || /^127\./.test(host);
  return {
    local,
    host: host || "This computer",
    libraryLabel: local ? "Local library" : "Team library",
    settingsLabel: local ? "This computer" : "Workspace settings",
    description: local
      ? "Film is saved in this computer's library."
      : "Saved film is available to authorized staff in this team workspace.",
  };
}

export function useWorkspaceContext() {
  const destination = useSyncExternalStore(subscribe, snapshot, () => process.env.NEXT_PUBLIC_API_URL ?? "");
  return describeWorkspace(destination);
}
