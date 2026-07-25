import { RouteRedirect } from "@/components/route-redirect";

// Compatibility route (ADR 0003): Opponent Scout lives in the Scouting
// workspace → Opponent Prep. Redirect instead of re-rendering the view inside
// a second shell so the hub owns the single mounted instance.
export default function OpponentScoutPage() {
  return <RouteRedirect to="/scouting/?tab=opponent" label="Opening Scouting → Opponent Prep…" />;
}
