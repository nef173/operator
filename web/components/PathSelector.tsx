"use client";

import { useRouter } from "next/navigation";
import { usePath } from "@/components/PathProvider";
import { PATH_ORDER, PATHS, pathEntryHref } from "@/lib/pipeline";

// The pre-selector: a compact segmented toggle between the General and Niche
// paths. Switching navigates to the chosen path's entry stage so the move is
// real, not just cosmetic. `variant="rail"` renders the labelled cards for the
// pipeline page; default renders the sidebar segmented control.
export function PathSelector({ variant = "compact" }: { variant?: "compact" | "rail" }) {
  const { path, setPath } = usePath();
  const router = useRouter();

  const choose = (p: typeof path) => {
    if (p === path) return;
    setPath(p);
    router.push(pathEntryHref(p));
  };

  if (variant === "rail") {
    return (
      <div className="grid gap-3 sm:grid-cols-2">
        {PATH_ORDER.map((p) => {
          const def = PATHS[p];
          const active = p === path;
          return (
            <button
              key={p}
              onClick={() => choose(p)}
              className={`rounded-xl border p-4 text-left transition-colors ${
                active
                  ? "border-[var(--accent)] ring-1 ring-[var(--ring)]"
                  : "border-[var(--border)] hover:border-[var(--accent)]"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-semibold">{def.label}</span>
                {active ? (
                  <span className="text-xs font-semibold text-[var(--accent)]">Active</span>
                ) : null}
              </div>
              <p className="mt-1 text-sm text-[var(--muted)]">{def.tagline}</p>
            </button>
          );
        })}
      </div>
    );
  }

  return (
    <div className="flex rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-0.5">
      {PATH_ORDER.map((p) => {
        const active = p === path;
        return (
          <button
            key={p}
            onClick={() => choose(p)}
            className="flex-1 rounded-md px-2 py-1.5 text-xs font-semibold transition-colors"
            style={
              active
                ? { background: "var(--accent)", color: "var(--accent-fg)" }
                : { color: "var(--muted)" }
            }
          >
            {PATHS[p].label}
          </button>
        );
      })}
    </div>
  );
}
