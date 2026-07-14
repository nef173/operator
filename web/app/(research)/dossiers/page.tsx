"use client";

import Link from "next/link";
import { api, type PainNiche } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";
import { ResearchStarter } from "@/components/ResearchStarter";

const VERDICT_COLOR: Record<string, string> = {
  GO: "var(--state-winner)",
  HOLD: "var(--state-testing)",
  SKIP: "var(--state-killed)",
};

function humanize(slug: string): string {
  return slug.replace(/-/g, " ");
}

function PainCard({ n }: { n: PainNiche }) {
  const color = n.verdict ? VERDICT_COLOR[n.verdict] ?? "var(--muted)" : "var(--muted)";
  return (
    <Link href={`/pain-first/${n.slug}`}>
      <Card className="p-4 transition-colors hover:border-[var(--accent)]">
        <div className="flex items-center justify-between gap-2">
          <div className="font-semibold capitalize">{humanize(n.slug)}</div>
          {n.verdict ? (
            <span
              className="rounded-md px-2 py-0.5 text-xs font-bold"
              style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
            >
              {n.verdict}
            </span>
          ) : null}
        </div>
        <div className="mt-2 flex items-center gap-2">
          {/* gate pips */}
          <span className="flex gap-0.5">
            {Array.from({ length: n.gates_total }).map((_, i) => (
              <span
                key={i}
                className="h-1.5 w-3.5 rounded-full"
                style={{
                  background:
                    i < n.gates_passed
                      ? color
                      : "color-mix(in srgb, var(--muted) 30%, transparent)",
                }}
              />
            ))}
          </span>
          <span className="text-xs tabular-nums text-[var(--muted)]">
            {n.score ?? "—"}/{n.max_score ?? "—"} score
          </span>
        </div>
      </Card>
    </Link>
  );
}

export default function DossiersPage() {
  const { data, error, loading } = useApi(() => api.dossiers());
  const pain = useApi(() => api.painFirst());

  const dossiers = (data?.dossiers ?? []).filter((d) => !d.is_pool);
  const painNiches = pain.data?.niches ?? [];

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="Niche Research"
        subtitle="Deep per-niche dossiers — search volume, CPC, trends, SERP, buyer voice and domains. Two discovery pipelines: keyword-first and pain-first."
      />

      <ResearchStarter surface="niche" />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        dossiers.length === 0 ? (
          <Empty>No dossiers yet.</Empty>
        ) : (
          <>
            <h2 className="mb-3 text-[var(--muted)]">Keyword-first dossiers</h2>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {dossiers.map((d) => (
                <Link key={d.slug} href={`/dossiers/${d.slug}`}>
                  <Card className="p-4 transition-colors hover:border-[var(--accent)]">
                    <div className="flex items-center justify-between gap-2">
                      <div className="font-semibold capitalize">
                        {d.slug.replace(/-/g, " ")}
                      </div>
                      {d.has_report ? (
                        <Pill>report</Pill>
                      ) : (
                        <span className="text-xs text-[var(--muted)]">no report</span>
                      )}
                    </div>
                  </Card>
                </Link>
              ))}
            </div>
          </>
        )
      ) : null}

      {/* Pain-first pipeline (01b) — a separate niche-discovery pipeline */}
      {painNiches.length > 0 ? (
        <div className="mt-10">
          <h2 className="mb-1">Pain-first pipeline</h2>
          <p className="mb-3 text-sm text-[var(--muted)]">
            Reddit pain mine → Trends gate → Amazon dissection → Meta ads. Each niche scored
            against 7 gates into a GO / HOLD / SKIP verdict.
          </p>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {painNiches.map((n) => (
              <PainCard key={n.slug} n={n} />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
