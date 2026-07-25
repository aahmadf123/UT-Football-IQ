import { RouteRedirect } from "@/components/route-redirect";

// Compatibility route (ADR 0003): College Data (CFBD) lives in the Scouting
// workspace → College Data. Redirect instead of re-rendering the view inside
// a second shell so the hub owns the single mounted instance.
export default function CollegeDataPage() {
  return <RouteRedirect to="/scouting/?tab=college" label="Opening Scouting → College Data…" />;
}
