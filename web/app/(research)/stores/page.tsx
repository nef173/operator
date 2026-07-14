"use client";

import Link from "next/link";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";

export default function StoresPage() {
  const { data, error, loading } = useApi(() => api.overview());

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="Stores & Queue"
        subtitle="Each store's listing queue — categories and their SKUs."
      />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        data.stores.length === 0 ? (
          <Empty>No stores have a listing queue yet.</Empty>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {data.stores.map((s) => (
              <Link key={s.store} href={`/stores/${s.store}`}>
                <Card className="p-5 transition-colors hover:border-[var(--accent)]">
                  <div className="text-lg font-semibold capitalize">{s.store}</div>
                  <div className="mt-1 text-sm text-[var(--muted)]">
                    {s.categories} categories · {s.skus_total} SKUs
                  </div>
                  <div className="mt-4 flex gap-4 text-sm">
                    <span>
                      <span className="font-semibold tabular-nums">
                        {s.skus_by_state.live}
                      </span>{" "}
                      <span className="text-[var(--muted)]">live</span>
                    </span>
                    <span>
                      <span className="font-semibold tabular-nums">
                        {s.skus_by_state.drafted}
                      </span>{" "}
                      <span className="text-[var(--muted)]">drafted</span>
                    </span>
                    <span>
                      <span className="font-semibold tabular-nums">
                        {s.skus_by_state.candidate}
                      </span>{" "}
                      <span className="text-[var(--muted)]">candidates</span>
                    </span>
                  </div>
                </Card>
              </Link>
            ))}
          </div>
        )
      ) : null}
    </div>
  );
}
