"use client";

import { use, useState } from "react";
import Link from "next/link";
import {
  api,
  type PainGate,
  type PainTrend,
  type PainSignal,
  type PainReview,
  type DossierDoc,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Stat, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState } from "@/components/PageState";

const VERDICT_COLOR: Record<string, string> = {
  GO: "var(--state-winner)",
  HOLD: "var(--state-testing)",
  SKIP: "var(--state-killed)",
};

function humanize(slug: string): string {
  return slug
    .split("-")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="mb-3 mt-8 first:mt-0">{children}</h2>;
}

/* gates checklist */
function GatePill({ g }: { g: PainGate }) {
  const color = g.passed ? "var(--state-winner)" : "var(--state-killed)";
  return (
    <div
      className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm"
      style={{ borderColor: "var(--border)" }}
    >
      <span style={{ color }} className="text-base leading-none">
        {g.passed ? "✓" : "✕"}
      </span>
      <span className={g.passed ? "" : "text-[var(--muted)]"}>{g.label}</span>
    </div>
  );
}

/* trend card */
function verdictColor(v: string | null): string {
  const k = (v ?? "").toLowerCase();
  if (k.includes("ris") || k.includes("grow")) return "var(--state-winner)";
  if (k.includes("fall") || k.includes("declin")) return "var(--state-killed)";
  return "var(--muted)";
}

function TrendRow({ t }: { t: PainTrend }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] px-4 py-3 last:border-0">
      <div className="text-sm font-medium capitalize">
        {t.keyword ?? "—"}
        {t.geo ? (
          <span className="ml-2 text-xs uppercase text-[var(--muted)]">{t.geo}</span>
        ) : null}
      </div>
      <div className="flex items-center gap-2 text-xs text-[var(--muted)]">
        <span className="tabular-nums">mean {t.mean_interest?.toFixed(0) ?? "—"}</span>
        <span className="tabular-nums">
          {t.growth_ratio != null ? `${t.growth_ratio.toFixed(2)}× growth` : "—"}
        </span>
        {t.trend_verdict ? (
          <span
            className="rounded px-1.5 py-0.5 font-medium capitalize"
            style={{
              color: verdictColor(t.trend_verdict),
              background: `color-mix(in srgb, ${verdictColor(t.trend_verdict)} 14%, transparent)`,
            }}
          >
            {t.trend_verdict}
          </span>
        ) : null}
      </div>
    </div>
  );
}

/* demand signal bars */
function SignalRow({ s }: { s: PainSignal }) {
  return (
    <div className="border-b border-[var(--border)] px-4 py-3 last:border-0">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium capitalize">
          {s.signal.replace(/_/g, " ")}
        </span>
        <span
          className="text-xs font-semibold tabular-nums"
          style={{ color: s.present ? "var(--state-live)" : "var(--muted)" }}
        >
          {s.count ?? 0} hits
        </span>
      </div>
      {s.examples.length > 0 ? (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {s.examples.map((ex, i) => (
            <span
              key={i}
              className="rounded px-1.5 py-0.5 text-[11px] text-[var(--muted)]"
              style={{ background: "var(--surface-2)" }}
            >
              “{ex}”
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/* low-star review */
function ReviewCard({ r }: { r: PainReview }) {
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-semibold">{r.title ?? "—"}</span>
        <span className="text-xs font-semibold" style={{ color: "var(--state-killed)" }}>
          {"★".repeat(Math.round(r.rating ?? 0))}
          <span className="text-[var(--muted)]">
            {"★".repeat(5 - Math.round(r.rating ?? 0))}
          </span>
        </span>
      </div>
      {r.body ? (
        <p className="mt-2 text-sm leading-relaxed text-[var(--muted)]">{r.body}</p>
      ) : null}
      {r.asin ? (
        <div className="mt-2 font-mono text-[11px] text-[var(--muted)]">{r.asin}</div>
      ) : null}
    </Card>
  );
}

/* doc loader (pain-first variant) */
function PainDoc({ slug, doc }: { slug: string; doc: DossierDoc }) {
  const [text, setText] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function load() {
    if (text != null || loading) return;
    setLoading(true);
    try {
      const res = await fetch(api.painFileUrl(slug, doc.name), { cache: "no-store" });
      if (!res.ok) throw new Error(`${res.status}`);
      setText(await res.text());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }
  return (
    <details
      className="group border-t border-[var(--border)] first:border-t-0"
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open) load();
      }}
    >
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-5 py-3.5 hover:bg-[var(--surface-2)]">
        <div className="flex items-center gap-2.5">
          <span className="text-[var(--muted)] transition-transform group-open:rotate-90">›</span>
          <span className="font-mono text-sm">{doc.name}</span>
        </div>
        <Pill>{doc.kind}</Pill>
      </summary>
      <div className="px-5 pb-4">
        {loading ? (
          <div className="text-sm text-[var(--muted)]">Loading…</div>
        ) : err ? (
          <div className="text-sm text-[var(--state-killed)]">Couldn’t load doc ({err})</div>
        ) : text != null ? (
          <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--surface-2)] p-4 font-mono text-xs leading-relaxed text-[var(--text)]">
            {text}
          </pre>
        ) : null}
      </div>
    </details>
  );
}

export default function PainDetailPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = use(params);
  const { data, error, loading } = useApi(() => api.painDetail(slug), [slug]);

  const back = (
    <Link
      href="/dossiers"
      className="rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-medium text-[var(--muted)] hover:text-[var(--text)]"
    >
      ← Niche Research
    </Link>
  );

  const vColor = data?.verdict ? VERDICT_COLOR[data.verdict] ?? "var(--muted)" : "var(--muted)";
  const m = data?.meta_ads;

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader title={humanize(slug)} subtitle="Pain-first pipeline" action={back} />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          {/* verdict banner */}
          <Card className="flex flex-wrap items-center justify-between gap-4 p-5">
            <div className="flex items-center gap-4">
              <span
                className="rounded-lg px-3 py-1.5 text-lg font-bold"
                style={{ color: vColor, background: `color-mix(in srgb, ${vColor} 14%, transparent)` }}
              >
                {data.verdict ?? "—"}
              </span>
              <div>
                <div className="text-2xl font-bold tabular-nums">
                  {data.score ?? "—"}
                  <span className="text-base font-medium text-[var(--muted)]">
                    /{data.max_score ?? "—"}
                  </span>
                </div>
                <div className="text-xs text-[var(--muted)]">gate score</div>
              </div>
            </div>
            <div className="flex gap-6">
              {data.growing_count != null ? (
                <Stat label="Growing terms" value={data.growing_count} />
              ) : null}
              {data.max_median != null ? (
                <Stat label="Median price" value={`$${data.max_median.toFixed(0)}`} />
              ) : null}
            </div>
          </Card>

          {/* gates */}
          {data.gates.length > 0 ? (
            <>
              <SectionTitle>Gates</SectionTitle>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
                {data.gates.map((g) => (
                  <GatePill key={g.id} g={g} />
                ))}
              </div>
            </>
          ) : null}

          {/* trends */}
          {data.trends.length > 0 ? (
            <>
              <SectionTitle>Demand trend</SectionTitle>
              <Card className="overflow-hidden">
                {data.trends.map((t, i) => (
                  <TrendRow key={i} t={t} />
                ))}
              </Card>
            </>
          ) : null}

          {/* demand signals + repurchase */}
          {data.sample_messages.length > 0 || data.repurchase ? (
            <>
              <SectionTitle>Demand signals</SectionTitle>
              <div className="grid gap-4 lg:grid-cols-3">
                <Card className="overflow-hidden lg:col-span-2">
                  {data.sample_messages.map((s) => (
                    <SignalRow key={s.signal} s={s} />
                  ))}
                </Card>
                {data.repurchase ? (
                  <Card className="p-5">
                    <div className="text-xs uppercase tracking-wide text-[var(--muted)]">
                      Repurchase
                    </div>
                    <div className="mt-1 text-3xl font-bold tabular-nums">
                      {data.repurchase.count}
                    </div>
                    <Pill>{data.repurchase.verdict}</Pill>
                    <div className="mt-3 flex flex-wrap gap-1.5">
                      {data.repurchase.examples.slice(0, 4).map((ex, i) => (
                        <span
                          key={i}
                          className="rounded px-1.5 py-0.5 text-[11px] text-[var(--muted)]"
                          style={{ background: "var(--surface-2)" }}
                        >
                          “{ex}”
                        </span>
                      ))}
                    </div>
                  </Card>
                ) : null}
              </div>
            </>
          ) : null}

          {/* table stakes */}
          {data.table_stakes.length > 0 ? (
            <>
              <SectionTitle>Table-stakes features</SectionTitle>
              <div className="flex flex-wrap gap-2">
                {data.table_stakes.map((ts) => (
                  <span
                    key={ts.feature}
                    className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm"
                  >
                    <span className="font-medium capitalize">{ts.feature}</span>
                    <span className="ml-2 text-xs tabular-nums text-[var(--muted)]">
                      {ts.count}/{ts.total}
                    </span>
                  </span>
                ))}
              </div>
            </>
          ) : null}

          {/* meta ads intel */}
          {m ? (
            <>
              <SectionTitle>Ad intelligence (90d)</SectionTitle>
              <Card className="p-5">
                <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                  <Stat label="Ads (90d)" value={m.n_ads_90d ?? "—"} />
                  <Stat label="Longest run" value={m.longest_ad_days != null ? `${m.longest_ad_days}d` : "—"} />
                  <Stat label="Scale signal" value={m.scale_signal ?? "—"} />
                  <Stat label="Top brands" value={m.top_brands?.length ?? 0} />
                </div>
                <div className="mt-4 flex flex-wrap gap-2 border-t border-[var(--border)] pt-4">
                  {m.hook_archetype ? <Pill>{`hook: ${m.hook_archetype}`}</Pill> : null}
                  {m.offer_archetype ? <Pill>{`offer: ${m.offer_archetype}`}</Pill> : null}
                  {m.visual_archetype ? <Pill>{`visual: ${m.visual_archetype}`}</Pill> : null}
                  {m.emotional_angle ? <Pill>{`angle: ${m.emotional_angle}`}</Pill> : null}
                </div>
                {m.top_brands?.length ? (
                  <div className="mt-3 text-xs text-[var(--muted)]">
                    Brands: {m.top_brands.join(", ")}
                  </div>
                ) : null}
              </Card>
            </>
          ) : null}

          {/* low-star reviews — the "solutions failing" evidence */}
          {data.low_star_reviews.length > 0 ? (
            <>
              <SectionTitle>Low-star review evidence</SectionTitle>
              <div className="grid gap-3 sm:grid-cols-2">
                {data.low_star_reviews.map((r, i) => (
                  <ReviewCard key={i} r={r} />
                ))}
              </div>
            </>
          ) : null}

          {/* docs */}
          {data.docs.length > 0 ? (
            <>
              <SectionTitle>Documents</SectionTitle>
              <Card className="overflow-hidden">
                {data.docs.map((doc) => (
                  <PainDoc key={doc.name} slug={slug} doc={doc} />
                ))}
              </Card>
            </>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
