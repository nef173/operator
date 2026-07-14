import Link from "next/link";
import { WorkerStrip } from "@/components/WorkerStrip";

// Chrome for OPERATION-LEVEL views (Settings, Activity Log). These span the whole
// Google operation — they are NOT part of any single app — so they do NOT use the
// Research & Listing sidebar. They are reached from the Operation-System shell and
// stand on their own with a back-to-shell affordance.
export default function OperationLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-[var(--border)] bg-[var(--surface)] px-6">
        <div className="flex items-center gap-4">
          <Link
            href="/dashboard"
            className="flex items-center gap-2 rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-medium hover:bg-[var(--surface-2)]"
          >
            <span aria-hidden>←</span> Dashboard
          </Link>
          <WorkerStrip />
        </div>
      </header>
      <main className="flex-1 overflow-y-auto px-6 py-5">
        <div className="mx-auto max-w-[1400px]">{children}</div>
      </main>
    </div>
  );
}
