"use client";

import { useStore } from "@/components/StoreProvider";
import { UiSelect } from "@/components/UiSelect";

// The global active-store control. Lives in the sidebar so the operator's store
// choice is visible + switchable from anywhere, and every page reads the same
// value via useStore(). Renders a native <select> (store lists are short), plus
// the active store's CATALOG PATH (General | Fashion) — the flag that steers the
// research funnel, competitor finding and listing for that store — so the path is
// visible and flippable right where the operator switches stores (Research &
// Listing included), not only buried in Settings → Connections.
//
// `showPath` — the PATH toggle only matters for the FRONTEND (build) apps whose
// pages it steers. Backend apps (Product Mgmt, Issues, Tasks, Store Mgmt) pass
// false: they keep the store dropdown their pages depend on, without the noise.
export function StoreSelect({ showPath = true }: { showPath?: boolean } = {}) {
  const { store, setStore, stores, label, mode, setMode, ready } = useStore();

  if (ready && stores.length === 0) {
    return (
      <div className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-1.5 text-xs text-[var(--muted)]">
        No stores registered
      </div>
    );
  }

  const activeMode = store ? mode(store) : "general";

  return (
    <div className="space-y-1.5">
      <UiSelect
        value={store}
        onChange={setStore}
        disabled={!ready}
        ariaLabel="Active store"
        placeholder={ready ? "Select a store" : "Loading…"}
        options={stores.map((s) => ({
          value: s,
          label: `${label(s)}${mode(s) === "fashion" ? " · Fashion" : mode(s) === "both" ? " · Both" : ""}`,
        }))}
      />
      {showPath && ready && store ? (
        <div
          className="flex items-center justify-between gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-1"
          title="Catalog path — steers research, competitor finding and listing for this store"
        >
          <span className="text-[10px] font-semibold uppercase tracking-wide text-[var(--muted)]">
            Path
          </span>
          <span className="flex items-center gap-0.5 rounded-md border border-[var(--border)] p-0.5">
            {(["general", "fashion", "both"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(store, m)}
                className={`rounded px-1.5 py-0.5 text-[10px] font-semibold capitalize transition-colors ${
                  activeMode === m ? "" : "text-[var(--muted)] hover:text-[var(--text)]"
                }`}
                style={activeMode === m ? { background: "var(--accent)", color: "var(--accent-fg)" } : undefined}
              >
                {m}
              </button>
            ))}
          </span>
        </div>
      ) : null}
    </div>
  );
}
