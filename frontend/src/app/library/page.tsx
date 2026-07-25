import { RouteRedirect } from "@/components/route-redirect";

// Compatibility route (ADR 0003): Library content lives in Film Room →
// Browse Film. Redirect instead of re-rendering the view inside a second
// shell so the hub owns the single mounted instance.
export default function LibraryPage() {
  return <RouteRedirect to="/film-room/?tab=browse" label="Opening Film Room → Browse Film…" />;
}
