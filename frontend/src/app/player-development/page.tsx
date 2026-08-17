import { RouteRedirect } from "@/components/route-redirect";

// Compatibility route: Player Development lives in Players → Development.
// Redirect instead of re-rendering the view inside a second shell so the hub
// owns the single mounted instance.
export default function PlayerDevelopmentPage() {
  return <RouteRedirect to="/players/?tab=development" label="Opening Players → Development…" />;
}
