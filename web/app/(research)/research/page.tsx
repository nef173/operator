import { redirect } from "next/navigation";

// The Research hub was removed from the nav (it duplicated the Dashboard + the individual
// research surfaces and had no unique action). Keep the route as a redirect so any old bookmark
// lands somewhere useful instead of 404-ing. The real research entry points are the per-surface
// pages: Trend / Keyword / Winning Products / Marketplace.
export default function ResearchHubRemoved() {
  redirect("/keyword-discovery");
}
