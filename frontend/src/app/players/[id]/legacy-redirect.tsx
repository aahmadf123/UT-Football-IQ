"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

/**
 * Forward old /players/<id> deep links to the canonical CSR profile route
 * (/players/detail/?id=<id>). The id is derived from the *runtime* URL, not
 * the build-time param, so any real id survives a static-export load that
 * falls back to this page's HTML.
 */
export function LegacyPlayerRedirect() {
  const router = useRouter();
  useEffect(() => {
    const match = /\/players\/([^/?#]+)/.exec(window.location.pathname);
    const raw = match ? decodeURIComponent(match[1]) : "";
    const target =
      raw && raw !== "detail" && raw !== "redirect"
        ? `/players/detail/?id=${encodeURIComponent(raw)}`
        : "/players/";
    router.replace(target);
  }, [router]);
  return (
    <div className="p-6">
      <p className="text-xs text-muted-foreground">Opening player profile…</p>
    </div>
  );
}
