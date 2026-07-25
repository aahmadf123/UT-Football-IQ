import { LegacyPlayerRedirect } from "./legacy-redirect";

// Compatibility route. The old dynamic profile page pre-rendered only the
// mock-data ids, so real player ids 404'd on the static export. The canonical
// profile now lives at /players/detail/?id=<id>; this segment keeps one
// placeholder path alive purely to redirect old links (the client component
// reads the real id from the runtime URL).
export function generateStaticParams() {
  return [{ id: "redirect" }];
}

export default function LegacyPlayerProfilePage() {
  return <LegacyPlayerRedirect />;
}
