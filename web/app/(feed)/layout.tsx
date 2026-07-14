import { FeedSidebar } from "@/components/FeedSidebar";

// Chrome for the standalone "Product Feed & Optimization" app. Store separation lives
// INSIDE the app (the store picker on the page), exactly like Research & Listing —
// the app spans the whole Google operation, not one store. Backend is a separate,
// later build; this app is the Frontend's second surface.
export default function FeedAppLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <div className="flex h-screen overflow-hidden">
      <FeedSidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <main className="flex-1 overflow-y-auto px-6 py-5">
          <div className="mx-auto max-w-[1400px]">{children}</div>
        </main>
      </div>
    </div>
  );
}
