import { Sidebar } from "@/components/Sidebar";
import { WorkerStrip } from "@/components/WorkerStrip";

// Chrome for the "Research & Listing" app — the full pipeline (discovery → SKU
// planning → sourcing match → daily listings → control layer). This is ONE app
// among several the Operation System launches; Product Feed & Optimization is a
// separate app with its own chrome. They share the root layout (and data, later),
// not this sidebar.
export default function ResearchAppLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-[var(--border)] bg-[var(--surface)] px-6">
          <WorkerStrip />
        </header>
        <main className="flex-1 overflow-y-auto px-6 py-5">
          <div className="mx-auto max-w-[1400px]">{children}</div>
        </main>
      </div>
    </div>
  );
}
