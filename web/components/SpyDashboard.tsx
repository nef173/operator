"use client";

import { useState } from "react";
import {
  api,
  type SpyStore,
  type MoverStore,
  type Mover,
  type SpyCandidate,
  type Top30State,
  type CompetitorScan,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Pill } from "@/components/ui";
import { Loading, ErrorState, Empty } from "@/components/PageState";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DetailModal } from "@/components/DetailModal";

function fmtVisits(n: number | null): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

// US + UK are the two primary target markets. GB traffic ships in `other_markets`
// (US-excluded, share 0-1) — pull it out so it can sit next to US, and keep the rest
// of the geo split for the "Other markets" column.
function marketSplit(s: SpyStore): {
  gbShare: number | null;
  rest: { country: string; share: number }[];
} {
  const others = s.other_markets ?? [];
  const gb = others.find((m) => m.country === "GB" || m.country === "UK");
  const rest = others.filter((m) => m.country !== "GB" && m.country !== "UK");
  return { gbShare: gb ? gb.share : null, rest };
}

function pct(share: number | null): string | null {
  return share == null ? null : `${Math.round(share * 100)}%`;
}

// Normalize a pasted competitor URL to a bare registrable domain: strip scheme, www.,
// path, query, and any trailing port/whitespace. "https://www.foo.com/bar?x=1" → "foo.com".
function normalizeDomain(raw: string): string {
  let d = (raw || "").trim().toLowerCase();
  d = d.replace(/^[a-z]+:\/\//, ""); // scheme
  d = d.replace(/^www\./, "");
  d = d.split("/")[0]; // path
  d = d.split("?")[0].split("#")[0]; // query / fragment
  d = d.split(":")[0]; // port
  d = d.trim();
  // Must look like a domain (has a dot, no spaces).
  return /^[a-z0-9.-]+\.[a-z]{2,}$/.test(d) ? d : "";
}

const TIER_LABEL: Record<string, string> = {
  T3: "350K–1M+",
  T2: "100–350K",
  T1: "30–100K",
};

function TierBadge({ tier }: { tier: string | null }) {
  if (!tier) return <span className="text-xs text-[var(--muted)]">—</span>;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-semibold"
      style={{
        color: "var(--accent)",
        background: "color-mix(in srgb, var(--accent) 13%, transparent)",
      }}
      title={`${TIER_LABEL[tier]} monthly visits`}
    >
      {tier}
    </span>
  );
}

function Mom({ pct, flagged }: { pct: number | null; flagged: boolean }) {
  if (pct == null) return <span className="text-xs text-[var(--muted)]">—</span>;
  const up = pct >= 0;
  const color = up ? "var(--state-live)" : "var(--state-killed)";
  return (
    <span className="inline-flex items-center gap-1.5 tabular-nums" style={{ color }}>
      <span>{up ? "▲" : "▼"}</span>
      <span className="text-sm font-semibold">{Math.abs(pct).toFixed(1)}%</span>
      {flagged ? (
        <span
          className="rounded px-1 text-[10px] font-bold uppercase"
          style={{ color: "var(--state-killed)", background: "color-mix(in srgb, var(--state-killed) 16%, transparent)" }}
          title="Down >30% MoM — review for removal"
        >
          remove?
        </span>
      ) : null}
    </span>
  );
}

// Self-contained inline-SVG sparkline of the 6-month visit history.
function Spark({ values }: { values: number[] }) {
  const pts = values.filter((v) => typeof v === "number");
  if (pts.length < 2) return <span className="text-xs text-[var(--muted)]">—</span>;
  const w = 96;
  const h = 28;
  const max = Math.max(...pts);
  const min = Math.min(...pts);
  const span = max - min || 1;
  const step = w / (pts.length - 1);
  const coords = pts.map((v, i) => [i * step, h - ((v - min) / span) * (h - 4) - 2] as const);
  const line = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const [lx, ly] = coords[coords.length - 1];
  const last = pts[pts.length - 1];
  const up = last >= pts[pts.length - 2];
  const color = up ? "var(--state-live)" : "var(--state-killed)";
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="overflow-visible">
      <polyline
        points={line}
        fill="none"
        stroke={color}
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      <circle cx={lx} cy={ly} r="2.2" fill={color} />
    </svg>
  );
}

// The competitor-spy roster (tracked pure-Google general stores by tier / breadth /
// momentum). The store-overview surface — analytics per competitor. Self-fetches.
// Split out so the Winning-Products → Competitor Spy sub-tabs can show it on its own.
export function SpyRoster() {
  const { data, error, loading, reload } = useApi(() => api.spyRoster());
  const [removeTarget, setRemoveTarget] = useState<SpyStore | null>(null);
  const [researchOpen, setResearchOpen] = useState(false);
  const [scanTarget, setScanTarget] = useState<SpyStore | null>(null);
  // Manual add-by-URL box (paste a competitor store → gate on live Google ads → append).
  const [addUrl, setAddUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [addResult, setAddResult] = useState<
    | { kind: "ok"; msg: string }
    | { kind: "skip"; msg: string }
    | { kind: "error"; msg: string }
    | null
  >(null);

  async function handleAdd() {
    const domain = normalizeDomain(addUrl);
    if (!domain) {
      setAddResult({ kind: "error", msg: "Enter a valid store URL." });
      return;
    }
    setAdding(true);
    setAddResult(null);
    try {
      const res = await api.spyAdmit([domain]);
      if (res.n_admitted > 0) {
        setAddUrl("");
        setAddResult({ kind: "ok", msg: `Added ${res.admitted.join(", ")} to the roster.` });
        reload();
      } else {
        const why = res.skipped[0]?.reason ?? "not running live Google Shopping ads";
        setAddResult({ kind: "skip", msg: `${domain} skipped — ${why}.` });
      }
    } catch (e) {
      setAddResult({ kind: "error", msg: e instanceof Error ? e.message : "Add failed" });
    } finally {
      setAdding(false);
    }
  }

  if (loading) return <Loading />;
  if (error) return <ErrorState message={error} />;
  if (!data) return null;

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <form
          className="flex min-w-0 flex-1 items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!adding) handleAdd();
          }}
        >
          <input
            type="text"
            value={addUrl}
            onChange={(e) => setAddUrl(e.target.value)}
            placeholder="Paste a competitor store URL"
            aria-label="Competitor store URL"
            className="w-full max-w-md rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
          />
          <button
            type="submit"
            disabled={adding || addUrl.trim() === ""}
            className="shrink-0 rounded-lg border border-[var(--border)] px-3.5 py-2 text-sm font-semibold hover:bg-[var(--surface-2)] disabled:opacity-50"
          >
            {adding ? "Scanning…" : "Add & scan"}
          </button>
        </form>
        <button
          type="button"
          onClick={() => setResearchOpen(true)}
          className="shrink-0 rounded-lg px-3.5 py-2 text-sm font-semibold"
          style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          + Research new stores
        </button>
      </div>

      {addResult ? (
        <div
          className="mb-4 flex items-start justify-between gap-3 rounded-lg px-3 py-2 text-xs"
          style={
            addResult.kind === "ok"
              ? { color: "var(--state-live)", background: "color-mix(in srgb, var(--state-live) 12%, transparent)" }
              : addResult.kind === "skip"
                ? { color: "var(--warning)", background: "color-mix(in srgb, var(--warning) 12%, transparent)" }
                : { color: "var(--state-killed)", background: "color-mix(in srgb, var(--state-killed) 12%, transparent)" }
          }
        >
          <span>{addResult.msg}</span>
          <button
            type="button"
            onClick={() => setAddResult(null)}
            aria-label="Dismiss"
            className="shrink-0 opacity-70 hover:opacity-100"
          >
            ✕
          </button>
        </div>
      ) : null}

      {data.stores.length === 0 ? (
        <Empty>No tracked competitors yet — use “Research new stores” to discover some.</Empty>
      ) : (
        <Card className="mt-2 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
                  <th className="px-4 py-3 font-semibold">Competitor</th>
                  <th className="px-4 py-3 text-right font-semibold">Visits/mo</th>
                  <th className="px-4 py-3 font-semibold">MoM</th>
                  <th className="px-4 py-3">6-mo trend</th>
                  <th className="px-4 py-3 text-right font-semibold">US / UK</th>
                  <th className="px-4 py-3 font-semibold">Other markets</th>
                  <th className="px-4 py-3 font-semibold">Category</th>
                  <th className="px-4 py-3 text-right font-semibold">Products</th>
                  <th className="px-4 py-3 text-right font-semibold">Google ads</th>
                  <th className="px-4 py-3 text-right font-semibold">Meta ads</th>
                  <th className="px-4 py-3 text-right font-semibold"></th>
                </tr>
              </thead>
              <tbody>
                {data.stores.map((s) => (
                  <Row
                    key={s.domain}
                    s={s}
                    onRemove={() => setRemoveTarget(s)}
                    onScan={() => setScanTarget(s)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <p className="mt-3 text-xs text-[var(--muted)]">
        Roster is pure Google/SEO-driven — Meta ad count should read ~0, while Google ads
        (live PLAs in the Ads Transparency Center) confirm an active Google advertiser. The
        Google-ads count is captured once, at admission — only stores showing live Google
        Shopping ads are admitted to the roster. Updated {data.updated ?? "—"}.
      </p>

      <ConfirmDialog
        open={removeTarget !== null}
        title="Remove tracked store?"
        tone="danger"
        confirmLabel="Remove"
        body={
          <>
            Drop <strong>{removeTarget?.domain}</strong> from the spy roster. It&apos;s removed from
            stores.txt and its cached traffic / breadth analytics are pruned. You can re-add it later
            via “Research new stores”.
          </>
        }
        onConfirm={async () => {
          if (removeTarget) await api.spyRemoveStore(removeTarget.domain);
        }}
        onClose={() => setRemoveTarget(null)}
        onDone={reload}
      />

      <ResearchNewStoresModal
        open={researchOpen}
        onClose={() => setResearchOpen(false)}
        onAdmitted={reload}
      />

      <CompetitorScanModal store={scanTarget} onClose={() => setScanTarget(null)} />
    </>
  );
}

// Per-competitor keyword scan — the "Google competitor catalog keyword" check. Starts from the
// store's OWN keyword set (its /search?q=<kw>, so deep-catalog products are caught), then ranks each
// as its own tier list by best-seller position (/collections/all?sort_by=best-selling). Surfaces the
// products the competitor actually SELLS for the keyword — even a #1 aircon sitting at global BS #80 —
// and lists the rest (listed but no sales signal) below. US/UK selectable per Shopify Market.
function fmtScanPrice(p: string | number | null | undefined): string {
  if (p == null || p === "") return "—";
  const n = typeof p === "number" ? p : parseFloat(String(p));
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : `$${p}`;
}

function CompetitorScanModal({ store, onClose }: { store: SpyStore | null; onClose: () => void }) {
  const [keyword, setKeyword] = useState("");
  const [pending, setPending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<CompetitorScan | null>(null);

  // Reset per-store at render time (no setState-in-effect) — track which store the current
  // form state belongs to and clear when a different row opens the modal.
  const [forDomain, setForDomain] = useState<string | null>(null);
  const domain = store?.domain ?? null;
  if (domain !== forDomain) {
    setForDomain(domain);
    setKeyword("");
    setResult(null);
    setErr(null);
    setPending(false);
  }

  if (!store) return null;

  // NO US/UK toggle here — a store's best-seller order is ONE order, not market-split. US/UK market
  // targeting belongs to the competitor DISCOVERY lanes (GoogleShopping Search + competitor search).
  async function runScan() {
    const kw = keyword.trim();
    if (!kw || !store) return;
    setPending(true);
    setErr(null);
    setResult(null);
    try {
      setResult(await api.competitorScan(store.domain, kw));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Scan failed");
    } finally {
      setPending(false);
    }
  }

  const rows = result ? (result.results ?? result.validated ?? []) : [];
  const validatedRows = rows.filter((r) => r.validated);
  const unrankedRows = rows.filter((r) => !r.validated);

  return (
    <DetailModal
      open={store !== null}
      onClose={pending ? () => {} : onClose}
      title={`Scan ${store.domain}`}
      subtitle="The store's products for a keyword, ranked by what it actually SELLS (its best-seller order)."
    >
      <form
        className="mb-4 flex items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!pending) runScan();
        }}
      >
        <input
          type="text"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder="e.g. aircon"
          aria-label="Keyword to scan for"
          autoFocus
          className="w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
        />
        <button
          type="submit"
          disabled={pending || keyword.trim() === ""}
          className="shrink-0 rounded-lg px-3.5 py-2 text-sm font-semibold disabled:opacity-50"
          style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          {pending ? "Scanning…" : "Scan"}
        </button>
      </form>

      {err ? (
        <div
          className="mb-3 rounded-lg px-3 py-2 text-xs"
          style={{ color: "var(--state-killed)", background: "color-mix(in srgb, var(--state-killed) 12%, transparent)" }}
        >
          {err}
        </div>
      ) : null}

      {result ? (
        rows.length === 0 ? (
          <p className="text-sm text-[var(--muted)]">
            {store.domain} lists nothing for “{result.keyword}”.
          </p>
        ) : (
          <>
            <p className="mb-1 text-xs text-[var(--muted)]">
              {result.keyword_matches ?? rows.length} listed for “{result.keyword}” ·{" "}
              <span className="font-semibold text-[var(--fg)]">{result.n_validated} the store actually
              sell{result.n_validated === 1 ? "s" : ""}</span>
            </p>
            {result.note ? (
              <div
                className="mb-2 rounded-lg px-3 py-2 text-xs"
                style={{ color: "var(--muted)", background: "color-mix(in srgb, var(--accent) 8%, transparent)" }}
              >
                {result.note}
              </div>
            ) : null}

            {validatedRows.length > 0 ? (
              <ul className="mb-2 flex flex-col gap-1.5">
                {validatedRows.map((p, i) => (
                  <li
                    key={`v-${p.handle ?? p.title ?? i}-${i}`}
                    className="flex items-center gap-3 rounded-lg border border-[var(--border)] px-3 py-2"
                  >
                    <span className="w-7 shrink-0 text-right text-xs font-bold tabular-nums text-[var(--accent)]">
                      #{i + 1}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm">
                        {p.url ? (
                          <a href={p.url} target="_blank" rel="noreferrer" className="hover:text-[var(--accent)] hover:underline">
                            {p.title ?? p.handle ?? "—"}
                          </a>
                        ) : (
                          p.title ?? p.handle ?? "—"
                        )}
                      </span>
                      {p.collection ? (
                        <span
                          className="mt-0.5 inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold"
                          style={
                            p.tier === "bestseller"
                              ? { background: "color-mix(in srgb, var(--accent) 16%, transparent)", color: "var(--accent)" }
                              : { background: "var(--surface)", color: "var(--muted)" }
                          }
                        >
                          {p.tier === "bestseller" ? "★ " : ""}
                          {p.collection}
                          {p.collection_rank != null ? ` #${p.collection_rank}` : ""}
                        </span>
                      ) : p.bestseller_rank != null ? (
                        <span
                          className="mt-0.5 inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold"
                          style={{ background: "color-mix(in srgb, var(--accent) 16%, transparent)", color: "var(--accent)" }}
                        >
                          ★ best-seller #{p.bestseller_rank}
                        </span>
                      ) : null}
                    </span>
                    <span className="shrink-0 self-start text-sm font-semibold tabular-nums">{fmtScanPrice(p.price)}</span>
                  </li>
                ))}
              </ul>
            ) : null}

            {unrankedRows.length > 0 ? (
              <details className="mt-1">
                <summary className="cursor-pointer text-xs text-[var(--muted)]">
                  {unrankedRows.length} listed but not in a best-seller / category collection (no sales signal)
                </summary>
                <ul className="mt-1.5 flex flex-col gap-1.5">
                  {unrankedRows.map((p, i) => (
                    <li
                      key={`u-${p.handle ?? p.title ?? i}-${i}`}
                      className="flex items-center gap-3 rounded-lg border border-dashed border-[var(--border)] px-3 py-2 opacity-70"
                    >
                      <span className="w-14 shrink-0 text-right text-[10px] tabular-nums text-[var(--muted)]">listed</span>
                      <span className="min-w-0 flex-1 truncate text-sm">
                        {p.url ? (
                          <a href={p.url} target="_blank" rel="noreferrer" className="hover:text-[var(--accent)] hover:underline">
                            {p.title ?? p.handle ?? "—"}
                          </a>
                        ) : (
                          p.title ?? p.handle ?? "—"
                        )}
                      </span>
                      <span className="shrink-0 text-sm tabular-nums">{fmtScanPrice(p.price)}</span>
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </>
        )
      ) : null}
    </DetailModal>
  );
}

// "Research new stores" review — opens a window-in-window listing the discovered candidate
// stores so the operator can approve all / admit a chosen subset / reject the rest before
// any are added to the tracked roster. When no candidates are on disk yet it surfaces the
// discovery command to run (discovery needs TrendTrack, so it runs in the operator's own
// Claude Code, then the results land here for review).
function ResearchNewStoresModal({
  open,
  onClose,
  onAdmitted,
}: {
  open: boolean;
  onClose: () => void;
  onAdmitted: () => void;
}) {
  const cands = useApi(() => api.spyCandidates(), [open]);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [pending, setPending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [skipped, setSkipped] = useState<
    { domain: string; reason: string; google_ads_count: number | null }[]
  >([]);

  if (!open) return null;

  const rows = cands.data?.candidates ?? [];
  const selected = rows.filter((r) => picked[r.domain]);
  const allOn = rows.length > 0 && selected.length === rows.length;

  async function admit(domains: string[]) {
    if (domains.length === 0) return;
    setPending(true);
    setErr(null);
    setSkipped([]);
    try {
      const res = await api.spyAdmitStores(domains);
      onAdmitted();
      setPicked({});
      if (res.n_skipped > 0) {
        // Some candidates show no live Google Shopping ads — keep the modal open and
        // surface why they were not admitted, so the operator sees the gate at work.
        setSkipped(res.skipped);
        cands.reload();
      } else {
        onClose();
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Admit failed");
    } finally {
      setPending(false);
    }
  }

  return (
    <DetailModal
      open={open}
      onClose={pending ? () => {} : onClose}
      title="Research new stores"
      subtitle={
        cands.data?.source
          ? `${rows.length} candidate${rows.length === 1 ? "" : "s"} discovered · ${cands.data.source}`
          : "Review discovered candidates before admitting them to the roster"
      }
      footer={
        rows.length > 0 ? (
          <>
            <button
              type="button"
              disabled={pending}
              onClick={() => admit(rows.map((r) => r.domain))}
              className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--surface-2)] disabled:opacity-50"
            >
              Approve all ({rows.length})
            </button>
            <button
              type="button"
              disabled={pending || selected.length === 0}
              onClick={() => admit(selected.map((r) => r.domain))}
              className="rounded-lg px-3 py-1.5 text-sm font-semibold disabled:opacity-50"
              style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
            >
              {pending ? "Admitting…" : `Admit selected (${selected.length})`}
            </button>
          </>
        ) : null
      }
    >
      {cands.loading ? <Loading /> : null}
      {cands.error ? <ErrorState message={cands.error} /> : null}
      {err ? (
        <div
          className="mb-3 rounded-lg px-3 py-2 text-xs"
          style={{ color: "var(--state-killed)", background: "color-mix(in srgb, var(--state-killed) 12%, transparent)" }}
        >
          {err}
        </div>
      ) : null}

      {skipped.length > 0 ? (
        <div
          className="mb-3 rounded-lg px-3 py-2 text-xs"
          style={{ color: "var(--state-killed)", background: "color-mix(in srgb, var(--state-killed) 12%, transparent)" }}
        >
          <p className="font-semibold">
            {skipped.length} store{skipped.length === 1 ? "" : "s"} not admitted — no live Google
            Shopping ads in the Transparency Center:
          </p>
          <ul className="mt-1 list-disc pl-4">
            {skipped.map((s) => (
              <li key={s.domain}>
                <span className="font-mono">{s.domain}</span> — {s.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {cands.data && rows.length === 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-[var(--muted)]">
          <p className="min-w-0 flex-1">
            No stores found yet. This step finds growing US general stores for you — it runs in your
            workspace session; copy it, run it there, then re-open this to approve or reject the results.
          </p>
          <button
            type="button"
            onClick={() => navigator.clipboard?.writeText(cands.data?.discover_cmd ?? "")}
            className="shrink-0 rounded-md border border-[var(--border)] px-2.5 py-1 text-[11px] font-semibold text-[var(--muted)] transition-colors hover:text-[var(--text)]"
          >
            Copy
          </button>
        </div>
      ) : null}

      {rows.length > 0 ? (
        <div className="flex flex-col gap-2">
          <p className="text-xs text-[var(--muted)]">
            On admit, each store is checked against the Google Ads Transparency Center — only
            stores showing live Google Shopping ads are added to the roster.
          </p>
          <label className="mb-1 flex cursor-pointer items-center gap-2 text-xs font-medium text-[var(--muted)]">
            <input
              type="checkbox"
              checked={allOn}
              onChange={(e) =>
                setPicked(e.target.checked ? Object.fromEntries(rows.map((r) => [r.domain, true])) : {})
              }
            />
            Select all
          </label>
          {rows.map((c) => (
            <CandidateRow
              key={c.domain}
              c={c}
              checked={!!picked[c.domain]}
              onToggle={(v) => setPicked((p) => ({ ...p, [c.domain]: v }))}
            />
          ))}
        </div>
      ) : null}
    </DetailModal>
  );
}

function CandidateRow({
  c,
  checked,
  onToggle,
}: {
  c: SpyCandidate;
  checked: boolean;
  onToggle: (v: boolean) => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-3 rounded-lg border border-[var(--border)] px-3 py-2 hover:bg-[var(--surface-2)]">
      <input type="checkbox" checked={checked} onChange={(e) => onToggle(e.target.checked)} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{c.domain}</div>
        {c.note ? <div className="truncate text-[11px] text-[var(--muted)]">{c.note}</div> : null}
      </div>
      {c.tier ? <TierBadge tier={c.tier} /> : null}
      <span className="text-xs tabular-nums text-[var(--muted)]">{fmtVisits(c.monthly_visits)}/mo</span>
    </label>
  );
}

// Best-seller rank-movers — the climbing/falling products each winning competitor is
// pushing (Shopify products.json rank-diff between two snapshots).
export function SpyMovers() {
  const movers = useApi(() => api.bestsellerMovers());
  const [top30Only, setTop30Only] = useState(true);

  if (movers.loading) return <Loading />;
  if (movers.error) return <ErrorState message={movers.error} />;
  if (!movers.data || movers.data.stores.length === 0)
    return (
      <Empty>
        No best-seller movers yet — need two snapshots per store to compute a rank delta.
      </Empty>
    );

  const top30 = movers.data.top30 ?? 30;
  const entered = movers.data.totals.entered_top30 ?? 0;
  const climbing = movers.data.totals.climbing_top30 ?? 0;

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <p className="m-0 max-w-2xl text-sm text-[var(--muted)]">
          Competitor Shopify best-seller rank-deltas between two snapshots. The demand signal that
          matters is the <strong>top {top30}</strong>: a product that just <strong>broke into</strong>{" "}
          the top {top30} is a fresh spike; one <strong>climbing inside</strong> it is accelerating.
          Generated {movers.data.generated_at ?? "—"}.
        </p>
        <label className="flex shrink-0 cursor-pointer items-center gap-2 text-xs font-medium text-[var(--muted)]">
          <input type="checkbox" checked={top30Only} onChange={(e) => setTop30Only(e.target.checked)} />
          Top {top30} signals only
        </label>
      </div>

      <div className="mb-5 flex flex-wrap gap-2 text-xs">
        <span
          className="rounded-md px-2 py-1 font-semibold"
          style={{ color: "var(--state-winner)", background: "color-mix(in srgb, var(--state-winner) 14%, transparent)" }}
        >
          ↑ {entered} entered top {top30}
        </span>
        <span
          className="rounded-md px-2 py-1 font-semibold"
          style={{ color: "var(--state-live)", background: "color-mix(in srgb, var(--state-live) 14%, transparent)" }}
        >
          ▲ {climbing} climbing in top {top30}
        </span>
      </div>

      <div className="space-y-6">
        {movers.data.stores.map((s) => (
          <MoverStoreCard key={s.store} s={s} top30Only={top30Only} top30={top30} />
        ))}
      </div>
    </div>
  );
}

// Back-compat combined view (roster + movers stacked). Prefer the split parts above.
export function SpyDashboard() {
  return (
    <>
      <SpyRoster />
      <div className="mt-12">
        <h2 className="mb-1">Best-seller movers</h2>
        <SpyMovers />
      </div>
    </>
  );
}

function MoverStoreCard({ s, top30Only, top30 }: { s: MoverStore; top30Only: boolean; top30: number }) {
  const shown = top30Only ? s.movers.filter((m) => m.top30 !== null) : s.movers;
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] px-4 py-3">
        <div className="font-semibold">{s.store}</div>
        <div className="flex items-center gap-3 text-xs text-[var(--muted)]">
          <span style={{ color: "var(--state-winner)" }} title={`Newly into the top ${top30}`}>
            ↑ {s.n_entered_top30} entered
          </span>
          <span style={{ color: "var(--state-live)" }} title={`Climbing inside the top ${top30}`}>
            ▲ {s.n_climbing_top30} climbing
          </span>
          {!top30Only ? (
            <>
              <span style={{ color: "var(--state-live)" }}>▲ {s.n_gainers} gainers</span>
              <span style={{ color: "var(--state-killed)" }}>▼ {s.n_fallers} fallers</span>
            </>
          ) : null}
          <span className="tabular-nums">
            {s.prior_date ?? "—"} → {s.latest_date ?? "—"}
          </span>
        </div>
      </div>
      {shown.length === 0 ? (
        <div className="px-4 py-6 text-center text-sm text-[var(--muted)]">
          {top30Only ? `No product entered or climbed in the top ${top30}.` : "No rank movement in the comparable depth."}
        </div>
      ) : (
        <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
          {shown.slice(0, 12).map((m, i) => (
            <MoverCard key={`${m.handle}-${i}`} m={m} />
          ))}
        </div>
      )}
    </Card>
  );
}

const TOP30_BADGE: Record<Exclude<Top30State, null>, { label: string; color: string }> = {
  entered: { label: "entered top 30", color: "var(--state-winner)" },
  climbing: { label: "climbing top 30", color: "var(--state-live)" },
};

function MoverCard({ m }: { m: Mover }) {
  const gain = m.class === "gainer";
  const color = gain ? "var(--state-live)" : "var(--state-killed)";
  const badge = m.top30 ? TOP30_BADGE[m.top30] : null;
  return (
    <div className="flex gap-3 rounded-lg border border-[var(--border)] p-3">
      {m.image ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={m.image}
          alt={m.title ?? ""}
          className="h-16 w-16 shrink-0 rounded-md bg-[var(--surface-2)] object-cover"
          loading="lazy"
        />
      ) : (
        <div className="h-16 w-16 shrink-0 rounded-md bg-[var(--surface-2)]" />
      )}
      <div className="min-w-0 flex-1">
        {badge ? (
          <span
            className="mb-1 inline-block rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
            style={{ color: badge.color, background: `color-mix(in srgb, ${badge.color} 16%, transparent)` }}
          >
            {badge.label}
          </span>
        ) : null}
        <div className="line-clamp-2 text-sm font-medium leading-snug">
          {m.url ? (
            <a href={m.url} target="_blank" rel="noreferrer" className="hover:text-[var(--accent)]">
              {m.title ?? m.handle ?? "—"}
            </a>
          ) : (
            (m.title ?? m.handle ?? "—")
          )}
        </div>
        <div className="mt-1 flex items-center gap-2">
          <span className="text-sm font-semibold tabular-nums">
            {m.price != null ? `$${m.price.toFixed(2)}` : "—"}
          </span>
          <span
            className="rounded px-1.5 py-0.5 text-[11px] font-semibold tabular-nums"
            style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
          >
            {gain ? "▲" : "▼"} {Math.abs(m.rank_delta ?? 0)} · #{m.rank ?? "—"}
          </span>
          {m.is_fresh ? (
            <span className="text-[10px] uppercase" style={{ color: "var(--accent)" }}>
              fresh
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function Row({ s, onRemove, onScan }: { s: SpyStore; onRemove: () => void; onScan: () => void }) {
  const { gbShare, rest } = marketSplit(s);
  const usPct = pct(s.us_share);
  const gbPct = pct(gbShare);
  return (
    <tr className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)]">
      <td className="px-4 py-3">
        <div className="font-medium">{s.domain}</div>
        {s.category ? (
          <div className="text-xs text-[var(--muted)]">{s.category}</div>
        ) : null}
      </td>
      <td className="px-4 py-3 text-right font-semibold tabular-nums">
        {fmtVisits(s.monthly_visits)}
      </td>
      <td className="px-4 py-3">
        <Mom pct={s.mom_pct} flagged={s.flag_remove} />
      </td>
      <td className="px-4 py-3">
        <Spark values={s.history} />
      </td>
      <td className="px-4 py-3 text-right tabular-nums">
        {usPct == null && gbPct == null ? (
          <span className="text-[var(--muted)]">—</span>
        ) : (
          <span className="inline-flex items-center justify-end gap-1.5">
            <span title="US traffic share">
              <span className="text-[10px] font-semibold text-[var(--muted)]">US</span>{" "}
              <span className="font-medium">{usPct ?? "—"}</span>
            </span>
            {gbPct ? (
              <span className="text-[var(--muted)]" title="UK traffic share">
                <span className="text-[10px] font-semibold">UK</span> {gbPct}
              </span>
            ) : null}
          </span>
        )}
      </td>
      <td className="px-4 py-3 text-xs text-[var(--muted)]">
        {rest.length > 0 ? rest.map((m) => m.country.toUpperCase()).join(" · ") : "—"}
      </td>
      <td className="px-4 py-3">
        {s.verdict ? (
          <span className="inline-flex flex-col gap-0.5">
            <Pill>{s.verdict.toLowerCase()}</Pill>
            {s.dominant_dept ? (
              <span className="text-[11px] text-[var(--muted)]">
                {s.dominant_dept.replace(/_/g, " ")}
                {s.dominant_share != null ? ` ${Math.round(s.dominant_share * 100)}%` : ""}
              </span>
            ) : null}
          </span>
        ) : (
          <span className="text-xs text-[var(--muted)]">unclassified</span>
        )}
      </td>
      <td className="px-4 py-3 text-right tabular-nums text-[var(--muted)]">
        {s.products == null ? "—" : s.products.toLocaleString()}
      </td>
      <td className="px-4 py-3 text-right tabular-nums">
        {s.google_ads_count == null ? (
          <span className="text-[var(--muted)]">—</span>
        ) : s.google_ads_count === 0 ? (
          <span className="text-[var(--muted)]" title="No live ads in Google Ads Transparency Center">
            0
          </span>
        ) : (
          <a
            href={`https://adstransparency.google.com/?region=anywhere&domain=${encodeURIComponent(s.domain)}`}
            target="_blank"
            rel="noreferrer"
            className="inline-flex flex-col items-end leading-tight hover:underline"
            title={`${s.google_ads_count}${s.google_ads_capped ? "+" : ""} live Google ads · ${s.google_ads_shopping ?? 0} Shopping/PLA${s.google_ads_last_shown ? ` · last shown ${s.google_ads_last_shown}` : ""} — open Transparency Center`}
          >
            <span className="font-semibold" style={{ color: "var(--state-live)" }}>
              {s.google_ads_count.toLocaleString()}
              {s.google_ads_capped ? "+" : ""}
            </span>
            {s.google_ads_shopping != null ? (
              <span className="text-[11px] text-[var(--muted)]">{s.google_ads_shopping} shopping</span>
            ) : null}
          </a>
        )}
      </td>
      <td className="px-4 py-3 text-right tabular-nums">
        {s.active_meta_ads == null ? (
          <span className="text-[var(--muted)]">—</span>
        ) : s.active_meta_ads === 0 ? (
          <span style={{ color: "var(--state-live)" }}>0</span>
        ) : (
          <span style={{ color: "var(--state-killed)" }} title="Runs Meta ads — not pure Google">
            {s.active_meta_ads}
          </span>
        )}
      </td>
      <td className="px-4 py-3 text-right">
        <div className="inline-flex items-center gap-1">
          <button
            type="button"
            onClick={onScan}
            aria-label={`Scan ${s.domain} for a keyword`}
            title="Scan this competitor's best-sellers for a keyword"
            className="rounded-md px-2 py-1 text-sm text-[var(--muted)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--accent)]"
          >
            🔍
          </button>
          <button
            type="button"
            onClick={onRemove}
            aria-label={`Remove ${s.domain}`}
            title="Remove from roster"
            className="rounded-md px-2 py-1 text-sm text-[var(--muted)] transition-colors hover:bg-[color-mix(in_srgb,var(--state-killed)_14%,transparent)] hover:text-[var(--state-killed)]"
          >
            ✕
          </button>
        </div>
      </td>
    </tr>
  );
}
