"use client";

import Link from "next/link";
import { api, type SkuState } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Stat, StateBadge } from "@/components/ui";
import { PageHeader, Loading, ErrorState } from "@/components/PageState";

const STATE_ORDER: SkuState[] = [
  "candidate",
  "keyword-clustered",
  "drafted",
  "live",
  "testing",
  "winner",
  "killed",
];

export default function DashboardPage() {
  const { data, error, loading } = useApi(() => api.overview());

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="Dashboard"
        subtitle="Live state of the Google Stores pipeline — research through listing."
        action={
          <Link
            href="/new-listing"
            className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-[var(--accent-fg)] transition-opacity hover:opacity-90"
          >
            + New Listing
          </Link>
        }
      />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Stat label="Stores" value={data.totals.stores} />
            <Stat label="Categories" value={data.totals.categories} hint="keyword clusters" />
            <Stat label="SKUs tracked" value={data.totals.skus} />
            <Stat
              label="Live"
              value={data.totals.skus_by_state.live}
              hint={`${data.totals.skus_by_state.drafted} drafted · ${data.totals.skus_by_state.candidate} candidates`}
            />
          </div>

          <div className="mt-4 grid grid-cols-2 gap-4">
            <Stat label="Niche dossiers" value={data.totals.dossiers} hint="completed research reports" />
            <Stat label="Niche launches" value={data.totals.niche_launches} hint="per-launch workspaces" />
          </div>

          <Card className="mt-6 p-5">
            <h2 className="mb-4">SKU pipeline</h2>
            <div className="flex flex-wrap gap-2">
              {STATE_ORDER.map((s) => (
                <div
                  key={s}
                  className="flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2"
                >
                  <StateBadge state={s} />
                  <span className="text-sm font-semibold tabular-nums">
                    {data.totals.skus_by_state[s]}
                  </span>
                </div>
              ))}
            </div>
          </Card>

          <Card className="mt-6 overflow-hidden">
            <div className="border-b border-[var(--border)] px-5 py-4">
              <h2>Stores</h2>
            </div>
            {data.stores.length === 0 ? (
              <div className="p-6 text-sm text-[var(--muted)]">No stores yet.</div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-xs uppercase tracking-wide text-[var(--muted)]">
                    <th className="px-5 py-2.5 font-medium">Store</th>
                    <th className="px-5 py-2.5 font-medium">Categories</th>
                    <th className="px-5 py-2.5 font-medium">SKUs</th>
                    <th className="px-5 py-2.5 font-medium">Live</th>
                    <th className="px-5 py-2.5 font-medium">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {data.stores.map((s) => (
                    <tr key={s.store} className="border-t border-[var(--border)] hover:bg-[var(--surface-2)]">
                      <td className="px-5 py-3">
                        <Link
                          href={`/stores/${s.store}`}
                          className="font-semibold capitalize hover:text-[var(--accent)]"
                        >
                          {s.store}
                        </Link>
                      </td>
                      <td className="px-5 py-3 tabular-nums">{s.categories}</td>
                      <td className="px-5 py-3 tabular-nums">{s.skus_total}</td>
                      <td className="px-5 py-3 tabular-nums">{s.skus_by_state.live}</td>
                      <td className="px-5 py-3 text-[var(--muted)]">{s.updated ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      ) : null}
    </div>
  );
}
