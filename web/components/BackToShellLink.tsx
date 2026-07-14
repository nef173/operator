"use client";

import Link from "next/link";
import { useEmbedded } from "@/lib/embed";

// The "← Dashboard" affordance on operation-level pages (Settings / Activity). Hidden when the
// app is embedded in the NN dashboard — there is no operator dashboard to go back to; NN is the
// shell. Standalone, it still links back to the Operation-System landing.
export function BackToShellLink() {
  const embedded = useEmbedded();
  if (embedded) return null;
  return (
    <Link
      href="/dashboard"
      className="flex items-center gap-2 rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-medium hover:bg-[var(--surface-2)]"
    >
      <span aria-hidden>←</span> Dashboard
    </Link>
  );
}
