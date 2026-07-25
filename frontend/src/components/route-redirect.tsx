"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/**
 * Client-side redirect used for compatibility routes (ADR 0003). The app is a
 * Next static export (`output: "export"`), so server redirects are not
 * available — we replace the history entry in the browser instead.
 */
export function RouteRedirect({ to, label }: { to: string; label?: string }) {
  const router = useRouter();
  useEffect(() => {
    router.replace(to);
  }, [router, to]);
  return (
    <div className="p-6">
      <p className="text-xs text-muted-foreground">
        {label ?? "Taking you to the new location…"}
      </p>
    </div>
  );
}
