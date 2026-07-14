"use client";

import { useMemo, useState, type ReactNode } from "react";
import {
  api,
  type AmazonBrowseView,
  type AmazonBrowseCategory,
  type AmazonMover,
  type BestsellerProduct,
  type Bestsellers,
  type LaneProduct,
  type NewProduct,
  type NewProducts,
  type MetaWinner,
  type SpyStore,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Stat, Pill } from "@/components/ui";
import { Loading, ErrorState } from "@/components/PageState";
import { ResearchStarter } from "@/components/ResearchStarter";
import { SpyRoster, SpyMovers } from "@/components/SpyDashboard";
import { KeywordProductsModal } from "@/components/KeywordProductsModal";
import { ProductsCell } from "@/components/MarketplaceResearch";

function fmt(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

function ageLabel(createdAt: string | null): string {
  if (!createdAt) return "—";
  const then = new Date(createdAt).getTime();
  if (Number.isNaN(then)) return "—";
  const days = Math.floor((Date.now() - then) / 86_400_000);
  if (days < 31) return `${days}d`;
  if (days < 365) return `${Math.floor(days / 30)}mo`;
  return `${(days / 365).toFixed(1)}y`;
}

// Visible source domain so the operator can SEE where each researched product was found.
function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function SourceLink({ url }: { url: string | null }) {
  if (!url) return null;
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      title={url}
      className="truncate text-[11px] font-medium text-[var(--accent)] hover:underline"
    >
      {hostOf(url)} ↗
    </a>
  );
}

// Two spy surfaces. The COMPETITOR SPY (Google) is the primary one — it gets its own
// proper overview with sub-tabs (Stores · Best Sellers · New · Movers). AD WINNERS
// (Amazon + Meta) gets its own tab. (Marketplace — AliExpress / Temu / 1688 — is now its
// own standalone surface at /marketplace; it's a product-discovery source, not a "winning".)
type Surface = "competitor" | "ads";

export function WinningProducts({ showStarter = true }: { showStarter?: boolean }) {
  const [surface, setSurface] = useState<Surface>("competitor");

  return (
    <>
      {showStarter ? (
        <ResearchStarter surface="product" />
      ) : null}

      <div className="mb-6 mt-2 flex flex-wrap gap-2">
        <SurfaceTab
          active={surface === "competitor"}
          status="live"
          name="Competitor Spy"
          hint="Stores that win on Google without ads — see who they are and their current best sellers"
          onClick={() => setSurface("competitor")}
        />
        <SurfaceTab
          active={surface === "ads"}
          status="live"
          name="Ad winners"
          hint="Amazon Best Sellers & New Releases plus products other stores are running ads on — already-proven demand"
          onClick={() => setSurface("ads")}
        />
      </div>

      {surface === "competitor" ? <CompetitorSpy /> : <AdWinners />}
    </>
  );
}

function SurfaceTab({
  active,
  status,
  name,
  hint,
  onClick,
}: {
  active: boolean;
  status: "live" | "pending";
  name: string;
  hint: string;
  onClick: () => void;
}) {
  const live = status === "live";
  const color = live ? "var(--state-live)" : "var(--muted)";
  return (
    <button
      type="button"
      onClick={onClick}
      title={hint}
      className="flex items-center gap-2 rounded-xl border px-4 py-2.5 text-sm font-semibold transition-colors"
      style={{
        borderColor: active ? "var(--accent)" : "var(--border)",
        background: active ? "color-mix(in srgb, var(--accent) 12%, transparent)" : "var(--surface)",
        color: active ? "var(--text)" : "var(--muted)",
      }}
    >
      {name}
      <span
        className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase"
        style={{ color, background: `color-mix(in srgb, ${color} 16%, transparent)` }}
      >
        {live ? "live" : "soon"}
      </span>
    </button>
  );
}

// ════════════════════════════════════════════════ Competitor Spy (the primary surface)
// Proper overview with sub-tabs, mirroring the old standalone dashboard:
//   Stores       — roster analytics table (tier / visits / MoM / breadth / meta ads)
//   Best Sellers — current top-ranked products as a GRID (filter by store + store overview)
//   New          — climbing best-sellers we don't already carry
//   Movers       — best-seller rank movers per store
type SpyTab = "stores" | "best" | "new" | "movers";

function CompetitorSpy() {
  const [tab, setTab] = useState<SpyTab>("stores");
  const boards = useApi(() => api.bestsellers());
  const fresh = useApi(() => api.newProducts());
  const roster = useApi(() => api.spyRoster());
  const movers = useApi(() => api.bestsellerMovers());

  const nStores = boards.data?.totals.stores ?? 0;
  const nProducts = boards.data?.totals.products ?? 0;
  const nNew = fresh.data?.totals.new ?? 0;
  const nMovers =
    (movers.data?.totals.entered_top30 ?? 0) + (movers.data?.totals.climbing_top30 ?? 0);

  return (
    <Card className="p-1.5">
      {/* Sub-tab bar */}
      <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] px-2.5 pb-3 pt-2">
        <SubTab active={tab === "stores"} onClick={() => setTab("stores")}>
          Stores
          <Chip n={roster.data?.totals.tracked ?? 0} />
        </SubTab>
        <SubTab active={tab === "best"} onClick={() => setTab("best")}>
          Best Sellers
          <Chip n={nProducts} />
        </SubTab>
        <SubTab active={tab === "new"} onClick={() => setTab("new")}>
          New products
          <Chip n={nNew} />
        </SubTab>
        <SubTab active={tab === "movers"} onClick={() => setTab("movers")}>
          Movers
          <Chip n={nMovers} />
        </SubTab>
      </div>

      <div className="p-4">
        {tab === "stores" ? <SpyRoster /> : null}
        {tab === "best" ? (
          <BestSellerGrid
            data={boards.data}
            roster={roster.data?.stores ?? []}
            error={boards.error}
            loading={boards.loading}
            storeCount={nStores}
          />
        ) : null}
        {tab === "new" ? (
          <NewProductsGrid data={fresh.data} error={fresh.error} loading={fresh.loading} />
        ) : null}
        {tab === "movers" ? <SpyMovers /> : null}
      </div>
    </Card>
  );
}

function SubTab({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-2 rounded-lg px-4 py-2.5 text-base font-semibold transition-colors"
      style={
        active
          ? { background: "var(--accent)", color: "var(--accent-fg)" }
          : { color: "var(--muted)" }
      }
    >
      {children}
    </button>
  );
}

function Chip({ n }: { n: number }) {
  return (
    <span className="rounded px-2 py-0.5 text-xs font-bold tabular-nums opacity-80">{n}</span>
  );
}

// ── Best Sellers: products as a GRID (columns next to each other), not table rows per
// store. A store filter doubles as a per-store overview (analytics header + that store's
// board). "All stores" interleaves every store's current board into one grid.
function BestSellerGrid({
  data,
  roster,
  error,
  loading,
  storeCount,
}: {
  data: Bestsellers | null;
  roster: SpyStore[];
  error: string | null;
  loading: boolean;
  storeCount: number;
}) {
  const [store, setStore] = useState<string>("__all__");

  const stores = useMemo(
    () => (data?.stores ?? []).filter((s) => s.products.length > 0),
    [data],
  );
  const rosterByDomain = useMemo(() => {
    const m = new Map<string, SpyStore>();
    for (const r of roster) m.set(r.domain, r);
    return m;
  }, [roster]);

  if (loading) return <Loading />;
  if (error) return <ErrorState message={error} />;
  if (!data) return null;

  if (stores.length === 0) {
    return (
      <Card className="p-8 text-center text-sm text-[var(--muted)]">
        No best-seller snapshots yet. Run the Best-Seller Spy to capture each store&apos;s
        best-seller rank board.
      </Card>
    );
  }

  const selected = store === "__all__" ? null : stores.find((s) => s.store === store) ?? null;
  // Products for the grid — single store, or all stores interleaved by rank.
  const tiles: { p: BestsellerProduct; store: string }[] = selected
    ? selected.products.map((p) => ({ p, store: selected.store }))
    : interleaveByRank(stores);
  const sel = selected ? rosterByDomain.get(selected.store) ?? null : null;

  return (
    <div>
      {/* Store filter — All + one chip per store. Doubles as the store overview switch. */}
      <div className="mb-4 flex flex-wrap gap-1.5">
        <StoreChip active={store === "__all__"} onClick={() => setStore("__all__")}>
          All stores
          <span className="ml-1 opacity-70">{storeCount}</span>
        </StoreChip>
        {stores.map((s) => (
          <StoreChip key={s.store} active={store === s.store} onClick={() => setStore(s.store)}>
            {s.store}
            <span className="ml-1 opacity-70">{s.count ?? s.products.length}</span>
          </StoreChip>
        ))}
      </div>

      {/* Per-store overview header (analytics from the roster) when one store is picked. */}
      {selected ? (
        <Card className="mb-4 flex flex-wrap items-center gap-x-6 gap-y-2 p-4">
          <div>
            <div className="text-base font-semibold">{selected.store}</div>
            <div className="text-xs text-[var(--muted)]">snapshot {selected.date ?? "—"}</div>
          </div>
          <OverviewStat label="Tier" value={sel?.tier ?? "—"} />
          <OverviewStat label="Visits/mo" value={fmt(sel?.monthly_visits ?? null)} />
          <OverviewStat
            label="MoM"
            value={sel?.mom_pct == null ? "—" : `${sel.mom_pct >= 0 ? "▲" : "▼"} ${Math.abs(sel.mom_pct).toFixed(1)}%`}
            tone={sel?.mom_pct == null ? undefined : sel.mom_pct >= 0 ? "up" : "down"}
          />
          <OverviewStat label="US" value={sel?.us_share == null ? "—" : `${Math.round(sel.us_share * 100)}%`} />
          <OverviewStat label="Catalog" value={sel?.products == null ? "—" : sel.products.toLocaleString()} />
          <OverviewStat label="Ranked board" value={selected.count ?? selected.products.length} />
        </Card>
      ) : (
        <p className="mb-4 text-sm text-[var(--muted)]">
          Live top-ranked products across {storeCount} competitors — newest snapshot, interleaved by
          rank. {data.totals.products} products · latest {data.generated_at ?? "—"}. Pick a store for
          its full board + analytics.
        </p>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {tiles.map(({ p, store: st }, i) => (
          <ProductTile key={`${st}-${p.handle}-${i}`} p={p} store={selected ? null : st} />
        ))}
      </div>
    </div>
  );
}

// Round-robin the per-store boards so the "all stores" grid mixes competitors evenly
// rather than dumping one store's whole board before the next.
function interleaveByRank(stores: Bestsellers["stores"]): { p: BestsellerProduct; store: string }[] {
  const out: { p: BestsellerProduct; store: string }[] = [];
  const max = Math.max(...stores.map((s) => s.products.length), 0);
  for (let i = 0; i < max; i++) {
    for (const s of stores) {
      if (i < s.products.length) out.push({ p: s.products[i], store: s.store });
    }
  }
  return out;
}

function StoreChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center rounded-full border px-3 py-1 text-xs font-semibold tabular-nums transition-colors"
      style={{
        borderColor: active ? "var(--accent)" : "var(--border)",
        background: active ? "color-mix(in srgb, var(--accent) 14%, transparent)" : "transparent",
        color: active ? "var(--text)" : "var(--muted)",
      }}
    >
      {children}
    </button>
  );
}

function OverviewStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: ReactNode;
  tone?: "up" | "down";
}) {
  const color = tone === "up" ? "var(--state-live)" : tone === "down" ? "var(--state-killed)" : undefined;
  return (
    <div>
      <div className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">{label}</div>
      <div className="text-sm font-semibold tabular-nums" style={color ? { color } : undefined}>
        {value}
      </div>
    </div>
  );
}

// A best-seller product tile (grid cell). Image on top, then title / price / rank / age.
function ProductTile({ p, store }: { p: BestsellerProduct; store: string | null }) {
  return (
    <div className="flex flex-col overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface-2)]">
      <div className="relative aspect-square w-full bg-[var(--surface)]">
        {p.image ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={p.image}
            alt={p.title ?? ""}
            className="h-full w-full object-cover"
            loading="lazy"
          />
        ) : (
          <div className="h-full w-full" />
        )}
        {p.rank != null ? (
          <span className="absolute left-2 top-2 rounded-md bg-black/65 px-1.5 py-0.5 text-[11px] font-bold tabular-nums text-white">
            #{p.rank}
          </span>
        ) : null}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5 p-3">
        <div className="line-clamp-2 text-sm font-medium leading-snug">
          {p.url ? (
            <a href={p.url} target="_blank" rel="noreferrer" className="hover:text-[var(--accent)]">
              {p.title ?? p.handle ?? "—"}
            </a>
          ) : (
            (p.title ?? p.handle ?? "—")
          )}
        </div>
        <div className="mt-auto flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-sm font-semibold tabular-nums">
            {p.price != null ? `$${p.price.toFixed(2)}` : "—"}
          </span>
          {p.compare_at != null ? (
            <span className="text-xs tabular-nums text-[var(--muted)] line-through">
              ${p.compare_at.toFixed(2)}
            </span>
          ) : null}
          <span className="ml-auto text-[11px] tabular-nums text-[var(--muted)]">
            {ageLabel(p.created_at)}
          </span>
        </div>
        {store ? (
          <div className="truncate text-[11px] text-[var(--muted)]">{store}</div>
        ) : null}
        <SourceLink url={p.url} />
      </div>
    </div>
  );
}

function NewProductsGrid({
  data,
  error,
  loading,
}: {
  data: NewProducts | null;
  error: string | null;
  loading: boolean;
}) {
  if (loading) return <Loading />;
  if (error) return <ErrorState message={error} />;
  if (!data) return null;

  return (
    <div>
      <p className="mb-2 text-sm text-[var(--muted)]">
        Competitor best-sellers we don&apos;t already carry — the duplicate-products scanner removes
        anything matching our listing queue. {data.totals.new} new of {data.totals.scanned} scanned ·{" "}
        {data.totals.duplicates} already carried · {data.catalog_terms} catalog terms. Generated{" "}
        {data.generated_at ?? "—"}.
      </p>
      <p className="mb-4 flex flex-wrap items-center gap-2 text-[11px] text-[var(--muted)]">
        <span
          className="rounded px-1.5 py-0.5 font-bold uppercase tracking-wide"
          style={{ color: "var(--state-winner)", background: "color-mix(in srgb, var(--state-winner) 14%, transparent)" }}
        >
          list directly
        </span>
        These are products already selling well right now — you can list them straight away, no keyword or
        trend needed. There&apos;s no need to double-check demand on Google; the competitor growing with them
        already proves people want them.
      </p>

      {data.products.length === 0 ? (
        <Card className="p-8 text-center text-sm text-[var(--muted)]">
          No new products — either no best-seller snapshots are on file yet, or every competitor
          best-seller already matches our catalog. Run the Best-Seller Spy to capture store boards.
        </Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {data.products.map((p, i) => (
            <NewProductTile key={`${p.handle}-${i}`} p={p} />
          ))}
        </div>
      )}
    </div>
  );
}

function NewProductTile({ p }: { p: NewProduct }) {
  return (
    <div className="flex flex-col overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface-2)]">
      <div className="aspect-square w-full bg-[var(--surface)]">
        {p.image ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={p.image} alt={p.title ?? ""} className="h-full w-full object-cover" loading="lazy" />
        ) : (
          <div className="h-full w-full" />
        )}
      </div>
      <div className="flex flex-1 flex-col gap-1.5 p-3">
        <div className="line-clamp-2 text-sm font-medium leading-snug">
          {p.url ? (
            <a href={p.url} target="_blank" rel="noreferrer" className="hover:text-[var(--accent)]">
              {p.title ?? p.handle ?? "—"}
            </a>
          ) : (
            (p.title ?? p.handle ?? "—")
          )}
        </div>
        <div className="mt-auto flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold tabular-nums">
            {p.price != null ? `$${p.price.toFixed(2)}` : "—"}
          </span>
          {p.rank_delta ? (
            <span
              className="rounded px-1.5 py-0.5 text-[11px] font-semibold tabular-nums"
              style={{
                color: "var(--state-live)",
                background: "color-mix(in srgb, var(--state-live) 14%, transparent)",
              }}
            >
              ▲ {Math.abs(p.rank_delta)} · #{p.rank ?? "—"}
            </span>
          ) : p.rank != null ? (
            <span className="rounded bg-[var(--surface)] px-1.5 py-0.5 text-[11px] font-semibold tabular-nums text-[var(--muted)]">
              #{p.rank} best-seller
            </span>
          ) : null}
          {p.is_fresh ? (
            <span className="text-[10px] uppercase" style={{ color: "var(--accent)" }}>
              fresh
            </span>
          ) : null}
        </div>
        <div className="truncate text-[11px] text-[var(--muted)]">{p.competitor ?? "—"}</div>
        <SourceLink url={p.url} />
        <a
          href="/sourcing-match"
          onClick={() => {
            try {
              sessionStorage.setItem(
                "sourcing-seed",
                JSON.stringify({
                  title: p.title ?? p.handle ?? "",
                  url: p.url ?? null,
                  image: p.image ?? null,
                  price: p.price ?? null,
                  competitor: p.competitor ?? null,
                }),
              );
            } catch {
              /* sessionStorage unavailable — link still navigates */
            }
          }}
          className="mt-1 inline-flex items-center justify-center rounded-md border border-[var(--border)] px-2 py-1.5 text-[11px] font-semibold text-[var(--muted)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)]"
        >
          → Sourcing Match
        </a>
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════ Ad winners surface (Amazon + Meta)
// Its own tab — Amazon Movers & Shakers + Meta dropship ad creatives. Each is a per-keyword
// table: every gated keyword shows how many products the lane found + validated, and a row
// opens a window-in-window drill-in to inspect those products.
type LaneOpen = {
  laneLabel: string;
  keyword: string | null;
  store: string | null;
  sv: number | null;
  found: number;
  validated: number;
  products: LaneProduct[];
};

function AdWinners() {
  const amazon = useApi(() => api.amazonMovers());
  const meta = useApi(() => api.metaDropship());
  const [open, setOpen] = useState<LaneOpen | null>(null);

  return (
    <div className="flex flex-col gap-10">
      <KeywordProductsModal
        open={open != null}
        keyword={open?.keyword ?? null}
        store={open?.store ?? null}
        sv={open?.sv ?? null}
        found={open?.found ?? 0}
        validated={open?.validated ?? 0}
        products={open?.products ?? []}
        laneLabel={open?.laneLabel ?? ""}
        onClose={() => setOpen(null)}
      />

      <section>
        <div className="mb-3 flex items-baseline gap-3">
          <h2 className="m-0">Amazon Best Sellers &amp; New Releases</h2>
          <span className="text-xs text-[var(--muted)]">top sellers × new arrivals × lots of reviews</span>
        </div>
        <AmazonBrowseLaunchers />
        {amazon.loading ? <Loading /> : null}
        {amazon.error ? <ErrorState message={amazon.error} /> : null}
        {amazon.data ? (
          <>
            <div className="mb-4 grid grid-cols-3 gap-3">
              <Stat label="Keywords" value={amazon.data.totals.keywords} hint="Amazon products we've pulled in" />
              <Stat label="Products found" value={amazon.data.totals.found} hint="across all keywords" />
              <Stat label="Checked" value={amazon.data.totals.validated} hint="strong sales rank and lots of reviews" />
            </div>
            {amazon.data.movers.length === 0 ? (
              <LaneEmpty
                ingestCmd={amazon.data.ingest_cmd}
                note="Nothing here yet. Browse the Best Sellers / New Releases boards above — each product's sales rank and review count then shows up here as we collect it."
              />
            ) : (
              <Card className="overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
                        <th className="px-4 py-3 font-semibold">Keyword</th>
                        <th className="px-4 py-3 text-right font-semibold">Searches/mo</th>
                        <th className="px-4 py-3 text-right font-semibold">Products</th>
                        <th className="px-4 py-3 text-right font-semibold">% moving up</th>
                        <th className="px-4 py-3 text-right font-semibold">Reviews</th>
                        <th className="px-4 py-3 text-right font-semibold">Score</th>
                        <th className="px-4 py-3 font-semibold">Store</th>
                      </tr>
                    </thead>
                    <tbody>
                      {amazon.data.movers.map((m, i) => (
                        <AmazonRow
                          key={`${m.store}-${m.keyword}-${i}`}
                          m={m}
                          onOpen={() =>
                            setOpen({
                              laneLabel: "Amazon best-sellers / new releases",
                              keyword: m.keyword,
                              store: m.store,
                              sv: m.sv,
                              found: m.found,
                              validated: m.validated,
                              products: m.products,
                            })
                          }
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            )}
          </>
        ) : null}
      </section>

      <section>
        <div className="mb-3 flex items-baseline gap-3">
          <h2 className="m-0">Winning ads (Meta)</h2>
          <span className="text-xs text-[var(--muted)]">how long ads have run × how many copies are running</span>
        </div>
        {meta.loading ? <Loading /> : null}
        {meta.error ? <ErrorState message={meta.error} /> : null}
        {meta.data ? (
          <>
            <div className="mb-4 grid grid-cols-3 gap-3">
              <Stat label="Keywords" value={meta.data.totals.keywords} hint="products other stores advertise heavily" />
              <Stat label="Products found" value={meta.data.totals.found} hint="across all keywords" />
              <Stat label="Checked" value={meta.data.totals.validated} hint="running a long time and in many copies" />
            </div>
            {meta.data.winners.length === 0 ? (
              <LaneEmpty
                ingestCmd={meta.data.ingest_cmd}
                note="Nothing here yet. Ads that have run a long time and in many copies show up here, with how long they've run and how many copies — as we collect them."
              />
            ) : (
              <Card className="overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
                        <th className="px-4 py-3 font-semibold">Keyword</th>
                        <th className="px-4 py-3 text-right font-semibold">Searches/mo</th>
                        <th className="px-4 py-3 text-right font-semibold">Products</th>
                        <th className="px-4 py-3 text-right font-semibold">Ad running</th>
                        <th className="px-4 py-3 text-right font-semibold">Ad copies</th>
                        <th className="px-4 py-3 text-right font-semibold">Score</th>
                        <th className="px-4 py-3 font-semibold">Store</th>
                      </tr>
                    </thead>
                    <tbody>
                      {meta.data.winners.map((w, i) => (
                        <MetaRow
                          key={`${w.store}-${w.keyword}-${i}`}
                          w={w}
                          onOpen={() =>
                            setOpen({
                              laneLabel: "Winning ads (Meta)",
                              keyword: w.keyword,
                              store: w.store_name ?? w.store,
                              sv: w.sv,
                              found: w.found,
                              validated: w.validated,
                              products: w.products,
                            })
                          }
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            )}
          </>
        ) : null}
      </section>
    </div>
  );
}

// Launchers into Amazon's two live demand boards (Best Sellers + New Releases). Amazon
// retired the "Movers & Shakers" page; these are the real pages the operator researches in.
function AmazonBrowseLaunchers() {
  const browse = useApi(() => api.amazonBrowse());
  if (browse.loading || browse.error || !browse.data) return null;
  return (
    <div className="mb-4">
      <p className="mb-3 text-sm text-[var(--muted)]">{browse.data.note}</p>
      <div className="grid gap-3 md:grid-cols-2">
        {browse.data.views.map((v) => (
          <AmazonViewLauncher
            key={v.id}
            view={v}
            categories={browse.data!.categories}
            searchTpl={browse.data!.search_tpl}
          />
        ))}
      </div>
    </div>
  );
}

function AmazonViewLauncher({
  view,
  categories,
  searchTpl,
}: {
  view: AmazonBrowseView;
  categories: AmazonBrowseCategory[];
  searchTpl: string;
}) {
  const [slug, setSlug] = useState(categories[0]?.slug ?? "");
  const [kw, setKw] = useState("");

  const buildUrl = (): string => {
    if (kw.trim()) return searchTpl.replace("{kw}", encodeURIComponent(kw.trim()));
    return slug ? `${view.base_url}${slug}` : view.base_url;
  };

  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-center gap-2">
        <span
          className="rounded px-2 py-0.5 text-xs font-semibold"
          style={{ color: view.color, background: `color-mix(in srgb, ${view.color} 14%, transparent)` }}
        >
          {view.name}
        </span>
      </div>
      <p className="text-[11px] leading-snug text-[var(--muted)]">{view.what}</p>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--muted)]">Department</div>
        <select
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
          disabled={kw.trim().length > 0}
          className="w-full rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-sm text-[var(--text)] disabled:opacity-50"
        >
          {categories.map((c) => (
            <option key={c.label} value={c.slug}>
              {c.label}
            </option>
          ))}
        </select>
      </div>

      <div>
        <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-[var(--muted)]">
          Or search a keyword (sorted by popularity)
        </div>
        <input
          value={kw}
          onChange={(e) => setKw(e.target.value)}
          placeholder="e.g. neck fan"
          className="w-full rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-sm text-[var(--text)]"
        />
      </div>

      <a
        href={buildUrl()}
        target="_blank"
        rel="noreferrer"
        className="mt-auto rounded-md px-3 py-2 text-center text-sm font-semibold text-white transition-opacity hover:opacity-90"
        style={{ background: view.color }}
      >
        Open Amazon {view.name} →
      </a>
    </Card>
  );
}

function AmazonRow({ m, onOpen }: { m: AmazonMover; onOpen: () => void }) {
  return (
    <tr
      onClick={onOpen}
      className="cursor-pointer border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)]"
    >
      <td className="px-4 py-3 font-medium">{m.keyword ?? "—"}</td>
      <td className="px-4 py-3 text-right tabular-nums">{fmt(m.sv)}</td>
      <td className="px-4 py-3 text-right">
        <ProductsCell found={m.found} validated={m.validated} />
      </td>
      <td className="px-4 py-3 text-right font-semibold tabular-nums">
        {m.pct_gain == null ? (
          "—"
        ) : (
          <span style={{ color: "var(--state-live)" }}>▲ {m.pct_gain.toFixed(0)}%</span>
        )}
      </td>
      <td className="px-4 py-3 text-right tabular-nums">{fmt(m.reviews)}</td>
      <td className="px-4 py-3 text-right tabular-nums">{m.score == null ? "—" : m.score.toFixed(1)}</td>
      <td className="px-4 py-3">
        <Pill>{m.store}</Pill>
      </td>
    </tr>
  );
}

function MetaRow({ w, onOpen }: { w: MetaWinner; onOpen: () => void }) {
  return (
    <tr
      onClick={onOpen}
      className="cursor-pointer border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)]"
    >
      <td className="px-4 py-3 font-medium">{w.keyword ?? "—"}</td>
      <td className="px-4 py-3 text-right tabular-nums">{fmt(w.sv)}</td>
      <td className="px-4 py-3 text-right">
        <ProductsCell found={w.found} validated={w.validated} />
      </td>
      <td className="px-4 py-3 text-right font-semibold tabular-nums">
        {w.ad_longevity_days == null ? (
          "—"
        ) : (
          <span style={{ color: "var(--state-winner)" }}>{w.ad_longevity_days}d</span>
        )}
      </td>
      <td className="px-4 py-3 text-right tabular-nums">{w.dup_creatives == null ? "—" : `×${w.dup_creatives}`}</td>
      <td className="px-4 py-3 text-right tabular-nums">{w.score == null ? "—" : w.score.toFixed(1)}</td>
      <td className="px-4 py-3">
        <Pill>{w.store_name ?? w.store}</Pill>
      </td>
    </tr>
  );
}

function LaneEmpty({ note }: { ingestCmd?: string; note: string }) {
  return (
    <Card className="p-6">
      <p className="text-sm text-[var(--muted)]">{note}</p>
    </Card>
  );
}
