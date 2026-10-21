"use client";
import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
/** Compatibility adapter for previously shared Model Insights links. */
function AnalystRedirect() {
  const router = useRouter();
  const params = useSearchParams();
  const query = params.toString();
  useEffect(() => {
    const next = new URLSearchParams(query);
    next.set("tab", "analyst");
    if (!next.has("layout")) next.set("layout", "analyst");
    router.replace(`/team-stats/?${next}`);
  }, [query, router]);
  return <p role="status">Opening analyst tools…</p>;
}
export default function AnalyticsPage() {
  return <Suspense fallback={<p role="status">Opening analyst tools…</p>}><AnalystRedirect /></Suspense>;
}
