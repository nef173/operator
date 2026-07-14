"use client";

import { useState } from "react";
import { api, type MarketplaceMover, type MarketplaceBrowsePlatform } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Stat, Pill } from "@/components/ui";
import { Loading, ErrorState } from "@/components/PageState";
import { KeywordProductsModal } from "@/components/KeywordProductsModal";

function fmt(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

// Standalone Marketplace surface (AliExpress / Temu / 1688).
// A product-DISCOVERY source, not "winning products" — orders & sold-count demand + a
// COGS basis. Research sources, never the supplier (the private agent fulfils chosen SKUs).
export function MarketplaceResearch() {
  const marketplace = useApi(() => api.marketplaceMovers());
  const [open, setOpen] = useState<MarketplaceMover | null>(null);

  return (
    <div className="flex flex-col gap-10">
      <section>
        <div className="mb-3 flex flex-wrap items-baseline gap-3">
          <h2 className="m-0">Keyword &amp; Trend Marketplace Products</h2>
          <span className="text-xs text-[var(--muted)]">
            per keyword / trend — products found, demand &amp; product cost
          </span>
          <div className="flex gap-1.5">
            {(marketplace.data?.sources ?? ["AliExpress", "Temu", "1688"]).map((s) => (
              <span
                key={s}
                className="rounded px-1.5 py-0.5 text-[10px] font-semibold"
                style={{ color: "var(--state-testing)", background: "color-mix(in srgb, var(--state-testing) 14%, transparent)" }}
              >
                {s}
              </span>
            ))}
          </div>
        </div>
        {marketplace.loading ? <Loading /> : null}
        {marketplace.error ? <ErrorState message={marketplace.error} /> : null}
        <KeywordProductsModal
          open={open != null}
          keyword={open?.keyword ?? null}
          store={open?.store_name ?? open?.store ?? null}
          sv={open?.sv ?? null}
          found={open?.found ?? 0}
          validated={open?.validated ?? 0}
          products={open?.products ?? []}
          laneLabel="Marketplace"
          onClose={() => setOpen(null)}
        />
        {marketplace.data ? (
          <>
            <div className="mb-4 grid grid-cols-3 gap-3">
              <Stat label="Keywords / trends" value={marketplace.data.totals.keywords} hint="researched and worth pursuing" />
              <Stat label="Products found" value={marketplace.data.totals.found} hint="across all keywords" />
              <Stat label="Checked" value={marketplace.data.totals.validated} hint="enough demand and a workable cost confirmed" />
            </div>
            {marketplace.data.movers.length === 0 ? (
              <LaneEmpty
                ingestCmd={marketplace.data.ingest_cmd}
                note="Nothing here yet. As we search the marketplaces for each keyword, the best movers show up here — with how many have sold and a rough product cost."
              />
            ) : (
              <Card className="overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
                        <th className="px-4 py-3 font-semibold">Keyword / trend</th>
                        <th className="px-4 py-3 font-semibold">Timing</th>
                        <th className="px-4 py-3 text-right font-semibold">Searches/mo</th>
                        <th className="px-4 py-3 text-right font-semibold">Products</th>
                        <th className="px-4 py-3 text-right font-semibold">Orders</th>
                        <th className="px-4 py-3 text-right font-semibold">Sold</th>
                        <th className="px-4 py-3 text-right font-semibold">Product cost</th>
                        <th className="px-4 py-3 text-right font-semibold">Score</th>
                        <th className="px-4 py-3 font-semibold">Store</th>
                      </tr>
                    </thead>
                    <tbody>
                      {marketplace.data.movers.map((m, i) => (
                        <MarketplaceRow key={`${m.store}-${m.keyword}-${i}`} m={m} onOpen={() => setOpen(m)} />
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            )}
          </>
        ) : null}
      </section>
      <MarketplaceBrowseLaunchers />
    </div>
  );
}

function MarketplaceRow({ m, onOpen }: { m: MarketplaceMover; onOpen: () => void }) {
  return (
    <tr
      onClick={onOpen}
      className="cursor-pointer border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)]"
    >
      <td className="px-4 py-3 font-medium">{m.keyword ?? "—"}</td>
      <td className="px-4 py-3">
        {m.gate ? (
          <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
            {m.gate}
          </span>
        ) : (
          <span className="text-xs text-[var(--muted)]">—</span>
        )}
      </td>
      <td className="px-4 py-3 text-right tabular-nums">{fmt(m.sv)}</td>
      <td className="px-4 py-3 text-right">
        <ProductsCell found={m.found} validated={m.validated} />
      </td>
      <td className="px-4 py-3 text-right font-semibold tabular-nums">{fmt(m.orders)}</td>
      <td className="px-4 py-3 text-right tabular-nums">{fmt(m.sold_count)}</td>
      <td className="px-4 py-3 text-right tabular-nums">{m.cogs == null ? "—" : `$${m.cogs.toFixed(2)}`}</td>
      <td className="px-4 py-3 text-right tabular-nums">{m.score == null ? "—" : m.score.toFixed(1)}</td>
      <td className="px-4 py-3">
        <Pill>{m.store_name ?? m.store}</Pill>
      </td>
    </tr>
  );
}

// "N found · M validated" pill that signals the row opens a product drill-in.
export function ProductsCell({ found, validated }: { found: number; validated: number }) {
  if (!found) return <span className="text-xs text-[var(--muted)]">—</span>;
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border)] px-2 py-0.5 text-xs font-semibold tabular-nums">
      <span>{found} found</span>
      {validated ? (
        <span style={{ color: "var(--state-live)" }}>· {validated} ✓</span>
      ) : null}
      <span className="text-[var(--muted)]">↗</span>
    </span>
  );
}

// Native best-seller browse launchers — jump straight into each marketplace's OWN
// top-selling ranking (Temu's native 7/14/30-day toggle, AliExpress sorted by orders,
// 1688 by transactions), scoped to a chosen category or "All".
function MarketplaceBrowseLaunchers() {
  const browse = useApi(() => api.marketplaceBrowse());

  if (browse.loading) return <Loading />;
  if (browse.error) return <ErrorState message={browse.error} />;
  if (!browse.data) return null;

  return (
    <section>
      <div className="mb-3 flex flex-wrap items-baseline gap-3">
        <h2 className="m-0">Native best-seller views</h2>
        <span className="text-xs text-[var(--muted)]">jump into each marketplace&apos;s own top-selling ranking</span>
      </div>
      <p className="mb-4 text-sm text-[var(--muted)]">{browse.data.note}</p>
      <div className="grid gap-3 md:grid-cols-3">
        {browse.data.platforms.map((p) => (
          <PlatformLauncher key={p.id} platform={p} categories={browse.data!.categories} />
        ))}
      </div>
    </section>
  );
}

function PlatformLauncher({
  platform,
  categories,
}: {
  platform: MarketplaceBrowsePlatform;
  categories: { label: string; term: string }[];
}) {
  const [cat, setCat] = useState(categories[0]?.term ?? "");
  // Default to the platform's preferred window (7-day = fastest movers on Temu), not just the
  // first listed — keeps the automatic/default research window at 7-day, never 30.
  const [tf, setTf] = useState(platform.default_timeframe ?? platform.timeframes[0]?.value ?? "");
  // A typed keyword OVERRIDES the category — search that exact term sorted by demand.
  const [kw, setKw] = useState("");
  const term = kw.trim();
  const searching = term.length > 0;

  const buildUrl = (): string => {
    // A keyword wins: search that term (demand-sorted) on the platform.
    if (searching) return platform.search_tpl.replace("{kw}", encodeURIComponent(term));
    // "All" + a platform with a native best-seller channel → its all_url (Temu's day-window is a
    // JS on-page toggle, NOT a URL param — opening lands on Temu's default, operator taps the tab).
    if (cat === "" && platform.supports_all && platform.all_url) return platform.all_url;
    const catTerm = cat || categories[0]?.label || "best sellers";
    return platform.search_tpl.replace("{kw}", encodeURIComponent(catTerm));
  };

  // Temu's day-window can't be set via URL — surface the operator's chosen window as an on-page
  // instruction so the "7-day" choice is honoured (tap the matching tab once the page loads).
  const tfLabel = platform.timeframes.find((t) => t.value === tf)?.label ?? "";
  const timeframeHint =
    platform.timeframe_hint && tfLabel ? platform.timeframe_hint.replace("{label}", tfLabel) : null;

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-center gap-2">
        <span
          className="rounded px-2 py-0.5 text-xs font-semibold"
          style={{ color: platform.color, background: `color-mix(in srgb, ${platform.color} 14%, transparent)` }}
        >
          {platform.name}
        </span>
      </div>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--muted)]">
          Search a keyword
        </div>
        <input
          type="text"
          value={kw}
          onChange={(e) => setKw(e.target.value)}
          placeholder="e.g. dog cooling mat — optional"
          className="w-full rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-sm text-[var(--text)]"
        />
      </div>

      {platform.timeframes.length > 0 ? (
        <div>
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--muted)]">Timeframe</div>
          <div className="flex flex-wrap gap-1.5">
            {platform.timeframes.map((t) => (
              <button
                key={t.value}
                type="button"
                onClick={() => setTf(t.value)}
                className="rounded-md border px-2 py-1 text-xs font-medium transition-colors"
                style={
                  tf === t.value
                    ? { borderColor: platform.color, color: platform.color, background: `color-mix(in srgb, ${platform.color} 12%, transparent)` }
                    : { borderColor: "var(--border)", color: "var(--muted)" }
                }
              >
                {t.label}
              </button>
            ))}
          </div>
          {timeframeHint ? (
            <p className="mt-1.5 text-[11px] leading-snug text-[var(--muted)]">{timeframeHint}</p>
          ) : null}
        </div>
      ) : null}

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--muted)]">Category</div>
        <select
          value={cat}
          onChange={(e) => setCat(e.target.value)}
          disabled={searching}
          className="w-full rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-sm text-[var(--text)] disabled:opacity-50"
        >
          {categories.map((c) => (
            <option key={c.label} value={c.term}>
              {c.label}
              {c.term === "" && !platform.supports_all ? " (sorted by demand)" : ""}
            </option>
          ))}
        </select>
        {searching ? (
          <p className="mt-1 text-[11px] text-[var(--muted)]">Searching “{term}” — category ignored.</p>
        ) : null}
      </div>

      <p className="text-[11px] leading-snug text-[var(--muted)]">{platform.sort_note}</p>

      <a
        href={buildUrl()}
        target="_blank"
        rel="noreferrer"
        className="mt-auto rounded-md px-3 py-2 text-center text-sm font-semibold text-white transition-opacity hover:opacity-90"
        style={{ background: platform.color }}
      >
        {searching ? `Search ${platform.name} for “${term}” →` : `Open ${platform.name} best-sellers →`}
      </a>
    </Card>
  );
}

function LaneEmpty({ note }: { ingestCmd?: string; note: string }) {
  return (
    <Card className="p-6">
      <p className="text-sm text-[var(--muted)]">{note}</p>
    </Card>
  );
}
