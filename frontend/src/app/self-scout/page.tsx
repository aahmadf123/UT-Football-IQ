import { RouteRedirect } from "@/components/route-redirect";

// Compatibility route (ADR 0003): Self-Scout lives in the Scouting workspace →
// Our Tendencies. Redirect instead of re-rendering the view inside a second
// shell so the hub owns the single mounted instance.
export default function SelfScoutPage() {
  return <RouteRedirect to="/scouting/?tab=tendencies" label="Opening Scouting → Our Tendencies…" />;
}
