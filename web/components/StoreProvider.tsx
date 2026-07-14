"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api } from "@/lib/api";

type StoreMode = "general" | "fashion" | "both";

type StoreCtx = {
  /** The operator's active store key (e.g. "nosura"). Empty string until known. */
  store: string;
  setStore: (s: string) => void;
  /** All registered store keys, from /api/settings. */
  stores: string[];
  /** slug → operator-set display name (from Settings · Connections). */
  labels: Record<string, string>;
  /** The friendly display name for a store key (falls back to the key itself). */
  label: (slug: string) => string;
  /** slug → catalog path (general | fashion | both), from /api/settings stores_health. */
  modes: Record<string, StoreMode>;
  /** The catalog path for a store key (defaults to general). */
  mode: (slug: string) => StoreMode;
  /** Flip a store's catalog path — persists via the API and updates every consumer. */
  setMode: (slug: string, m: StoreMode) => Promise<void>;
  /** Re-fetch the store list + labels + modes from the server (call after a Settings change so
   * the sidebar badge / path reflect immediately, without a hard reload). */
  reload: () => void;
  /** True once the store list has been fetched + the active store reconciled. */
  ready: boolean;
};

const Ctx = createContext<StoreCtx | null>(null);
const STORAGE_KEY = "gs-operator-store";

// Single source of truth for "which store am I operating on" across every page.
// Before this, each page kept its own useState(stores[0]) — switching store on one
// surface didn't carry to the next, and sourcing-match silently always used the
// first store. This provider holds ONE active store, persisted to localStorage and
// reconciled against the live registry (a stored key that no longer exists falls
// back to the first registered store). It also carries each store's catalog path
// (general | fashion) so the sidebar switcher can show + flip it from any surface.
export function StoreProvider({ children }: { children: React.ReactNode }) {
  const [store, setStoreState] = useState("");
  const [stores, setStores] = useState<string[]>([]);
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [modes, setModes] = useState<Record<string, StoreMode>>({});
  const [ready, setReady] = useState(false);

  // Fetch store list + labels + modes from the server. Returns the store-key list so the mount
  // effect can reconcile the active store. Stable identity (no deps) so it's safe in effect deps.
  const loadStores = useCallback(async (): Promise<string[]> => {
    const s = await api.settings();
    const list = s.stores ?? [];
    setStores(list);
    setLabels(s.store_labels ?? {});
    setModes(
      Object.fromEntries(
        (s.stores_health ?? []).map((h) => [
          h.store,
          h.mode === "fashion" || h.mode === "both" ? h.mode : "general",
        ]),
      ),
    );
    return list;
  }, []);

  // Mount: load stores + reconcile the active store (a stored key that no longer exists falls back
  // to the first registered store). The active-store reconciliation runs ONCE, on mount only.
  useEffect(() => {
    let cancelled = false;
    loadStores()
      .then((list) => {
        if (cancelled) return;
        const stored = window.localStorage.getItem(STORAGE_KEY);
        const active = stored && list.includes(stored) ? stored : list[0] ?? "";
        setStoreState(active);
        if (active) window.localStorage.setItem(STORAGE_KEY, active);
      })
      .catch(() => {
        /* settings unreachable — leave store empty, consumers fall back */
      })
      .finally(() => {
        if (!cancelled) setReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [loadStores]);

  // Refresh labels/modes when the tab regains focus, so a catalog-path change made elsewhere
  // (or in another tab) reflects without a hard reload. Does NOT touch the active store.
  useEffect(() => {
    const onFocus = () => { loadStores().catch(() => {}); };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [loadStores]);

  const reload = useCallback(() => { loadStores().catch(() => {}); }, [loadStores]);

  const setStore = (s: string) => {
    setStoreState(s);
    window.localStorage.setItem(STORAGE_KEY, s);
  };

  const label = (slug: string) => labels[slug] || slug;
  const mode = (slug: string): StoreMode => modes[slug] ?? "general";

  const setMode = async (slug: string, m: StoreMode) => {
    const prev = modes[slug] ?? "general";
    setModes((cur) => ({ ...cur, [slug]: m })); // optimistic — revert on failure
    try {
      await api.setStoreMode(slug, m);
      await loadStores(); // reconcile against server truth so every consumer converges
    } catch {
      setModes((cur) => ({ ...cur, [slug]: prev }));
    }
  };

  return (
    <Ctx.Provider value={{ store, setStore, stores, labels, label, modes, mode, setMode, reload, ready }}>
      {children}
    </Ctx.Provider>
  );
}

export function useStore(): StoreCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useStore must be used within StoreProvider");
  return ctx;
}
