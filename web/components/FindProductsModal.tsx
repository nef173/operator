"use client";

import { useEffect, useState } from "react";
import { api, type LaneProduct } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { DetailModal } from "@/components/DetailModal";
import { PriceRulesCard } from "@/components/PriceRulesCard";

// A stable identity for a found product — matches the backend's SKU-id basis (url ▸ title) so
// selection survives the warm-reload that swaps the whole products list in.
const pickKey = (p: LaneProduct, i: number): string => p.url || p.title || `p-${i}`;

/**
 * The cross-feed: a validated keyword / trend / event → find products to list for it. The
 * Google competitor lane (spy roster + direct Google Shopping) is the WEIGHTED PRIMARY, matching
 * the project-wide Google-LED source order; marketplace search is secondary (the COGS basis +
 * supplementary finds). A dedup step sits in between, and a Gemini-vision identity check is a
 * deliberately-pending later phase. The listing price rules ride along at the bottom so the
 * price decision lives next to the product decision.
 *
 * Opened from a keyword candidate row (source="keyword"), a trend row (source="trend"), or an
 * event card (source="event") — every research path funnels into this one handoff.
 */
export function FindProductsModal({
  keyword,
  sv,
  source,
  store,
  open,
  onClose,
}: {
  keyword: string;
  sv?: number | null;
  source: string; // "keyword" | "trend" | "event"
  store?: string | null; // target store for the "List" handoff (null = no store picked yet)
  open: boolean;
  onClose: () => void;
}) {
  const { data, error, loading, reload } = useApi(
    () => (open ? api.findProducts(keyword, { sv, source }) : Promise.resolve(null)),
    [open, keyword, source],
  );

  // A cold keyword returns instantly with `warming: true` while the backend fetches AliExpress /
  // 1688 / Amazon in the background — re-poll every 15s until the products land.
  useEffect(() => {
    if (!open || !data?.warming) return;
    const t = setTimeout(reload, 15000);
    return () => clearTimeout(t);
  }, [open, data?.warming, reload]);

  // Selection for the found-products → listing-queue handoff.
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [listing, setListing] = useState(false);
  const [listMsg, setListMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Reset the picks whenever the modal (re)opens on a different keyword — done at render time
  // (not in an effect), the React-blessed "reset state when a prop changes" pattern, which
  // avoids the extra commit that setState-in-effect causes.
  const pickScope = `${keyword}|${open}`;
  const [pickScopeSeen, setPickScopeSeen] = useState(pickScope);
  if (pickScope !== pickScopeSeen) {
    setPickScopeSeen(pickScope);
    setPicked(new Set());
    setListMsg(null);
  }

  const togglePick = (key: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const addPicked = async () => {
    if (!store || !data?.products?.length || listing) return;
    const chosen = data.products.filter((p, i) => picked.has(pickKey(p, i)));
    if (!chosen.length) return;
    setListing(true);
    setListMsg(null);
    try {
      const res = await api.addFoundProducts(store, { keyword, products: chosen });
      setListMsg({ ok: true, text: `Added ${res.count} to ${store}'s listing queue → ${res.slug}` });
      setPicked(new Set());
    } catch (e) {
      setListMsg({ ok: false, text: e instanceof Error ? e.message : "Could not add to listing queue" });
    } finally {
      setListing(false);
    }
  };

  return (
    <DetailModal
      open={open}
      onClose={onClose}
      badge={
        <span className="rounded bg-[var(--surface-2)] px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-[var(--muted)]">
          {source} → find products
        </span>
      }
      title={keyword}
      subtitle={sv != null ? `${sv.toLocaleString()} monthly searches` : undefined}
    >
      {loading ? (
        <div className="text-sm text-[var(--muted)]">Loading…</div>
      ) : error ? (
        <div className="text-sm text-[var(--state-killed)]">{error}</div>
      ) : data ? (
        <div className="flex flex-col gap-5">
          {/* REAL FINDS — products found on AliExpress / Temu / 1688 / Amazon for this trend/keyword.
              Select any + "List" to add them to the store's listing queue as candidate SKUs. */}
          {data.products && data.products.length ? (
            <section>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <h4 className="text-sm font-semibold">Found products</h4>
                <span className="text-[11px] text-[var(--muted)]">
                  {data.products.length} · AliExpress · Temu · 1688 · Amazon
                </span>
                <div className="ml-auto flex items-center gap-2">
                  {store ? (
                    <button
                      type="button"
                      onClick={() =>
                        setPicked((prev) =>
                          prev.size === data.products!.length
                            ? new Set()
                            : new Set(data.products!.map((p, i) => pickKey(p, i))),
                        )
                      }
                      className="text-[11px] font-medium text-[var(--accent)] hover:underline"
                    >
                      {picked.size === data.products.length ? "Clear" : "Select all"}
                    </button>
                  ) : null}
                  <button
                    type="button"
                    disabled={!store || picked.size === 0 || listing}
                    onClick={addPicked}
                    className="rounded-md px-2.5 py-1 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-40"
                    style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
                    title={
                      store
                        ? `Add the selected products to ${store}'s listing queue`
                        : "Pick a store first (top-left)"
                    }
                  >
                    {listing ? "Adding…" : picked.size ? `List ${picked.size}` : "List products"}
                  </button>
                </div>
              </div>
              {listMsg ? (
                <div
                  className="mb-2 rounded-md px-2 py-1 text-[11px] font-medium"
                  style={{
                    color: listMsg.ok ? "var(--state-winner)" : "var(--state-killed)",
                    background: `color-mix(in srgb, ${
                      listMsg.ok ? "var(--state-winner)" : "var(--state-killed)"
                    } 12%, transparent)`,
                  }}
                >
                  {listMsg.text}
                </div>
              ) : null}
              {!store ? (
                <p className="mb-2 text-[11px] text-[var(--muted)]">
                  Pick a store (top-left) to list these into its queue.
                </p>
              ) : null}
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {data.products.map((p, i) => {
                  const key = pickKey(p, i);
                  const isPicked = picked.has(key);
                  return (
                    <div
                      key={`${key}-${i}`}
                      className="relative flex flex-col overflow-hidden rounded-lg border bg-[var(--surface-2)]"
                      style={{ borderColor: isPicked ? "var(--accent)" : "var(--border)" }}
                    >
                      {store ? (
                        <button
                          type="button"
                          onClick={() => togglePick(key)}
                          aria-pressed={isPicked}
                          title={isPicked ? "Remove from listing selection" : "Select to list"}
                          className="absolute left-1 top-1 z-10 flex h-5 w-5 items-center justify-center rounded border text-[11px] font-bold"
                          style={{
                            background: isPicked
                              ? "var(--accent)"
                              : "color-mix(in srgb, var(--surface) 80%, transparent)",
                            color: isPicked ? "var(--accent-fg)" : "var(--muted)",
                            borderColor: isPicked ? "var(--accent)" : "var(--border)",
                          }}
                        >
                          {isPicked ? "✓" : "+"}
                        </button>
                      ) : null}
                      <a
                        href={p.url ?? undefined}
                        target="_blank"
                        rel="noreferrer"
                        className="flex flex-1 flex-col hover:opacity-95"
                      >
                        <div className="aspect-square w-full bg-[var(--surface)]">
                          {p.image ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img src={p.image} alt={p.title ?? ""} className="h-full w-full object-cover" loading="lazy" />
                          ) : (
                            <div className="flex h-full w-full items-center justify-center text-[10px] text-[var(--muted)]">
                              no image
                            </div>
                          )}
                        </div>
                        <div className="flex flex-1 flex-col gap-1 p-2">
                          <div className="line-clamp-2 text-[11px] font-medium leading-snug">{p.title ?? "—"}</div>
                          <div className="mt-auto flex items-center justify-between text-[10px] text-[var(--muted)]">
                            <span className="font-bold uppercase tracking-wide">{p.store_name ?? p.source}</span>
                            {p.price != null ? <span className="tabular-nums">${p.price}</span> : null}
                          </div>
                          {p.sold_count != null ? (
                            <div className="text-[10px] text-[var(--muted)]">{p.sold_count.toLocaleString()} sold</div>
                          ) : p.note ? (
                            <div className="line-clamp-1 text-[10px] text-[var(--muted)]">{p.note}</div>
                          ) : null}
                        </div>
                      </a>
                    </div>
                  );
                })}
              </div>
            </section>
          ) : data.warming ? (
            <section
              className="flex items-center gap-2 rounded-lg border border-[var(--border)] p-3 text-xs text-[var(--muted)]"
              style={{ background: "var(--surface)" }}
            >
              <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-[var(--muted)] border-t-transparent" />
              Finding products across AliExpress · 1688 · Amazon… this updates automatically in a few
              seconds.
            </section>
          ) : (
            <section
              className="rounded-lg border border-[var(--border)] p-3 text-xs text-[var(--muted)]"
              style={{ background: "var(--surface)" }}
            >
              No products found on AliExpress / Temu / 1688 / Amazon for this keyword — the launchers
              below open each source to research manually.
            </section>
          )}

          {/* STEP 0 — keyword + trend research (the seed is a starting point, not the final list) */}
          {data.keyword_research ? (
            <section className="rounded-lg border border-[var(--border)] p-3" style={{ background: "var(--surface)" }}>
              <div className="flex items-center gap-2">
                <h4 className="text-sm font-semibold">Keyword &amp; trend research</h4>
                <span
                  className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
                  style={{ color: "var(--accent)", background: "color-mix(in srgb, var(--accent) 14%, transparent)" }}
                >
                  do this first
                </span>
                <span
                  className="ml-auto rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
                  style={{ color: "var(--state-winner)", background: "color-mix(in srgb, var(--state-winner) 14%, transparent)" }}
                  title="Volume tier of the seed keyword — the focus is the biggest terms"
                >
                  {data.keyword_research.seed_tier.label}
                </span>
              </div>
              <p className="mt-1 text-xs text-[var(--muted)]">{data.keyword_research.what}</p>
              <p className="mt-1.5 text-[11px] text-[var(--muted)]">{data.keyword_research.floor_vs_focus}</p>
              <p className="mt-1.5 text-[11px] font-medium text-[var(--text)]">{data.keyword_research.then}</p>
            </section>
          ) : null}

          {/* PRIMARY — Google competitor (spy roster + direct Google Shopping) */}
          <section>
            <div className="flex items-center gap-2">
              <h4 className="text-sm font-semibold">Google competitor</h4>
              <span
                className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
                style={{ color: "var(--state-winner)", background: "color-mix(in srgb, var(--state-winner) 14%, transparent)" }}
              >
                primary
              </span>
              <span className="text-[11px] text-[var(--muted)]">{data.competitor_scan.depth}</span>
            </div>
            <p className="mt-1 text-xs text-[var(--muted)]">{data.competitor_scan.what}</p>

            {/* path 1 — the tracked spy roster (products.json, deeper than top-30) */}
            <div className="mt-2 text-[11px] font-medium text-[var(--text)]">
              Best-Seller Spy roster · {data.competitor_scan.n_stores} tracked store
              {data.competitor_scan.n_stores === 1 ? "" : "s"}
            </div>
            {data.competitor_scan.stores.length ? (
              <ul className="mt-1 flex flex-col gap-1">
                {data.competitor_scan.stores.slice(0, 8).map((s) => (
                  <li key={s.domain} className="flex items-center justify-between gap-2 text-[11px]">
                    <span className="truncate font-medium">{s.domain}</span>
                    <a
                      href={`https://${s.domain}/search?q=${encodeURIComponent(keyword)}&type=product`}
                      target="_blank"
                      rel="noreferrer"
                      className="shrink-0 text-[var(--accent)] hover:underline"
                    >
                      Search this store →
                    </a>
                  </li>
                ))}
              </ul>
            ) : null}

            {/* path 2 — direct non-branded Google Shopping */}
            {data.competitor_scan.google_shopping ? (
              <div className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-[var(--border)] p-2">
                <span className="text-sm font-medium">{data.competitor_scan.google_shopping.label}</span>
                <span className="text-[11px] text-[var(--muted)]">{data.competitor_scan.google_shopping.what}</span>
                <a
                  href={data.competitor_scan.google_shopping.url}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-auto rounded-md px-2 py-1 text-xs font-semibold"
                  style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
                >
                  Open →
                </a>
              </div>
            ) : null}
          </section>

          {/* SECONDARY — marketplace search (COGS basis + supplementary) */}
          <section>
            <div className="flex items-center gap-2">
              <h4 className="text-sm font-semibold">Marketplace search</h4>
              <span className="rounded bg-[var(--surface-2)] px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-[var(--muted)]">
                secondary · COGS basis
              </span>
            </div>
            <p className="mt-1 text-xs text-[var(--muted)]">{data.marketplace.what}</p>
            <div className="mt-2 flex flex-col gap-2">
              {data.marketplace.platforms.map((p) => (
                <div
                  key={p.id}
                  className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--border)] p-2"
                >
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-full"
                    style={{ background: p.color }}
                  />
                  <span className="text-sm font-medium">{p.name}</span>
                  {p.sort_note ? (
                    <span className="text-[11px] text-[var(--muted)]">{p.sort_note}</span>
                  ) : null}
                  {p.timeframes.length ? (
                    <span className="text-[11px] text-[var(--muted)]">
                      · {p.timeframes.map((t) => t.label).join(" / ")}
                    </span>
                  ) : null}
                  {p.url ? (
                    <a
                      href={p.url}
                      target="_blank"
                      rel="noreferrer"
                      className="ml-auto rounded-md px-2 py-1 text-xs font-semibold"
                      style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
                    >
                      Search →
                    </a>
                  ) : null}
                </div>
              ))}
            </div>
          </section>

          {/* dedup + vision check */}
          <section className="flex flex-col gap-2 rounded-lg border border-[var(--border)] p-3">
            <div>
              <div className="text-xs font-semibold">Catalog dedup</div>
              <p className="mt-0.5 text-[11px] text-[var(--muted)]">{data.dedup.what}</p>
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-xs font-semibold">Vision identity check</span>
                <span
                  className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
                  style={{ color: "var(--state-testing)", background: "color-mix(in srgb, var(--state-testing) 14%, transparent)" }}
                >
                  {data.vision_check.status}
                </span>
              </div>
              <p className="mt-0.5 text-[11px] text-[var(--muted)]">{data.vision_check.what}</p>
              <p className="mt-0.5 text-[11px] text-[var(--muted)]">{data.vision_check.note}</p>
            </div>
          </section>

          {/* listing price rules — the price decision lives next to the product decision */}
          <PriceRulesCard rules={data.pricing} />
        </div>
      ) : null}
    </DetailModal>
  );
}
