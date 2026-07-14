"use client";

import Link from "next/link";
import { api, type AlertItem } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";

// Cross-store performance alerts — the products that need attention (bleeding money, wasting ad
// spend, refund spikes, thin margins) plus winners to scale. Computed live from the always-fresh
// Product Performance snapshots; ranked by € impact. Read-only digest — click through to act.

const SEV: Record<string, { color: string; label: string }> = {
  high: { color: "var(--state-killed)", label: "High" },
  medium: { color: "var(--state-drafted)", label: "Medium" },
  positive: { color: "var(--state-winner)", label: "Winner" },
};
const KIND_LABEL: Record<string, string> = {
  bleeding: "Losing money",
  wasting_ads: "Wasting ad spend",
  refund_spike: "Refund spike",
  thin_margin: "Thin margin",
  winner: "Scale opportunity",
};

function money(n: number | null | undefined): string {
  if (n == null) return "—";
  return `${Math.round(n).toLocaleString()} €`;
}

function AlertRow({ a }: { a: AlertItem }) {
  const sev = SEV[a.severity] ?? SEV.medium;
  return (
    <div className="flex items-center justify-between gap-4 border-b border-[var(--border)] px-4 py-3 last:border-0">
      <div className="flex min-w-0 items-start gap-3">
        <span className="mt-1 h-2 w-2 flex-shrink-0 rounded-full" style={{ background: sev.color }} />
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate font-semibold text-[var(--text)]">{a.title}</span>
            <span
              className="rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide"
              style={{ color: sev.color, borderColor: `color-mix(in srgb, ${sev.color} 40%, transparent)`, background: `color-mix(in srgb, ${sev.color} 12%, transparent)` }}
            >
              {KIND_LABEL[a.kind] ?? a.kind}
            </span>
            <span className="rounded bg-[var(--surface-2)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--muted)]">{a.store}</span>
          </div>
          <div className="mt-0.5 text-xs text-[var(--muted)]">{a.headline}</div>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-[var(--muted)]">
            <span>Revenue <span className="font-semibold text-[var(--text)]">{money(a.revenue)}</span></span>
            <span>Ad spend <span className="font-semibold text-[var(--text)]">{money(a.ad_spend)}</span></span>
            <span>ROAS <span className="font-semibold text-[var(--text)]">{a.roas == null ? "—" : `${a.roas}×`}</span></span>
            <span>Profit <span className="font-semibold" style={{ color: a.profit != null && a.profit < 0 ? "var(--state-killed)" : "var(--text)" }}>{money(a.profit)}</span></span>
            {a.margin_pct != null ? <span>Margin <span className="font-semibold text-[var(--text)]">{a.margin_pct}%</span></span> : null}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function AlertsPage() {
  const { data, error, loading, reload } = useApi(() => api.alerts());

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Alerts"
        subtitle="Products that need attention across every store — bleeding money, wasting ad spend, refund spikes, thin margins — plus winners to scale. Live from the Product Performance snapshots, ranked by € impact."
      />

      {loading && !data ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Card className="px-4 py-3 text-center">
              <div className="text-2xl font-bold" style={{ color: data.counts.high ? "var(--state-killed)" : "var(--text)" }}>{data.counts.high}</div>
              <div className="mt-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">High priority</div>
            </Card>
            <Card className="px-4 py-3 text-center">
              <div className="text-2xl font-bold" style={{ color: data.counts.medium ? "var(--state-drafted)" : "var(--text)" }}>{data.counts.medium}</div>
              <div className="mt-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">Medium</div>
            </Card>
            <Card className="px-4 py-3 text-center">
              <div className="text-2xl font-bold" style={{ color: data.counts.positive ? "var(--state-winner)" : "var(--text)" }}>{data.counts.positive}</div>
              <div className="mt-0.5 text-[10px] font-medium uppercase tracking-wide text-[var(--muted)]">Winners</div>
            </Card>
            <Card className="flex flex-col items-center justify-center px-4 py-3 text-center">
              <button type="button" onClick={reload} className="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-xs font-semibold text-[var(--accent-fg)]">Refresh</button>
              <Link href="/feed/optimize" className="mt-1.5 text-[11px] text-[var(--muted)] underline hover:text-[var(--text)]">Open Product Performance</Link>
            </Card>
          </div>

          {data.by_store?.length ? (
            <div className="mb-3 flex flex-wrap gap-2">
              {data.by_store.map((s) => (
                <span key={s.store} className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1 text-xs">
                  <span className="font-semibold text-[var(--text)]">{s.store}</span>
                  {s.high ? <span className="text-[var(--state-killed)]">{s.high} high</span> : null}
                  {s.medium ? <span className="text-[var(--state-drafted)]">{s.medium} med</span> : null}
                  {s.positive ? <span className="text-[var(--state-winner)]">{s.positive} win</span> : null}
                </span>
              ))}
            </div>
          ) : null}

          {data.items.length ? (
            <Card>
              {data.items.map((a) => (
                <AlertRow key={`${a.store}-${a.product_id}-${a.kind}`} a={a} />
              ))}
            </Card>
          ) : (
            <Empty>No alerts — every product is inside its guardrails. Nice.</Empty>
          )}
        </>
      ) : null}
    </div>
  );
}
