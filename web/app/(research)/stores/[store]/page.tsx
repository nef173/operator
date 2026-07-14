"use client";

import { use } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, StateBadge, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";

function money(n: number | null) {
  return n == null ? "—" : `$${n.toFixed(2)}`;
}

export default function StoreDetailPage({
  params,
}: {
  params: Promise<{ store: string }>;
}) {
  const { store } = use(params);
  const { data, error, loading } = useApi(() => api.store(store), [store]);

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title={store.charAt(0).toUpperCase() + store.slice(1)}
        subtitle={
          data
            ? `${data.summary.categories} categories · ${data.summary.skus_total} SKUs · updated ${data.summary.updated ?? "—"}`
            : "Listing queue"
        }
        action={
          <Link
            href="/stores"
            className="rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-medium text-[var(--muted)] hover:text-[var(--text)]"
          >
            ← All stores
          </Link>
        }
      />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        data.categories.length === 0 ? (
          <Empty>This store's queue is empty.</Empty>
        ) : (
          <div className="space-y-5">
            {data.categories.map((cat) => (
              <Card key={cat.slug} className="overflow-hidden">
                <Link
                  href={`/stores/${store}/${cat.slug}`}
                  className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-4 transition-colors hover:bg-[var(--surface-2)]"
                >
                  <div>
                    <div className="text-base font-semibold capitalize">
                      {cat.keyword ?? cat.slug}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2">
                      {cat.sv != null ? (
                        <Pill>SV {cat.sv.toLocaleString()}</Pill>
                      ) : null}
                      {cat.capture_bucket ? <Pill>{cat.capture_bucket}</Pill> : null}
                      {cat.state ? <Pill>{cat.state}</Pill> : null}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 text-sm text-[var(--muted)]">
                    {cat.skus.length} SKUs
                    <span aria-hidden className="text-[var(--accent)]">→</span>
                  </div>
                </Link>

                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs uppercase tracking-wide text-[var(--muted)]">
                      <th className="px-5 py-2.5 font-medium">Title</th>
                      <th className="px-5 py-2.5 font-medium">Cost</th>
                      <th className="px-5 py-2.5 font-medium">Price</th>
                      <th className="px-5 py-2.5 font-medium">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cat.skus.map((sku) => (
                      <tr
                        key={sku.id}
                        className="border-t border-[var(--border)] align-top"
                      >
                        <td className="px-5 py-3">
                          <div className="font-medium">{sku.title ?? sku.id}</div>
                          <div className="mt-0.5 font-mono text-xs text-[var(--muted)]">
                            {sku.id}
                          </div>
                        </td>
                        <td className="px-5 py-3 tabular-nums">{money(sku.cogs)}</td>
                        <td className="px-5 py-3 tabular-nums">{money(sku.price)}</td>
                        <td className="px-5 py-3">
                          <StateBadge state={sku.state} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            ))}
          </div>
        )
      ) : null}
    </div>
  );
}
