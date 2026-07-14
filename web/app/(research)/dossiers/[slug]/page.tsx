"use client";

import { use } from "react";
import Link from "next/link";
import {
  api,
  type DossierKeyword,
  type DossierTrend,
  type BuyerVoice,
  type DossierDomains,
  type DomainRow,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Stat, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";
import { Sparkline } from "@/components/research/Sparkline";
import { CompetitionMeter } from "@/components/research/CompetitionMeter";
import { PriceBand } from "@/components/research/PriceBand";
import { DocRow } from "@/components/research/DocRow";

/* ---- helpers ---- */

function fmtSv(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1000).toFixed(0)}K`;
  if (n >= 1_000) return `${(n / 1000).toFixed(1)}K`;
  return n.toLocaleString();
}

function fmtFull(n: number | null): string {
  return n == null ? "—" : n.toLocaleString();
}

function money(n: number | null): string {
  return n == null ? "—" : `$${n.toFixed(2)}`;
}

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];
function monthLabel(m: number | null): string {
  return m != null && m >= 1 && m <= 12 ? MONTHS[m - 1] : "—";
}

function humanize(slug: string): string {
  return slug
    .split("-")
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

// Pill accent color for a trend verdict.
function verdictColor(v: string | null): string {
  const k = (v ?? "").toLowerCase();
  if (k.includes("ris") || k.includes("grow") || k.includes("up"))
    return "var(--state-winner)";
  if (k.includes("fall") || k.includes("declin") || k.includes("down"))
    return "var(--state-killed)";
  return "var(--muted)";
}

/* ---- section: keyword cards ---- */

function KeywordCard({ kw }: { kw: DossierKeyword }) {
  const series = kw.sv_series
    .map((p) => p.sv)
    .filter((v): v is number => v != null);
  return (
    <Card className="p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold capitalize">
            {kw.keyword ?? "—"}
          </div>
          <div className="mt-1 text-3xl font-bold tracking-tight tabular-nums">
            {fmtSv(kw.sv)}
            <span className="ml-1.5 text-xs font-medium text-[var(--muted)]">
              /mo
            </span>
          </div>
        </div>
        <div className="shrink-0 text-right text-xs text-[var(--muted)]">
          <div className="tabular-nums">{money(kw.cpc)} CPC</div>
          {kw.low_bid != null && kw.high_bid != null ? (
            <div className="mt-0.5 tabular-nums">
              {money(kw.low_bid)}–{money(kw.high_bid)}
            </div>
          ) : null}
        </div>
      </div>

      <div className="mt-3">
        <Sparkline values={series} height={48} />
        <div className="mt-1 text-[11px] text-[var(--muted)]">
          12-mo search volume
        </div>
      </div>

      <div className="mt-3 border-t border-[var(--border)] pt-3">
        <CompetitionMeter index={kw.competition_index} label={kw.competition} />
      </div>
    </Card>
  );
}

/* ---- section: trend chart ---- */

function TrendCard({ t }: { t: DossierTrend }) {
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-semibold capitalize">
          {t.keyword ?? "—"}
          {t.geo ? (
            <span className="ml-2 text-xs font-normal uppercase text-[var(--muted)]">
              {t.geo}
            </span>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          {t.trend_verdict ? (
            <span
              className="inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium capitalize"
              style={{
                color: verdictColor(t.trend_verdict),
                background: `color-mix(in srgb, ${verdictColor(t.trend_verdict)} 14%, transparent)`,
              }}
            >
              {t.trend_verdict}
            </span>
          ) : null}
          {t.evergreen_verdict ? (
            <Pill>{t.evergreen_verdict.replace(/_/g, " ")}</Pill>
          ) : null}
        </div>
      </div>

      <div className="mt-3">
        <Sparkline values={t.raw_series ?? []} height={72} strokeWidth={1.5} />
        <div className="mt-1 text-[11px] text-[var(--muted)]">
          Google Trends interest (0–100, weekly)
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-3 border-t border-[var(--border)] pt-3 text-center">
        <div>
          <div className="text-[11px] uppercase tracking-wide text-[var(--muted)]">
            Growth
          </div>
          <div className="mt-0.5 text-sm font-semibold tabular-nums">
            {t.growth_ratio != null ? `${t.growth_ratio.toFixed(2)}×` : "—"}
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wide text-[var(--muted)]">
            Peak
          </div>
          <div className="mt-0.5 text-sm font-semibold">
            {monthLabel(t.peak_month)}
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-wide text-[var(--muted)]">
            Mean
          </div>
          <div className="mt-0.5 text-sm font-semibold tabular-nums">
            {t.mean_interest != null ? t.mean_interest.toFixed(0) : "—"}
          </div>
        </div>
      </div>
    </Card>
  );
}

/* ---- section: buyer voice (Step 03 ICP mining) ---- */

function BuyerVoiceSection({ bv }: { bv: BuyerVoice }) {
  const maxTag = bv.top_tags[0]?.count ?? 1;
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="text-sm font-semibold">
          {bv.n_quotes} verbatim buyer quotes
        </div>
        <div className="font-mono text-xs text-[var(--muted)]">{bv.file}</div>
      </div>

      {bv.key_findings.length > 0 ? (
        <div className="mt-3 space-y-2">
          {bv.key_findings.map((kf, i) => (
            <p key={i} className="text-sm leading-relaxed text-[var(--muted)]">
              {kf.label ? (
                <span className="font-semibold capitalize text-[var(--text)]">
                  {kf.label}:{" "}
                </span>
              ) : null}
              {kf.text}
            </p>
          ))}
        </div>
      ) : null}

      {/* Ranked tag bars — what the corpus is mostly about */}
      {bv.top_tags.length > 0 ? (
        <div className="mt-4 space-y-1.5">
          {bv.top_tags.slice(0, 10).map((t) => (
            <div key={t.tag} className="flex items-center gap-3">
              <div className="w-44 shrink-0 truncate text-xs text-[var(--muted)]">
                {t.tag.replace(/-/g, " ")}
              </div>
              <div className="h-2 flex-1 overflow-hidden rounded-full bg-[var(--surface-2)]">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${Math.max(4, (t.count / maxTag) * 100)}%`,
                    background: "var(--accent)",
                  }}
                />
              </div>
              <div className="w-7 shrink-0 text-right text-xs font-semibold tabular-nums">
                {t.count}
              </div>
            </div>
          ))}
        </div>
      ) : null}

      {/* A sample of the actual quotes */}
      <div className="mt-5 space-y-3 border-t border-[var(--border)] pt-4">
        {bv.quotes.slice(0, 6).map((q, i) => (
          <blockquote
            key={i}
            className="border-l-2 border-[var(--accent)] pl-3 text-sm leading-relaxed"
          >
            <span className="text-[var(--text)]">“{q.text}”</span>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              {q.source ? (
                <span className="text-[11px] text-[var(--muted)]">
                  {q.source}
                </span>
              ) : null}
              {q.tags.slice(0, 3).map((tag) => (
                <span
                  key={tag}
                  className="rounded px-1.5 py-0.5 text-[10px] text-[var(--muted)]"
                  style={{ background: "var(--surface-2)" }}
                >
                  {tag.replace(/-/g, " ")}
                </span>
              ))}
            </div>
          </blockquote>
        ))}
      </div>
      {bv.quotes.length > 6 ? (
        <div className="mt-3 text-xs text-[var(--muted)]">
          +{bv.quotes.length - 6} more quotes in {bv.file}
        </div>
      ) : null}
    </Card>
  );
}

/* ---- section: domains (Step 04 domain discovery) ---- */

function money0(n: number | null): string {
  return n == null ? "—" : `$${n.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function DomainRowItem({ d }: { d: DomainRow }) {
  return (
    <tr className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)]">
      <td className="px-4 py-2.5">
        {d.listing_url ? (
          <a
            href={d.listing_url}
            target="_blank"
            rel="noreferrer"
            className="font-medium hover:text-[var(--accent)]"
          >
            {d.name}
          </a>
        ) : (
          <span className="font-medium">{d.name}</span>
        )}
        {d.is_premium ? (
          <span className="ml-2 text-[10px] uppercase text-[var(--accent)]">
            premium
          </span>
        ) : null}
      </td>
      <td className="px-4 py-2.5">
        {d.brandability_score != null ? (
          <span className="inline-flex items-center gap-1.5">
            <span className="h-1.5 w-12 overflow-hidden rounded-full bg-[var(--surface-2)]">
              <span
                className="block h-full rounded-full"
                style={{
                  width: `${(d.brandability_score / 10) * 100}%`,
                  background: "var(--accent)",
                }}
              />
            </span>
            <span className="text-xs tabular-nums text-[var(--muted)]">
              {d.brandability_score}
            </span>
          </span>
        ) : (
          <span className="text-xs text-[var(--muted)]">—</span>
        )}
      </td>
      <td className="px-4 py-2.5 text-right tabular-nums">{money0(d.price_usd)}</td>
      <td className="px-4 py-2.5 text-right tabular-nums text-[var(--muted)]">
        {money0(d.valuation_usd)}
      </td>
      <td className="px-4 py-2.5 text-right tabular-nums text-[var(--muted)]">
        {d.bid_count ?? "—"}
      </td>
    </tr>
  );
}

function DomainTable({ rows }: { rows: DomainRow[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
            <th className="px-4 py-2.5 font-semibold">Domain</th>
            <th className="px-4 py-2.5 font-semibold">Brandability</th>
            <th className="px-4 py-2.5 text-right font-semibold">Price</th>
            <th className="px-4 py-2.5 text-right font-semibold">Est. value</th>
            <th className="px-4 py-2.5 text-right font-semibold">Bids</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => (
            <DomainRowItem key={d.name} d={d} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DomainsSection({ domains }: { domains: DossierDomains }) {
  const brandable = domains.brandable.slice(0, 12);
  const auctions = domains.auctions.slice(0, 12);
  return (
    <div className="space-y-4">
      {brandable.length > 0 ? (
        <Card className="overflow-hidden">
          <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
            Brandable · top {brandable.length} of {domains.brandable.length}
          </div>
          <DomainTable rows={brandable} />
        </Card>
      ) : null}
      {auctions.length > 0 ? (
        <Card className="overflow-hidden">
          <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
            Auctions · top {auctions.length} of {domains.auctions.length}
          </div>
          <DomainTable rows={auctions} />
        </Card>
      ) : null}
    </div>
  );
}

/* ---- section heading ---- */

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="mb-3 mt-8 first:mt-0">{children}</h2>;
}

/* ---- page ---- */

export default function DossierDetailPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = use(params);
  const { data, error, loading } = useApi(() => api.dossier(slug), [slug]);

  const back = (
    <Link
      href="/dossiers"
      className="rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-medium text-[var(--muted)] hover:text-[var(--text)]"
    >
      ← All dossiers
    </Link>
  );

  const subtitleParts: string[] = [];
  if (data?.location) subtitleParts.push(data.location);
  if (data?.keyword_file) subtitleParts.push(data.keyword_file);

  const serpEntries = data?.serp_summary
    ? Object.entries(data.serp_summary)
    : [];

  const hasAnything =
    data &&
    (data.keywords.length > 0 ||
      data.trends.length > 0 ||
      serpEntries.length > 0 ||
      data.buyer_voice != null ||
      data.domains != null ||
      data.docs.length > 0 ||
      data.images.length > 0);

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title={humanize(slug)}
        subtitle={subtitleParts.length ? subtitleParts.join(" · ") : undefined}
        action={back}
      />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          {data.is_pool ? (
            <div className="mb-5">
              <Pill>Keyword pool</Pill>
            </div>
          ) : null}

          {/* Summary stat row */}
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            {data.summary.n_keywords > 0 ? (
              <Stat
                label="Keywords"
                value={data.summary.n_keywords}
                hint={
                  data.summary.total_sv
                    ? `${fmtFull(data.summary.total_sv)} total searches/mo`
                    : undefined
                }
              />
            ) : null}
            {data.summary.total_sv > 0 ? (
              <Stat
                label="Monthly search volume"
                value={fmtSv(data.summary.total_sv)}
                hint="across all keywords"
              />
            ) : null}
            {data.summary.top_keyword ? (
              <Stat
                label="Top keyword"
                value={fmtSv(data.summary.top_sv)}
                hint={data.summary.top_keyword}
              />
            ) : null}
            <Stat
              label="Trends · Docs"
              value={`${data.summary.n_trends} · ${data.summary.n_docs}`}
              hint={
                data.summary.n_images
                  ? `${data.summary.n_images} charts`
                  : undefined
              }
            />
          </div>

          {!hasAnything ? (
            <div className="mt-6">
              <Empty>
                This dossier has no captured keyword, trend, or SERP data yet.
              </Empty>
            </div>
          ) : null}

          {/* Keywords — the centerpiece */}
          {data.keywords.length > 0 ? (
            <>
              <SectionTitle>Keywords</SectionTitle>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {data.keywords.map((kw, i) => (
                  <KeywordCard key={`${kw.keyword}-${i}`} kw={kw} />
                ))}
              </div>
            </>
          ) : null}

          {/* Trends */}
          {data.trends.length > 0 ? (
            <>
              <SectionTitle>Demand trend</SectionTitle>
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {data.trends.map((t, i) => (
                  <TrendCard key={`${t.keyword}-${t.geo}-${i}`} t={t} />
                ))}
              </div>
            </>
          ) : null}

          {/* SERP price landscape */}
          {serpEntries.length > 0 ? (
            <>
              <SectionTitle>Price landscape</SectionTitle>
              <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                {serpEntries.map(([keyword, geos]) => (
                  <Card key={keyword} className="p-5">
                    <div className="text-sm font-semibold capitalize">
                      {keyword}
                    </div>
                    <div className="mt-4 space-y-4">
                      {Object.entries(geos).map(([geo, s]) => (
                        <div key={geo}>
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                              {geo}
                            </span>
                            <span className="text-xs tabular-nums text-[var(--muted)]">
                              {s.n_listings} listings · {s.unique_merchants}{" "}
                              merchants
                            </span>
                          </div>
                          <div className="mt-2">
                            <PriceBand
                              low={s.price_low}
                              med={s.price_med}
                              high={s.price_high}
                            />
                          </div>
                          {s.top_sources?.length ? (
                            <div className="mt-2.5 flex flex-wrap gap-1.5">
                              {s.top_sources.slice(0, 5).map((src) => (
                                <Pill key={src}>{src}</Pill>
                              ))}
                            </div>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  </Card>
                ))}
              </div>
            </>
          ) : null}

          {/* Buyer voice — verbatim ICP corpus */}
          {data.buyer_voice ? (
            <>
              <SectionTitle>Buyer voice</SectionTitle>
              <BuyerVoiceSection bv={data.buyer_voice} />
            </>
          ) : null}

          {/* Domains — Step 04 discovery candidates */}
          {data.domains ? (
            <>
              <SectionTitle>Domain candidates</SectionTitle>
              <DomainsSection domains={data.domains} />
            </>
          ) : null}

          {/* Pre-rendered chart images */}
          {data.images.length > 0 ? (
            <>
              <SectionTitle>Charts</SectionTitle>
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                {data.images.map((name) => (
                  <Card key={name} className="overflow-hidden">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={api.dossierImageUrl(slug, name)}
                      alt={name}
                      className="w-full bg-[var(--surface-2)] object-contain"
                      loading="lazy"
                    />
                    <div className="border-t border-[var(--border)] px-4 py-2.5 font-mono text-xs text-[var(--muted)]">
                      {name}
                    </div>
                  </Card>
                ))}
              </div>
            </>
          ) : null}

          {/* Documents */}
          {data.docs.length > 0 ? (
            <>
              <SectionTitle>Documents</SectionTitle>
              <Card className="overflow-hidden">
                {data.docs.map((doc) => (
                  <DocRow key={doc.name} slug={slug} doc={doc} />
                ))}
              </Card>
            </>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
