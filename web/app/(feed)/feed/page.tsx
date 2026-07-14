import { redirect } from "next/navigation";

// The old "Feed Readiness" landing was removed — the app now opens on Product Performance.
// This slot is reserved for future feed surfaces; until then old links land on Performance.
export default function FeedPage() {
  redirect("/feed/optimize");
}
