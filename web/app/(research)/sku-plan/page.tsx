"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  api,
  type SkuPlan,
  type SkuPlanHead,
  type SkuPlanRow,
  type SourceQuota,
  type SkuRole,
  type SupplyBlock,
  type SupplyState,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Stat, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState } from "@/components/PageState";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { KeywordProductsModal } from "@/components/KeywordProductsModal";
import type { LaneProduct } from "@/lib/api";

function fmtSv(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

const SEGMENT_SOURCE_LABEL: Record<string, string> = {
  shopping_scan: "Shopping scan",
  serp: "Similar SERP",
  dataforseo: "DataForSEO",
};

const BUCKET_COLOR: Record<string, string> = {
  BREAKOUT: "var(--state-winner)",
  "LIST-NOW": "var(--state-live)",
  "BUILD-AHEAD": "var(--state-testing)",
  EVERGREEN: "var(--muted)",
  SKIP: "var(--state-killed)",
};

// Volume tier → the winner-probability band. Bigger SV = brighter + more products. The 10k floor
// is the gate; the FOCUS is the biggest keywords, so prime/strong read the loudest.
const TIER_COLOR: Record<string, string> = {
  prime: "var(--state-winner)",
  strong: "var(--state-live)",
  solid: "var(--accent)",
  entry: "var(--muted)",
  below: "var(--state-killed)",
};

// Role → how it reads in the plan. Anchor + standalone-build = real SKUs; combine rides a
// title; supporting feeds copy only.
const ROLE_META: Record<SkuRole, { label: string; color: string; hint: string }> = {
  anchor: { label: "Main", color: "var(--state-winner)", hint: "the main keyword — the leader; we always make this one a product" },
  standalone: { label: "Own product", color: "var(--state-live)", hint: "enough people search this on its own, so it becomes its own product" },
  combine: { label: "In title", color: "var(--state-testing)", hint: "a feature or detail we fold into another product's title instead of a separate product" },
  supporting: { label: "Copy only", color: "var(--muted)", hint: "too few searches to be its own product; we just use it in the product description" },
};

function QuotaBar({ quota }: { quota: SourceQuota }) {
  const total =
    quota.google_shopping + quota.competitor_catalog + quota.marketplace + quota.amazon + quota.meta;
  if (total <= 0) return <span className="text-xs text-[var(--muted)]">—</span>;
  const parts: { k: keyof SourceQuota; color: string; label: string; short: string }[] = [
    { k: "google_shopping", color: "var(--state-live)", label: "GoogleShopping Search", short: "Shopping" },
    { k: "competitor_catalog", color: "var(--state-live)", label: "Google Competitor Catalog", short: "Catalog" },
    { k: "marketplace", color: "var(--state-testing)", label: "Marketplace", short: "Mkt" },
    { k: "amazon", color: "var(--state-winner)", label: "Amazon Movers & Shakers", short: "Amazon" },
    { k: "meta", color: "var(--accent)", label: "Meta ads", short: "Meta" },
  ];
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5 text-[11px] tabular-nums">
      {parts.map((p) =>
        quota[p.k] > 0 ? (
          <span
            key={p.k}
            className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-semibold"
            style={{ color: p.color, background: `color-mix(in srgb, ${p.color} 14%, transparent)` }}
            title={`Find ${quota[p.k]} candidate product${quota[p.k] === 1 ? "" : "s"} from ${p.label}`}
          >
            {p.short} {quota[p.k]}
          </span>
        ) : null,
      )}
      <span className="text-[10px] text-[var(--muted)]">products to find</span>
    </span>
  );
}

export default function SkuPlanPage() {
  const { data, error, loading, reload } = useApi(() => api.skuPlan());

  // Light auto-refresh so a just-promoted keyword appears without a manual reload — this page
  // is computed live from the backlog + listing queue, so re-polling is cheap and keeps it fresh.
  useEffect(() => {
    const id = setInterval(reload, 12000);
    return () => clearInterval(id);
  }, [reload]);

  return (
    <div>
      <PageHeader
        title="SKU Plan"
        subtitle="Turns one keyword into a shopping list of products to find. The main product gets most of the effort; the rest spread across related searches. Hit go and it finds them for you."
        action={
          <button
            type="button"
            onClick={reload}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--surface-2)]"
          >
            Refresh
          </button>
        }
      />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? <PlanBody plan={data} onResearched={reload} /> : null}
    </div>
  );
}

function PlanBody({ plan, onResearched }: { plan: SkuPlan; onResearched: () => void }) {
  const t = plan.totals;
  const s = plan.settings;
  // Editable "products to find per keyword" — the base count each promoted keyword goes hunting
  // for. Variable: the operator sets it here; it's still auto-scaled up for bigger-volume heads.
  const [ppb, setPpb] = useState(String(s.products_per_build));
  const [savingPpb, setSavingPpb] = useState(false);
  async function savePpb() {
    const n = parseInt(ppb, 10);
    if (!Number.isFinite(n) || n === s.products_per_build) return;
    setSavingPpb(true);
    try {
      await api.skuPlanSettingsSave({ products_per_build: Math.max(1, n) });
      onResearched();
    } finally {
      setSavingPpb(false);
    }
  }
  return (
    <>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Main keywords" value={t.heads} hint="keywords with a plan ready" />
        <Stat label="Products to make" value={t.build_skus} hint="main products + extra related ones" />
        <Stat label="Extra products" value={t.tier2_builds} hint="related searches worth their own product" />
        <Stat label="Folded into titles" value={t.combine} hint="details added into a product title" />
        <Stat label="Copy only" value={t.supporting} hint="used in descriptions, not their own product" />
        <Stat label="Products to find" value={t.research_budget} hint="how many we'll go search for" />
      </div>

      {/* Settings + dedup banner — the operator-tunable knobs this plan ran with. */}
      <Card className="mt-4 px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-[var(--muted)]">
          <span>
            Main product gets <strong className="text-[var(--text)]">{s.anchor_pct}%</strong> of the effort
          </span>
          <span>
            Covers <strong className="text-[var(--text)]">{s.coverage_pct}%</strong> of related searches
          </span>
          <span className="inline-flex items-center gap-1.5">
            Products to find per keyword
            <input
              type="number"
              min={1}
              value={ppb}
              onChange={(e) => setPpb(e.target.value)}
              onBlur={savePpb}
              onKeyDown={(e) => e.key === "Enter" && savePpb()}
              disabled={savingPpb}
              className="w-16 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-0.5 text-center text-xs font-semibold tabular-nums text-[var(--text)] focus:border-[var(--accent)] focus:outline-none disabled:opacity-50"
              title="Base count each promoted keyword goes to find — no upper limit; still scaled up automatically for bigger-volume head keywords"
            />
            {savingPpb ? <span className="text-[10px]">saving…</span> : null}
          </span>
          <span>
            Where we look{" "}
            <strong className="text-[var(--text)]">
              {s.source_weights.google_shopping}/{s.source_weights.competitor_catalog}/
              {s.source_weights.marketplace}/{s.source_weights.amazon}/{s.source_weights.meta}
            </strong>{" "}
            (Google Shopping / Competitor catalogs / Marketplace / Amazon / Meta)
          </span>
          <span>
            Same product at most <strong className="text-[var(--text)]">{plan.dedup.cap}×</strong>
          </span>
          <span className="inline-flex items-center gap-1" title={plan.dedup.vision_hint ?? undefined}>
            Product duplicate check
            <span
              className="rounded px-1.5 py-0.5 font-semibold"
              style={{
                color: plan.dedup.vision_status === "ready" ? "var(--state-live)" : "var(--state-testing)",
                background: `color-mix(in srgb, ${
                  plan.dedup.vision_status === "ready" ? "var(--state-live)" : "var(--state-testing)"
                } 14%, transparent)`,
              }}
            >
              {plan.dedup.vision_status}
            </span>
          </span>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-[var(--muted)]">{plan.dedup.rule}</p>
        <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-[var(--muted)]">
          <span className="font-semibold text-[var(--text)]">Where on Google we look:</span>
          {plan.google_paths.map((p) => (
            <span key={p.id} title={p.what} className="rounded border border-[var(--border)] px-1.5 py-0.5">
              {p.label}
            </span>
          ))}
        </div>
      </Card>

      <SupplyStrip supply={plan.supply} labels={plan.source_labels} onChanged={onResearched} />

      <h2 className="mb-3 mt-6">The plan</h2>

      {plan.heads.length === 0 ? (
        <Card className="p-8 text-center text-sm text-[var(--muted)]">
          No plans yet. Find some product ideas in Keyword Research first, then come back here
          to turn one into a shopping list of products to build.
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          {plan.heads.map((h, i) => (
            <HeadPlan key={`${h.store}-${h.keyword}-${i}`} h={h} onResearched={onResearched} />
          ))}
        </div>
      )}
    </>
  );
}

// Source-supply strip — live/dry per source + the adaptive redistribution it triggers. When a
// source has no NEW products to find for a while, mark it dry: its find-budget shifts onto the
// live sources so the daily listing count is still met. Flip back to live the moment new
// products appear and its weight returns.
function SupplyStrip({
  supply,
  labels,
  onChanged,
}: {
  supply: SupplyBlock;
  labels: Record<string, string>;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  // Editable weight split — the operator's override, saved to the MAIN settings (the same
  // persisted default every plan starts from). Weights are relative shares: "google 100,
  // rest 0" sends everything to the Google competitor lane; "google 50, meta 50" splits.
  const [draft, setDraft] = useState<Record<string, string> | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const order: (keyof typeof supply.sources)[] = [
    "google_shopping", "competitor_catalog", "marketplace", "amazon", "meta",
  ];

  async function toggle(source: string, next: SupplyState) {
    setBusy(source);
    try {
      await api.skuPlanSetSourceSupply(source, next);
      onChanged();
    } finally {
      setBusy(null);
    }
  }

  function startEdit() {
    setSaveErr(null);
    setDraft(
      Object.fromEntries(order.map((k) => [k, String(supply.sources[k].configured_weight)])),
    );
  }

  async function saveDraft() {
    if (!draft) return;
    const weights: Record<string, number> = {};
    for (const k of order) {
      const v = Number(draft[k]);
      weights[k] = Number.isFinite(v) && v >= 0 ? v : 0;
    }
    if (Object.values(weights).every((v) => v === 0)) {
      setSaveErr("At least one source needs a weight above 0.");
      return;
    }
    setSaving(true);
    setSaveErr(null);
    try {
      await api.skuPlanSettingsSave({ source_weights: weights as never });
      setDraft(null);
      onChanged();
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="mt-3 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-[var(--text)]">Where products come from</span>
        {supply.adapted ? (
          <span
            className="rounded px-1.5 py-0.5 text-[11px] font-semibold"
            style={{ color: "var(--state-testing)", background: "color-mix(in srgb, var(--state-testing) 14%, transparent)" }}
          >
            adjusting
          </span>
        ) : (
          <span className="text-[11px] text-[var(--muted)]">all working</span>
        )}
        <span className="ml-auto flex items-center gap-2">
          {draft ? (
            <>
              <button
                type="button"
                disabled={saving}
                onClick={() => setDraft(null)}
                className="rounded border border-[var(--border)] px-2 py-0.5 text-[11px] font-semibold text-[var(--muted)] disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={saving}
                onClick={saveDraft}
                className="rounded px-2 py-0.5 text-[11px] font-semibold disabled:opacity-50"
                style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
                title="Saves this split to the main settings — every future plan and Find-products run uses it."
              >
                {saving ? "…" : "Save split"}
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={startEdit}
              className="rounded border border-[var(--border)] px-2 py-0.5 text-[11px] font-semibold text-[var(--muted)] hover:bg-[var(--surface-2)]"
              title="Change how the find-budget splits across the sources (e.g. Google 100 / rest 0, or Google 50 / Meta 50). Saved to the main settings."
            >
              Edit split
            </button>
          )}
        </span>
      </div>

      <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
        {order.map((k) => {
          const s = supply.sources[k];
          const dry = s.state === "dry";
          const color = dry ? "var(--state-killed)" : "var(--state-live)";
          return (
            <div
              key={k}
              className="flex items-center gap-2 rounded-lg border border-[var(--border)] px-2.5 py-1.5"
            >
              <span className="h-2 w-2 rounded-full" style={{ background: color }} />
              <span className="text-xs font-medium text-[var(--text)]">{labels[k] ?? k}</span>
              {draft ? (
                <input
                  type="number"
                  min={0}
                  max={100}
                  value={draft[k]}
                  onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}
                  className="w-16 rounded border border-[var(--border)] bg-[var(--surface)] px-1.5 py-0.5 text-right text-[11px] tabular-nums"
                  aria-label={`${labels[k] ?? k} weight`}
                />
              ) : (
                <span className="text-[11px] tabular-nums text-[var(--muted)]">
                  {s.configured_weight}
                  {s.effective_weight !== s.configured_weight ? (
                    <>
                      {" → "}
                      <strong style={{ color }}>{s.effective_weight}</strong>
                    </>
                  ) : null}
                </span>
              )}
              <button
                type="button"
                disabled={busy === k}
                onClick={() => toggle(k, dry ? "live" : "dry")}
                className="rounded border border-[var(--border)] px-1.5 py-0.5 text-[11px] font-semibold disabled:opacity-50"
                style={{ color }}
                title={dry ? "Turn back on — new products are showing up here again" : "Turn off — no new products to find here right now"}
              >
                {busy === k ? "…" : dry ? "Turn back on" : "Mark dry"}
              </button>
            </div>
          );
        })}
      </div>

      {saveErr ? <p className="mt-2 text-[11px] text-[var(--state-killed)]">{saveErr}</p> : null}
      <p className="mt-2 text-[11px] leading-relaxed text-[var(--muted)]">
        {draft
          ? "Weights are relative shares of the find-budget — set Google 100 and the rest 0 to source everything from the Google competitor lane (whole competitor catalogs + direct Google Shopping), or 50/50 between any two. Saved to the main settings."
          : supply.note}
      </p>
    </Card>
  );
}

function HeadPlan({ h, onResearched }: { h: SkuPlanHead; onResearched: () => void }) {
  const [confirm, setConfirm] = useState(false);
  const [validateConfirm, setValidateConfirm] = useState(false);
  const [promoteConfirm, setPromoteConfirm] = useState(false);
  // Flips once the head keyword has been promoted into a store category — that's the
  // moment the plan crosses from "research" into "ready for daily listing".
  const [promoted, setPromoted] = useState(false);
  // Found-products drill-in — kept behind a click so the plan table stays clean. Lazily
  // fetched on open; the products live in the modal, not inline, for a tighter overview.
  const [foundOpen, setFoundOpen] = useState(false);
  const [foundLoading, setFoundLoading] = useState(false);
  const [found, setFound] = useState<{ found: number; validated: number; products: LaneProduct[] } | null>(null);
  // Gemini photo-duplicate run over the found products — flags same-physical-product photos.
  const [dedupBusy, setDedupBusy] = useState(false);
  const [dedupErr, setDedupErr] = useState<string | null>(null);
  // "Find products" progress — while the scan runs we show a live indicator on the row + View found,
  // then auto-refresh the found products when they land (so clicking View found shows the finds).
  const [finding, setFinding] = useState(false);
  const [findMsg, setFindMsg] = useState<string | null>(null);
  const findAlive = useRef(true);
  useEffect(() => () => { findAlive.current = false; }, []);
  const bucketColor = h.capture_bucket ? BUCKET_COLOR[h.capture_bucket] ?? "var(--muted)" : "var(--muted)";

  async function openFound() {
    setFoundOpen(true);
    if (found) return; // already loaded once for this head
    setFoundLoading(true);
    try {
      const r = await api.skuPlanFound(h.store ?? "", h.keyword);
      setFound({ found: r.found, validated: r.validated, products: r.products });
    } finally {
      setFoundLoading(false);
    }
  }

  // Kick the finder, then poll the found count so the row reflects real progress (the scan runs
  // ~30-60s server-side). Non-blocking: the confirm dialog returns immediately; the row shows the
  // spinner; when products land we cache them so View found opens straight to the results.
  function startFind() {
    setFinding(true);
    setFindMsg(null);
    setFound(null); // fresh scan → drop the stale cache so View found reloads
    (async () => {
      for (let i = 0; i < 15 && findAlive.current; i++) {
        await new Promise((r) => setTimeout(r, 6000));
        if (!findAlive.current) return;
        try {
          const r = await api.skuPlanFound(h.store ?? "", h.keyword);
          if (!findAlive.current) return;
          if (r.found > 0) {
            setFound({ found: r.found, validated: r.validated, products: r.products });
            setFinding(false);
            setFindMsg(`${r.found} found`);
            onResearched();
            return;
          }
        } catch { /* keep polling */ }
      }
      if (findAlive.current) {
        setFinding(false);
        setFindMsg("still scanning — check back shortly");
      }
    })();
  }

  async function runPhotoDedup() {
    if (dedupBusy) return;
    setDedupBusy(true);
    setDedupErr(null);
    try {
      await api.skuPlanPhotoDedup(h.store ?? "", h.keyword);
      // Re-fetch so every card carries its dup-group stamp from the fresh run.
      const r = await api.skuPlanFound(h.store ?? "", h.keyword);
      setFound({ found: r.found, validated: r.validated, products: r.products });
    } catch (e) {
      setDedupErr(e instanceof Error ? e.message : String(e));
    } finally {
      setDedupBusy(false);
    }
  }

  const builds = h.segments.filter((r) => r.selected === "build");
  const holds = h.segments.filter((r) => r.selected !== "build");

  return (
    <Card className="overflow-hidden">
      {/* header */}
      <div className="flex flex-wrap items-center gap-3 border-b border-[var(--border)] px-4 py-3">
        <span className="text-base font-semibold">{h.keyword || "—"}</span>
        <span className="text-xs tabular-nums text-[var(--muted)]">{fmtSv(h.sv)} searches/mo</span>
        {h.volume_tier ? (
          <span
            className="inline-flex items-center rounded-md px-2 py-0.5 text-xs font-semibold"
            style={{
              color: TIER_COLOR[h.volume_tier] ?? "var(--muted)",
              background: `color-mix(in srgb, ${TIER_COLOR[h.volume_tier] ?? "var(--muted)"} 14%, transparent)`,
            }}
            title={`Volume tier — bigger keywords get more products (${h.products_per_build} to find here vs ${h.products_per_build_base} base)`}
          >
            {h.volume_tier_label}
          </span>
        ) : null}
        {h.capture_bucket ? (
          <span
            className="inline-flex items-center rounded-md px-2 py-0.5 text-xs font-semibold"
            style={{ color: bucketColor, background: `color-mix(in srgb, ${bucketColor} 14%, transparent)` }}
          >
            {h.capture_bucket}
          </span>
        ) : null}
        <span className="text-xs text-[var(--muted)]">
          {h.counts.build} to make · {h.counts.combine} folded into titles · {h.counts.supporting} copy only
          {h.counts.built ? ` · ${h.counts.built} done` : ""}
        </span>
        <span className="ml-auto flex items-center gap-2">
          <Pill>{h.store}</Pill>
          <button
            type="button"
            onClick={openFound}
            className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-1 text-xs font-semibold hover:bg-[var(--surface)]"
            title={finding ? "Finding products right now…" : findMsg ?? "See the real products we found for this keyword, and check them, from one panel"}
          >
            {finding ? (
              <>
                <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-[var(--muted)] border-t-transparent" />
                Finding…
              </>
            ) : findMsg ? (
              <span style={{ color: "var(--state-winner)" }}>View found · {findMsg}</span>
            ) : (
              "View found"
            )}
          </button>
          <button
            type="button"
            onClick={() => setConfirm(true)}
            disabled={finding}
            className="rounded-lg px-2.5 py-1 text-xs font-semibold disabled:opacity-60"
            style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
            title="Go find real products for every keyword in this plan, using the weighted source mix in 'Where products come from' (Google competitor first, then Marketplace / Amazon / Meta)"
          >
            {finding ? "Scanning…" : "Find products →"}
          </button>
          {promoted ? (
            <Link
              href="/listing-plan"
              className="rounded-lg px-2.5 py-1 text-xs font-semibold"
              style={{ color: "var(--state-live)", background: "color-mix(in srgb, var(--state-live) 16%, transparent)" }}
              title="This keyword is now a store category — ready for daily listing. Open the daily listing overview."
            >
              Ready — daily listing →
            </Link>
          ) : (
            <button
              type="button"
              onClick={() => setPromoteConfirm(true)}
              className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-1 text-xs font-semibold hover:bg-[var(--surface)]"
              title="Add this keyword to the store as a category, ready to build into listings — the same Promote step as the ranked ideas table."
            >
              Promote →
            </button>
          )}
        </span>
      </div>

      {/* anchor cluster */}
      <div className="border-b border-[var(--border)] bg-[var(--surface-2)] px-4 py-3">
        <div className="flex flex-wrap items-center gap-3">
          <span
            className="rounded px-1.5 py-0.5 text-[11px] font-bold"
            style={{ color: ROLE_META.anchor.color, background: `color-mix(in srgb, ${ROLE_META.anchor.color} 16%, transparent)` }}
            title={ROLE_META.anchor.hint}
          >
            MAIN
          </span>
          <span className="font-semibold">{h.anchor.term}</span>
          <span className="text-xs tabular-nums text-[var(--muted)]">
            find {h.anchor.budget} products · {h.anchor_share_pct}% of the effort
          </span>
          <span className="ml-auto">
            <QuotaBar quota={h.anchor.quota} />
          </span>
        </div>
        {h.anchor.combine_children.length ? (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--muted)]">
            <span className="font-semibold">Added into the product title:</span>
            {h.anchor.combine_children.map((t) => (
              <span key={t} className="rounded border border-[var(--border)] px-1.5 py-0.5">
                {t}
              </span>
            ))}
          </div>
        ) : null}
        <div className="mt-2 text-[11px] text-[var(--muted)]">
          Extra keywords cover {fmtSv(h.demand.tier2_captured)} of {fmtSv(h.demand.tier2_total)} monthly
          searches ({h.demand.coverage_pct_actual}%)
        </div>
      </div>

      {/* rows */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--border)] text-left text-[11px] uppercase tracking-wide text-[var(--muted)]">
              <th className="px-4 py-2 font-semibold">Keyword</th>
              <th className="px-4 py-2 text-right font-semibold">Searches/mo</th>
              <th className="px-4 py-2 font-semibold">Plan</th>
              <th className="px-4 py-2 font-semibold">Idea from</th>
              <th className="px-4 py-2 text-right font-semibold">Find</th>
              <th className="px-4 py-2 font-semibold">Where to find</th>
              <th className="px-4 py-2 font-semibold">Status</th>
            </tr>
          </thead>
          <tbody>
            {builds.map((r, i) => (
              <PlanRow key={`b-${r.term}-${i}`} r={r} />
            ))}
            {holds.map((r, i) => (
              <PlanRow key={`h-${r.term}-${i}`} r={r} />
            ))}
          </tbody>
        </table>
      </div>

      <ConfirmDialog
        open={confirm}
        title={`Find products — ${h.keyword}`}
        body={
          <span>
            We&apos;ll find real, live products that match{" "}
            <strong>{h.build_terms.length}</strong> keyword{h.build_terms.length === 1 ? "" : "s"} in this plan
            (the main keyword plus the extra ones you picked), using the weighted source mix in{" "}
            <strong>Where products come from</strong> — Google competitor first, then Marketplace, Amazon and
            Meta. This runs on its own in the background — you don&apos;t have to do anything while it works.
          </span>
        }
        confirmLabel="Find products"
        onConfirm={async () => {
          await api.skuPlanResearch(h.store ?? "", h.keyword);
          startFind(); // live progress on the row + auto-load the finds when they land
        }}
        onClose={() => setConfirm(false)}
        onDone={onResearched}
      />

      <ConfirmDialog
        open={validateConfirm}
        title={`Check found products — ${h.keyword}`}
        body={
          <span>
            We&apos;ll go through the products we found for{" "}
            <strong>{h.keyword}</strong> and run two quick checks on each one: skip anything your store
            already sells, and make sure we can actually get the product from a supplier. The results show up
            in the <strong>Sourcing Match</strong> tab. This runs on its own in the background.
          </span>
        }
        confirmLabel="Check products"
        onConfirm={async () => {
          await api.skuPlanFoundValidated(h.store ?? "", h.keyword);
        }}
        onClose={() => setValidateConfirm(false)}
        onDone={onResearched}
      />

      <ConfirmDialog
        open={promoteConfirm}
        title="Promote to a store category"
        confirmLabel="Promote"
        body={
          <span>
            Add <span className="font-semibold">{h.keyword}</span> to{" "}
            <span className="font-medium">{h.store}</span> as a live category. This moves it out of
            research into the store&apos;s <span className="font-medium">daily listing pipeline</span> —
            where products get found and drafted into listings. (You&apos;re already viewing it in the
            SKU Plan; Promote is what makes it a real, buildable category.)
          </span>
        }
        onConfirm={async () => {
          await api.promoteCandidate(h.store ?? "", h.keyword);
        }}
        onDone={() => {
          setPromoted(true);
          onResearched();
        }}
        onClose={() => setPromoteConfirm(false)}
      />

      <KeywordProductsModal
        open={foundOpen}
        keyword={h.keyword}
        store={h.store}
        sv={h.sv}
        found={foundLoading ? 0 : found?.found ?? 0}
        validated={foundLoading ? 0 : found?.validated ?? 0}
        products={found?.products ?? []}
        laneLabel="Found for this plan"
        onValidate={() => {
          setFoundOpen(false);
          setValidateConfirm(true);
        }}
        onPhotoDedup={runPhotoDedup}
        photoDedupBusy={dedupBusy}
        photoDedupError={dedupErr}
        onClose={() => setFoundOpen(false)}
      />
    </Card>
  );
}

function PlanRow({ r }: { r: SkuPlanRow }) {
  const role = ROLE_META[r.role];
  const dim = r.selected === "hold" ? "opacity-60" : "";
  return (
    <tr className={`border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)] ${dim}`}>
      <td className="px-4 py-2 font-medium">{r.term}</td>
      <td className="px-4 py-2 text-right tabular-nums">{fmtSv(r.sv)}</td>
      <td className="px-4 py-2">
        <span
          className="rounded px-1.5 py-0.5 text-[11px] font-semibold"
          style={{ color: role.color, background: `color-mix(in srgb, ${role.color} 14%, transparent)` }}
          title={role.hint}
        >
          {role.label}
        </span>
      </td>
      <td className="px-4 py-2 text-xs text-[var(--muted)]">
        {r.source ? SEGMENT_SOURCE_LABEL[r.source] ?? r.source : "—"}
      </td>
      <td className="px-4 py-2 text-right tabular-nums text-xs">{r.budget || "—"}</td>
      <td className="px-4 py-2">{r.quota ? <QuotaBar quota={r.quota} /> : <span className="text-xs text-[var(--muted)]">—</span>}</td>
      <td className="px-4 py-2">
        {r.in_catalog ? (
          <span className="text-xs font-semibold" style={{ color: "var(--state-live)" }}>
            ✓ done
          </span>
        ) : r.selected === "build" ? (
          <span className="text-xs font-semibold" style={{ color: "var(--state-winner)" }}>
            to make
          </span>
        ) : (
          <span className="text-xs text-[var(--muted)]">later</span>
        )}
      </td>
    </tr>
  );
}
