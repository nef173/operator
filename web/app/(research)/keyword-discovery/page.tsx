"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, type KeywordCandidate, type KeywordSegment } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState } from "@/components/PageState";
import { ResearchStarter } from "@/components/ResearchStarter";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FindProductsModal } from "@/components/FindProductsModal";

function fmtSv(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

const BUCKET_COLOR: Record<string, string> = {
  BREAKOUT: "var(--state-winner)",
  "LIST-NOW": "var(--state-live)",
  "BUILD-AHEAD": "var(--state-testing)",
  EVERGREEN: "var(--muted)",
  SKIP: "var(--state-killed)",
};

// Volume tier — the 10k gate is the FLOOR; bigger keywords win more listings, so surface how big
// this one is (prime ≥200k / strong 100-200k / solid 30-100k / entry 10-30k). Mirrors the backend
// volume_tier() so the operator sees WHY a keyword ranks where it does, not just a raw number.
function volumeTier(sv: number | null): { name: string; color: string } | null {
  if (sv == null) return null;
  if (sv >= 200_000) return { name: "prime", color: "var(--state-winner)" };
  if (sv >= 100_000) return { name: "strong", color: "var(--state-live)" };
  if (sv >= 30_000) return { name: "solid", color: "var(--state-testing)" };
  if (sv >= 10_000) return { name: "entry", color: "var(--muted)" };
  return { name: "below floor", color: "var(--state-killed)" };
}

export default function KeywordDiscoveryPage() {
  const { data, error, loading, reload } = useApi(() => api.keywordDiscovery());

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="Keyword Research"
        subtitle="Your product ideas, ranked best-first. Pick one to plan and build into a listing."
      />

      <ResearchStarter surface="keyword" />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          {/* Ranked product ideas — the operator picks one to plan and build. A compact summary
              line replaces the old stat-card row (the numbers live here, not in five big cards). */}
          {data.candidates.length > 0 ? (
            <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-[var(--muted)]">
              <span>
                <span className="font-semibold text-[var(--text)]">{data.totals.candidates}</span> ranked ideas
              </span>
              <span>
                <span className="font-semibold text-[var(--text)]">{data.totals.gated_pass}</span> passed the demand
                check (≥{fmtSv(data.gate_sv)}/mo)
              </span>
              <span>
                across <span className="font-semibold text-[var(--text)]">{data.totals.stores}</span> stores
              </span>
            </div>
          ) : null}
          {data.candidates.length === 0 ? (
            <Card className="p-8 text-center text-sm text-[var(--muted)]">
              No ideas yet. Run a research method above to fill this list — the best product
              ideas land here, ranked best-first.
            </Card>
          ) : (
            <Card className="overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
                      <th className="px-4 py-3 font-semibold">Keyword</th>
                      <th className="px-4 py-3 text-right font-semibold">Searches/mo</th>
                      <th className="px-4 py-3 font-semibold">Timing</th>
                      <th className="px-4 py-3 font-semibold">SKU plan</th>
                      <th className="px-4 py-3 text-right font-semibold">Score</th>
                      <th className="px-4 py-3 font-semibold">Store</th>
                      <th className="px-4 py-3 font-semibold">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.candidates.map((c, i) => (
                      <CandidateRow
                        key={`${c.store}-${c.keyword}-${i}`}
                        c={c}
                        onChange={reload}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
          )}

          {/* Score guide — how the ranking number is built, so a high score is legible. */}
          {data.candidates.length > 0 ? (
            <details className="mt-3 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-xs text-[var(--muted)]">
              <summary className="cursor-pointer font-semibold text-[var(--text)]">
                How the Score works
              </summary>
              <p className="mt-2 leading-relaxed">
                Ideas are ranked best-first by a single number. It only scores keywords that pass
                the demand check (≥{fmtSv(data.gate_sv)} searches/mo) — everything else is 0. A{" "}
                <span className="font-medium text-[var(--text)]">higher score</span> means bigger,
                more-corroborated, better-timed demand you don&apos;t already sell:
              </p>
              <ul className="mt-2 space-y-1">
                <li>
                  <span className="font-medium text-[var(--text)]">Search volume</span> — +1 per
                  10,000 searches/mo, up to +10 (this is the biggest lever).
                </li>
                <li>
                  <span className="font-medium text-[var(--text)]">Corroboration</span> — +3 for
                  each independent source that also found it (Google Shopping, a competitor,
                  Amazon, a marketplace, Meta ads).
                </li>
                <li>
                  <span className="font-medium text-[var(--text)]">Timing</span> — Breakout +6,
                  List-now +5, Build-ahead +2, Evergreen +0.5.
                </li>
                <li>
                  <span className="font-medium text-[var(--text)]">Momentum</span> — rising demand
                  adds up to +5; falling demand subtracts up to −3.
                </li>
                <li>
                  <span className="font-medium text-[var(--text)]">Not already yours</span> — +3
                  when your store doesn&apos;t already sell it.
                </li>
                <li>
                  <span className="font-medium text-[var(--text)]">Extras</span> — small boosts from
                  Amazon rank-climb, a competitor&apos;s rank jump, and long-running Meta ads.
                </li>
              </ul>
              <p className="mt-2 leading-relaxed">
                So a top score = high search volume, confirmed by several sources, on a breakout or
                list-now trend, with rising momentum, that you don&apos;t yet carry.
              </p>
            </details>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function CandidateRow({
  c,
  onChange,
}: {
  c: KeywordCandidate;
  onChange: () => void;
}) {
  const router = useRouter();
  const [confirm, setConfirm] = useState(false);
  const [findOpen, setFindOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [removing, setRemoving] = useState(false);
  const bucketColor = c.capture_bucket ? BUCKET_COLOR[c.capture_bucket] ?? "var(--muted)" : "var(--muted)";
  const passed = (c.gate ?? "").toUpperCase().startsWith("PASS");
  const tier = volumeTier(c.sv);

  async function remove() {
    if (!c.keyword || removing) return;
    setRemoving(true);
    try {
      await api.removeCandidate(c.store, c.keyword);
      onChange();
    } catch {
      setRemoving(false); // keep the row on failure so the operator can retry
    }
  }
  const hasPlan = c.n_segments > 0;
  return (
    <>
    <tr
      className={`border-b border-[var(--border)] hover:bg-[var(--surface-2)] ${expanded ? "" : "last:border-0"}`}
    >
      <td className="px-4 py-3">
        <div className="font-medium">{c.keyword ?? "—"}</div>
        {c.n_validation > 0 ? (
          <div
            className="mt-0.5 text-[11px] text-[var(--muted)]"
            title={`Also found by: ${c.validation_lanes.join(", ")}`}
          >
            {c.n_validation} source{c.n_validation === 1 ? "" : "s"} confirm this
          </div>
        ) : null}
      </td>
      <td className="px-4 py-3 text-right">
        <div className="font-semibold tabular-nums">{fmtSv(c.sv)}</div>
        {tier ? (
          <div className="text-[10px] font-semibold uppercase tracking-wide" style={{ color: tier.color }}>
            {tier.name}
          </div>
        ) : null}
      </td>
      <td className="px-4 py-3">
        {c.capture_bucket ? (
          <span
            className="inline-flex items-center rounded-md px-2 py-0.5 text-xs font-semibold"
            style={{ color: bucketColor, background: `color-mix(in srgb, ${bucketColor} 14%, transparent)` }}
          >
            {c.capture_bucket}
          </span>
        ) : (
          <span className="text-xs text-[var(--muted)]">—</span>
        )}
      </td>
      <td className="px-4 py-3">
        {hasPlan ? (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border)] px-2 py-0.5 text-xs font-semibold tabular-nums transition-colors hover:border-[var(--accent)] hover:text-[var(--text)]"
            title="See the related products this keyword breaks into"
          >
            <span>{c.n_segments} products</span>
            <span className={`text-[var(--muted)] transition-transform ${expanded ? "rotate-90" : ""}`}>›</span>
          </button>
        ) : (
          <span className="text-xs text-[var(--muted)]" title="Not planned yet — open the SKU plan to break this into products">
            not planned
          </span>
        )}
      </td>
      <td className="px-4 py-3 text-right font-semibold tabular-nums">
        {c.score == null ? "—" : c.score.toFixed(1)}
      </td>
      <td className="px-4 py-3">
        <Pill>{c.store}</Pill>
      </td>
      <td className="px-4 py-3">
        {/* Promote is the SINGLE entry point: it adds the keyword to the store AND auto-runs the
            SKU plan + product-find (chain_after_promote). So the row shows only Promote + remove;
            the SKU plan is the "N products" drill-down, and Find products lives inside it. */}
        <div className="flex flex-wrap items-center gap-2">
          {passed && c.keyword ? (
            <button
              type="button"
              onClick={() => setConfirm(true)}
              className="rounded-lg px-3 py-1 text-xs font-semibold"
              style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
              title="Add to the store and start the pipeline — auto-plans SKUs and finds products"
            >
              Promote →
            </button>
          ) : (
            <span
              className="text-[11px] text-[var(--muted)]"
              title={c.gate ?? "Not gate-cleared"}
            >
              {c.gate ?? "—"}
            </span>
          )}
          {c.keyword ? (
            <button
              type="button"
              onClick={remove}
              disabled={removing}
              aria-label="Remove this keyword"
              title="Remove this keyword from the backlog"
              className="rounded-lg border border-[var(--border)] px-2 py-1 text-xs font-semibold text-[var(--muted)] transition-colors hover:border-[var(--danger)] hover:text-[var(--danger)] disabled:opacity-50"
            >
              {removing ? "…" : "✕"}
            </button>
          ) : null}
        </div>
        {c.keyword ? (
          <FindProductsModal
            keyword={c.keyword}
            sv={c.sv}
            source="keyword"
            store={c.store}
            open={findOpen}
            onClose={() => setFindOpen(false)}
          />
        ) : null}
        {c.keyword ? (
          <ConfirmDialog
            open={confirm}
            title="Promote into the SKU Plan"
            confirmLabel="Promote"
            body={
              <span>
                Add <span className="font-semibold">{c.keyword}</span> to{" "}
                <span className="font-medium">{c.store}</span> as a category. It goes into the{" "}
                <span className="font-medium">SKU Plan</span>, where you set how many products to
                find and start product finding.
              </span>
            }
            onConfirm={async () => {
              await api.promoteCandidate(c.store, c.keyword as string);
            }}
            onDone={() => {
              onChange();
              router.push("/sku-plan"); // land the operator in the SKU Plan where it now appears
            }}
            onClose={() => setConfirm(false)}
          />
        ) : null}
      </td>
    </tr>
    {expanded && hasPlan ? (
      <tr className="border-b border-[var(--border)] last:border-0 bg-[var(--surface-2)]">
        <td colSpan={7} className="px-4 py-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-xs text-[var(--muted)]">
              This is the SKU plan Promote builds automatically. Preview products for it:
            </span>
            {c.keyword ? (
              <button
                type="button"
                onClick={() => setFindOpen(true)}
                className="shrink-0 rounded-lg border border-[var(--border)] px-2.5 py-1 text-xs font-semibold text-[var(--muted)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)]"
                title="Preview products to list for this keyword"
              >
                Find products →
              </button>
            ) : null}
          </div>
          <SkuPlan keyword={c.keyword} segments={c.segments} />
        </td>
      </tr>
    ) : null}
    </>
  );
}

// The reviewable SKU building plan for one head keyword: the searched sub-keywords it fans
// out into. Each row is a candidate catalog entry — already-built ones are flagged in-catalog.
const SEGMENT_SOURCE_LABEL: Record<string, string> = {
  shopping_scan: "Shopping scan",
  serp: "Similar SERP",
  dataforseo: "DataForSEO",
};

function SkuPlan({ keyword, segments }: { keyword: string | null; segments: KeywordSegment[] }) {
  const built = segments.filter((s) => s.in_catalog).length;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          SKU plan
        </span>
        <span className="text-xs text-[var(--muted)]">
          {segments.length} sub-keyword{segments.length === 1 ? "" : "s"} expanded from{" "}
          <span className="font-medium text-[var(--text)]">{keyword ?? "—"}</span>
          {built ? ` · ${built} already in catalog` : ""}
        </span>
      </div>
      <div className="overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface)]">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--border)] text-left text-[11px] uppercase tracking-wide text-[var(--muted)]">
              <th className="px-3 py-2 font-semibold">Sub-keyword</th>
              <th className="px-3 py-2 text-right font-semibold">Searches/mo</th>
              <th className="px-3 py-2 font-semibold">Source</th>
              <th className="px-3 py-2 font-semibold">Price band</th>
              <th className="px-3 py-2 font-semibold">In catalog</th>
            </tr>
          </thead>
          <tbody>
            {segments.map((s, i) => (
              <tr key={`${s.term}-${i}`} className="border-b border-[var(--border)] last:border-0">
                <td className="px-3 py-2 font-medium">{s.term}</td>
                <td className="px-3 py-2 text-right tabular-nums">{fmtSv(s.sv)}</td>
                <td className="px-3 py-2 text-xs text-[var(--muted)]">
                  {s.source ? SEGMENT_SOURCE_LABEL[s.source] ?? s.source : "—"}
                </td>
                <td className="px-3 py-2 text-xs tabular-nums">
                  {s.price_band == null || s.price_band === ""
                    ? "—"
                    : typeof s.price_band === "number"
                      ? `$${s.price_band}`
                      : s.price_band}
                </td>
                <td className="px-3 py-2">
                  {s.in_catalog ? (
                    <span className="text-xs font-semibold" style={{ color: "var(--state-live)" }}>
                      ✓ built
                    </span>
                  ) : (
                    <span className="text-xs text-[var(--muted)]">to build</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
