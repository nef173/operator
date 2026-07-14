"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { api, type AlertItem } from "@/lib/api";
import { useStore } from "@/components/StoreProvider";
import { useApi } from "@/lib/useApi";
import { Card } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";

// Product Optimization → Alerts. The "fix these first" work view for the selected store, with
// filters: severity (the summary cards), kind (chips), a search box, and a sort. Live from the
// same snapshot Product Performance reads; click a row to open it there.

const SEV_COLOR: Record<string, string> = {
  high: "var(--state-killed)",
  medium: "var(--state-drafted)",
  positive: "var(--state-winner)",
};
const KINDS: { key: string; label: string; sev: string }[] = [
  { key: "bleeding", label: "Losing money", sev: "high" },
  { key: "wasting_ads", label: "Wasting ad spend", sev: "high" },
  { key: "refund_spike", label: "Refund spike", sev: "medium" },
  { key: "thin_margin", label: "Thin margin", sev: "medium" },
  { key: "winner", label: "Winners", sev: "positive" },
];
const KIND_LABEL: Record<string, string> = Object.fromEntries(KINDS.map((k) => [k.key, k.label]));
const SORTS: { key: string; label: string }[] = [
  { key: "impact", label: "€ impact" },
  { key: "profit", label: "Profit (worst first)" },
  { key: "ad_spend", label: "Ad spend" },
  { key: "revenue", label: "Revenue" },
  { key: "roas", label: "ROAS (worst first)" },
];

function money(n: number | null | undefined): string {
  if (n == null) return "—";
  return `${Math.round(n).toLocaleString()} €`;
}

function Metric({ label, value, tone }: { label: string; value: string; tone?: "bad" | "good" }) {
  return (
    <span className="whitespace-nowrap">
      <span className="text-[var(--muted)]">{label} </span>
      <span className="font-semibold" style={{ color: tone === "bad" ? "var(--state-killed)" : tone === "good" ? "var(--state-winner)" : "var(--text)" }}>{value}</span>
    </span>
  );
}

function AlertRow({ a }: { a: AlertItem }) {
  const color = SEV_COLOR[a.severity] ?? SEV_COLOR.medium;
  return (
    <Link
      href={`/feed/optimize?q=${encodeURIComponent(a.title)}`}
      className="group flex items-center gap-3 border-b border-[var(--border)] pl-0 pr-4 last:border-0 hover:bg-[var(--surface-2)]"
    >
      <span className="h-full min-h-[64px] w-1 flex-shrink-0 self-stretch rounded-r" style={{ background: color }} />
      <div className="flex min-w-0 flex-1 items-center justify-between gap-4 py-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate font-semibold text-[var(--text)]">{a.title}</span>
            <span
              className="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
              style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
            >
              {KIND_LABEL[a.kind] ?? a.kind}
            </span>
          </div>
          <div className="mt-0.5 text-xs text-[var(--muted)]">{a.headline}</div>
          <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px]">
            <Metric label="Revenue" value={money(a.revenue)} />
            <Metric label="Ad spend" value={money(a.ad_spend)} />
            <Metric label="ROAS" value={a.roas == null ? "—" : `${a.roas}×`} tone={a.roas != null && a.roas < 1 ? "bad" : undefined} />
            <Metric label="Profit" value={money(a.profit)} tone={a.profit != null && a.profit < 0 ? "bad" : a.profit != null ? "good" : undefined} />
            {a.margin_pct != null ? <Metric label="Margin" value={`${a.margin_pct}%`} tone={a.margin_pct < 0 ? "bad" : undefined} /> : null}
          </div>
        </div>
        <span className="hidden shrink-0 text-xs font-medium text-[var(--muted)] group-hover:text-[var(--accent)] sm:block">Open →</span>
      </div>
    </Link>
  );
}

export default function FeedAlertsPage() {
  const { store, ready } = useStore();
  const { data, error, loading, reload } = useApi(() => (store ? api.optimizationAlerts(store) : Promise.resolve(null)), [store]);

  const [sev, setSev] = useState<string | null>(null);     // null = all severities
  const [kind, setKind] = useState<string | null>(null);   // null = all kinds
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("impact");

  const items = useMemo(() => data?.items ?? [], [data]);
  const kindCounts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const a of items) c[a.kind] = (c[a.kind] || 0) + 1;
    return c;
  }, [items]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const rows = items.filter((a) =>
      (!sev || a.severity === sev) &&
      (!kind || a.kind === kind) &&
      (!needle || a.title.toLowerCase().includes(needle)));
    const val = (a: AlertItem): number => {
      switch (sort) {
        case "profit": return a.profit ?? 0;                 // asc → worst (most negative) first
        case "ad_spend": return -(a.ad_spend ?? 0);          // desc
        case "revenue": return -(a.revenue ?? 0);            // desc
        case "roas": return a.roas ?? Infinity;              // asc → worst first, nulls last
        default: return -(a.impact ?? 0);                    // impact desc
      }
    };
    return [...rows].sort((x, y) => val(x) - val(y));
  }, [items, sev, kind, q, sort]);

  if (!ready) return <Loading />;
  if (!store) return <Empty>No store selected. Pick a store in the sidebar.</Empty>;

  const anyFilter = sev !== null || kind !== null || q.trim() !== "";
  const sevCard = (key: string, label: string) => {
    const n = data?.counts?.[key as "high" | "medium" | "positive"] ?? 0;
    const active = sev === key;
    return (
      <button
        type="button"
        onClick={() => { setSev(active ? null : key); setKind(null); }}
        className={`rounded-xl border px-4 py-3 text-center transition-colors ${active ? "border-current" : "border-[var(--border)] hover:bg-[var(--surface-2)]"}`}
        style={active ? { color: SEV_COLOR[key], background: `color-mix(in srgb, ${SEV_COLOR[key]} 8%, transparent)` } : undefined}
      >
        <div className="text-2xl font-bold" style={{ color: n ? SEV_COLOR[key] : "var(--text)" }}>{n}</div>
        <div className="mt-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">{label}</div>
      </button>
    );
  };

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Alerts"
        subtitle={`The 'fix these first' list for ${store} — losing money, wasting ad spend, refund spikes, thin margins, plus winners to scale. Ranked by € impact.`}
      />

      {loading && !data ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          {/* Severity summary — clickable to filter */}
          <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {sevCard("high", "High priority")}
            {sevCard("medium", "Medium")}
            {sevCard("positive", "Winners")}
            <Card className="flex flex-col items-center justify-center px-4 py-3 text-center">
              <button type="button" onClick={reload} className="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-xs font-semibold text-[var(--accent-fg)]">Refresh</button>
              <span className="mt-1.5 text-[11px] text-[var(--muted)]">{data.total} flagged</span>
            </Card>
          </div>

          {/* Toolbar: search + sort + kind chips */}
          <div className="mb-3 flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search product…"
                className="min-w-[180px] flex-1 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-sm outline-none focus:border-[var(--accent)]"
              />
              <label className="flex items-center gap-1.5 text-xs text-[var(--muted)]">
                Sort
                <select value={sort} onChange={(e) => setSort(e.target.value)} className="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 text-sm text-[var(--text)] outline-none">
                  {SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
                </select>
              </label>
              {anyFilter ? (
                <button type="button" onClick={() => { setSev(null); setKind(null); setQ(""); }} className="rounded-lg border border-[var(--border)] px-2.5 py-1.5 text-xs text-[var(--muted)] hover:text-[var(--text)]">Clear</button>
              ) : null}
            </div>
            <div className="flex flex-wrap gap-1.5">
              <button type="button" onClick={() => { setKind(null); setSev(null); }} className={`rounded-full border px-3 py-1 text-xs font-medium ${!kind && !sev ? "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] text-[var(--accent)]" : "border-[var(--border)] text-[var(--muted)] hover:bg-[var(--surface-2)]"}`}>
                All <span className="opacity-60">{items.length}</span>
              </button>
              {KINDS.filter((k) => kindCounts[k.key]).map((k) => {
                const active = kind === k.key;
                return (
                  <button
                    key={k.key}
                    type="button"
                    onClick={() => { setKind(active ? null : k.key); setSev(null); }}
                    className="rounded-full border px-3 py-1 text-xs font-medium transition-colors"
                    style={active
                      ? { borderColor: SEV_COLOR[k.sev], color: SEV_COLOR[k.sev], background: `color-mix(in srgb, ${SEV_COLOR[k.sev]} 12%, transparent)` }
                      : { borderColor: "var(--border)", color: "var(--muted)" }}
                  >
                    {k.label} <span className="opacity-60">{kindCounts[k.key]}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {shown.length ? (
            <Card className="overflow-hidden">
              {shown.map((a) => <AlertRow key={`${a.product_id}-${a.kind}`} a={a} />)}
            </Card>
          ) : items.length ? (
            <Empty>No alerts match these filters.</Empty>
          ) : (
            <Empty>No alerts for {store} — every product is inside its guardrails. Nice.</Empty>
          )}
        </>
      ) : null}
    </div>
  );
}
