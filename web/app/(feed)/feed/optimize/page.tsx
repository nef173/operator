"use client";

import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode, type CSSProperties } from "react";
import {
  api,
  type OptimizationSnapshot,
  type OptimizationProduct,
  type OptimizationWindow,
  type OptHistoryEntry,
  type VariantBreakdownRow,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { useStore } from "@/components/StoreProvider";
import { Card } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";

// Optimization — the core surface of the Product Feed & Optimization app, rebuilt to mirror the
// Pythago "Product Performance" dashboard 1:1: the 6-card KPI header, dual-currency banner, the
// full column set, pagination and column/sort affordances. The Shopify side (units / conversions /
// conversion value / refunds, plus per-window conversion value) is live off the last-synced
// snapshot. Every Google-Ads-derived column (Cost, Clicks, CTR, CPC, CPA, per-window
// ROAS / spend / impressions and the ad-based KPI cards) renders an honest "—" until that store's
// Google Ads read path is connected — never a fake zero. Store separation lives inside the app.

const RANGE_WIN = "all"; // the primary columns + KPI cards read the ALL-TIME (lifetime) bucket
const WINDOWS = ["7", "14", "30"] as const; // the per-window CV breakdown columns (rolling)
const WINDOW_LABEL: Record<string, string> = { "7": "7D", "14": "14D", "30": "30D", all: "All-time" };
const AUTO_SYNC_TTL = 1800; // auto-refresh the snapshot when it's older than 30 min (no button click)
const PAGE_SIZE = 25; // Pythago paginates 25/page
const ADS_HINT = "Connect Google Ads to populate this column";
// Metric-source colors: Shopify sales data tints GREEN, Google-Ads data BLUE, and app-CALCULATED
// columns (COGS from invoices + Profit + Margin, derived by this app) ORANGE — so the operator can
// tell at a glance where each number comes from.
const SHOPIFY_COLOR = "#16a34a";
const ADS_COLOR = "#2563eb";
const APP_COLOR = "#ea580c";

// ISO currency code → display symbol. Falls back to "<CODE> " (trailing space) for anything
// unmapped so an exotic store currency still reads clearly instead of a bare number.
const CURRENCY_SYMBOL: Record<string, string> = {
  EUR: "€", GBP: "£", USD: "$", AUD: "A$", CAD: "C$", NZD: "NZ$",
  CHF: "CHF", SEK: "kr", DKK: "kr", NOK: "kr", PLN: "zł", JPY: "¥", CZK: "Kč",
};
function curSymbol(cur: string | null | undefined): string {
  if (!cur) return "";
  return CURRENCY_SYMBOL[cur.toUpperCase()] ?? cur.toUpperCase();
}
function fmtMoney(n: number | null | undefined, currency: string | null): string {
  if (n == null) return "—";
  const sym = curSymbol(currency);
  const v = Math.round(n).toLocaleString();
  // Currency AFTER the number (European style), non-breaking space so it never wraps: "141 €".
  return sym ? `${v} ${sym}` : v;
}
function fmtInt(n: number | null | undefined): string {
  return n == null ? "—" : n.toLocaleString();
}
function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  // EU format DD.MM.YYYY (UTC so the calendar date matches the stored timestamp, no TZ shift).
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  return `${dd}.${mm}.${d.getUTCFullYear()}`;
}
function fmtAgo(seconds: number | null): string {
  if (seconds == null) return "never";
  if (seconds < 90) return "just now";
  const m = Math.round(seconds / 60);
  if (m < 90) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

type StatusFilter = "all" | "ACTIVE" | "DRAFT" | "ARCHIVED";

// ROAS = Return On Ad Spend = revenue ÷ ad spend (excludes COGS + refunds — pure ad efficiency).
// Null when there's no ad spend, so it never shows a misleading 0.
function roasOf(w?: OptimizationWindow): number | null {
  if (!w || !w.cost || w.cost <= 0) return null;
  return (w.cv ?? 0) / w.cost;
}

// Frozen (sticky) left columns — Pythago-style: Product · Status · Variants stay put while the
// metric columns scroll. Fixed pixel offsets via inline style (bulletproof vs Tailwind purge);
// each left = 32px checkbox + cumulative widths. Shared so header + body rows stay in lockstep.
const FZ = {
  product: { left: 32, width: 260 },
  status: { left: 292, width: 92 },
  variants: { left: 384, width: 96 },
} as const;
function fzStyle(c: { left: number; width: number }): CSSProperties {
  return { left: c.left, width: c.width, minWidth: c.width, maxWidth: c.width };
}

export default function OptimizationPage() {
  const { store } = useStore();
  const { data, error, loading, reload } = useApi<OptimizationSnapshot>(
    () =>
      store ? api.optimization(store) : Promise.resolve(null as unknown as OptimizationSnapshot),
    [store],
  );

  const [syncing, setSyncing] = useState(false);
  const [syncErr, setSyncErr] = useState<string | null>(null);
  const autoSynced = useRef<string | null>(null); // one auto-pull per store per mount

  async function runSync() {
    if (!store || syncing) return;
    setSyncing(true);
    setSyncErr(null);
    try {
      const res = await api.optimizationSync(store);
      if (!res.ok) setSyncErr(res.error || "Sync failed");
      reload();
    } catch (e) {
      setSyncErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  }

  // Auto-pull: the operator shouldn't have to click Sync. When the store's snapshot is missing or
  // stale (>30 min), pull it once automatically. The per-store guard stops it from re-firing after
  // the fresh snapshot lands (which resets synced_ago to ~0).
  useEffect(() => {
    if (!store || !data || syncing) return;
    const stale = data.synced_ago_seconds == null || data.synced_ago_seconds > AUTO_SYNC_TTL;
    if ((!data.has_snapshot || stale) && autoSynced.current !== store) {
      autoSynced.current = store;
      runSync();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store, data]);

  return (
    <div className="mx-auto max-w-[1400px]">
      <PageHeader
        title="Product Performance"
        subtitle="Track your product performance and Google Ads metrics — per product, in the store's own currency. All-time revenue (bounded by the store's data start date), refreshed automatically."
      />

      {syncErr ? (
        <Card className="mb-3 border-[var(--state-killed)] p-3 text-sm text-[var(--state-killed)]">
          {syncErr}
        </Card>
      ) : null}

      {loading && !data ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}
      {data ? <Body key={store} snap={data} syncing={syncing} onSync={runSync} onReload={reload} /> : null}
    </div>
  );
}

// ── Sortable column keys (Shopify-derived + product facets are sortable; ad columns are locked) ──
type SortKey =
  | "title"
  | "status"
  | "variants"
  | "conv"
  | "qty"
  | "cv"
  | "refunds"
  | "cogs"
  | "profit"
  | "margin"
  | "roas"
  | "published"
  // Ads layer (sortable once the Google Ads Script feeds the columns):
  | "cost"
  | "clicks"
  | "ctr"
  | "cpc"
  | "cpa"
  | "roas-7" | "spend-7" | "impr-7"
  | "roas-14" | "spend-14" | "impr-14"
  | "roas-30" | "spend-30" | "impr-30";

function Body({
  snap,
  syncing,
  onSync,
  onReload,
}: {
  snap: OptimizationSnapshot;
  syncing: boolean;
  onSync: () => void;
  onReload: () => void;
}) {
  const cur = snap.currency;
  // Ads layer live? True once the Google Ads Script has pushed per-product rows (or the
  // OAuth id is set) — unlocks cost/clicks/CTR/CPC/CPA/ROAS/spend/impressions everywhere.
  const adsOn = !!snap.ads_connected;
  // Which window the PRIMARY columns + KPI cards read (Pythago's date-range, on our
  // snapshot's rolling windows): 7D / 14D / 30D / All-time.
  const [rangeWin, setRangeWin] = useState<string>(RANGE_WIN);
  // Row selection → bulk actions (Set to Active / Draft / Archived), like Pythago.
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkMsg, setBulkMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // Fullscreen table (Pythago's ⛶).
  const [fs, setFs] = useState(false);
  const [pageSize, setPageSize] = useState<number>(PAGE_SIZE);
  const [status, setStatus] = useState<StatusFilter>("all");
  const [country, setCountry] = useState("all");
  // Seed the search from ?q= so the Alerts tab can deep-link "Open →" straight to a flagged product.
  const [q, setQ] = useState(() =>
    typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("q") || "" : "");
  const [showFilters, setShowFilters] = useState(false);
  const [revenue, setRevenue] = useState<Range>(EMPTY_RANGE);
  const [units, setUnits] = useState<Range>(EMPTY_RANGE);
  const [net, setNet] = useState<Range>(EMPTY_RANGE);
  const [refundsF, setRefundsF] = useState<Range>(EMPTY_RANGE);
  const [roasF, setRoasF] = useState<Range>(EMPTY_RANGE);
  const [tagsF, setTagsF] = useState<string[]>([]);
  const [sortKey, setSortKey] = useState<SortKey>("cv");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(0);
  const [hiddenCols, setHiddenCols] = useState<Set<string>>(() => new Set());
  const [saved, setSaved] = useState<SavedFilter[]>(() => loadSaved(snap.store));
  // Saved filters are now server-side (sync across devices). Seed from localStorage for instant
  // paint, then override with the server list once it loads.
  useEffect(() => {
    let alive = true;
    api.optimizationSavedFilters(snap.store)
      .then((r) => { if (alive && r?.filters) setSaved(r.filters as SavedFilter[]); })
      .catch(() => {});
    return () => { alive = false; };
  }, [snap.store]);
  // Per-row Notes — server-persisted (pm_optimization_flags), seeded from the snapshot (p.note) and
  // written through the flag endpoint, so they sync across devices.
  const [notes, setNotes] = useState<Record<string, string>>(
    () => Object.fromEntries(
      snap.products.filter((p) => p.note && p.product_id).map((p) => [p.product_id!, p.note as string]),
    ),
  );
  // Per-row app-side tag edits (optimistic; seeded from p.app_tags). {product_id: string[]}.
  const [appTagEdits, setAppTagEdits] = useState<Record<string, string[]>>({});
  // Transient "Task added ✓" confirmation, keyed by product_id.
  const [taskAdded, setTaskAdded] = useState<Record<string, boolean>>({});
  // Product Variant Breakdown modal (Pythago's variants-badge click) — the product whose modal is open.
  const [breakdown, setBreakdown] = useState<{ id: string; title: string } | null>(null);
  // Per-market (country) expand — product_ids whose globe breakdown is open.
  const [marketsOpen, setMarketsOpen] = useState<Set<string>>(new Set());
  function toggleMarkets(id: string) {
    setMarketsOpen((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  const titleOf = (id: string) => snap.products.find((p) => p.product_id === id)?.title;
  function saveNote(id: string, text: string) {
    const t = text.trim();
    setNotes((prev) => {
      const next = { ...prev };
      if (t) next[id] = t;
      else delete next[id];
      return next;
    });
    api.optimizationFlag(snap.store, id, { note: t, product_title: titleOf(id) }).catch(() => {});
  }

  // Tags — Shopify tags are read-only here; the app-side subset (app_tags) is editable per row.
  const effAppTags = (p: OptimizationProduct): string[] =>
    (p.product_id && appTagEdits[p.product_id]) || p.app_tags || [];
  const shopTagsOf = (p: OptimizationProduct): string[] =>
    (p.tags || []).filter((t) => !(p.app_tags || []).includes(t));
  const tagsOf = (p: OptimizationProduct): string[] =>
    [...new Set([...shopTagsOf(p), ...effAppTags(p)])].sort((a, b) => a.localeCompare(b));
  function setProductTags(id: string, title: string, tags: string[]) {
    setAppTagEdits((prev) => ({ ...prev, [id]: tags }));
    api.optimizationSetTags(snap.store, id, tags, title).catch(() => {});
  }
  async function addTask(p: OptimizationProduct) {
    if (!p.product_id) return;
    try {
      await api.tasksCreate({
        title: `Optimize: ${p.title}`,
        store_key: snap.store,
        detail: `Product ${p.product_id} — flagged from Product Performance.`,
        priority: "med",
      });
      setTaskAdded((prev) => ({ ...prev, [p.product_id!]: true }));
      setTimeout(() => setTaskAdded((prev) => { const n = { ...prev }; delete n[p.product_id!]; return n; }), 2500);
    } catch {
      /* non-fatal */
    }
  }

  const show = (key: string) => !hiddenCols.has(key);

  // Every distinct tag across the catalog (sorted) — powers the Tags multiselect.
  const allTags = useMemo(() => {
    const s = new Set<string>();
    for (const p of snap.products) for (const t of tagsOf(p)) s.add(t);
    return [...s].sort((a, b) => a.localeCompare(b));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snap.products, appTagEdits]);

  // Every country any product sold in — powers the Country filter (from the per-market breakdown).
  const allMarkets = useMemo(() => {
    const s = new Set<string>();
    for (const p of snap.products) for (const c of Object.keys(p.markets || {})) s.add(c);
    return [...s].sort();
  }, [snap.products]);

  const activeCount =
    (status !== "all" ? 1 : 0) +
    (country !== "all" ? 1 : 0) +
    (tagsF.length ? 1 : 0) +
    [revenue, units, net, refundsF, roasF].filter(rangeActive).length;

  // The window a row/metric reads: the whole product, or ONE country's slice when the Country
  // filter is set — so every number (table, KPIs, sort) reflects the chosen market.
  function w(p: OptimizationProduct, win: string): OptimizationWindow | undefined {
    if (country !== "all") return p.markets?.[country]?.[win];
    return p.windows[win];
  }

  const filtered = useMemo(() => {
    let rows = snap.products;
    if (status !== "all") rows = rows.filter((p) => (p.status || "") === status);
    if (tagsF.length)
      rows = rows.filter((p) => tagsF.every((t) => tagsOf(p).includes(t)));
    const needle = q.trim().toLowerCase();
    if (needle)
      rows = rows.filter(
        (p) =>
          (p.title || "").toLowerCase().includes(needle) ||
          (p.product_id || "").toLowerCase().includes(needle),
      );
    if (country !== "all") rows = rows.filter((p) => p.markets?.[country]); // only products sold there
    rows = rows.filter((p) => {
      const rw = w(p, rangeWin);
      if (rangeActive(roasF)) {
        const pv = roasOf(rw);
        if (pv == null || !inRange(pv, roasF)) return false;
      }
      return (
        inRange(rw?.cv ?? 0, revenue) &&
        inRange(rw?.qty ?? 0, units) &&
        inRange(rw?.net ?? 0, net) &&
        inRange(rw?.refunds ?? 0, refundsF)
      );
    });
    return rows;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snap.products, status, country, q, revenue, units, net, refundsF, roasF, tagsF, appTagEdits, rangeWin]);

  const sorted = useMemo(() => {
    const rows = [...filtered];
    const val = (p: OptimizationProduct): number | string => {
      const rw = w(p, rangeWin);
      // per-window ads sort keys: "roas-7" | "spend-7" | "impr-7" | …
      // w() is country-aware, so these quick-sorts follow the Country filter too.
      const m = /^(roas|spend|impr)-(\d+)$/.exec(sortKey);
      if (m) {
        const ww = w(p, m[2]);
        if (m[1] === "spend") return ww?.cost ?? 0;
        if (m[1] === "impr") return ww?.impressions ?? 0;
        return (ww?.cost ?? 0) > 0 ? (ww?.cv ?? 0) / (ww?.cost ?? 1) : -Infinity;
      }
      switch (sortKey) {
        case "title":
          return (p.title || "").toLowerCase();
        case "status":
          return p.status || "";
        case "variants":
          return p.variants_count ?? -1;
        case "conv":
          return rw?.orders ?? 0;
        case "qty":
          return rw?.qty ?? 0;
        case "cv":
          return rw?.cv ?? 0;
        case "refunds":
          return rw?.refunds ?? 0;
        case "cogs":
          return rw?.cog ?? 0;
        case "profit":
          return rw?.profit ?? 0;
        case "margin":
          return rw?.margin_pct ?? -Infinity;
        case "roas":
          return roasOf(rw) ?? -Infinity;
        case "published":
          return p.published_at ? Date.parse(p.published_at) || 0 : 0;
        case "cost":
          return rw?.cost ?? 0;
        case "clicks":
          return rw?.clicks ?? 0;
        case "ctr":
          return (rw?.impressions ?? 0) > 0 ? (rw?.clicks ?? 0) / (rw?.impressions ?? 1) : -1;
        case "cpc":
          return (rw?.clicks ?? 0) > 0 ? (rw?.cost ?? 0) / (rw?.clicks ?? 1) : -1;
        case "cpa":
          return (rw?.conversions ?? 0) > 0 ? (rw?.cost ?? 0) / (rw?.conversions ?? 1) : -1;
        default:
          return 0;
      }
    };
    rows.sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const cmp = typeof va === "string" || typeof vb === "string"
        ? String(va).localeCompare(String(vb))
        : (va as number) - (vb as number);
      return sortDir === "asc" ? cmp : -cmp;
    });
    return rows;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtered, sortKey, sortDir, rangeWin, country]);

  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const pageRows = sorted.slice(safePage * pageSize, safePage * pageSize + pageSize);

  // KPI rollup over the filtered set, RANGE window (the "filtered" cards, exactly like Pythago).
  const kpi = useMemo(() => {
    let qty = 0, cv = 0, refunds = 0, cost = 0;
    for (const p of filtered) {
      const rw = w(p, rangeWin);
      qty += rw?.qty ?? 0;
      cv += rw?.cv ?? 0;
      refunds += rw?.refunds ?? 0;
      cost += rw?.cost ?? 0;
    }
    return { count: filtered.length, qty, cv, refunds, cost };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtered, rangeWin, country]);

  function toggleSort(k: SortKey) {
    if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(k);
      setSortDir("desc");
    }
    setPage(0);
  }

  function clearFilters() {
    setStatus("all");
    setCountry("all");
    setRevenue(EMPTY_RANGE);
    setUnits(EMPTY_RANGE);
    setNet(EMPTY_RANGE);
    setRefundsF(EMPTY_RANGE);
    setRoasF(EMPTY_RANGE);
    setTagsF([]);
  }

  // Bulk Shopify status write on the selected rows, then re-sync so the table reflects it.
  // Passes each product's KNOWN current status so the change is recorded in the revertable history.
  async function bulkStatus(status: "ACTIVE" | "DRAFT" | "ARCHIVED") {
    const ids = [...sel];
    if (!ids.length || bulkBusy) return;
    setBulkBusy(true);
    setBulkMsg(null);
    try {
      const prev: Record<string, string> = {};
      const titles: Record<string, string> = {};
      for (const p of snap.products) {
        if (p.product_id && sel.has(p.product_id)) {
          const num = p.product_id.split("/").pop() || p.product_id;
          if (p.status) prev[num] = p.status;
          titles[num] = p.title;
        }
      }
      const res = await api.optimizationSetStatus(snap.store, ids, status, { prev, titles });
      const label = status.charAt(0) + status.slice(1).toLowerCase();
      const nFailed = res.failed?.length || 0;
      if (!res.ok || nFailed > 0) {
        setBulkMsg({ ok: false, text: `${res.updated} set to ${label}, ${nFailed} failed${res.failed?.[0] ? `: ${res.failed[0].error}` : ""}.` });
      } else {
        setBulkMsg({ ok: true, text: `${res.updated} product${res.updated === 1 ? "" : "s"} set to ${label}.` });
      }
      setSel(new Set());
      onReload();  // backend patches the snapshot in place → new statuses show without a full re-sync
    } catch (e) {
      setBulkMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBulkBusy(false);
    }
  }

  function currentFilterState(): FilterState {
    return { status, revenue, units, net, refundsF, roasF, tagsF };
  }
  function applyFilterState(f: FilterState) {
    setStatus(f.status);
    setRevenue(f.revenue);
    setUnits(f.units);
    setNet(f.net);
    setRefundsF(f.refundsF);
    setRoasF(f.roasF || EMPTY_RANGE);
    setTagsF(f.tagsF);
    setPage(0);
  }
  function saveCurrent(name: string) {
    const state = currentFilterState();
    const next = [
      ...saved.filter((s) => s.name !== name),
      { name, state },
    ].sort((a, b) => a.name.localeCompare(b.name));
    setSaved(next);
    api.optimizationSaveFilter(snap.store, name, state).catch(() => {});
  }
  function deleteSaved(name: string) {
    setSaved((prev) => prev.filter((s) => s.name !== name));
    api.optimizationDeleteFilter(snap.store, name).catch(() => {});
  }

  function toggleCol(key: string) {
    setHiddenCols((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  if (!snap.has_snapshot) {
    return (
      <Empty>
        <div className="font-semibold text-[var(--text)]">
          {syncing ? "Pulling this store's order history…" : "No snapshot yet for this store"}
        </div>
        <p className="mt-1">
          {syncing ? (
            "Building the all-time product-performance table from Shopify orders. This runs automatically — one moment."
          ) : (
            <>
              Pulling automatically. If it doesn&rsquo;t start, hit{" "}
              <button
                onClick={onSync}
                disabled={syncing}
                className="font-semibold text-[var(--accent)] underline disabled:opacity-50"
              >
                Sync now
              </button>{" "}
              to pull this store&rsquo;s full order history (from its data start date) and build the table.
              Needs this store&rsquo;s Shopify credentials set in Connections.
            </>
          )}
        </p>
      </Empty>
    );
  }
  if (snap.error) {
    return (
      <Card className="border-[var(--state-killed)] p-4">
        <div className="text-sm font-semibold text-[var(--state-killed)]">Snapshot error</div>
        <div className="mt-1 text-sm text-[var(--muted)]">{snap.error}</div>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {/* Toolbar card — mirrors Pythago's search/filter panel. */}
      <Card className="p-4">
        <div className="relative">
          <svg
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--muted)]"
            width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
          >
            <circle cx="11" cy="11" r="8" /><path d="m21 21-4.3-4.3" />
          </svg>
          <input
            value={q}
            onChange={(e) => { setQ(e.target.value); setPage(0); }}
            placeholder="Search by name, product ID, or variant ID…"
            className="w-full rounded-md border border-[var(--border)] bg-[var(--surface)] py-2 pl-9 pr-3 text-sm outline-none focus:border-[var(--accent)]"
          />
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold text-[var(--muted)]">Status:</span>
          <select
            value={status}
            onChange={(e) => { setStatus(e.target.value as StatusFilter); setPage(0); }}
            className="rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 text-xs"
          >
            <option value="all">All</option>
            <option value="ACTIVE">Active</option>
            <option value="DRAFT">Draft</option>
            <option value="ARCHIVED">Archived</option>
          </select>

          <span className="text-xs font-semibold text-[var(--muted)]">Country:</span>
          <select
            value={country}
            onChange={(e) => { setCountry(e.target.value); setPage(0); }}
            title="Show ONE market's numbers — the table, KPI cards and sort all switch to that country."
            className="rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 text-xs"
          >
            <option value="all">All Countries</option>
            {allMarkets.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>

          {/* Tags — real multiselect over the catalog's product tags. */}
          <Menu
            label={`Tags${tagsF.length ? ` (${tagsF.length})` : ""}`}
            active={tagsF.length > 0}
            width="w-56"
          >
            {allTags.length === 0 ? (
              <div className="px-3 py-2 text-xs text-[var(--muted)]">No tags on this catalog</div>
            ) : (
              <div className="max-h-64 overflow-y-auto py-1">
                {allTags.map((t) => (
                  <label key={t} className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-xs hover:bg-[var(--surface-2)]">
                    <input
                      type="checkbox"
                      checked={tagsF.includes(t)}
                      onChange={(e) => {
                        setTagsF((prev) => (e.target.checked ? [...prev, t] : prev.filter((x) => x !== t)));
                        setPage(0);
                      }}
                    />
                    <span className="truncate">{t}</span>
                  </label>
                ))}
              </div>
            )}
            {tagsF.length ? (
              <button
                onClick={() => { setTagsF([]); setPage(0); }}
                className="w-full border-t border-[var(--border)] px-3 py-1.5 text-left text-xs text-[var(--muted)] hover:text-[var(--text)]"
              >
                Clear tags
              </button>
            ) : null}
          </Menu>

          <button
            onClick={() => setShowFilters((v) => !v)}
            className={`inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-semibold transition-colors ${
              showFilters || activeCount
                ? "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] text-[var(--accent)]"
                : "border-[var(--border)] text-[var(--muted)] hover:text-[var(--text)]"
            }`}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="4" y1="6" x2="20" y2="6" /><line x1="7" y1="12" x2="17" y2="12" /><line x1="10" y1="18" x2="14" y2="18" />
            </svg>
            Filters{activeCount ? ` (${activeCount})` : ""}
          </button>

          {/* Saved filters — persisted to localStorage per store. */}
          <Menu label="Saved filters" width="w-64">
            {saved.length === 0 ? (
              <div className="px-3 py-2 text-xs text-[var(--muted)]">No saved filters yet</div>
            ) : (
              <div className="py-1">
                {saved.map((s) => (
                  <div key={s.name} className="flex items-center gap-1 px-2 py-1 text-xs hover:bg-[var(--surface-2)]">
                    <button onClick={() => applyFilterState(s.state)} className="flex-1 truncate px-1 py-0.5 text-left hover:text-[var(--accent)]">
                      {s.name}
                    </button>
                    <button onClick={() => deleteSaved(s.name)} title="Delete" className="px-1 text-[var(--muted)] hover:text-[var(--state-killed)]">
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}
            <button
              onClick={() => {
                if (!activeCount) return;
                const name = window.prompt("Name this filter set:");
                if (name && name.trim()) saveCurrent(name.trim());
              }}
              disabled={!activeCount}
              className="w-full border-t border-[var(--border)] px-3 py-1.5 text-left text-xs font-semibold text-[var(--accent)] hover:bg-[var(--surface-2)] disabled:opacity-40"
            >
              + Save current filters
            </button>
          </Menu>

          {activeCount ? (
            <button
              onClick={clearFilters}
              className="text-xs font-medium text-[var(--muted)] underline hover:text-[var(--text)]"
            >
              Clear
            </button>
          ) : null}
        </div>

        {showFilters ? (
          <div className="mt-3 grid grid-cols-1 gap-x-8 gap-y-4 border-t border-[var(--border)] pt-4 sm:grid-cols-2 lg:grid-cols-4">
            <FilterGroup title="Performance">
              <RangeRow label="Conversion value" sub={cur ?? "store currency"} value={revenue} onChange={(r) => { setRevenue(r); setPage(0); }} />
              <RangeRow label="Units" value={units} onChange={(r) => { setUnits(r); setPage(0); }} />
            </FilterGroup>
            <FilterGroup title="Profitability">
              <RangeRow label="Net" sub="conv value − refunds" value={net} onChange={(r) => { setNet(r); setPage(0); }} />
              <RangeRow label="Refunds" sub={cur ?? "store currency"} value={refundsF} onChange={(r) => { setRefundsF(r); setPage(0); }} />
            </FilterGroup>
            <FilterGroup title="Ad performance" locked>
              <RangeRow label="ROAS" sub="revenue ÷ ad spend" value={roasF} onChange={(r) => { setRoasF(r); setPage(0); }} />
              <RangeRow label="Ad cost" disabled />
            </FilterGroup>
            <FilterGroup title="Catalog">
              <DisabledRow label="Published from / to" hint="date range — soon" />
              <DisabledRow label="Product type" hint="soon" />
            </FilterGroup>
          </div>
        ) : null}

        {/* range + synced + export/sync row */}
        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 border-t border-[var(--border)] pt-3 text-xs text-[var(--muted)]">
          <span className="inline-flex items-center gap-1.5 font-medium text-[var(--text)]" title="Which rolling window the primary columns + KPI cards read (bounded below by the store's data start date)">
            Performance Data ·
            <select
              value={rangeWin}
              onChange={(e) => { setRangeWin(e.target.value); setPage(0); }}
              className="rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-1.5 py-0.5 text-xs font-semibold text-[var(--text)] focus:border-[var(--accent)] focus:outline-none"
              aria-label="Primary range"
            >
              <option value="7">Last 7 days</option>
              <option value="14">Last 14 days</option>
              <option value="30">Last 30 days</option>
              <option value="all">All-time</option>
            </select>
            <span className="text-[var(--muted)]">·</span>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="4" width="18" height="18" rx="2" /><path d="M16 2v4M8 2v4M3 10h18" />
            </svg>
            {allTimeLabel(snap)}
          </span>
          {snap.truncated ? (
            <span className="text-[var(--state-killed)]">(capped — totals are a floor)</span>
          ) : null}
          <span className="ml-auto inline-flex items-center gap-3">
            <span>
              Last synced: <strong className="text-[var(--text)]">{fmtAgo(snap.synced_ago_seconds)}</strong>
              {" · "}{snap.orders_scanned.toLocaleString()} orders
            </span>
            <button
              onClick={() => exportCsv(sorted, snap)}
              className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border)] px-2.5 py-1.5 font-semibold text-[var(--muted)] hover:text-[var(--text)]"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />
              </svg>
              Export CSV
            </button>
            <button
              onClick={onSync}
              disabled={syncing}
              className="inline-flex items-center gap-1.5 rounded-md border border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] px-2.5 py-1.5 font-semibold hover:bg-[color-mix(in_srgb,var(--accent)_22%,transparent)] disabled:opacity-50"
            >
              {syncing ? "Syncing…" : "Sync now"}
            </button>
          </span>
        </div>
      </Card>

      {/* Dual-currency banner (Pythago shows both feeds; ours is honest about the ads feed). */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_8%,transparent)] px-4 py-2.5 text-sm">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3M12 17h.01" />
        </svg>
        <span className="inline-flex items-center gap-1.5 font-semibold" style={{ color: ADS_COLOR }}>
          <span className="h-2 w-2 rounded-full" style={{ background: ADS_COLOR }} />Google Ads:
        </span>
        <span className={adsOn ? "text-[var(--text)]" : "text-[var(--muted)]"}>
          {adsOn
            ? snap.ads_source === "reconciled"
              ? "connected · total tied to P&L, per-product by attribution + revenue share"
              : snap.ads_source === "script"
                ? "connected · Ads Script"
                : snap.ads_source === "allocated"
                  ? "connected · spend allocated by revenue (estimate)"
                  : "connected"
            : "not connected"}
        </span>
        <span className="text-[var(--muted)]">|</span>
        <span className="inline-flex items-center gap-1.5 font-semibold" style={{ color: SHOPIFY_COLOR }}>
          <span className="h-2 w-2 rounded-full" style={{ background: SHOPIFY_COLOR }} />Shopify:
        </span>
        <span className="text-[var(--text)]">{cur ?? "—"}</span>
        <span className="text-[var(--muted)]">|</span>
        <span className="inline-flex items-center gap-1.5 font-semibold" style={{ color: APP_COLOR }}>
          <span className="h-2 w-2 rounded-full" style={{ background: APP_COLOR }} />App-calc:
        </span>
        <span className="text-[var(--muted)]">Profit · Profit % · <span title="Per-product landed cost from Product Management (invoice-derived pm_product_costs / per-market pm_product_costs_market), joined into this view — the same COGS the Margin tools use." className="cursor-help font-semibold text-[var(--text)] underline decoration-dotted underline-offset-2">COGS from Product Management</span></span>
        {!adsOn ? (
          <span className="text-xs text-[var(--muted)]">
            (ad-cost, ROAS &amp; impressions layer in once the Google Ads Script runs — copy it from
            the store&apos;s Google Ads tab in Connections)
          </span>
        ) : null}
      </div>

      {/* KPI cards — the 6 Pythago cards, 1:1. Ad-derived cards read "—" until the ads layer runs. */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <KpiCard value={fmtInt(kpi.count)} label="Products Filtered" />
        {adsOn ? (
          <KpiCard value={fmtMoney(kpi.cost, cur)} label="Product Ad Spend (Filtered)" />
        ) : (
          <KpiCard value="—" label="Product Ad Spend (Filtered)" footer={<span className="text-[10px] text-[var(--muted)]">Run the Google Ads Script · Connections</span>} muted />
        )}
        <KpiCard value={fmtInt(kpi.qty)} label="Shopify · Quantity (Items)" />
        <KpiCard value={fmtMoney(kpi.cv, cur)} label="Shopify · Conversion Value" />
        <KpiCard value={fmtMoney(kpi.refunds, cur)} label="Refunds" tone="killed" />
        {adsOn ? (
          <KpiCard value={fmtRoas(kpi.cv, kpi.cost)} label="ROAS based on Shopify Conversion Value" />
        ) : (
          <KpiCard value="—" label="ROAS based on Shopify Conversion Value" muted />
        )}
      </div>

      {/* Product Performance Data table (⛶ = fullscreen takeover, like Pythago) */}
      <Card className={fs
        ? "fixed inset-0 z-[70] flex flex-col overflow-y-auto rounded-none"
        : "overflow-hidden"}>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] px-4 py-3">
          <div>
            <div className="text-sm font-semibold">Product Performance Data</div>
            <div className="flex items-center gap-2 text-xs text-[var(--muted)]">
              <span>Showing {pageRows.length.toLocaleString()} of {sorted.length.toLocaleString()} products</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {/* Actions — bulk Shopify status writes on the SELECTED rows (like Pythago), plus
                the filtered-set utilities. */}
            <Menu label="Actions" width="w-56" align="right">
              <div className="py-1">
                <MenuItem onClick={() => bulkStatus("DRAFT")} disabled={!sel.size || bulkBusy}>
                  {bulkBusy ? "Working…" : `Set to Draft (${sel.size} selected)`}
                </MenuItem>
                <MenuItem onClick={() => bulkStatus("ARCHIVED")} disabled={!sel.size || bulkBusy}>
                  Set to Archived ({sel.size} selected)
                </MenuItem>
                <div className="my-1 border-t border-[var(--border)]" />
                <MenuItem onClick={() => exportCsv(sorted, snap)}>Export CSV (filtered)</MenuItem>
                <MenuItem onClick={() => copyIds(sorted)}>Copy product IDs</MenuItem>
                <MenuItem onClick={clearFilters} disabled={!activeCount}>Clear all filters</MenuItem>
              </div>
            </Menu>
            {/* Fullscreen table (Pythago's ⛶) */}
            <button
              type="button"
              onClick={() => setFs((v) => !v)}
              title={fs ? "Exit fullscreen" : "Fullscreen table"}
              className="rounded-lg border border-[var(--border)] px-2.5 py-1.5 text-xs font-semibold hover:bg-[var(--surface-2)]"
            >
              {fs ? "✕" : "⛶"}
            </button>
            {/* Column config — real show/hide. */}
            <Menu label="Columns" width="w-56" align="right" icon="cols">
              <div className="max-h-72 overflow-y-auto py-1">
                {COLS.map((c) => (
                  <label key={c.key} className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-xs hover:bg-[var(--surface-2)]">
                    <input type="checkbox" checked={show(c.key)} onChange={() => toggleCol(c.key)} />
                    <span>{c.label}</span>
                  </label>
                ))}
              </div>
              {hiddenCols.size ? (
                <button
                  onClick={() => setHiddenCols(new Set())}
                  className="w-full border-t border-[var(--border)] px-3 py-1.5 text-left text-xs text-[var(--muted)] hover:text-[var(--text)]"
                >
                  Show all columns
                </button>
              ) : null}
            </Menu>
          </div>
        </div>

        {bulkMsg ? (
          <div className={`mb-3 flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm ${bulkMsg.ok ? "border-[var(--state-winner)] text-[var(--state-winner)]" : "border-[var(--state-killed)] text-[var(--state-killed)]"}`}>
            <span>{bulkMsg.text}</span>
            <button type="button" onClick={() => setBulkMsg(null)} className="shrink-0 text-[var(--muted)] hover:text-[var(--text)]" aria-label="Dismiss">✕</button>
          </div>
        ) : null}

        <div className="hscroll">
          <table className="w-full text-[15px]">
            <thead>
              <tr className="border-b-2 border-[var(--border)] text-left text-[13px] font-bold uppercase tracking-wide text-[var(--text)]">
                <th className="sticky left-0 z-20 w-8 bg-[var(--surface)] px-2 py-3" style={{ width: 32, minWidth: 32 }}>
                  <input
                    type="checkbox"
                    aria-label="Select all on page"
                    checked={pageRows.length > 0 && pageRows.every((p) => !p.product_id || sel.has(p.product_id))}
                    onChange={(e) => {
                      const next = new Set(sel);
                      for (const p of pageRows) {
                        if (!p.product_id) continue;
                        if (e.target.checked) next.add(p.product_id);
                        else next.delete(p.product_id);
                      }
                      setSel(next);
                    }}
                  />
                </th>
                <SortTh label="Product" k="title" cur={{ sortKey, sortDir }} onSort={toggleSort} freeze={FZ.product} />
                {show("status") && <SortTh label="Status" k="status" cur={{ sortKey, sortDir }} onSort={toggleSort} freeze={FZ.status} />}
                {show("variants") && <SortTh label="Variants" k="variants" cur={{ sortKey, sortDir }} onSort={toggleSort} right freeze={FZ.variants} freezeLast />}
                {show("qty") && <SortTh label="Qty." k="qty" cur={{ sortKey, sortDir }} onSort={toggleSort} right tint="shopify" />}
                {show("conv") && <SortTh label="Conv." k="conv" cur={{ sortKey, sortDir }} onSort={toggleSort} right tint="shopify" />}
                {show("cost") && <AdsTh label="Ad Spend" on={adsOn} k="cost" cur={{ sortKey, sortDir }} onSort={toggleSort} />}
                {show("cv") && <SortTh label="Revenue" k="cv" cur={{ sortKey, sortDir }} onSort={toggleSort} right tint="shopify" />}
                {show("refunds") && <SortTh label="Refunds" k="refunds" cur={{ sortKey, sortDir }} onSort={toggleSort} right tint="shopify" />}
                {show("cogs") && <SortTh label="COGS" k="cogs" cur={{ sortKey, sortDir }} onSort={toggleSort} right tint="app" />}
                {show("profit") && <SortTh label="Profit" k="profit" cur={{ sortKey, sortDir }} onSort={toggleSort} right tint="app" />}
                {show("margin") && <SortTh label="Profit %" k="margin" cur={{ sortKey, sortDir }} onSort={toggleSort} right tint="app" />}
                {show("roas") && <SortTh label="ROAS" k="roas" cur={{ sortKey, sortDir }} onSort={toggleSort} right tint="ads" />}
                {show("windows") && WINDOWS.map((win) => (
                  <Fragment key={`h${win}`}>
                    <AdsTh label={`ROAS ${WINDOW_LABEL[win]}`} on={adsOn} k={`roas-${win}` as SortKey} cur={{ sortKey, sortDir }} onSort={toggleSort} />
                    <AdsTh label={`Spend ${WINDOW_LABEL[win]}`} on={adsOn} k={`spend-${win}` as SortKey} cur={{ sortKey, sortDir }} onSort={toggleSort} />
                    <th className="whitespace-nowrap px-3 py-3 text-right font-bold" style={{ color: SHOPIFY_COLOR }}>
                      Rev {WINDOW_LABEL[win]}
                    </th>
                  </Fragment>
                ))}
                {show("published") && <SortTh label="Published" k="published" cur={{ sortKey, sortDir }} onSort={toggleSort} />}
              </tr>
            </thead>
            <tbody>
              {pageRows.map((p) => (
                <Row
                  key={p.product_id || p.title}
                  p={p}
                  cur={cur}
                  rangeWin={rangeWin}
                  winKey={w}
                  show={show}
                  adsOn={adsOn}
                  selected={!!p.product_id && sel.has(p.product_id)}
                  note={(p.product_id && notes[p.product_id]) || ""}
                  onSaveNote={(t) => { if (p.product_id) saveNote(p.product_id, t); }}
                  onOpenBreakdown={() => { if (p.product_id) setBreakdown({ id: p.product_id, title: p.title }); }}
                  marketsOpen={!!p.product_id && marketsOpen.has(p.product_id)}
                  onToggleMarkets={() => { if (p.product_id) toggleMarkets(p.product_id); }}
                  onSelect={() => {
                    if (!p.product_id) return;
                    const next = new Set(sel);
                    if (next.has(p.product_id)) next.delete(p.product_id);
                    else next.add(p.product_id);
                    setSel(next);
                  }}
                  store={snap.store}
                  appTags={effAppTags(p)}
                  tagSuggestions={allTags}
                  onSetTags={(tags) => { if (p.product_id) setProductTags(p.product_id, p.title, tags); }}
                  onAddTask={() => addTask(p)}
                  taskAdded={!!p.product_id && !!taskAdded[p.product_id]}
                  onReload={onReload}
                />
              ))}
            </tbody>
          </table>
        </div>

        {pageRows.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-[var(--muted)]">
            No products match these filters.
          </div>
        ) : null}

        {/* Pagination — numbered pages + page-size selector, Pythago-style */}
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--border)] px-4 py-3 text-xs text-[var(--muted)]">
          <span>
            Showing {sorted.length === 0 ? 0 : safePage * pageSize + 1}–
            {Math.min((safePage + 1) * pageSize, sorted.length)} of {sorted.length.toLocaleString()} products
          </span>
          <div className="flex items-center gap-1">
            <PageBtn disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>
              ‹ Prev
            </PageBtn>
            {Array.from({ length: Math.min(5, pageCount) }, (_, i) => {
              // sliding window of page numbers centred on the current page
              const start = Math.max(0, Math.min(safePage - 2, pageCount - 5));
              const n = start + i;
              if (n >= pageCount) return null;
              return (
                <button
                  key={n}
                  type="button"
                  onClick={() => setPage(n)}
                  className={`rounded-md px-2.5 py-1 font-semibold ${
                    n === safePage
                      ? ""
                      : "border border-[var(--border)] text-[var(--muted)] hover:bg-[var(--surface-2)]"
                  }`}
                  style={n === safePage ? { background: "var(--accent)", color: "var(--accent-fg)" } : undefined}
                >
                  {n + 1}
                </button>
              );
            })}
            <PageBtn disabled={safePage >= pageCount - 1} onClick={() => setPage(safePage + 1)}>
              Next ›
            </PageBtn>
          </div>
          <label className="flex items-center gap-1.5">
            Show
            <select
              value={pageSize}
              onChange={(e) => { setPageSize(Number(e.target.value)); setPage(0); }}
              className="rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-1.5 py-0.5 text-xs focus:border-[var(--accent)] focus:outline-none"
            >
              {[10, 25, 50, 100].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
            per page
          </label>
        </div>
      </Card>

      {breakdown ? (
        <VariantModal
          store={snap.store}
          productId={breakdown.id}
          title={breakdown.title}
          cur={cur}
          onClose={() => setBreakdown(null)}
        />
      ) : null}
    </div>
  );
}

// ── Product Variant Breakdown modal (Pythago's variants-badge click) ─────────
function VariantModal({
  store,
  productId,
  title,
  cur,
  onClose,
}: {
  store: string;
  productId: string;
  title: string;
  cur: string | null;
  onClose: () => void;
}) {
  const [days, setDays] = useState(30);
  const { data, loading, error } = useApi(() => api.pmVariantBreakdown(store, productId, days), [store, productId, days]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  const kpi = data?.kpi;
  const rows = data?.variants ?? [];
  // Net = the TRUE bottom line: revenue − refunds − COGS − ad cost. (The old "profit" stopped at
  // revenue − COGS and never subtracted ad spend.) Null when COGS is unknown. ad_cost null → 0.
  const rowNet = (v: VariantBreakdownRow): number | null =>
    v.cogs == null ? null : v.revenue - v.refunds - v.cogs - (v.ad_cost ?? 0);
  const rowMargin = (v: VariantBreakdownRow): number | null => {
    const n = rowNet(v);
    return n == null || !v.revenue ? null : Math.round((n / v.revenue) * 1000) / 10;
  };
  const kpiNet = !kpi || kpi.cogs == null ? null : kpi.net - kpi.cogs - (kpi.ad_cost ?? 0);
  const kpiMargin = kpiNet == null || !kpi?.revenue ? null : Math.round((kpiNet / kpi.revenue) * 1000) / 10;
  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:p-8" onClick={onClose}>
      <div className="w-full max-w-5xl rounded-xl border border-[var(--border)] bg-[var(--surface)] shadow-2xl" onClick={(e) => e.stopPropagation()}>
        {/* header */}
        <div className="flex items-start justify-between gap-4 border-b border-[var(--border)] px-5 py-4">
          <div className="min-w-0">
            <div className="text-lg font-bold">Product Variant Breakdown</div>
            <div className="mt-0.5 truncate text-sm text-[var(--text)]">{data?.product_title || title}</div>
            <div className="text-xs text-[var(--muted)]">ID: {productId}</div>
          </div>
          <button type="button" onClick={onClose} className="rounded-md p-1.5 text-[var(--muted)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]" title="Close">✕</button>
        </div>

        {loading && !data ? (
          <div className="px-5 py-10 text-center text-sm text-[var(--muted)]">Loading variant performance…</div>
        ) : error ? (
          <div className="px-5 py-10 text-center text-sm text-[var(--state-killed)]">{error}</div>
        ) : (
          <>
            {/* KPI cards — P&L order: units → revenue → refunds → COGS → ad cost → NET (after ad) → margin */}
            <div className="grid grid-cols-2 gap-3 px-5 py-4 sm:grid-cols-3 lg:grid-cols-8">
              <MiniKpi label="Variants" value={fmtInt(kpi?.variants)} />
              <MiniKpi label="Units" value={fmtInt(kpi?.units)} />
              <MiniKpi label="Revenue" value={fmtMoney(kpi?.revenue, cur)} />
              <MiniKpi label="Refunds" value={kpi?.refunds ? `-${fmtMoney(kpi.refunds, cur)}` : fmtMoney(0, cur)} tone={kpi?.refunds ? "killed" : undefined} />
              <MiniKpi label="COGS" value={kpi?.cogs == null ? "—" : fmtMoney(kpi.cogs, cur)} />
              <MiniKpi label="Ad Cost" value={kpi?.ad_cost == null ? "—" : fmtMoney(kpi.ad_cost, cur)} />
              <MiniKpi label="Net" value={kpiNet == null ? "—" : fmtMoney(kpiNet, cur)} tone={kpiNet != null && kpiNet < 0 ? "killed" : undefined} />
              <MiniKpi label="Margin %" value={kpiMargin == null ? "—" : `${kpiMargin}%`} tone={kpiMargin != null && kpiMargin < 0 ? "killed" : undefined} />
            </div>

            {/* window toggle */}
            <div className="flex items-center gap-2 px-5 pb-2">
              <span className="text-sm font-semibold">Variant Performance Details</span>
              <span className="text-xs text-[var(--muted)]">· {rows.length} variant{rows.length === 1 ? "" : "s"}</span>
              <div className="ml-auto flex overflow-hidden rounded-lg border border-[var(--border)]">
                {[7, 14, 30].map((d) => (
                  <button
                    key={d}
                    onClick={() => setDays(d)}
                    className={`px-3 py-1.5 text-xs font-semibold ${days === d ? "bg-[var(--accent)] text-[var(--accent-fg)]" : "text-[var(--muted)] hover:text-[var(--text)]"}`}
                  >
                    {d} Days
                  </button>
                ))}
              </div>
            </div>

            {/* variant table */}
            <div className="max-h-[52vh] overflow-auto px-2 pb-4">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-[var(--surface)]">
                  <tr className="border-b border-[var(--border)] text-left text-[11px] font-bold uppercase tracking-wide text-[var(--muted)] [&_th]:whitespace-nowrap">
                    <th className="px-3 py-2">Product</th>
                    <th className="px-3 py-2 text-right">Price</th>
                    <th className="px-3 py-2 text-right">Units</th>
                    <th className="px-3 py-2 text-right">Revenue</th>
                    <th className="px-3 py-2 text-right">COGS</th>
                    <th className="px-3 py-2 text-right">Refunds</th>
                    <th className="px-3 py-2 text-right" title={ADS_HINT}>Ad Cost</th>
                    <th className="px-3 py-2 text-right">Net</th>
                    <th className="px-3 py-2 text-right">Margin %</th>
                    <th className="px-3 py-2 text-right" title={ADS_HINT}>ROAS</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((v) => (
                    <tr key={v.variant_id} className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)]">
                      <td className="px-3 py-2">
                        <div className="font-medium">{v.name}</div>
                        {v.sku ? <div className="text-[11px] text-[var(--muted)]">SKU: {v.sku}</div> : null}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">{v.price == null ? "—" : fmtMoney(v.price, cur)}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{fmtInt(v.units)}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{fmtMoney(v.revenue, cur)}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-[var(--muted)]">{v.cogs == null ? "—" : fmtMoney(v.cogs, cur)}</td>
                      <td className="px-3 py-2 text-right tabular-nums" style={{ color: v.refunds > 0 ? "var(--state-killed)" : undefined }}>
                        {v.refunds > 0 ? `-${fmtMoney(v.refunds, cur)}` : fmtMoney(0, cur)}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-[var(--muted)]">{v.ad_cost == null ? "—" : fmtMoney(v.ad_cost, cur)}</td>
                      <td className="px-3 py-2 text-right font-semibold tabular-nums" style={{ color: rowNet(v) == null ? undefined : rowNet(v)! >= 0 ? "var(--accent)" : "var(--state-killed)" }}>
                        {rowNet(v) == null ? "—" : fmtMoney(rowNet(v)!, cur)}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums" style={{ color: rowMargin(v) == null ? undefined : rowMargin(v)! >= 0 ? "var(--accent)" : "var(--state-killed)" }}>
                        {rowMargin(v) == null ? "—" : `${rowMargin(v)}%`}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums" style={{ color: v.roas == null ? undefined : v.roas >= 1 ? "var(--accent)" : "var(--state-killed)" }}>{v.roas == null ? "—" : `${v.roas}x`}</td>
                    </tr>
                  ))}
                  {rows.length === 0 ? (
                    <tr><td colSpan={10} className="px-3 py-8 text-center text-sm text-[var(--muted)]">No variants found for this product.</td></tr>
                  ) : null}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between gap-2 border-t border-[var(--border)] px-5 py-3 text-xs text-[var(--muted)]">
              <span>Sales windowed to the last {days} days · <strong>Net = Revenue − COGS − Refunds − Ad Cost</strong> (true bottom line) · Margin % = Net ÷ Revenue · refunds + ad spend allocated across variants by revenue share (Google Ads has no per-variant data)</span>
              <button type="button" onClick={onClose} className="rounded-lg bg-[var(--accent)] px-4 py-1.5 text-sm font-semibold text-[var(--accent-fg)]">Close</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function MiniKpi({ label, value, tone }: { label: string; value: string; tone?: "killed" }) {
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2.5 text-center">
      <div className={`text-lg font-bold tabular-nums ${tone === "killed" ? "text-[var(--state-killed)]" : "text-[var(--text)]"}`}>{value}</div>
      <div className="mt-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">{label}</div>
    </div>
  );
}

// ── Table row ───────────────────────────────────────────────────────────────
function Row({
  p,
  cur,
  rangeWin,
  winKey,
  show,
  adsOn,
  selected,
  note,
  onSaveNote,
  onOpenBreakdown,
  marketsOpen,
  onToggleMarkets,
  onSelect,
  store,
  appTags,
  tagSuggestions,
  onSetTags,
  onAddTask,
  taskAdded,
  onReload,
}: {
  p: OptimizationProduct;
  cur: string | null;
  rangeWin: string;
  winKey: (p: OptimizationProduct, win: string) => OptimizationWindow | undefined;
  show: (key: string) => boolean;
  adsOn: boolean;
  selected: boolean;
  note: string;
  onSaveNote: (text: string) => void;
  onOpenBreakdown: () => void;
  marketsOpen: boolean;
  onToggleMarkets: () => void;
  onSelect: () => void;
  store: string;
  appTags: string[];
  tagSuggestions: string[];
  onSetTags: (tags: string[]) => void;
  onAddTask: () => void;
  taskAdded: boolean;
  onReload: () => void;
}) {
  const rw = winKey(p, rangeWin);
  const [noteOpen, setNoteOpen] = useState(false);
  const [tagOpen, setTagOpen] = useState(false);
  const [tagDraft, setTagDraft] = useState("");
  const [histOpen, setHistOpen] = useState(false);
  const [hist, setHist] = useState<OptHistoryEntry[] | null>(null);
  const [histBusy, setHistBusy] = useState(false);
  const [statusOpen, setStatusOpen] = useState(false);
  const [statusBusy, setStatusBusy] = useState(false);
  const [statusErr, setStatusErr] = useState<string | null>(null);
  async function loadHist() {
    if (!p.product_id) return;
    setHistBusy(true);
    try {
      const r = await api.optimizationHistory(store, p.product_id);
      setHist(r.entries || []);
    } catch {
      setHist([]);
    } finally {
      setHistBusy(false);
    }
  }
  function addTag() {
    const t = tagDraft.trim();
    if (t && !appTags.includes(t)) onSetTags([...appTags, t]);
    setTagDraft("");
    setTagOpen(false);
  }
  // Per-market rows for the CURRENT window, richest first (by revenue).
  const marketRows = Object.entries(p.markets || {})
    .map(([code, wins]) => ({ code, w: wins[rangeWin] }))
    .filter((m) => m.w && ((m.w.cv ?? 0) > 0 || (m.w.qty ?? 0) > 0 || (m.w.refunds ?? 0) > 0))
    .sort((a, b) => (b.w!.cv ?? 0) - (a.w!.cv ?? 0));
  const [noteDraft, setNoteDraft] = useState(note);
  const statusLabel = p.status ? p.status.charAt(0) + p.status.slice(1).toLowerCase() : null;
  // Clickable status pill (Pythago parity): pick a status → confirm → write to Shopify → reload.
  // The backend patches the snapshot in place, so the new status shows on reload without a re-sync.
  async function changeStatus(next: "ACTIVE" | "DRAFT" | "ARCHIVED") {
    setStatusOpen(false);
    if (!p.product_id || statusBusy || next === (p.status || "")) return;
    const to = next.charAt(0) + next.slice(1).toLowerCase();
    if (typeof window !== "undefined" && !window.confirm(`Change "${p.title}" from ${statusLabel || "—"} to ${to}?`)) return;
    setStatusBusy(true);
    setStatusErr(null);
    try {
      const num = p.product_id.split("/").pop() || p.product_id;
      const res = await api.optimizationSetStatus(store, [p.product_id], next, {
        prev: p.status ? { [num]: p.status } : undefined,
        titles: { [num]: p.title },
      });
      // Only treat it as done when Shopify actually applied it — otherwise surface the reason and
      // leave the pill unchanged (don't reload into a false "it worked").
      if (!res.ok || res.updated < 1 || (res.failed && res.failed.length > 0)) {
        setStatusErr(res.failed?.[0]?.error || "Shopify rejected the status change.");
        return;
      }
      onReload();
    } catch (e) {
      setStatusErr(e instanceof Error ? e.message : String(e));
    } finally {
      setStatusBusy(false);
    }
  }
  return (
    <>
    <tr className="group border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)]">
      <td className="sticky left-0 z-10 w-8 bg-[var(--surface)] px-2 py-2 group-hover:bg-[var(--surface-2)]" style={{ width: 32, minWidth: 32 }}>
        <input type="checkbox" aria-label="Select row" checked={selected} onChange={onSelect} disabled={!p.product_id} />
      </td>
      <td className="sticky z-10 bg-[var(--surface)] px-3 py-2 group-hover:bg-[var(--surface-2)]" style={fzStyle(FZ.product)}>
        <div className="flex items-start gap-2.5">
          {p.image ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={p.image} alt="" className="h-11 w-11 flex-shrink-0 rounded-md border border-[var(--border)] object-cover" />
          ) : (
            <div className="h-11 w-11 flex-shrink-0 rounded-md border border-[var(--border)] bg-[var(--surface-2)]" />
          )}
          <div className="min-w-0">
            <span className="line-clamp-2 max-w-[260px] font-medium leading-snug">{p.title}</span>
            {/* Per-row action row (Pythago parity) — all live: note, tags, country breakdown,
                add-task, history. */}
            <div className="relative mt-1 flex flex-wrap items-center gap-0.5">
              {isOptimized(p) ? (
                <span className="mr-1 inline-flex items-center gap-0.5 rounded border border-[var(--state-live)] bg-[color-mix(in_srgb,var(--state-live)_12%,transparent)] px-1 py-0.5 text-[10px] font-semibold text-[var(--state-live)]">
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
                  Optimized
                </span>
              ) : null}
              {taskAdded ? (
                <span className="mr-1 inline-flex items-center gap-0.5 rounded border border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] px-1 py-0.5 text-[10px] font-semibold text-[var(--accent)]">Task added ✓</span>
              ) : null}
              <RowIcon title={note ? "Edit note" : "Add note"} active={!!note} kind="notes" onClick={() => { setNoteDraft(note); setNoteOpen((v) => !v); }} />
              <RowIcon title="Add tag" active={appTags.length > 0} kind="tag" onClick={() => setTagOpen((v) => !v)} />
              <RowIcon title={marketsOpen ? "Hide per-market breakdown" : "Per-market (country) breakdown"} active={marketsOpen} kind="globe" onClick={onToggleMarkets} />
              <RowIcon title="Add task" kind="task" onClick={onAddTask} />
              <RowIcon title="History — changes" active={histOpen} kind="history" onClick={() => { const n = !histOpen; setHistOpen(n); if (n) loadHist(); }} />
              {appTags.length ? (
                <span className="ml-1 inline-flex flex-wrap items-center gap-1">
                  {appTags.map((t) => (
                    <span key={t} className="inline-flex items-center gap-0.5 rounded border border-[var(--border)] bg-[var(--surface-2)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--muted)]">
                      {t}
                      <button type="button" aria-label={`Remove tag ${t}`} onClick={() => onSetTags(appTags.filter((x) => x !== t))} className="ml-0.5 leading-none hover:text-[var(--state-killed)]">×</button>
                    </span>
                  ))}
                </span>
              ) : null}
              {noteOpen ? (
                <div className="absolute left-0 top-7 z-20 w-56 rounded-lg border border-[var(--border)] bg-[var(--surface)] p-2 text-left shadow-lg">
                  <textarea
                    value={noteDraft}
                    onChange={(e) => setNoteDraft(e.target.value)}
                    rows={3}
                    autoFocus
                    placeholder="Note for this product…"
                    className="w-full resize-none rounded-md border border-[var(--border)] bg-[var(--surface-2)] p-1.5 text-xs outline-none focus:border-[var(--accent)]"
                  />
                  <div className="mt-1.5 flex items-center justify-end gap-1.5">
                    <button type="button" onClick={() => setNoteOpen(false)} className="rounded px-2 py-1 text-[11px] text-[var(--muted)] hover:text-[var(--text)]">Cancel</button>
                    <button type="button" onClick={() => { onSaveNote(noteDraft); setNoteOpen(false); }} className="rounded bg-[var(--accent)] px-2 py-1 text-[11px] font-semibold text-[var(--accent-fg)]">Save</button>
                  </div>
                </div>
              ) : null}
              {tagOpen ? (
                <div className="absolute left-0 top-7 z-20 w-56 rounded-lg border border-[var(--border)] bg-[var(--surface)] p-2 text-left shadow-lg">
                  <input
                    value={tagDraft}
                    onChange={(e) => setTagDraft(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") addTag(); if (e.key === "Escape") setTagOpen(false); }}
                    autoFocus
                    placeholder="Add a tag…"
                    className="w-full rounded-md border border-[var(--border)] bg-[var(--surface-2)] p-1.5 text-xs outline-none focus:border-[var(--accent)]"
                  />
                  {tagSuggestions.filter((t) => !appTags.includes(t) && t.toLowerCase().includes(tagDraft.trim().toLowerCase())).slice(0, 6).length ? (
                    <div className="mt-1.5 flex flex-wrap gap-1">
                      {tagSuggestions.filter((t) => !appTags.includes(t) && t.toLowerCase().includes(tagDraft.trim().toLowerCase())).slice(0, 6).map((t) => (
                        <button key={t} type="button" onClick={() => { onSetTags([...appTags, t]); setTagDraft(""); }} className="rounded border border-[var(--border)] bg-[var(--surface-2)] px-1.5 py-0.5 text-[10px] text-[var(--muted)] hover:border-[var(--accent)] hover:text-[var(--text)]">+ {t}</button>
                      ))}
                    </div>
                  ) : null}
                  <div className="mt-1.5 flex items-center justify-end gap-1.5">
                    <button type="button" onClick={() => setTagOpen(false)} className="rounded px-2 py-1 text-[11px] text-[var(--muted)] hover:text-[var(--text)]">Close</button>
                    <button type="button" onClick={addTag} disabled={!tagDraft.trim()} className="rounded bg-[var(--accent)] px-2 py-1 text-[11px] font-semibold text-[var(--accent-fg)] disabled:opacity-40">Add</button>
                  </div>
                </div>
              ) : null}
              {histOpen ? (
                <div className="absolute left-0 top-7 z-20 max-h-64 w-72 overflow-auto rounded-lg border border-[var(--border)] bg-[var(--surface)] p-2 text-left shadow-lg">
                  <div className="mb-1 px-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--muted)]">Change history</div>
                  {histBusy && hist == null ? (
                    <div className="px-1 py-2 text-[11px] text-[var(--muted)]">Loading…</div>
                  ) : (hist && hist.length) ? (
                    <ul className="flex flex-col gap-1">
                      {hist.map((e) => (
                        <li key={e.id} className="rounded border border-[var(--border)] px-1.5 py-1">
                          <span className="block truncate text-[11px] font-medium text-[var(--text)]">{e.label || e.field}</span>
                          <span className="block text-[9px] text-[var(--muted)]">{(e.at || "").replace("T", " ").replace("Z", "").slice(0, 16)}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <div className="px-1 py-2 text-[11px] text-[var(--muted)]">No changes yet.</div>
                  )}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </td>
      {show("status") && (
        <td className="sticky z-10 bg-[var(--surface)] px-3 py-2 group-hover:bg-[var(--surface-2)]" style={fzStyle(FZ.status)}>
          {statusLabel ? (
            <div className="relative inline-block">
              <button
                type="button"
                onClick={() => { if (p.product_id && !statusBusy) setStatusOpen((v) => !v); }}
                disabled={!p.product_id || statusBusy}
                title="Click to change status"
                className="inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-2 py-0.5 text-[11px] font-semibold transition-opacity enabled:hover:opacity-75 disabled:cursor-default"
                style={{
                  color: STATUS_COLOR[p.status!] || "var(--muted)",
                  borderColor: `color-mix(in srgb, ${STATUS_COLOR[p.status!] || "var(--muted)"} 40%, transparent)`,
                  background: `color-mix(in srgb, ${STATUS_COLOR[p.status!] || "var(--muted)"} 14%, transparent)`,
                }}
              >
                {statusBusy ? "…" : statusLabel}
                {p.product_id && !statusBusy ? (
                  <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="m6 9 6 6 6-6" /></svg>
                ) : null}
              </button>
              {statusOpen ? (
                <>
                  {/* click-catcher to close the menu when clicking elsewhere */}
                  <button type="button" aria-label="Close" tabIndex={-1} className="fixed inset-0 z-20 cursor-default" onClick={() => setStatusOpen(false)} />
                  <div className="absolute left-0 top-7 z-30 w-40 overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface)] py-1 text-left shadow-lg">
                    <div className="px-2.5 py-1 text-[9px] font-semibold uppercase tracking-wide text-[var(--muted)]">Set status</div>
                    {(["DRAFT", "ARCHIVED"] as const).map((s) => {
                      const isCur = s === (p.status || "");
                      return (
                        <button
                          key={s}
                          type="button"
                          onClick={() => changeStatus(s)}
                          disabled={isCur}
                          className="flex w-full items-center gap-2 px-2.5 py-1.5 text-[11px] hover:bg-[var(--surface-2)] disabled:cursor-default disabled:opacity-50"
                        >
                          <span className="h-2 w-2 flex-shrink-0 rounded-full" style={{ background: STATUS_COLOR[s] }} />
                          <span className="font-medium text-[var(--text)]">{s.charAt(0) + s.slice(1).toLowerCase()}</span>
                          {isCur ? <span className="ml-auto text-[9px] uppercase tracking-wide text-[var(--muted)]">current</span> : null}
                        </button>
                      );
                    })}
                  </div>
                </>
              ) : null}
              {statusErr ? (
                <div className="absolute left-0 top-7 z-30 w-56 rounded-lg border border-[var(--state-killed)] bg-[var(--surface)] p-2 text-left shadow-lg">
                  <div className="text-[11px] font-semibold text-[var(--state-killed)]">Change didn’t apply</div>
                  <div className="mt-0.5 break-words text-[11px] text-[var(--muted)]">{statusErr}</div>
                  <button type="button" onClick={() => setStatusErr(null)} className="mt-1.5 rounded border border-[var(--border)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--muted)] hover:text-[var(--text)]">Dismiss</button>
                </div>
              ) : null}
            </div>
          ) : (
            <span className="text-[var(--muted)]">—</span>
          )}
        </td>
      )}
      {show("variants") && (
        <td className="sticky z-10 border-r border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-right group-hover:bg-[var(--surface-2)]" style={fzStyle(FZ.variants)}>
          {p.variants_count != null ? (
            <button
              type="button"
              onClick={onOpenBreakdown}
              title="Open variant breakdown"
              className="inline-flex items-center gap-1 rounded-md border border-[color-mix(in_srgb,var(--accent)_35%,var(--border))] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] px-2 py-0.5 text-xs font-semibold tabular-nums text-[var(--accent)] hover:bg-[color-mix(in_srgb,var(--accent)_22%,transparent)]"
            >
              {p.variants_count}
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>
            </button>
          ) : (
            <span className="text-[var(--muted)]">—</span>
          )}
        </td>
      )}
      {show("qty") && <td className="px-3 py-2 text-right tabular-nums">{fmtInt(rw?.qty ?? 0)}</td>}
      {show("conv") && <td className="px-3 py-2 text-right tabular-nums text-[var(--muted)]">{fmtInt(rw?.orders ?? 0)}</td>}
      {show("cost") && <AdsTd on={adsOn}>{fmtMoney(rw?.cost ?? 0, cur)}</AdsTd>}
      {show("cv") && <td className="px-3 py-2 text-right font-semibold tabular-nums">{fmtMoney(rw?.cv ?? 0, cur)}</td>}
      {show("refunds") && (
        <td className="px-3 py-2 text-right tabular-nums" style={{ color: (rw?.refunds ?? 0) > 0 ? "var(--state-killed)" : undefined }}>
          {(rw?.refunds ?? 0) > 0 ? `-${fmtMoney(rw?.refunds ?? 0, cur)}` : fmtMoney(rw?.refunds ?? 0, cur)}
        </td>
      )}
      {show("cogs") && <td className="px-3 py-2 text-right tabular-nums text-[var(--muted)]">{rw?.cog ? fmtMoney(rw.cog, cur) : "—"}</td>}
      {show("profit") && (
        <td className="px-3 py-2 text-right font-semibold tabular-nums" style={{ color: rw?.profit == null ? undefined : rw.profit >= 0 ? "var(--accent)" : "var(--state-killed)" }}>
          {rw?.profit == null ? "—" : fmtMoney(rw.profit, cur)}
        </td>
      )}
      {show("margin") && (
        <td className="px-3 py-2 text-right tabular-nums" style={{ color: rw?.margin_pct == null ? undefined : rw.margin_pct >= 0 ? "var(--accent)" : "var(--state-killed)" }}>
          {rw?.margin_pct == null ? "—" : `${rw.margin_pct}%`}
        </td>
      )}
      {show("roas") && (
        <td className="px-3 py-2 text-right tabular-nums" style={{ color: roasColor(rw?.cv ?? 0, rw?.cost ?? 0) }}>
          {fmtRoas(rw?.cv ?? 0, rw?.cost ?? 0)}
        </td>
      )}
      {show("windows") && WINDOWS.map((win) => {
        const ww = winKey(p, win);
        return (
          <Fragment key={`w${win}`}>
            <AdsTd on={adsOn} color={roasColor(ww?.cv ?? 0, ww?.cost ?? 0)}>
              {fmtRoas(ww?.cv ?? 0, ww?.cost ?? 0)}
            </AdsTd>
            <AdsTd on={adsOn}>{fmtMoney(ww?.cost ?? 0, cur)}</AdsTd>
            <td className="px-3 py-2 text-right tabular-nums">{fmtMoney(ww?.cv ?? 0, cur)}</td>
          </Fragment>
        );
      })}
      {show("published") && <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums text-[var(--muted)]">{fmtDate(p.published_at)}</td>}
    </tr>
    {marketsOpen
      ? marketRows.length > 0
        ? marketRows.map((m) => (
            <MarketRow key={m.code} code={m.code} w={m.w!} cur={cur} show={show} adsOn={adsOn} />
          ))
        : (
          <tr className="border-b border-[var(--border)] bg-[var(--surface-2)]">
            <td colSpan={40} className="px-12 py-1.5 text-xs text-[var(--muted)]">No market data for this window.</td>
          </tr>
        )
      : null}
    </>
  );
}

// One per-country row shown under a product when its globe is expanded — same column order as
// the main table so the numbers line up. Country carries revenue / qty / ad spend (reconciled,
// split by revenue share) / COGS / profit / margin; per-market ad detail (clicks/CTR/CPC) and
// the rolling-window columns aren't available per country, so they read "—".
function MarketRow({ code, w, cur, show, adsOn }: {
  code: string;
  w: OptimizationWindow;
  cur: string | null;
  show: (key: string) => boolean;
  adsOn: boolean;
}) {
  const dash = <span className="text-[var(--muted)]">—</span>;
  return (
    <tr className="border-b border-[var(--border)] bg-[color-mix(in_srgb,var(--accent)_4%,transparent)] text-[13px]">
      <td className="sticky left-0 z-10 w-8 bg-[color-mix(in_srgb,var(--accent)_4%,var(--surface))] px-2 py-1.5" style={{ width: 32, minWidth: 32 }} />
      <td className="sticky z-10 bg-[color-mix(in_srgb,var(--accent)_4%,var(--surface))] px-3 py-1.5" style={fzStyle(FZ.product)}>
        <span className="ml-8 inline-flex items-center gap-1.5 font-medium text-[var(--muted)]">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10" /><path d="M2 12h20M12 2a15 15 0 0 1 0 20a15 15 0 0 1 0-20" /></svg>
          {code}
        </span>
      </td>
      {show("status") && <td className="sticky z-10 bg-[color-mix(in_srgb,var(--accent)_4%,var(--surface))] px-3 py-1.5" style={fzStyle(FZ.status)}>{dash}</td>}
      {show("variants") && <td className="sticky z-10 border-r border-[var(--border)] bg-[color-mix(in_srgb,var(--accent)_4%,var(--surface))] px-3 py-1.5 text-right" style={fzStyle(FZ.variants)}>{dash}</td>}
      {show("qty") && <td className="px-3 py-1.5 text-right tabular-nums">{fmtInt(w.qty ?? 0)}</td>}
      {show("conv") && <td className="px-3 py-1.5 text-right">{dash}</td>}
      {show("cost") && <AdsTd on={adsOn}>{fmtMoney(w.cost ?? 0, cur)}</AdsTd>}
      {show("cv") && <td className="px-3 py-1.5 text-right font-semibold tabular-nums">{fmtMoney(w.cv ?? 0, cur)}</td>}
      {show("refunds") && (
        <td className="px-3 py-1.5 text-right tabular-nums" style={{ color: (w.refunds ?? 0) > 0 ? "var(--state-killed)" : undefined }}>
          {(w.refunds ?? 0) > 0 ? `-${fmtMoney(w.refunds ?? 0, cur)}` : fmtMoney(w.refunds ?? 0, cur)}
        </td>
      )}
      {show("cogs") && <td className="px-3 py-1.5 text-right tabular-nums text-[var(--muted)]">{w.cog ? fmtMoney(w.cog, cur) : "—"}</td>}
      {show("profit") && (
        <td className="px-3 py-1.5 text-right font-semibold tabular-nums" style={{ color: w.profit == null ? undefined : w.profit >= 0 ? "var(--accent)" : "var(--state-killed)" }}>
          {w.profit == null ? "—" : fmtMoney(w.profit, cur)}
        </td>
      )}
      {show("margin") && (
        <td className="px-3 py-1.5 text-right tabular-nums" style={{ color: w.margin_pct == null ? undefined : w.margin_pct >= 0 ? "var(--accent)" : "var(--state-killed)" }}>
          {w.margin_pct == null ? "—" : `${w.margin_pct}%`}
        </td>
      )}
      {show("roas") && (
        <td className="px-3 py-1.5 text-right tabular-nums" style={{ color: roasColor(w.cv ?? 0, w.cost ?? 0) }}>{fmtRoas(w.cv ?? 0, w.cost ?? 0)}</td>
      )}
      {show("windows") && WINDOWS.map((win) => (
        <Fragment key={`m${win}`}>
          <td className="px-3 py-1.5 text-right">{dash}</td>
          <td className="px-3 py-1.5 text-right">{dash}</td>
          <td className="px-3 py-1.5 text-right">{dash}</td>
        </Fragment>
      ))}
      {show("published") && <td className="px-3 py-1.5 text-right">{dash}</td>}
    </tr>
  );
}

const STATUS_COLOR: Record<string, string> = {
  ACTIVE: "var(--state-live)",
  DRAFT: "var(--state-testing)",
  ARCHIVED: "var(--state-killed)",
};

// Per-row action icon (Pythago parity). Icons with an onClick are live; the rest are tooltip-only
// placeholders until the server-sync pass wires them.
const ROW_ICON_PATHS: Record<string, ReactNode> = {
  notes: (<><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /><path d="M8 13h8M8 17h5" /></>),
  globe: (<><circle cx="12" cy="12" r="10" /><path d="M2 12h20M12 2a15 15 0 0 1 0 20a15 15 0 0 1 0-20" /></>),
  store: (<><path d="M3 9l1.5-5h15L21 9M4 9v11h16V9M9 20v-6h6v6" /></>),
  history: (<><path d="M3 3v5h5" /><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8" /><path d="M12 7v5l3 2" /></>),
  ban: (<><circle cx="12" cy="12" r="10" /><path d="m4.9 4.9 14.2 14.2" /></>),
  tag: (<><path d="M20.59 13.41 13.42 20.6a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z" /><circle cx="7" cy="7" r="1.2" /></>),
  task: (<><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></>),
};
function RowIcon({
  title,
  kind,
  active,
  danger,
  onClick,
}: {
  title: string;
  kind: keyof typeof ROW_ICON_PATHS;
  active?: boolean;
  danger?: boolean;
  onClick?: () => void;
}) {
  const color = active ? (danger ? "var(--state-killed)" : "var(--accent)") : "var(--muted)";
  return (
    // Wrapper carries the Pythago-style hover tooltip: a dark pill that appears instantly (peer-hover)
    // above the icon, telling the operator exactly what the icon does.
    <span className="relative inline-flex">
      <button
        type="button"
        onClick={onClick}
        aria-label={title}
        className={`peer rounded p-1 hover:bg-[var(--surface-2)] ${onClick ? "hover:text-[var(--text)]" : "cursor-default"}`}
        style={{ color }}
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          {ROW_ICON_PATHS[kind]}
        </svg>
      </button>
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-30 mb-1.5 -translate-x-1/2 whitespace-nowrap rounded-md bg-[var(--text)] px-2 py-1 text-[11px] font-medium text-[var(--surface)] opacity-0 shadow-lg transition-opacity duration-100 peer-hover:opacity-100"
      >
        {title}
      </span>
    </span>
  );
}

// ── Header cells ─────────────────────────────────────────────────────────────
function SortTh({
  label,
  k,
  cur,
  onSort,
  right,
  freeze,
  freezeLast,
  tint,
}: {
  label: string;
  k: SortKey;
  cur: { sortKey: SortKey; sortDir: "asc" | "desc" };
  onSort: (k: SortKey) => void;
  right?: boolean;
  freeze?: { left: number; width: number };
  freezeLast?: boolean;
  tint?: "shopify" | "ads" | "app";
}) {
  const active = cur.sortKey === k;
  const tintColor = tint === "shopify" ? SHOPIFY_COLOR : tint === "ads" ? ADS_COLOR : tint === "app" ? APP_COLOR : undefined;
  return (
    <th
      className={`whitespace-nowrap px-3 py-3 font-bold ${right ? "text-right" : "text-left"} ${
        freeze ? `sticky z-20 bg-[var(--surface)]${freezeLast ? " border-r border-[var(--border)]" : ""}` : ""
      }`}
      style={freeze ? fzStyle(freeze) : undefined}
    >
      <button
        onClick={() => onSort(k)}
        className={`inline-flex items-center gap-1 hover:opacity-80 ${right ? "flex-row-reverse" : ""}`}
        style={{ color: tintColor }}
      >
        {label}
        <span className="text-[9px] opacity-70">{active ? (cur.sortDir === "asc" ? "▲" : "▼") : "⇅"}</span>
      </button>
    </th>
  );
}

// Ads-layer cells: locked (with the connect hint) until the store's Google Ads Script has
// pushed per-product rows — then they render real, sortable columns.
function AdsTh({
  label,
  on,
  k,
  cur,
  onSort,
}: {
  label: string;
  on: boolean;
  k?: SortKey;
  cur?: { sortKey: SortKey; sortDir: "asc" | "desc" };
  onSort?: (k: SortKey) => void;
}) {
  if (!on) return <LockedTh label={label} />;
  if (k && cur && onSort) return <SortTh label={label} k={k} cur={cur} onSort={onSort} right tint="ads" />;
  return <th className="whitespace-nowrap px-3 py-3 text-right font-bold" style={{ color: ADS_COLOR }}>{label}</th>;
}

function AdsTd({ on, children, color }: { on: boolean; children: React.ReactNode; color?: string }) {
  if (!on) return <LockedTd />;
  return (
    <td
      className="px-3 py-2 text-right tabular-nums"
      style={color ? { color, background: `color-mix(in srgb, ${color} 8%, transparent)`, fontWeight: 600 } : undefined}
    >
      {children}
    </td>
  );
}

// Pythago-style ROAS traffic light: losing money red, near-breakeven amber, profitable green.
function roasColor(cv: number, cost: number): string | undefined {
  if (cost <= 0) return undefined;
  const r = cv / cost;
  if (r < 1) return "var(--state-killed)";
  if (r < 2) return "var(--state-testing)";
  return "var(--state-winner)";
}

function fmtRoas(cv: number, cost: number): string {
  return cost > 0 ? `${(cv / cost).toFixed(2)}x` : "—";
}

function LockedTh({ label }: { label: string }) {
  return (
    <th className="whitespace-nowrap px-3 py-3 text-right font-bold" style={{ color: ADS_COLOR }} title={ADS_HINT}>
      <span className="inline-flex items-center gap-1 opacity-60">
        {label}
        <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" />
        </svg>
      </span>
    </th>
  );
}

function LockedTd() {
  return (
    <td className="px-3 py-2 text-right tabular-nums text-[var(--muted)] opacity-60" title={ADS_HINT}>
      —
    </td>
  );
}

// ── Small primitives ─────────────────────────────────────────────────────────
function KpiCard({
  value,
  label,
  footer,
  tone,
  muted,
}: {
  value: string;
  label: string;
  footer?: ReactNode;
  tone?: "killed";
  muted?: boolean;
}) {
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-4 text-center">
      <div
        className={`text-xl font-bold tabular-nums ${
          tone === "killed" ? "text-[var(--state-killed)]" : muted ? "text-[var(--muted)]" : "text-[var(--text)]"
        }`}
      >
        {value}
      </div>
      <div className="mt-1 text-[11px] font-medium uppercase leading-tight tracking-wide text-[var(--muted)]">
        {label}
      </div>
      {footer ? <div className="mt-1">{footer}</div> : null}
    </div>
  );
}

function PageBtn({ children, disabled, onClick }: { children: ReactNode; disabled?: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="rounded-md border border-[var(--border)] px-2.5 py-1 font-semibold text-[var(--muted)] hover:text-[var(--text)] disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}

// ── Filter primitives (unchanged shape) ──────────────────────────────────────
type Range = { min: string; max: string };
const EMPTY_RANGE: Range = { min: "", max: "" };
function rangeActive(r: Range): boolean {
  return r.min.trim() !== "" || r.max.trim() !== "";
}
function inRange(v: number, r: Range): boolean {
  const lo = r.min.trim() === "" ? null : Number(r.min);
  const hi = r.max.trim() === "" ? null : Number(r.max);
  if (lo != null && !Number.isNaN(lo) && v < lo) return false;
  if (hi != null && !Number.isNaN(hi) && v > hi) return false;
  return true;
}
function FilterGroup({ title, locked, children }: { title: string; locked?: boolean; children: ReactNode }) {
  return (
    <div>
      <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
        {title}
        {locked ? (
          <span title="Connect Google Ads in Connections" className="text-[var(--muted)]">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="11" width="18" height="11" rx="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" />
            </svg>
          </span>
        ) : null}
      </div>
      <div className="space-y-2">{children}</div>
    </div>
  );
}
function RangeRow({
  label,
  sub,
  value,
  onChange,
  disabled,
}: {
  label: string;
  sub?: string;
  value?: Range;
  onChange?: (r: Range) => void;
  disabled?: boolean;
}) {
  const v = value ?? EMPTY_RANGE;
  const inputCls =
    "w-16 rounded-md border border-[var(--border)] bg-[var(--surface)] px-1.5 py-1 text-xs tabular-nums outline-none focus:border-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-50";
  return (
    <div className="flex items-center justify-between gap-2">
      <div className="min-w-0">
        <div className={`text-xs ${disabled ? "text-[var(--muted)]" : "text-[var(--text)]"}`}>{label}</div>
        {sub ? <div className="text-[10px] text-[var(--muted)]">{sub}</div> : null}
      </div>
      <div className="flex items-center gap-1">
        <input type="number" inputMode="decimal" placeholder="Min" disabled={disabled} value={v.min}
          onChange={(e) => onChange?.({ ...v, min: e.target.value })} className={inputCls} />
        <span className="text-[var(--muted)]">–</span>
        <input type="number" inputMode="decimal" placeholder="Max" disabled={disabled} value={v.max}
          onChange={(e) => onChange?.({ ...v, max: e.target.value })} className={inputCls} />
      </div>
    </div>
  );
}
function DisabledRow({ label, hint }: { label: string; hint: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-xs text-[var(--muted)]">{label}</span>
      <span className="text-[10px] text-[var(--muted)]">{hint}</span>
    </div>
  );
}
function exportCsv(products: OptimizationProduct[], snap: OptimizationSnapshot) {
  const cur = snap.currency || "";
  const head = [
    "product_id", "title", "status", "variants", "published_at",
    "cost", // ad — blank until Google Ads connected
    "conversions_alltime", "units_alltime", `conversion_value_alltime_${cur}`, `refunds_alltime_${cur}`,
    `cogs_${cur}`, `profit_${cur}`, "margin_pct",
    "clicks", "ctr", "cpc", "cpa",
    ...WINDOWS.flatMap((w) => [`roas_${w}d`, `spend_${w}d`, `cv_${w}d_${cur}`, `impr_${w}d`]),
  ];
  const esc = (v: unknown) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [head.join(",")];
  const r2 = (n: number) => Math.round(n * 100) / 100;
  for (const p of products) {
    const rw = p.windows[RANGE_WIN];
    const clicks = rw?.clicks ?? 0;
    const impr = rw?.impressions ?? 0;
    const cost = rw?.cost ?? 0;
    const convG = rw?.conversions ?? 0;
    lines.push(
      [
        p.product_id, p.title, p.status, p.variants_count, p.published_at,
        r2(cost),
        rw?.orders ?? 0, rw?.qty ?? 0, rw?.cv ?? 0, rw?.refunds ?? 0,
        rw?.cog != null ? r2(rw.cog) : "", rw?.profit != null ? r2(rw.profit) : "", rw?.margin_pct ?? "",
        clicks,
        impr > 0 ? r2((clicks / impr) * 100) : "",
        clicks > 0 ? r2(cost / clicks) : "",
        convG > 0 ? r2(cost / convG) : "",
        ...WINDOWS.flatMap((w) => {
          const ww = p.windows[w];
          const wc = ww?.cost ?? 0;
          return [
            wc > 0 ? r2((ww?.cv ?? 0) / wc) : "",
            r2(wc),
            ww?.cv ?? 0,
            ww?.impressions ?? 0,
          ];
        }),
      ].map(esc).join(","),
    );
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `product-performance-${snap.store}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

function copyIds(products: OptimizationProduct[]) {
  const ids = products.map((p) => p.product_id).filter(Boolean).join("\n");
  if (navigator.clipboard) navigator.clipboard.writeText(ids);
}

// True when a product carries an "optimized" tag (case-insensitive) — drives the green
// Optimized chip, mirroring Pythago's per-row tag.
function isOptimized(p: OptimizationProduct): boolean {
  return (p.tags || []).some((t) => t.trim().toLowerCase() === "optimized");
}

// Per-row Exclude + Notes are now server-persisted via api.optimizationFlag (pm_optimization_flags),
// seeded from the snapshot's p.hidden / p.note — no localStorage.

// ── Toggleable table columns (Product is always shown; ad columns stay locked when shown) ──
const COLS: { key: string; label: string }[] = [
  { key: "status", label: "Status" },
  { key: "variants", label: "Variants" },
  { key: "qty", label: "Qty." },
  { key: "conv", label: "Conv." },
  { key: "cost", label: "Ad Spend (ads)" },
  { key: "cv", label: "Revenue" },
  { key: "refunds", label: "Refunds" },
  { key: "cogs", label: "COGS" },
  { key: "profit", label: "Profit" },
  { key: "margin", label: "Profit %" },
  { key: "roas", label: "ROAS" },
  { key: "windows", label: "Per-window (7/14/30D)" },
  { key: "published", label: "Published" },
];

// ── Saved filters (persisted to localStorage, per store) ──────────────────────
type FilterState = {
  status: StatusFilter;
  revenue: Range;
  units: Range;
  net: Range;
  refundsF: Range;
  roasF?: Range;
  tagsF: string[];
};
type SavedFilter = { name: string; state: FilterState };
// Saved filters live server-side (pm_saved_filters); localStorage is only an instant-paint seed
// that's immediately overridden by the server list on mount.
const savedKey = (store: string) => `gs-optimize-saved-filters:${store}`;
function loadSaved(store: string): SavedFilter[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(savedKey(store));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

// All-time range label for the toolbar: "<data start> to <last sync>", or "all history to <last sync>"
// when the store has no data start date set.
function allTimeLabel(snap: OptimizationSnapshot): string {
  const end = snap.synced_at ? new Date(snap.synced_at) : new Date();
  const endStr = Number.isNaN(end.getTime()) ? "now" : end.toISOString().slice(0, 10);
  const start = snap.data_start_date ? snap.data_start_date.slice(0, 10) : "all history";
  return `${start} to ${endStr}`;
}

// ── Menu: a small click-outside dropdown used for Tags / Saved filters / Actions / Columns ──
function Menu({
  label,
  children,
  width = "w-56",
  align = "left",
  active,
  icon,
}: {
  label: string;
  children: ReactNode;
  width?: string;
  align?: "left" | "right";
  active?: boolean;
  icon?: "cols";
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className={`inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-semibold transition-colors ${
          active
            ? "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] text-[var(--accent)]"
            : "border-[var(--border)] text-[var(--muted)] hover:text-[var(--text)]"
        }`}
      >
        {icon === "cols" ? (
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M9 3v18M15 3v18" />
          </svg>
        ) : null}
        {label}
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {open ? (
        <div
          className={`absolute top-full z-30 mt-1 overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface)] shadow-lg ${width} ${
            align === "right" ? "right-0" : "left-0"
          }`}
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}

function MenuItem({ children, onClick, disabled }: { children: ReactNode; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="block w-full px-3 py-1.5 text-left text-xs hover:bg-[var(--surface-2)] disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}
