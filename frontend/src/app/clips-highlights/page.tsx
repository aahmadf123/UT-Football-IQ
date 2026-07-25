import { RouteRedirect } from "@/components/route-redirect";

// ADR 0003: Clips & Highlights folded into Film Room. The mock clip grid was
// removed (#96) — real clips live under Browse Film (sessions → videos →
// clips) and Review & Tag Plays.
export default function ClipsHighlightsPage() {
  return <RouteRedirect to="/film-room/?tab=browse" label="Opening Film Room → Browse Film…" />;
}
