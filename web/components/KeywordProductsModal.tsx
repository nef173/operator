"use client";

import { useEffect, useState } from "react";
import { api, type LaneProduct } from "@/lib/api";

function fmt(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

// The visible source domain for a researched product URL, so the operator can SEE where
// each product was found (not just a title hyperlink). Falls back to the raw string.
function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

// Stable identity for a found product — matches the backend SKU-id basis (url ▸ title).
const pickKey = (p: LaneProduct, i: number): string => p.url || p.title || `p-${i}`;

// A "window-in-window" drill-in over a single researched keyword: the products the
// discovery lane found for it, with their validation state and per-lane signals. Used by
// the Marketplace and Ad-winner (Amazon / Meta) surfaces so every keyword row can be
// opened to inspect the underlying products. Lane-agnostic — it only renders the product
// signals that are present (orders/sold for marketplace, %gain/reviews for Amazon,
// longevity/creatives for Meta).
export function KeywordProductsModal({
  open,
  keyword,
  store,
  sv,
  found,
  validated,
  products,
  laneLabel,
  onValidate,
  onPhotoDedup,
  photoDedupBusy,
  photoDedupError,
  onClose,
}: {
  open: boolean;
  keyword: string | null;
  store: string | null;
  sv: number | null;
  found: number;
  validated: number;
  products: LaneProduct[];
  laneLabel: string;
  // When provided, renders a "Check products" action in the header — the dedup + sourcing
  // check that used to be a separate top-level button. Optional so the other callers
  // (Marketplace / Ad-winner surfaces) that only inspect products are unaffected.
  onValidate?: () => void;
  // When provided, renders the Gemini photo-duplicate action: groups the product photos
  // that show the SAME physical product and stamps a dup badge on each grouped card.
  onPhotoDedup?: () => void;
  photoDedupBusy?: boolean;
  photoDedupError?: string | null;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Found-products → listing-queue handoff (same as FindProductsModal — this modal is the other
  // surface that shows the found products, so it gets the same "List" action).
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [listing, setListing] = useState(false);
  const [listMsg, setListMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // Reset the picks when the modal (re)opens on a different keyword — at render time (not in an
  // effect), the React-blessed "reset state when a prop changes" pattern (avoids the extra commit).
  const pickScope = `${keyword}|${open}`;
  const [pickScopeSeen, setPickScopeSeen] = useState(pickScope);
  if (pickScope !== pickScopeSeen) {
    setPickScopeSeen(pickScope);
    setPicked(new Set());
    setListMsg(null);
  }
  const togglePick = (k: string) =>
    setPicked((prev) => {
      const n = new Set(prev);
      if (n.has(k)) n.delete(k);
      else n.add(k);
      return n;
    });
  const canList = Boolean(store && keyword);
  const addPicked = async () => {
    if (!store || !keyword || listing) return;
    const chosen = products.filter((p, i) => picked.has(pickKey(p, i)));
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

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/50 p-4 sm:p-8"
      onClick={onClose}
    >
      <div
        className="my-auto w-full max-w-4xl rounded-2xl border border-[var(--border)] bg-[var(--surface)] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-4 border-b border-[var(--border)] px-5 py-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
                style={{ color: "var(--accent)", background: "color-mix(in srgb, var(--accent) 14%, transparent)" }}
              >
                {laneLabel}
              </span>
              <h3 className="m-0 truncate text-lg font-semibold">{keyword ?? "—"}</h3>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--muted)]">
              {sv != null ? <span>{fmt(sv)} searches/mo</span> : null}
              <span>
                <strong className="text-[var(--text)]">{found}</strong> found
              </span>
              <span>
                <strong style={{ color: "var(--state-live)" }}>{validated}</strong> checked
              </span>
              {store ? <span>· {store}</span> : null}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {onPhotoDedup ? (
              <button
                type="button"
                disabled={photoDedupBusy}
                onClick={onPhotoDedup}
                title="Flag duplicate products: matches by TITLE + Gemini vision on the PHOTOS (same product across different names/backgrounds/angles) and scans your store's catalog — so the same item never gets built twice."
                className="rounded-lg border border-[var(--border)] px-2.5 py-1 text-sm font-semibold hover:bg-[var(--surface-2)] disabled:opacity-50"
              >
                {photoDedupBusy ? "Checking…" : "Product dup check"}
              </button>
            ) : null}
            {onValidate ? (
              <button
                type="button"
                onClick={onValidate}
                title="Check these products: skip anything your store already sells, and confirm each one can be sourced. Results show in Sourcing Match."
                className="rounded-lg px-2.5 py-1 text-sm font-semibold"
                style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
              >
                Check products
              </button>
            ) : null}
            {canList ? (
              <button
                type="button"
                disabled={picked.size === 0 || listing}
                onClick={addPicked}
                title={`Add the selected products to ${store}'s listing queue as candidate SKUs`}
                className="rounded-lg border border-[var(--accent)] px-2.5 py-1 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-40"
                style={{ color: "var(--accent)" }}
              >
                {listing ? "Adding…" : picked.size ? `List ${picked.size}` : "List"}
              </button>
            ) : null}
            <button
              type="button"
              onClick={onClose}
              aria-label="Close"
              className="rounded-lg border border-[var(--border)] px-2.5 py-1 text-sm font-medium text-[var(--muted)] hover:bg-[var(--surface-2)]"
            >
              Esc ✕
            </button>
          </div>
        </div>

        {/* Products grid */}
        <div className="max-h-[70vh] overflow-y-auto p-5">
          {photoDedupError ? (
            <p className="mb-3 rounded-lg border border-[var(--border)] px-3 py-2 text-xs text-[var(--state-killed)]">
              {photoDedupError}
            </p>
          ) : null}
          {listMsg ? (
            <p
              className="mb-3 rounded-lg px-3 py-2 text-xs font-medium"
              style={{
                color: listMsg.ok ? "var(--state-winner)" : "var(--state-killed)",
                background: `color-mix(in srgb, ${
                  listMsg.ok ? "var(--state-winner)" : "var(--state-killed)"
                } 12%, transparent)`,
              }}
            >
              {listMsg.text}
            </p>
          ) : null}
          {products.length > 0 && canList ? (
            <div className="mb-3 flex items-center gap-3 text-xs">
              <button
                type="button"
                onClick={() =>
                  setPicked((prev) =>
                    prev.size === products.length
                      ? new Set()
                      : new Set(products.map((p, i) => pickKey(p, i))),
                  )
                }
                className="font-medium text-[var(--accent)] hover:underline"
              >
                {picked.size === products.length ? "Clear selection" : "Select all"}
              </button>
              {picked.size ? <span className="text-[var(--muted)]">{picked.size} selected to list</span> : null}
            </div>
          ) : null}
          {products.length === 0 ? (
            <p className="py-8 text-center text-sm text-[var(--muted)]">
              No products to show for this keyword yet — the number above is just a count
              so far. The actual products will appear here as the search finishes.
            </p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {products.map((p, i) => {
                const k = pickKey(p, i);
                return (
                  <ProductCard
                    key={`${k}-${i}`}
                    p={p}
                    selectable={canList}
                    picked={picked.has(k)}
                    onToggle={() => togglePick(k)}
                  />
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ProductCard({
  p,
  selectable,
  picked,
  onToggle,
}: {
  p: LaneProduct;
  selectable?: boolean;
  picked?: boolean;
  onToggle?: () => void;
}) {
  // Some lanes (Amazon Movers) seed a product with only a url + note (no scraped
  // title/image yet). Fall back so the card stays informative instead of blank "—".
  const label = p.title ?? p.note ?? p.store_name ?? (p.url ? hostOf(p.url) : "—");
  const placeholder = p.url ? hostOf(p.url) : "no image";
  const signals: { label: string; value: string }[] = [];
  if (p.orders != null) signals.push({ label: "orders", value: fmt(p.orders) });
  if (p.sold_count != null) signals.push({ label: "sold", value: fmt(p.sold_count) });
  if (p.pct_gain != null) signals.push({ label: "moving up", value: `▲ ${p.pct_gain.toFixed(0)}%` });
  if (p.bought_past_month != null) signals.push({ label: "bought/mo", value: `${fmt(p.bought_past_month)}+` });
  if (p.rating != null) signals.push({ label: "★", value: p.rating.toFixed(1) });
  if (p.reviews != null) signals.push({ label: "reviews", value: fmt(p.reviews) });
  if (p.ad_longevity_days != null) signals.push({ label: "ad running", value: `${p.ad_longevity_days}d` });
  if (p.dup_creatives != null) signals.push({ label: "ads", value: `×${p.dup_creatives}` });
  if (p.cogs != null) signals.push({ label: "cost", value: `$${p.cogs.toFixed(2)}` });

  return (
    <div
      className="flex flex-col overflow-hidden rounded-lg border bg-[var(--surface-2)]"
      style={{ borderColor: picked ? "var(--accent)" : "var(--border)" }}
    >
      <div className="relative aspect-square w-full bg-[var(--surface)]">
        {p.image ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={p.image} alt={p.title ?? ""} className="h-full w-full object-cover" loading="lazy" />
        ) : (
          <div className="flex h-full w-full flex-col items-center justify-center gap-1 px-3 text-center">
            <span className="text-[11px] font-medium text-[var(--muted)]">{placeholder}</span>
            <span className="text-[10px] text-[var(--muted)]">image loading…</span>
          </div>
        )}
        <span
          className="absolute left-2 top-2 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
          style={
            p.validated
              ? { color: "var(--state-live)", background: "color-mix(in srgb, var(--state-live) 18%, black)" }
              : { color: "var(--muted)", background: "color-mix(in srgb, var(--muted) 22%, black)" }
          }
        >
          {p.validated ? "checked" : "not checked yet"}
        </span>
        {p.rank != null ? (
          <span className="absolute right-2 top-2 rounded-md bg-black/65 px-1.5 py-0.5 text-[11px] font-bold tabular-nums text-white">
            #{p.rank}
          </span>
        ) : null}
        {p.photo_dup ? (
          <span
            className="absolute bottom-2 left-2 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
            style={
              p.photo_dup_drop
                ? { color: "var(--state-killed)", background: "color-mix(in srgb, var(--state-killed) 22%, black)" }
                : { color: "var(--state-testing)", background: "color-mix(in srgb, var(--state-testing) 22%, black)" }
            }
            title={
              p.photo_dup_drop
                ? `Same physical product as ${(p.dup_group_size ?? 2) - 1} other photo(s) — beyond the A/B dedup cap, drop this one`
                : `Same physical product as ${(p.dup_group_size ?? 2) - 1} other photo(s) — within the A/B cap`
            }
          >
            {p.photo_dup_drop ? "dup — over cap" : `same product ×${p.dup_group_size ?? 2}`}
          </span>
        ) : null}
        {p.in_store ? (
          <span
            className="absolute top-2 left-2 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
            style={{ color: "var(--state-killed)", background: "color-mix(in srgb, var(--state-killed) 22%, black)" }}
            title={
              p.in_store_label
                ? `Already in your store — matches “${p.in_store_label}”. Skip building it again.`
                : "Already in your store's catalog — skip building it again."
            }
          >
            in store
          </span>
        ) : null}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5 p-3">
        <div className="line-clamp-2 text-sm font-medium leading-snug">
          {p.url ? (
            <a href={p.url} target="_blank" rel="noreferrer" className="hover:text-[var(--accent)]">
              {label}
            </a>
          ) : (
            label
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {p.price != null ? (
            <span className="text-sm font-semibold tabular-nums">${p.price.toFixed(2)}</span>
          ) : null}
          {signals.map((s) => (
            <span key={s.label} className="text-[11px] tabular-nums text-[var(--muted)]">
              {s.value} {s.label}
            </span>
          ))}
        </div>
        {p.store_name && p.title ? (
          <div className="truncate text-[11px] text-[var(--muted)]">{p.store_name}</div>
        ) : null}
        {p.url ? (
          <a
            href={p.url}
            target="_blank"
            rel="noreferrer"
            title={p.url}
            className="mt-auto truncate text-[11px] font-medium text-[var(--accent)] hover:underline"
          >
            {hostOf(p.url)} ↗
          </a>
        ) : null}
        {selectable ? (
          <button
            type="button"
            onClick={onToggle}
            aria-pressed={picked}
            title={picked ? "Remove from listing selection" : "Select to add to the listing queue"}
            className={`${p.url ? "mt-2" : "mt-auto"} rounded-md border px-2 py-1 text-[11px] font-semibold`}
            style={{
              borderColor: picked ? "var(--accent)" : "var(--border)",
              background: picked ? "var(--accent)" : "transparent",
              color: picked ? "var(--accent-fg)" : "var(--muted)",
            }}
          >
            {picked ? "✓ Selected to list" : "+ Select to list"}
          </button>
        ) : null}
      </div>
    </div>
  );
}
