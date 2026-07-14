"use client";

import { useEffect, useRef, useState } from "react";
import {
  api,
  type SourcingMatchRow,
  type SourcingCandidate,
  type MatchLearning,
  type MatchVerdict,
  type MatchVerification,
  type SourceOfTruth,
  type CatalogScanRow,
  type CatalogVerdict,
  type CatalogAction,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { useStore } from "@/components/StoreProvider";
import { Card, Stat } from "@/components/ui";
import { PageHeader, Loading, ErrorState } from "@/components/PageState";

const VERDICT_COLOR: Record<MatchVerdict, string> = {
  IDENTICAL: "var(--state-winner)",
  UNCERTAIN: "var(--state-testing)",
  DIFFERENT: "var(--state-killed)",
};

// Plain-language label for the photo-match result shown to the operator.
const VERDICT_LABEL: Record<MatchVerdict, string> = {
  IDENTICAL: "Same product",
  UNCERTAIN: "Not sure",
  DIFFERENT: "Different product",
};

// How the verdict was produced. An "unverified" row is a stub with no vision/agent judgement on
// file — it can never set a build's source of truth (the backend forces it to "verify").
const VERIFICATION: Record<MatchVerification, { label: string; hint: string; color: string }> = {
  vision: {
    label: "✓ Checked by AI",
    hint: "We compared the product photos with AI to confirm it's the same item.",
    color: "var(--state-winner)",
  },
  agent: {
    label: "✓ Checked",
    hint: "The product photos were compared and confirmed to be the same item.",
    color: "var(--state-winner)",
  },
  unverified: {
    label: "⚠ Not checked yet",
    hint: "Nothing has confirmed this is the same item yet, so it can't be set as the chosen supplier until it's checked. Run the photo check to confirm.",
    color: "var(--state-killed)",
  },
  operator: {
    label: "✓ You reviewed it",
    hint: "You looked at the matches and confirmed (or corrected) which product is right — the most trusted result, and it helps the AI get better next time.",
    color: "var(--state-winner)",
  },
};

function VerificationBadge({ v }: { v: MatchVerification }) {
  const c = VERIFICATION[v];
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
      style={{ color: c.color, background: `color-mix(in srgb, ${c.color} 16%, transparent)` }}
      title={c.hint}
    >
      {c.label}
    </span>
  );
}

// Which source grounds the listing build. 1688-first precedence.
const SOURCE_OF_TRUTH: Record<SourceOfTruth, { label: string; hint: string; color: string }> = {
  "1688": {
    label: "Build from · supplier",
    hint: "We found a matching supplier — build the listing's photos, options and specs from there. The PRICE still comes from the retail price you researched, not the supplier's wholesale price (unless you found the product on a marketplace directly).",
    color: "var(--state-winner)",
  },
  researched: {
    label: "Build from · where you found it",
    hint: "No matching supplier found — build from where you originally found the product (Google / a competitor / AliExpress). The price comes from that source too.",
    color: "var(--state-testing)",
  },
  verify: {
    label: "Build from · take a look first",
    hint: "Not sure the supplier match is right — take a look before deciding where to build the listing from.",
    color: "var(--state-testing)",
  },
};

const CATALOG_VERDICT_COLOR: Record<CatalogVerdict, string> = {
  ALREADY_LISTED: "var(--state-killed)", // already sell it → skip sourcing
  NEW: "var(--state-winner)", // not in catalog → proceed to source
  UNCERTAIN: "var(--state-testing)",
};

// Plain-language label for the store-check result.
const CATALOG_VERDICT_LABEL: Record<CatalogVerdict, string> = {
  ALREADY_LISTED: "Already sell it",
  NEW: "Not in your store",
  UNCERTAIN: "Not sure",
};

// What to DO about the verdict. ALREADY_LISTED forks on how many copies already exist.
const CATALOG_ACTION: Record<CatalogAction, { label: string; hint: string; color: string }> = {
  SOURCE: { label: "Find a supplier", hint: "Your store doesn't sell this yet — go find a supplier for it.", color: "var(--state-winner)" },
  REVIEW: { label: "Take a look", hint: "Not sure if this is the same product — check it yourself before finding a supplier.", color: "var(--state-testing)" },
  ADD_VARIANT_OR_OPTIMIZE: {
    label: "Add another version or improve it",
    hint: "You already sell this, but not too many times — you can add ONE different version (different photo / price / title) to test, or improve the listing you already have.",
    color: "var(--state-testing)",
  },
  OPTIMIZE_EXISTING: {
    label: "Improve what you have",
    hint: "You already sell this enough times — don't add another. Lower the price or improve the main photo and title on the listing you already have.",
    color: "var(--state-killed)",
  },
};

// Stashed by the "→ Sourcing Match" link on a New-Products tile (sessionStorage handoff,
// avoids the Next 16 useSearchParams suspense dance for a one-shot cross-page payload).
type SourcingSeed = {
  title?: string;
  url?: string;
  image?: string;
  price?: string | number;
  competitor?: string;
};

function SeedBanner() {
  const [seed, setSeed] = useState<SourcingSeed | null>(null);

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("sourcing-seed");
      if (raw) setSeed(JSON.parse(raw) as SourcingSeed);
    } catch {
      /* malformed payload — ignore */
    }
  }, []);

  if (!seed) return null;

  const dismiss = () => {
    sessionStorage.removeItem("sourcing-seed");
    setSeed(null);
  };

  return (
    <Card className="mb-6 border-l-4 border-l-[var(--state-winner)] p-4">
      <div className="flex items-start gap-3">
        {seed.image ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={seed.image}
            alt=""
            className="h-16 w-16 flex-shrink-0 rounded object-cover"
          />
        ) : null}
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-bold uppercase tracking-wide text-[var(--state-winner)]">
            Seeded from New Products
          </div>
          <div className="mt-0.5 truncate text-sm font-semibold">
            {seed.title ?? "Untitled product"}
          </div>
          <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-[var(--muted)]">
            {seed.competitor ? <span>spotted on {seed.competitor}</span> : null}
            {seed.price ? <span>~{seed.price}</span> : null}
            {seed.url ? (
              <a
                href={seed.url}
                target="_blank"
                rel="noreferrer"
                className="text-[var(--accent)] hover:underline"
              >
                view source listing ↗
              </a>
            ) : null}
          </div>
          <p className="mt-2 text-xs text-[var(--muted)]">
            Run the store check below to make sure you don&apos;t already sell it, then find a
            supplier for it before adding it to the listing plan.
          </p>
        </div>
        <button
          onClick={dismiss}
          className="flex-shrink-0 rounded px-2 py-1 text-xs text-[var(--muted)] hover:bg-[var(--surface-2)]"
          aria-label="Dismiss seed"
        >
          ✕
        </button>
      </div>
    </Card>
  );
}

// The master ON/OFF for the whole 1688 sourcing workflow. OFF = listing builds never ground on a
// matched 1688 factory; they build straight from their researched source (Google / competitor /
// AliExpress ref). This is the single switch the operator asked for. Reversible at any time.
function Sourcing1688Toggle() {
  const { data, reload } = useApi(() => api.sourcing1688Enabled());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const on = data?.enabled ?? true;

  async function toggle() {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      await api.setSourcing1688Enabled(!on);
      reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Toggle failed");
    } finally {
      setBusy(false);
    }
  }

  const color = on ? "var(--state-winner)" : "var(--muted)";
  return (
    <div
      className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-3"
      style={{
        borderColor: on ? "color-mix(in srgb, var(--state-winner) 35%, var(--border))" : "var(--border)",
        background: on ? "color-mix(in srgb, var(--state-winner) 6%, transparent)" : "var(--surface-2)",
      }}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full" style={{ background: color }} />
          <span className="text-sm font-semibold">Match products to a supplier</span>
          <span className="rounded-md px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide"
            style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}>
            {on ? "ON" : "OFF"}
          </span>
        </div>
        <p className="mt-1 text-[11px] leading-snug text-[var(--muted)]">
          {on
            ? "We look for a matching supplier first; when one has the product, the listing is built from the supplier's photos, options and specs. The PRICE still comes from the retail price you researched — never the supplier's wholesale price (unless you found the product on a marketplace directly)."
            : "Turned off — listings are built straight from where you found the product (Google / a competitor / AliExpress). No supplier is matched in."}
        </p>
        {err ? <p className="mt-1 text-[11px]" style={{ color: "var(--state-killed)" }}>{err}</p> : null}
      </div>
      <button
        type="button"
        onClick={toggle}
        disabled={busy}
        role="switch"
        aria-checked={on}
        className="relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50"
        style={{ background: on ? "var(--state-winner)" : "var(--border)" }}
      >
        <span
          className="inline-block h-5 w-5 transform rounded-full bg-white transition-transform"
          style={{ transform: on ? "translateX(22px)" : "translateX(2px)" }}
        />
      </button>
    </div>
  );
}

// Optional ON/OFF for Step 0 (the store dedup-check). OFF = skip it and go straight to matching a
// supplier. Client-side + persisted (localStorage) — a display preference, not a workflow gate.
function StoreCheckToggle({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  const color = on ? "var(--state-winner)" : "var(--muted)";
  return (
    <div
      className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border px-4 py-2.5"
      style={{
        borderColor: on ? "color-mix(in srgb, var(--state-winner) 30%, var(--border))" : "var(--border)",
        background: on ? "color-mix(in srgb, var(--state-winner) 5%, transparent)" : "var(--surface-2)",
      }}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full" style={{ background: color }} />
          <span className="text-sm font-semibold">Store check</span>
          <span className="rounded-md px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide"
            style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}>
            {on ? "ON" : "OFF"}
          </span>
        </div>
        <p className="mt-1 text-[11px] leading-snug text-[var(--muted)]">
          {on
            ? "Before matching a supplier, we check your store doesn't already sell the product (skip the ones you have)."
            : "Turned off — the store dedup-check is skipped; you go straight to matching a supplier."}
        </p>
      </div>
      <button
        type="button"
        onClick={onToggle}
        role="switch"
        aria-checked={on}
        className="relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors"
        style={{ background: on ? "var(--state-winner)" : "var(--border)" }}
      >
        <span
          className="inline-block h-5 w-5 transform rounded-full bg-white transition-transform"
          style={{ transform: on ? "translateX(22px)" : "translateX(2px)" }}
        />
      </button>
    </div>
  );
}

export default function SourcingMatchPage() {
  const { store } = useStore();
  // Use the ACTIVE store as-is — never silently fall back to a hardcoded one, or a job
  // could be created against the wrong business. The store picker (sidebar) auto-selects
  // the first registered store on load, so this is only "" for a blink before hydration.
  const activeStore = store;
  // Scope both the store-check and the supplier-match rows to the active business, and
  // refetch whenever the operator switches stores — two businesses never see each other's rows.
  const catalog = useApi(() => api.catalogScan(activeStore || undefined), [activeStore]);
  const { data, error, loading, reload } = useApi(
    () => api.sourcingMatch(activeStore || undefined),
    [activeStore],
  );
  const [selected, setSelected] = useState<SourcingMatchRow | null>(null);
  // Step 0 (the store-check) is optional — some operators don't want the dedup gate. Client-side,
  // persisted, default ON. OFF = skip it entirely and go straight to supplier matching.
  const [catalogOn, setCatalogOn] = useState<boolean>(() =>
    typeof window !== "undefined" ? localStorage.getItem("sourcing_store_check") !== "0" : true,
  );
  useEffect(() => {
    try { localStorage.setItem("sourcing_store_check", catalogOn ? "1" : "0"); } catch { /* private mode */ }
  }, [catalogOn]);

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Sourcing Match"
        subtitle="The last check before a product goes on the listing calendar. First we make sure your store doesn't already sell it, then we look for a matching supplier. If a supplier has it, we build the listing from there; if not, we build from where you originally found the product (Google / a competitor)."
      />

      <Sourcing1688Toggle />

      <SeedBanner />

      {/* ── STEP 0 — catalog dedup (optional), runs BEFORE the supplier gate below ── */}
      <StoreCheckToggle on={catalogOn} onToggle={() => setCatalogOn((v) => !v)} />
      {catalogOn ? (
        <CatalogCheckSection
          data={catalog.data}
          loading={catalog.loading}
          error={catalog.error}
        />
      ) : null}

      <div className="mt-8 mb-3 flex items-center gap-2">
        <span className="rounded-full bg-[var(--surface-2)] px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide text-[var(--muted)]">
          Step 1
        </span>
        <h2 className="text-sm font-semibold">Match a supplier&apos;s listing</h2>
      </div>

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <Stat label="Matched" value={data.totals.matched} />
            <Stat label="Same product" value={data.totals.IDENTICAL} hint="a supplier has the exact item — ready to source" />
            <Stat label="Not sure" value={data.totals.UNCERTAIN} hint="needs a look" />
            <Stat label="Different" value={data.totals.DIFFERENT} hint="not the same item — look for another supplier" />
            <Stat label="Not checked" value={data.totals.unverified} hint="no photo check done yet" />
          </div>
          {data.totals.unverified > 0 ? (
            <p className="mt-2 rounded-lg border border-[var(--state-killed)] bg-[color-mix(in_srgb,var(--state-killed)_8%,transparent)] px-3 py-2 text-[11px] leading-snug text-[var(--muted)]">
              <span className="font-semibold text-[var(--state-killed)]">
                {data.totals.unverified} match{data.totals.unverified === 1 ? "" : "es"} not checked yet.
              </span>{" "}
              No photo check has been done on these, so they aren&apos;t confirmed as the same product and
              can&apos;t be used to build a listing yet — run the photo check below before listing.
            </p>
          ) : null}

          <LearningBar learning={data.learning} />

          {data.available ? (
            <>
              <p className="mt-5 text-[11px] text-[var(--muted)]">
                Click a match to compare the product you found against the supplier&apos;s product side by side.
              </p>
              <div className="mt-2 grid gap-2.5 sm:grid-cols-3 lg:grid-cols-4">
                {data.results.map((r, i) => (
                  <MatchCard key={`${r.offer_id}-${i}`} r={r} onOpen={() => setSelected(r)} />
                ))}
              </div>
            </>
          ) : (
            <Card className="mt-6 p-6">
              <p className="text-sm text-[var(--muted)]">{data.note}</p>
              <div className="mt-4 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                Run these steps yourself — results appear here
              </div>
              <ol className="mt-2 flex flex-col gap-2">
                <CmdStep n={1} label="Find possible suppliers" cmd={data.commands.find} />
                <CmdStep n={2} label="Get full photos, options &amp; how many sold" cmd={data.commands.enrich} />
                <CmdStep n={3} label="Check the photos to confirm the match" cmd={data.commands.judge} />
              </ol>
            </Card>
          )}
        </>
      ) : null}

      {selected ? (
        <MatchModal
          r={selected}
          store={activeStore}
          onClose={() => setSelected(null)}
          onEnriched={reload}
        />
      ) : null}
    </div>
  );
}

// Step 0 — scan the store's own catalog so we never re-source a product we already sell.
// Identity is judged on the product IMAGE (background-invariant), not the title.
function CatalogCheckSection({
  data,
  loading,
  error,
}: {
  data: import("@/lib/api").CatalogScan | null;
  loading: boolean;
  error: string | null;
}) {
  return (
    <>
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-semibold">Store check — do you already sell it?</h2>
      </div>

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Checked" value={data.totals.checked} />
            <Stat label="Already selling" value={data.totals.already_listed} hint="skip — you already have it" />
            <Stat label="New" value={data.totals.new} hint="safe to find a supplier for" />
            <Stat label="Not sure" value={data.totals.uncertain} hint="take a look" />
          </div>

          {data.available ? (
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {data.results.map((r, i) => (
                <CatalogMatchCard key={`${r.subject}-${i}`} r={r} />
              ))}
            </div>
          ) : (
            <Card className="mt-4 p-6">
              <p className="text-sm text-[var(--muted)]">{data.note}</p>
              <div className="mt-4 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                Run the check yourself — results appear here
              </div>
              <ol className="mt-2 flex flex-col gap-2">
                <CmdStep n={1} label="List everything your store already sells" cmd={data.commands.index} />
                <CmdStep n={2} label="Compare the products you found against what you already sell" cmd={data.commands.check} />
              </ol>
              {data.indexes.length > 0 ? (
                <p className="mt-4 text-[11px] text-[var(--muted)]">
                  Already scanned:{" "}
                  {data.indexes
                    .map((ix) => `${ix.store ?? "?"} (${ix.count} products)`)
                    .join(" · ")}
                </p>
              ) : null}
            </Card>
          )}
        </>
      ) : null}
    </>
  );
}

function CatalogMatchCard({ r }: { r: CatalogScanRow }) {
  const color = CATALOG_VERDICT_COLOR[r.verdict] ?? "var(--muted)";
  const already = r.verdict === "ALREADY_LISTED";
  return (
    <Card className="flex flex-col gap-1.5 p-3">
      <div className="line-clamp-2 text-sm font-medium leading-snug">{r.subject ?? "—"}</div>
      <div className="flex flex-wrap items-center gap-2">
        <span
          className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase"
          style={{ color, background: `color-mix(in srgb, ${color} 16%, transparent)` }}
        >
          {CATALOG_VERDICT_LABEL[r.verdict] ?? r.verdict.replace("_", " ")}
        </span>
        {r.confidence != null ? (
          <span className="text-[11px] tabular-nums text-[var(--muted)]">
            {Math.round(r.confidence * 100)}% sure
          </span>
        ) : null}
      </div>
      {already && r.matched_handle ? (
        <div className="mt-1 rounded-lg bg-[var(--surface-2)] px-2.5 py-1.5 text-[11px]">
          <span className="text-[var(--muted)]">Matches: </span>
          {r.store_url ? (
            <a href={r.store_url} target="_blank" rel="noreferrer" className="font-medium hover:text-[var(--accent)]">
              {r.matched_title ?? r.matched_handle}
            </a>
          ) : (
            <span className="font-medium">{r.matched_title ?? r.matched_handle}</span>
          )}
          {r.store_price != null ? (
            <span className="ml-1.5 font-semibold tabular-nums text-[var(--text)]">
              {r.store_currency === "USD" ? "$" : ""}
              {r.store_price}
              {r.store_currency && r.store_currency !== "USD" ? ` ${r.store_currency}` : ""}
            </span>
          ) : null}
        </div>
      ) : null}
      {r.recommended_action ? (
        <CatalogActionRow
          action={r.recommended_action}
          nAlready={r.n_already_listed}
          cap={r.cap}
        />
      ) : null}
      {r.n_checked != null ? (
        <div className="mt-auto text-[11px] tabular-nums text-[var(--muted)]">
          {r.n_checked} catalog match{r.n_checked === 1 ? "" : "es"} checked
        </div>
      ) : null}
    </Card>
  );
}

// The decision the operator actually wants: list a (differentiated) 2nd copy, or optimize the
// existing listing instead. Driven by how many copies of the same product already exist vs the cap.
function CatalogActionRow({
  action,
  nAlready,
  cap,
}: {
  action: CatalogAction;
  nAlready: number | null;
  cap: number | null;
}) {
  const a = CATALOG_ACTION[action];
  return (
    <div className="mt-1 flex flex-col gap-1 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-1.5">
      <div className="flex items-center justify-between gap-2">
        <span
          className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
          style={{ color: a.color, background: `color-mix(in srgb, ${a.color} 16%, transparent)` }}
        >
          {a.label}
        </span>
        {nAlready != null && cap != null ? (
          <span className="text-[10px] tabular-nums text-[var(--muted)]">
            {nAlready}/{cap} copies used
          </span>
        ) : null}
      </div>
      <p className="text-[11px] leading-snug text-[var(--muted)]">{a.hint}</p>
    </div>
  );
}

// A step in an operator-run flow. We deliberately DON'T render the raw command — it's dev gore.
// The label is plain English; a small Copy button hands the underlying action to the operator's
// workspace session without cluttering the UI with script paths.
function CmdStep({ n, label, cmd }: { n: number; label: string; cmd: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <li className="flex items-center gap-3">
      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-[var(--surface-2)] text-[11px] font-bold tabular-nums">
        {n}
      </span>
      <div className="min-w-0 flex-1 text-xs font-semibold">{label}</div>
      <button
        type="button"
        onClick={() => {
          navigator.clipboard?.writeText(cmd);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}
        className="shrink-0 rounded-md border border-[var(--border)] px-2 py-1 text-[11px] font-semibold text-[var(--muted)] transition-colors hover:text-[var(--text)]"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </li>
  );
}

// The learning loop's running scoreboard — how often the operator judged the AI's 1688 find
// good. Only shows once at least one match has been rated.
function LearningBar({ learning }: { learning: MatchLearning }) {
  if (!learning || learning.reviewed === 0) {
    return (
      <p className="mt-3 rounded-lg border border-dashed border-[var(--border)] px-3 py-2 text-[11px] leading-snug text-[var(--muted)]">
        <span className="font-semibold text-[var(--text)]">How the AI is doing · no ratings yet.</span>{" "}
        Open a match, click <span className="font-medium">See all the products it checked</span> to review
        them, then rate whether the AI&apos;s pick was right — that builds its score here.
      </p>
    );
  }
  const pct = learning.accuracy != null ? Math.round(learning.accuracy * 100) : null;
  const color =
    pct == null ? "var(--muted)" : pct >= 80 ? "var(--state-winner)" : pct >= 50 ? "var(--state-testing)" : "var(--state-killed)";
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2">
      <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
        How the AI is doing
      </span>
      <span className="text-[11px]">
        <span className="font-bold tabular-nums" style={{ color }}>
          {pct}% right
        </span>{" "}
        <span className="text-[var(--muted)]">of the AI&apos;s matches were right</span>
      </span>
      <span className="text-[11px] tabular-nums text-[var(--muted)]">
        {learning.good} good · {learning.bad} bad · {learning.reviewed} rated
      </span>
      <span
        className="ml-auto rounded-full px-2 py-0.5 text-[10px] font-semibold"
        style={{ color: "var(--state-winner)", background: "color-mix(in srgb, var(--state-winner) 14%, transparent)" }}
        title="Every time you rate a match, the AI learns from it and gets better at the next one."
      >
        ↻ learning from your ratings
      </span>
    </div>
  );
}

// The "N judged" grid: every candidate the VLM evaluated, as image + url + verdict. The AI's
// chosen offer is flagged; in bad-find mode each tile is clickable to teach the correct one.
function CandidatesPanel({
  candidates,
  picking,
  correctOfferId,
  onPickCorrect,
}: {
  candidates: SourcingCandidate[];
  picking: boolean;
  correctOfferId: string | null;
  onPickCorrect: (offerId: string) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
      {candidates.map((c, i) => {
        const color = VERDICT_COLOR[c.verdict] ?? "var(--muted)";
        const isCorrect = correctOfferId != null && c.offer_id === correctOfferId;
        const borderColor = isCorrect
          ? "var(--state-winner)"
          : c.picked
            ? "var(--accent)"
            : "var(--border)";
        return (
          <div
            key={`${c.offer_id}-${i}`}
            className="flex flex-col overflow-hidden rounded-lg border bg-[var(--surface)]"
            style={{ borderColor }}
          >
            <div className="relative aspect-square bg-[var(--surface-2)]">
              {c.image ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={c.image} alt={c.title ?? c.offer_id ?? "candidate"} className="h-full w-full object-cover" loading="lazy" />
              ) : (
                <div className="flex h-full w-full items-center justify-center text-[10px] text-[var(--muted)]">
                  no image
                </div>
              )}
              {c.picked ? (
                <span className="absolute left-1 top-1 rounded bg-[var(--accent)] px-1 py-0.5 text-[9px] font-bold uppercase tracking-wide text-white">
                  AI pick
                </span>
              ) : null}
              <span
                className="absolute right-1 top-1 rounded px-1 py-0.5 text-[9px] font-bold uppercase"
                style={{ color, background: "color-mix(in srgb, black 55%, transparent)" }}
              >
                {VERDICT_LABEL[c.verdict] ?? c.verdict}
              </span>
            </div>
            <div className="flex flex-col gap-1 p-1.5">
              {c.title ? (
                <p className="line-clamp-2 text-[11px] leading-snug text-[var(--text)]" title={c.title}>
                  {c.title}
                </p>
              ) : null}
              {c.matching_variant ? (
                <span
                  className="w-fit max-w-full truncate rounded px-1 py-0.5 text-[10px] text-[var(--muted)]"
                  style={{ background: "var(--surface-2)", border: "1px solid var(--border)" }}
                >
                  Variant: {c.matching_variant}
                </span>
              ) : null}
              {c.differences && c.differences.length > 0 ? (
                <ul className="flex flex-col gap-0.5">
                  {c.differences.map((d, di) => (
                    <li key={di} className="flex gap-1 text-[10px] leading-snug text-[var(--muted)]">
                      <span aria-hidden>⚠</span>
                      <span className="min-w-0">{d}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
              <div className="flex items-center justify-between gap-1 text-[10px]">
                <span className="truncate font-mono text-[var(--muted)]">{c.offer_id ?? "—"}</span>
                {c.confidence != null ? (
                  <span className="tabular-nums text-[var(--muted)]">{Math.round(c.confidence * 100)}%</span>
                ) : null}
              </div>
              <div className="flex items-center gap-2">
                {c.url ? (
                  <a
                    href={c.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[10px] font-medium text-[var(--accent)] hover:underline"
                  >
                    open ↗
                  </a>
                ) : null}
                {c.sold != null ? (
                  <span className="text-[10px] tabular-nums text-[var(--muted)]">sold {c.sold}</span>
                ) : null}
              </div>
              {picking ? (
                <button
                  onClick={() => onPickCorrect(c.offer_id ?? "")}
                  className="mt-0.5 rounded border px-1.5 py-1 text-[10px] font-semibold"
                  style={{
                    borderColor: isCorrect ? "var(--state-winner)" : "var(--border)",
                    background: isCorrect
                      ? "color-mix(in srgb, var(--state-winner) 16%, transparent)"
                      : "var(--surface-2)",
                  }}
                >
                  {isCorrect ? "✓ correct one" : "mark correct"}
                </button>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// Small, clean tile — the two thumbnails (researched vs 1688) sit side by side so the
// comparison is legible at a glance; click opens the full side-by-side window.
function MatchCard({ r, onOpen }: { r: SourcingMatchRow; onOpen: () => void }) {
  const color = VERDICT_COLOR[r.verdict] ?? "var(--muted)";
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group flex flex-col overflow-hidden rounded-xl border bg-[var(--surface)] text-left transition hover:border-[var(--accent)] hover:shadow-sm"
      style={{ borderColor: r.verified ? "var(--border)" : "var(--state-killed)" }}
    >
      <div className="flex">
        <Thumb src={r.research?.image} label="You found" />
        <div className="w-px bg-[var(--border)]" />
        <Thumb src={r.image} label="Supplier" />
      </div>
      <div className="flex flex-col gap-1 p-2.5">
        <div className="line-clamp-1 text-xs font-medium leading-snug">{r.subject ?? r.offer_id ?? "—"}</div>
        {!r.verified ? (
          <div>
            <VerificationBadge v={r.verification} />
          </div>
        ) : null}
        <div className="flex items-center gap-1.5">
          <span
            className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase"
            style={{ color, background: `color-mix(in srgb, ${color} 16%, transparent)` }}
          >
            {VERDICT_LABEL[r.verdict] ?? r.verdict}
          </span>
          {r.confidence != null ? (
            <span className="text-[10px] tabular-nums text-[var(--muted)]">
              {Math.round(r.confidence * 100)}%
            </span>
          ) : null}
          <span className="ml-auto text-[10px] text-[var(--muted)] group-hover:text-[var(--accent)]">
            compare →
          </span>
        </div>
      </div>
    </button>
  );
}

function Thumb({ src, label }: { src?: string | null; label: string }) {
  return (
    <div className="relative aspect-square w-1/2 bg-[var(--surface-2)]">
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt={label} className="h-full w-full object-cover" loading="lazy" />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-[10px] text-[var(--muted)]">
          no image
        </div>
      )}
      <span className="absolute left-1 top-1 rounded bg-black/55 px-1 py-0.5 text-[9px] font-bold uppercase tracking-wide text-white">
        {label}
      </span>
    </div>
  );
}

// The window the operator asked for: LEFT = the researched product (what Product Research
// found), RIGHT = the 1688 finding (the source-of-truth candidate) — so differences are visible.
function MatchModal({
  r,
  store,
  onClose,
  onEnriched,
}: {
  r: SourcingMatchRow;
  store: string;
  onClose: () => void;
  onEnriched: () => void;
}) {
  const color = VERDICT_COLOR[r.verdict] ?? "var(--muted)";
  const res = r.research;

  // Run buttons: create the (manual) sourcing jobs that earn a real verdict / pull specs.
  // These need AdsPower + a vision key + the TMAPI key, so the app records the exact command
  // (needs-operator) the operator runs in their Claude Code — surfaced inline to copy.
  const [busy, setBusy] = useState<null | "china-verify" | "china-enrich">(null);
  const [command, setCommand] = useState<string | null>(null);
  const [jobErr, setJobErr] = useState<string | null>(null);
  const [ranInApp, setRanInApp] = useState<string | null>(null);

  // ── Learning loop: rate whether the AI's 1688 find was a good match ──────────
  const [showCands, setShowCands] = useState(false);
  // Full-window gallery of ALL judged candidate image-products — the operator opens this to
  // eyeball every offer the VLM evaluated (bigger tiles than the inline grid) and confirm the
  // pick was good. Same picking/teach affordance as the inline panel.
  const [candWindow, setCandWindow] = useState(false);
  const candsRef = useRef<HTMLDivElement>(null);
  // Open the "N judged" grid AND bring it into view — wired to the clickable
  // "Candidates: N judged" row so the operator can double-check the find in one click.
  const openCandidates = () => {
    setShowCands(true);
    requestAnimationFrame(() =>
      candsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }),
    );
  };
  const [fbVerdict, setFbVerdict] = useState<"good" | "bad" | null>(r.feedback?.verdict ?? null);
  const [fbCorrect, setFbCorrect] = useState<string | null>(r.feedback?.correct_offer_id ?? null);
  const [fbBadMode, setFbBadMode] = useState(false);
  const [fbSaving, setFbSaving] = useState(false);
  const [fbErr, setFbErr] = useState<string | null>(null);
  const saveFeedback = async (verdict: "good" | "bad", correctId: string | null) => {
    setFbSaving(true);
    setFbErr(null);
    try {
      await api.sourcingFeedback({
        key: r.key,
        verdict,
        correct_offer_id: correctId,
        ai_verdict: r.verdict,
        ai_offer_id: r.offer_id,
        subject: r.subject,
      });
      setFbVerdict(verdict);
      if (verdict === "good") setFbBadMode(false);
      onEnriched(); // refresh the list → learning accuracy + this row's saved feedback update
    } catch (e) {
      setFbErr(e instanceof Error ? e.message : "Failed to save feedback");
    } finally {
      setFbSaving(false);
    }
  };
  // One-click "Pick this instead": choose a different candidate as THIS match's 1688 source
  // of truth. The backend swaps it in (offer_id/url/image/price/variants/specs all re-point),
  // so the listing builds from the chosen product. Refreshes the row to reflect the new pick.
  const [picking, setPicking] = useState<string | null>(null);
  const pickInstead = async (offerId: string | null | undefined) => {
    const oid = (offerId ?? "").toString().trim();
    if (!oid || picking) return;
    setPicking(oid);
    setFbErr(null);
    try {
      await api.sourcingFeedback({
        key: r.key,
        verdict: "bad",
        correct_offer_id: oid,
        ai_verdict: r.verdict,
        ai_offer_id: r.offer_id,
        subject: r.subject,
      });
      setFbVerdict("bad");
      setFbCorrect(oid);
      setFbBadMode(false);
      onEnriched(); // re-point the row → modal reflects the chosen product as the source
    } catch (e) {
      setFbErr(e instanceof Error ? e.message : "Failed to pick this candidate");
    } finally {
      setPicking(null);
    }
  };
  const runJob = async (spec: "china-verify" | "china-enrich") => {
    if (!store) {
      setJobErr("Pick a store first (top-left) so this runs against the right business.");
      return;
    }
    setBusy(spec);
    setJobErr(null);
    setCommand(null);
    setRanInApp(null);
    try {
      const job = await api.createJob(spec, store, {
        subject: r.subject ?? undefined,
        offer_id: r.offer_id ?? undefined,
        source: r.source ?? undefined,
      });
      // Manual / needs-operator → the backend hands back a copy-paste command.
      if (job.command) {
        setCommand(job.command);
        return;
      }
      // auto-local (e.g. china-enrich when the TMAPI key is present) → it runs IN the
      // app on a background thread. Poll the job to completion, then refresh so the
      // freshly-pulled 1688 variants/specs surface in this modal.
      let cur = job;
      for (let i = 0; i < 60 && (cur.status === "queued" || cur.status === "running"); i++) {
        await new Promise((res) => setTimeout(res, 2000));
        cur = await api.job(job.id);
      }
      if (cur.status === "done") {
        setRanInApp("Done — pulled the supplier's options and specs. Refreshing…");
        onEnriched();
      } else if (cur.status === "needs-operator") {
        setCommand(cur.command ?? null);
      } else if (cur.status === "failed") {
        setJobErr(cur.detail || cur.output || "Job failed");
      } else {
        setJobErr("Job still running — check Activity, then refresh.");
      }
    } catch (e) {
      setJobErr(e instanceof Error ? e.message : "Failed to create job");
    } finally {
      setBusy(null);
    }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-2xl border border-[var(--border)] bg-[var(--surface)] shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-start justify-between gap-3 border-b border-[var(--border)] p-4">
          <div className="min-w-0">
            <div className="text-sm font-semibold">{r.subject ?? r.offer_id ?? "Match"}</div>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <span
                className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase"
                style={{ color, background: `color-mix(in srgb, ${color} 16%, transparent)` }}
              >
                {VERDICT_LABEL[r.verdict] ?? r.verdict}
              </span>
              <VerificationBadge v={r.verification} />
              {r.operator_reviewed === "corrected" ? (
                <span
                  className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase"
                  style={{ color: "var(--state-winner)", background: "color-mix(in srgb, var(--state-winner) 16%, transparent)" }}
                  title={`Operator corrected the AI pick${r.corrected_from ? ` (was offer ${r.corrected_from})` : ""}.`}
                >
                  ✎ corrected{r.corrected_from ? ` from ${r.corrected_from}` : ""}
                </span>
              ) : null}
              {r.confidence != null ? (
                <span className="text-[11px] tabular-nums text-[var(--muted)]">
                  {Math.round(r.confidence * 100)}% sure
                </span>
              ) : null}
              {/* compact good/bad-find feedback — trains the matcher */}
              <span className="ml-1 flex items-center gap-1 border-l border-[var(--border)] pl-2">
                <button
                  onClick={() => saveFeedback("good", null)}
                  disabled={fbSaving}
                  title="Good match — helps the AI get better"
                  className="rounded border px-1.5 py-0.5 text-[11px] font-semibold disabled:opacity-50"
                  style={{
                    borderColor: fbVerdict === "good" ? "var(--state-winner)" : "var(--border)",
                    background:
                      fbVerdict === "good"
                        ? "color-mix(in srgb, var(--state-winner) 16%, transparent)"
                        : "transparent",
                  }}
                >
                  👍
                </button>
                <button
                  onClick={() => {
                    setFbBadMode(true);
                    setShowCands(true);
                    requestAnimationFrame(() =>
                      candsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }),
                    );
                  }}
                  disabled={fbSaving}
                  title="Wrong match — then click the correct product below to show the right answer"
                  className="rounded border px-1.5 py-0.5 text-[11px] font-semibold disabled:opacity-50"
                  style={{
                    borderColor: fbVerdict === "bad" ? "var(--state-killed)" : "var(--border)",
                    background:
                      fbVerdict === "bad"
                        ? "color-mix(in srgb, var(--state-killed) 16%, transparent)"
                        : "transparent",
                  }}
                >
                  👎
                </button>
                {fbBadMode ? (
                  <button
                    onClick={() => saveFeedback("bad", fbCorrect)}
                    disabled={fbSaving}
                    title={fbCorrect ? `Correct candidate = ${fbCorrect}` : "Pick the correct candidate below first (optional), then save"}
                    className="rounded border border-[var(--state-killed)] bg-[color-mix(in_srgb,var(--state-killed)_12%,transparent)] px-1.5 py-0.5 text-[10px] font-semibold disabled:opacity-50"
                  >
                    {fbSaving ? "Saving…" : fbCorrect ? `Save (${fbCorrect})` : "Save bad"}
                  </button>
                ) : null}
              </span>
              {/* open every judged 1688 image-search candidate in a full window for manual review */}
              {(r.candidates.length > 0 || (r.n_candidates ?? 0) > 0) ? (
                <button
                  onClick={() => setCandWindow(true)}
                  title="Open a window with every supplier product the AI looked at — review them all and decide which is the right match"
                  className="rounded-md border border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] px-3 py-1.5 text-[14px] font-bold hover:bg-[color-mix(in_srgb,var(--accent)_22%,transparent)]"
                >
                  ⛶ Open all {r.candidates.length || r.n_candidates}
                </button>
              ) : null}
              {fbErr ? (
                <span className="text-[10px] text-[var(--state-killed)]">{fbErr}</span>
              ) : null}
            </div>
          </div>
          <button
            onClick={onClose}
            className="flex-shrink-0 rounded px-2 py-1 text-sm text-[var(--muted)] hover:bg-[var(--surface-2)]"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {/* side-by-side */}
        <div className="grid gap-px bg-[var(--border)] sm:grid-cols-2">
          {/* LEFT — researched */}
          <CompareColumn
            tag="Product you found"
            tagColor="var(--state-testing)"
            image={res?.image}
            title={res?.title}
            url={res?.url}
            urlLabel="View the listing you found ↗"
            variants={res?.variants}
            specs={res?.specs}
            empty="No found product saved for this match."
          />
          {/* RIGHT — supplier match */}
          <CompareColumn
            tag="Supplier match"
            tagColor="var(--state-winner)"
            image={r.image}
            title={r.subject}
            url={r.url}
            urlLabel="Open supplier listing ↗"
            variants={r.variants}
            specs={r.specs}
            empty="No supplier match yet."
          />
        </div>

        {/* ── inline judged-candidate strip (used when picking the correct one in bad mode) ── */}
        <div ref={candsRef} className="flex flex-col gap-3 border-t border-[var(--border)] p-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold">Products the AI checked</span>
            {r.candidates.length > 0 ? (
              <button
                onClick={() => setShowCands((v) => !v)}
                className="rounded-full bg-[var(--surface-2)] px-2 py-0.5 text-[11px] font-medium hover:bg-[var(--surface)]"
              >
                {showCands ? "Hide inline ▴" : `Show inline (${r.candidates.length}) ▾`}
              </button>
            ) : (
              <span className="text-[11px] text-[var(--muted)]">
                {r.n_candidates
                  ? `${r.n_candidates} checked — use “⛶ Open all” at the top to look at each one (photo + link)`
                  : "none saved yet"}
              </span>
            )}
            <span className="text-[11px] text-[var(--muted)]">
              Mark it good or wrong, and open all the checked products from the top.
            </span>
          </div>

          {showCands && r.candidates.length > 0 ? (
            <CandidatesPanel
              candidates={r.candidates}
              picking={fbBadMode}
              correctOfferId={fbCorrect}
              onPickCorrect={(oid) => setFbCorrect(oid)}
            />
          ) : null}
        </div>

        {/* footer — verdict reasoning + source of truth */}
        <div className="flex flex-col gap-3 border-t border-[var(--border)] p-4">
          {!r.verified ? (
            <div className="rounded-lg border border-[var(--state-killed)] bg-[color-mix(in_srgb,var(--state-killed)_8%,transparent)] px-3 py-2 text-[11px] leading-snug text-[var(--muted)]">
              <span className="font-semibold text-[var(--state-killed)]">⚠ Not checked yet. </span>
              No photo check has been done on this match — the result above was just typed in, not
              confirmed. It won&apos;t be used to build a listing until it&apos;s checked. Run the photo
              check below to confirm before listing.
            </div>
          ) : null}
          {(r.variants?.length ?? 0) === 0 &&
          (r.specs?.length ?? 0) === 0 &&
          (res?.variants?.length ?? 0) === 0 &&
          (res?.specs?.length ?? 0) === 0 ? (
            <div className="rounded-lg border border-dashed border-[var(--border)] px-3 py-2 text-[11px] leading-snug text-[var(--muted)]">
              No options or specs yet — run <span className="font-medium text-[var(--text)]">Get full
              details</span> below to pull this supplier&apos;s options, specs, photos and how many have
              sold.
            </div>
          ) : null}

          {/* RUN buttons — turn the "go run a script" note into one click. These create the
              hybrid sourcing jobs (manual: app records the exact command the operator runs). */}
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => runJob("china-verify")}
              disabled={busy !== null}
              className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-1.5 text-[11px] font-semibold hover:bg-[var(--surface)] disabled:opacity-50"
            >
              {busy === "china-verify" ? "Creating…" : "✓ Check the match by photo"}
            </button>
            <button
              onClick={() => runJob("china-enrich")}
              disabled={busy !== null}
              className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-1.5 text-[11px] font-semibold hover:bg-[var(--surface)] disabled:opacity-50"
            >
              {busy === "china-enrich" ? "Creating…" : "⌗ Get full details (options + specs)"}
            </button>
          </div>
          {jobErr ? (
            <div className="rounded-lg border border-[var(--state-killed)] px-3 py-2 text-[11px] text-[var(--state-killed)]">
              {jobErr}
            </div>
          ) : null}
          {ranInApp ? (
            <div className="rounded-lg border border-[var(--state-winner)] bg-[color-mix(in_srgb,var(--state-winner)_8%,transparent)] px-3 py-2 text-[11px] text-[var(--state-winner)]">
              {ranInApp}
            </div>
          ) : null}
          {command ? (
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-3">
              <span className="text-[11px] leading-snug text-[var(--muted)]">
                This step runs in your workspace session — copy it, run it there, and the result
                (options &amp; specs) shows up here when it finishes.
              </span>
              <button
                onClick={() => navigator.clipboard?.writeText(command)}
                className="shrink-0 rounded-md border border-[var(--border)] px-2.5 py-1 text-[11px] font-semibold text-[var(--muted)] transition-colors hover:text-[var(--text)]"
              >
                Copy
              </button>
            </div>
          ) : null}
          {r.source_of_truth ? <SourceOfTruthRow sot={r.source_of_truth} /> : null}
          {r.note ? (
            <div className="rounded-lg bg-[var(--surface-2)] px-3 py-2 text-[11px] leading-snug text-[var(--muted)]">
              <span className="font-semibold text-[var(--text)]">
                {r.verification === "vision"
                  ? "AI photo check: "
                  : r.verification === "agent"
                    ? "Check note: "
                    : "Note: "}
              </span>
              {r.note}
            </div>
          ) : null}
        </div>
      </div>
    </div>

    {/* Full-window gallery of every judged candidate — eyeball all ~10 image-products at once */}
    {candWindow ? (
      <div
        className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 p-4"
        onClick={() => setCandWindow(false)}
        role="dialog"
        aria-modal="true"
      >
        <div
          className="flex max-h-[92vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-[var(--border)] bg-[var(--surface)] shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-start justify-between gap-3 border-b border-[var(--border)] p-4">
            <div className="flex min-w-0 items-start gap-3">
              {/* The original researched product, pinned here so it stays visible while the
                  candidate grid scrolls — eyeball every 1688 listing against THIS. */}
              <div className="flex flex-shrink-0 flex-col items-center gap-1">
                <div className="relative h-20 w-20 overflow-hidden rounded-lg border-2 bg-[var(--surface-2)]" style={{ borderColor: "var(--state-live)" }}>
                  {r.research?.image ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={r.research.image}
                      alt={r.research.title ?? r.subject ?? "researched product"}
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    <div className="flex h-full w-full items-center justify-center px-1 text-center text-[9px] leading-tight text-[var(--muted)]">
                      no photo yet
                    </div>
                  )}
                </div>
                <span className="rounded px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wide text-white" style={{ background: "var(--state-live)" }}>
                  Your product
                </span>
              </div>
              <div className="min-w-0">
                <div className="text-sm font-semibold">
                  {r.candidates.length || r.n_candidates || 0} supplier options · {r.subject ?? r.offer_id ?? "match"}
                </div>
                <p className="mt-0.5 text-[11px] text-[var(--muted)]">
                  Left = the product you found{r.research?.platform ? ` (${r.research.platform})` : ""}. Compare it against every supplier listing below — the AI&apos;s pick is flagged.
                  {fbBadMode ? " Click the right one to teach it." : " Mark the match wrong to pick a better one."}
                </p>
                {r.research?.title ? (
                  <p className="mt-0.5 truncate text-[11px] text-[var(--muted)]" title={r.research.title}>
                    {r.research.title}
                  </p>
                ) : null}
              </div>
            </div>
            <button
              onClick={() => setCandWindow(false)}
              className="flex-shrink-0 rounded px-2 py-1 text-sm text-[var(--muted)] hover:bg-[var(--surface-2)]"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
          <div className="overflow-y-auto p-4">
            {r.candidates.length === 0 ? (
              <div className="flex flex-col items-center gap-3 py-12 text-center">
                <p className="max-w-md text-[12px] leading-relaxed text-[var(--muted)]">
                  {r.n_candidates
                    ? `This match found ${r.n_candidates} possible suppliers, but their photos aren’t saved yet. Run the match again to pull each one’s photo and link here so you can review them.`
                    : "No supplier options saved for this match yet."}
                </p>
                {r.url ? (
                  <a
                    href={r.url}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-[11px] font-semibold text-[var(--accent)] hover:bg-[var(--surface-2)]"
                  >
                    Open the AI&apos;s supplier pick ↗
                  </a>
                ) : null}
              </div>
            ) : (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
              {r.candidates.map((c, i) => {
                const cc = VERDICT_COLOR[c.verdict] ?? "var(--muted)";
                const isCorrect = fbCorrect != null && c.offer_id === fbCorrect;
                const borderColor = isCorrect
                  ? "var(--state-winner)"
                  : c.picked
                    ? "var(--accent)"
                    : "var(--border)";
                return (
                  <div
                    key={`win-${c.offer_id}-${i}`}
                    className="flex flex-col overflow-hidden rounded-lg border-2 bg-[var(--surface)] transition-colors"
                    style={{ borderColor }}
                  >
                    <div className="relative aspect-square bg-[var(--surface-2)]">
                      {c.image ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={c.image} alt={c.title ?? c.offer_id ?? "candidate"} className="h-full w-full object-cover" loading="lazy" />
                      ) : (
                        <div className="flex h-full w-full items-center justify-center text-[10px] text-[var(--muted)]">no image</div>
                      )}
                      {c.picked ? (
                        <span className="absolute left-1 top-1 rounded bg-[var(--accent)] px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-white shadow">
                          ★ building from this
                        </span>
                      ) : null}
                      <span
                        className="absolute right-1 top-1 rounded px-1 py-0.5 text-[9px] font-bold uppercase"
                        style={{ color: cc, background: "color-mix(in srgb, black 55%, transparent)" }}
                      >
                        {VERDICT_LABEL[c.verdict] ?? c.verdict}
                      </span>
                    </div>
                    <div className="flex flex-1 flex-col gap-1 p-2">
                      {c.title ? (
                        <p className="line-clamp-2 text-[11px] leading-snug text-[var(--text)]" title={c.title}>
                          {c.title}
                        </p>
                      ) : null}
                      {c.matching_variant ? (
                        <span className="w-fit max-w-full truncate rounded border border-[var(--border)] bg-[var(--surface-2)] px-1 py-0.5 text-[10px] text-[var(--muted)]">
                          Variant: {c.matching_variant}
                        </span>
                      ) : null}
                      <div className="mt-auto flex items-center justify-between gap-1 text-[10px]">
                        {c.url ? (
                          <a href={c.url} target="_blank" rel="noreferrer" className="font-medium text-[var(--accent)] hover:underline">
                            open ↗
                          </a>
                        ) : <span />}
                        <span className="flex items-center gap-1.5 tabular-nums text-[var(--muted)]">
                          {c.price != null ? <span>¥{c.price}</span> : null}
                          {c.sold != null ? <span>· {c.sold} sold</span> : null}
                        </span>
                      </div>
                      {c.picked ? (
                        <div className="mt-1 rounded border border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] px-1.5 py-1 text-center text-[10px] font-semibold text-[var(--accent)]">
                          ★ Listing builds from this
                        </div>
                      ) : (
                        <button
                          onClick={() => pickInstead(c.offer_id)}
                          disabled={picking != null}
                          className="mt-1 rounded border border-[var(--accent)] px-1.5 py-1 text-[10px] font-semibold text-[var(--accent)] transition-colors hover:bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] disabled:opacity-50"
                          title="Build the listing from THIS supplier instead"
                        >
                          {picking === (c.offer_id ?? "") ? "Picking…" : "Pick this instead →"}
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
            )}
          </div>
        </div>
      </div>
    ) : null}
    </>
  );
}

function CompareColumn({
  tag,
  tagColor,
  image,
  title,
  url,
  urlLabel,
  rows,
  variants,
  specs,
  empty,
}: {
  tag: string;
  tagColor: string;
  image?: string | null;
  title?: string | null;
  url?: string | null;
  urlLabel: string;
  rows?: [string, React.ReactNode][];
  variants?: string[] | null;
  specs?: [string, string][] | null;
  empty: string;
}) {
  const has = Boolean(image || title);
  return (
    <div className="flex flex-col gap-3 bg-[var(--surface)] p-4">
      <span
        className="w-fit rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
        style={{ color: tagColor, background: `color-mix(in srgb, ${tagColor} 16%, transparent)` }}
      >
        {tag}
      </span>
      {has ? (
        <>
          <div className="aspect-square w-full overflow-hidden rounded-lg bg-[var(--surface-2)]">
            {image ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={image} alt={title ?? tag} className="h-full w-full object-contain" />
            ) : (
              <div className="flex h-full w-full items-center justify-center text-xs text-[var(--muted)]">
                no image
              </div>
            )}
          </div>
          {title ? <div className="text-xs font-medium leading-snug">{title}</div> : null}
          {rows && rows.length > 0 ? (
            <dl className="flex flex-col gap-1">
              {rows.map(([k, v]) => (
                <div key={k} className="flex items-baseline justify-between gap-2 text-[11px]">
                  <dt className="text-[var(--muted)]">{k}</dt>
                  <dd className="tabular-nums font-medium text-[var(--text)]">{v}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {variants && variants.length > 0 ? (
            <div className="flex flex-col gap-1">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-[var(--muted)]">
                Variants
              </span>
              <div className="flex flex-wrap gap-1">
                {variants.map((v, i) => (
                  <span
                    key={`${v}-${i}`}
                    className="rounded bg-[var(--surface-2)] px-1.5 py-0.5 text-[10px] leading-snug text-[var(--text)]"
                  >
                    {v}
                  </span>
                ))}
              </div>
            </div>
          ) : null}
          {specs && specs.length > 0 ? (
            <div className="flex flex-col gap-1">
              <span className="text-[10px] font-semibold uppercase tracking-wide text-[var(--muted)]">
                Specs
              </span>
              <dl className="flex flex-col gap-0.5">
                {specs.map(([k, v], i) => (
                  <div
                    key={`${k}-${i}`}
                    className="truncate text-[11px] leading-snug"
                    title={`${k}: ${v || "—"}`}
                  >
                    <span className="text-[var(--muted)]">{k}: </span>
                    <span className="font-medium text-[var(--text)]">{v || "—"}</span>
                  </div>
                ))}
              </dl>
            </div>
          ) : null}
          {url ? (
            <a
              href={url}
              target="_blank"
              rel="noreferrer"
              className="mt-auto text-[11px] font-medium text-[var(--accent)] hover:underline"
            >
              {urlLabel}
            </a>
          ) : null}
        </>
      ) : (
        <p className="text-[11px] text-[var(--muted)]">{empty}</p>
      )}
    </div>
  );
}

// 1688-first source-of-truth resolution: which source grounds the listing build.
function SourceOfTruthRow({ sot }: { sot: SourceOfTruth }) {
  const s = SOURCE_OF_TRUTH[sot];
  return (
    <div className="flex flex-col gap-0.5 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-1.5">
      <span
        className="w-fit rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
        style={{ color: s.color, background: `color-mix(in srgb, ${s.color} 16%, transparent)` }}
      >
        {s.label}
      </span>
      <p className="text-[11px] leading-snug text-[var(--muted)]">{s.hint}</p>
    </div>
  );
}
