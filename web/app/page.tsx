import { redirect } from "next/navigation";

// Root "/" has no screen of its own — the operation home is the Dashboard launcher.
// Redirect there so a bare visit lands on /dashboard (the post-login home).
export default function RootPage() {
  redirect("/dashboard");
}
