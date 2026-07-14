"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  api,
  type JobRecord,
  type JobStatus,
  type KeywordCandidate,
  type LaneProduct,
  type ListingPlan,
  type Overview,
  type SkuPlanHead,
  type TrendRow,
  type WorkerStatus,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { useStore } from "@/components/StoreProvider";
import { Card } from "@/components/ui";
import { DetailModal } from "@/components/DetailModal";
import { PageHeader, ErrorState, Empty } from "@/components/PageState";

// ────────────────────────────────────────────────────────────────────────────
// The Dashboard is the operation cockpit: ONE button starts the whole pipeline
// (research → trend → keyword → SKU plan → find products → listing queue), the
// five stages show live job status + the real content they produced, and each
// stage opens a window-in-window drill-in over that content. Everything the
// operator needs to run and read the project lives here.
//
// "Start whole pipeline" fires the research half as REAL jobs (createJob bypasses
// the suggest-gate → runs server-side now) then materializes the listing queue
// (daily-listings tick). Steps whose spec needs creds surface as "needs you"
// instead of running — shown honestly on each stage card.
// ────────────────────────────────────────────────────────────────────────────

type StageKey = "trend" | "keyword" | "sku" | "find" | "listing";

type Stage = {
  key: StageKey;
  n: number;
  label: string;
  blurb: string;
  // Job spec that runs this stage (null = manual/derived, no single auto job).
  spec: string | null;
  href: string;
};

const STAGES: Stage[] = [
  { key: "trend", n: 1, label: "Trend Research", blurb: "Breakout & seasonal demand signals", spec: "trend-radar", href: "/trends" },
  { key: "keyword", n: 2, label: "Keyword Research", blurb: "Head keywords + sub-keywords past the 10k gate", spec: "keyword-discovery", href: "/keyword-discovery" },
  { key: "sku", n: 3, label: "SKU Plan", blurb: "Scored queue → per-keyword build plan", spec: "candidate-score", href: "/sku-plan" },
  { key: "find", n: 4, label: "Find Products", blurb: "Supplier products located per keyword", spec: null, href: "/product-research" },
  { key: "listing", n: 5, label: "Listing Queue", blurb: "Drafts → live Google-Shopping listings", spec: "daily-listings", href: "/stores" },
];

// The research half fired as real jobs by "Start whole pipeline" (in order). The
// listing queue is materialized separately (daily-listings is a synthetic tick step).
const PIPELINE_JOB_SPECS = ["trend-radar", "keyword-discovery", "candidate-score"];

const n = (v?: number | null) => (v == null ? "—" : new Intl.NumberFormat("en-US").format(v));

function fmtDur(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m ${sec % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

// Server stamps are naive UTC (datetime.now() on a UTC Railway container). Parse as UTC so
// elapsed is correct regardless of the operator's timezone; guard against a non-UTC server
// clock (negative / absurd) by returning null (→ no misleading number, just "running").
function elapsedSec(ts: string | null | undefined, now: number): number | null {
  if (!ts) return null;
  const start = Date.parse(ts.replace(" ", "T") + "Z");
  if (Number.isNaN(start)) return null;
  const sec = Math.floor((now - start) / 1000);
  return sec >= 0 && sec <= 4 * 3600 ? sec : null;
}

const eur = (v?: number | null) =>
  v == null ? "—" : "€" + new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(v);

function statusTone(s: JobStatus | "idle" | "queued" | "running"): {
  color: string;
  label: string;
} {
  switch (s) {
    case "running":
      return { color: "var(--state-testing)", label: "running" };
    case "queued":
      return { color: "var(--state-testing)", label: "queued" };
    case "done":
      return { color: "var(--state-winner)", label: "done" };
    case "failed":
      return { color: "var(--state-killed)", label: "failed" };
    case "needs-operator":
      return { color: "var(--state-drafted)", label: "needs you" };
    default:
      return { color: "var(--muted)", label: "idle" };
  }
}

export default function DashboardPage() {
  const { store, label } = useStore();
  const router = useRouter();

  // Content readers (operation-wide; store-scoped rows are filtered client-side).
  const overview = useApi(() => api.overview());
  const trends = useApi(() => api.trends());
  const keywords = useApi(() => api.keywordDiscovery());
  const skuplan = useApi(() => api.skuPlan());
  const plan = useApi(() => (store ? api.listingPlan({ store }) : Promise.resolve(null)), [store]);

  // Live worker + jobs — polled, so stage status updates while the pipeline runs.
  const [worker, setWorker] = useState<WorkerStatus | null>(null);
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  useEffect(() => {
    let alive = true;
    const poll = () => {
      api.worker().then((d) => alive && setWorker(d)).catch(() => {});
      api.jobs(60, store || undefined).then((d) => alive && setJobs(d.jobs)).catch(() => {});
    };
    poll();
    const id = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [store]);

  // 1s ticker so the "running · Nm Ns" elapsed timer on active stages advances smoothly.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // Start-pipeline control.
  const [starting, setStarting] = useState(false);
  const [runMsg, setRunMsg] = useState<{ tone: "ok" | "err"; text: string } | null>(null);

  const runningSpecs = useMemo(
    () => new Set([...(worker?.running ?? []), ...(worker?.queued ?? [])].map((j) => j.spec)),
    [worker],
  );
  const lastBySpec = useMemo(() => {
    const m = new Map<string, JobRecord>();
    for (const j of jobs) if (!m.has(j.spec)) m.set(j.spec, j); // jobs are newest-first
    return m;
  }, [jobs]);

  const pipelineBusy = PIPELINE_JOB_SPECS.some((s) => runningSpecs.has(s));

  async function startPipeline() {
    if (!store) {
      setRunMsg({ tone: "err", text: "Pick a store first (top-left)." });
      return;
    }
    setStarting(true);
    setRunMsg(null);
    try {
      // Research half → real jobs (createJob runs server-side, bypassing the suggest gate).
      for (const spec of PIPELINE_JOB_SPECS) {
        await api.createJob(spec, store);
      }
      // Listing queue → materialize the store's gameplan (synthetic tick step, forced).
      await api.workerTick(store, ["daily-listings"], true);
      setRunMsg({
        tone: "ok",
        text: `Pipeline started for ${label(store)} — trend + keyword research running, candidates scoring, listing queue materializing. Steps needing creds show as "needs you".`,
      });
      overview.reload();
      trends.reload();
      keywords.reload();
      skuplan.reload();
      plan.reload();
    } catch (e) {
      setRunMsg({ tone: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setStarting(false);
    }
  }

  async function runStage(stage: Stage) {
    if (!store) return;
    try {
      if (stage.spec === "daily-listings") {
        await api.workerTick(store, ["daily-listings"], true);
      } else if (stage.spec) {
        await api.createJob(stage.spec, store);
      } else {
        router.push(stage.href);
        return;
      }
      setRunMsg({ tone: "ok", text: `${stage.label} started for ${label(store)}.` });
      api.jobs(60, store).then((d) => setJobs(d.jobs)).catch(() => {});
    } catch (e) {
      setRunMsg({ tone: "err", text: e instanceof Error ? e.message : String(e) });
    }
  }

  // ── Store-scoped content slices ──────────────────────────────────────────
  const storeCandidates = useMemo<KeywordCandidate[]>(() => {
    const all = keywords.data?.candidates ?? [];
    if (!store) return all;
    return all.filter((c) => c.store === store);
  }, [keywords.data, store]);

  const storeHeads = useMemo<SkuPlanHead[]>(() => {
    const all = skuplan.data?.heads ?? [];
    if (!store) return all;
    return all.filter((h) => h.store === store);
  }, [skuplan.data, store]);

  const trendRows = trends.data?.trends ?? [];
  const foundTotal = useMemo(
    () => (plan.data?.schedule ?? []).reduce((a, it) => a + (it.found_products ?? 0), 0),
    [plan.data],
  );
  // Daily-listing plan rollups (the Listing Queue card + modal). `scheduled` is what the modal
  // headlines ("N listings scheduled"); the card must match it instead of skus_by_state.live
  // (the queue STATE, which is 0 until a build actually drafts SKUs — the "shows 0" the operator hit).
  const scheduledTotal = plan.data?.totals?.scheduled ?? 0;
  const planPerDay = plan.data?.params?.per_day ?? 0;
  const planKeywords = useMemo(
    () => new Set((plan.data?.schedule ?? [])
      .map((it) => (it.keyword ?? "").trim().toLowerCase())
      .filter(Boolean)).size,
    [plan.data],
  );

  // Per-stage summary numbers.
  const summaries: Record<StageKey, { value: string; hint: string }> = {
    trend: {
      value: n(trends.data?.totals.trends),
      hint: `${n(trends.data?.totals.rising)} rising · ${n(trends.data?.totals.breakout)} breakout`,
    },
    keyword: {
      value: n(storeCandidates.length),
      hint: `${n(storeCandidates.filter((c) => c.gate === "PASS" || (c.gate ?? "").toUpperCase().includes("PASS")).length)} gate-cleared · ${n(storeCandidates.reduce((a, c) => a + (c.n_segments ?? 0), 0))} sub-keywords`,
    },
    sku: {
      // Reflect the REAL plan the SKU-plan modal shows ("50 to build"), not counts.build (segments
      // to build as separate SKUs, often 0 when they're all title-combines) — that mismatch made the
      // card read "0 SKUs planned to build" while the plan clearly had 50.
      value: n(storeHeads.length),
      hint: `${n(storeHeads.reduce((a, h) => a + (h.products_per_build ?? 0), 0))} products to build · ${n(storeHeads.reduce((a, h) => a + (h.segments?.length ?? 0), 0))} sub-keywords`,
    },
    find: {
      value: n(foundTotal),
      hint: foundTotal > 0
        ? "products located · open to inspect"
        : `${n(planKeywords)} keywords queued · open to inspect`,
    },
    // Store selected → show the daily-listing plan (what the modal shows). No store (TOTAL) →
    // fall back to the cross-store queue-state aggregate.
    listing: plan.data
      ? {
          value: n(scheduledTotal),
          hint: `${n(planKeywords)} keywords · ${n(planPerDay)}/day`,
        }
      : {
          value: n(overview.data?.totals.skus_by_state.live),
          hint: `${n(overview.data?.totals.skus_by_state.drafted)} drafted · ${n(overview.data?.totals.skus)} total`,
        },
  };

  const [openStage, setOpenStage] = useState<StageKey | null>(null);

  const err = overview.error || trends.error || keywords.error || skuplan.error;

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="Dashboard"
        subtitle={`Live state of the ${store ? label(store) : "Google Stores"} pipeline — research through listing.`}
        action={
          <button
            onClick={startPipeline}
            disabled={starting || pipelineBusy || !store}
            className="inline-flex items-center gap-2 rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-[var(--accent-fg)] transition-opacity hover:opacity-90 disabled:opacity-60"
          >
            {starting || pipelineBusy ? (
              <>
                <Spinner /> Pipeline running…
              </>
            ) : (
              <>
                <PlayIcon /> Start whole pipeline
              </>
            )}
          </button>
        }
      />

      {runMsg ? (
        <div
          className={`mb-4 rounded-lg border px-4 py-2.5 text-sm ${
            runMsg.tone === "ok"
              ? "border-[color-mix(in_srgb,var(--accent)_40%,var(--border))] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] text-[var(--text)]"
              : "border-[color-mix(in_srgb,var(--state-killed)_40%,var(--border))] bg-[color-mix(in_srgb,var(--state-killed)_10%,transparent)] text-[var(--text)]"
          }`}
        >
          {runMsg.text}
        </div>
      ) : null}

      {err ? <ErrorState message={err} /> : null}

      {/* Pipeline spine — the five stages, each a live status + real content + drill-in. */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {STAGES.map((stage) => {
          const s = summaries[stage.key];
          const runningJob = stage.spec ? worker?.running.find((j) => j.spec === stage.spec) : undefined;
          const isQueued = stage.spec ? (worker?.queued.some((j) => j.spec === stage.spec) ?? false) : false;
          const lastJob = stage.spec ? lastBySpec.get(stage.spec) : undefined;
          const live: JobStatus | "idle" | "queued" | "running" = runningJob
            ? "running"
            : isQueued
              ? "queued"
              : (lastJob?.status ?? "idle");
          const tone = statusTone(live);
          const spin = live === "running";
          const active = !!runningJob || isQueued;
          const elapsed = runningJob ? elapsedSec(runningJob.ts, now) : null;
          const failed = live === "failed";
          const failReason = failed ? (lastJob?.detail || lastJob?.title || "job failed") : null;
          return (
            <Card
              key={stage.key}
              className="flex flex-col p-4 transition-colors hover:border-[var(--accent)]"
              {...(failed ? { style: { borderColor: "color-mix(in srgb, var(--state-killed) 45%, var(--border))" } } : {})}
            >
              <div className="flex items-center justify-between">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-[var(--surface-2)] text-xs font-bold tabular-nums text-[var(--muted)]">
                  {stage.n}
                </span>
                <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold" title={failReason ?? undefined}>
                  <span
                    className="h-1.5 w-1.5 rounded-full"
                    style={{
                      background: tone.color,
                      boxShadow: spin ? `0 0 0 3px color-mix(in srgb, ${tone.color} 25%, transparent)` : "none",
                    }}
                  />
                  <span style={{ color: tone.color }}>
                    {stage.spec ? tone.label : "manual"}
                    {spin && elapsed != null ? ` · ${fmtDur(elapsed)}` : ""}
                  </span>
                </span>
              </div>

              <div className="mt-2.5 text-sm font-semibold">{stage.label}</div>
              <div className="mt-2 text-2xl font-bold tracking-tight tabular-nums">{s.value}</div>
              <div className="mt-0.5 text-[11px] leading-tight text-[var(--muted)]">{s.hint}</div>
              {failReason ? (
                <div className="mt-1 line-clamp-2 text-[11px] leading-tight text-[var(--state-killed)]" title={failReason}>
                  {failReason}
                </div>
              ) : (
                <div className="mt-1 text-[11px] leading-tight text-[var(--muted)]">{stage.blurb}</div>
              )}

              <div className="mt-3 flex items-center gap-1.5 border-t border-[var(--border)] pt-2.5">
                <button
                  onClick={() => setOpenStage(stage.key)}
                  className="flex-1 rounded-md bg-[var(--surface-2)] px-2 py-1.5 text-xs font-semibold text-[var(--text)] transition-colors hover:bg-[var(--border)]"
                >
                  View
                </button>
                {stage.spec ? (
                  <button
                    onClick={() => runStage(stage)}
                    disabled={!store || active}
                    title={active ? `${stage.label} is already ${live}` : failed ? `Retry ${stage.label}` : `Run ${stage.label} now`}
                    className="rounded-md border border-[var(--border)] px-2 py-1.5 text-xs font-semibold text-[var(--muted)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)] disabled:opacity-50"
                  >
                    {failed ? "Retry" : "Run"}
                  </button>
                ) : null}
                <Link
                  href={stage.href}
                  title={`Open ${stage.label}`}
                  className="rounded-md border border-[var(--border)] px-2 py-1.5 text-xs font-semibold text-[var(--muted)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
                >
                  ↗
                </Link>
              </div>
            </Card>
          );
        })}
      </div>

      {/* Needs-you nudge */}
      {worker && worker.counts.needs_operator + worker.counts.pending_decisions > 0 ? (
        <Link
          href="/decisions"
          className="mt-4 flex items-center justify-between rounded-lg border border-[color-mix(in_srgb,var(--state-drafted)_40%,var(--border))] bg-[color-mix(in_srgb,var(--state-drafted)_10%,transparent)] px-4 py-3 text-sm transition-colors hover:bg-[color-mix(in_srgb,var(--state-drafted)_16%,transparent)]"
        >
          <span className="font-semibold">
            {worker.counts.needs_operator + worker.counts.pending_decisions} step
            {worker.counts.needs_operator + worker.counts.pending_decisions === 1 ? "" : "s"} need you
          </span>
          <span className="text-[var(--muted)]">Open Decisions →</span>
        </Link>
      ) : null}

      {/* Pipeline overview — the waterfall of what each stage has produced, top to bottom, so the
          operator sees the whole flow (trend → keyword → SKU → find → listing) and where it stalls.
          Each row opens that stage's drill-in; the top cards are the quick status, this is the story. */}
      <Card className="mt-6 overflow-hidden">
        <div className="flex items-center justify-between border-b border-[var(--border)] px-5 py-4">
          <h2 className="font-semibold">Pipeline overview</h2>
          <span className="text-xs text-[var(--muted)]">
            {store ? label(store) : "all stores"} · research → listing
          </span>
        </div>
        <ol className="p-3">
          {STAGES.map((stage, i) => {
            const s = summaries[stage.key];
            const runningJob = stage.spec ? worker?.running.find((j) => j.spec === stage.spec) : undefined;
            const isQueued = stage.spec ? (worker?.queued.some((j) => j.spec === stage.spec) ?? false) : false;
            const lastJob = stage.spec ? lastBySpec.get(stage.spec) : undefined;
            const live: JobStatus | "idle" | "queued" | "running" = runningJob
              ? "running"
              : isQueued
                ? "queued"
                : (lastJob?.status ?? "idle");
            const tone = statusTone(live);
            const isLast = i === STAGES.length - 1;
            return (
              <li key={stage.key} className="flex gap-3">
                {/* number + connecting spine */}
                <div className="flex flex-col items-center">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--surface-2)] text-xs font-bold tabular-nums text-[var(--muted)]">
                    {stage.n}
                  </span>
                  {!isLast ? <span className="my-1 w-px flex-1 bg-[var(--border)]" /> : null}
                </div>
                <button
                  onClick={() => setOpenStage(stage.key)}
                  className="mb-1 flex flex-1 items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-left transition-colors hover:bg-[var(--surface-2)]"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-semibold">{stage.label}</span>
                      <span className="inline-flex items-center gap-1 text-[10px] font-semibold" style={{ color: tone.color }}>
                        <span className="h-1.5 w-1.5 rounded-full" style={{ background: tone.color }} />
                        {stage.spec ? tone.label : "manual"}
                      </span>
                    </div>
                    <div className="mt-0.5 text-[11px] leading-tight text-[var(--muted)]">{s.hint}</div>
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    <span className="text-xl font-bold tabular-nums">{s.value}</span>
                    <span className="text-xs font-semibold text-[var(--accent)]">View →</span>
                  </div>
                </button>
              </li>
            );
          })}
        </ol>
      </Card>

      {/* ── Window-in-window drill-ins ──────────────────────────────────── */}
      <DetailModal
        open={openStage === "trend"}
        title="Trend Research"
        subtitle="Demand signals feeding the pipeline"
        badge={<CountBadge n={trendRows.length} />}
        onClose={() => setOpenStage(null)}
      >
        <TrendList
          rows={trendRows}
          store={store}
          onChange={() => {
            trends.reload();
            keywords.reload();
            skuplan.reload();
          }}
        />
      </DetailModal>

      <DetailModal
        open={openStage === "keyword"}
        title="Keyword Research"
        subtitle={store ? `Gate-cleared keywords for ${label(store)}` : "Gate-cleared keywords"}
        badge={<CountBadge n={storeCandidates.length} />}
        onClose={() => setOpenStage(null)}
      >
        <KeywordList
          rows={storeCandidates}
          onChange={() => {
            keywords.reload();
            skuplan.reload();
          }}
        />
      </DetailModal>

      <DetailModal
        open={openStage === "sku"}
        title="SKU Plan"
        subtitle={store ? `Build plan for ${label(store)}` : "Build plan"}
        badge={<CountBadge n={storeHeads.length} />}
        onClose={() => setOpenStage(null)}
      >
        <SkuList rows={storeHeads} onChange={() => skuplan.reload()} />
      </DetailModal>

      <DetailModal
        open={openStage === "find"}
        title="Find Products"
        subtitle={store ? `Products located per keyword for ${label(store)}` : "Products located per keyword"}
        badge={<CountBadge n={foundTotal} />}
        onClose={() => setOpenStage(null)}
      >
        <FindList store={store} heads={storeHeads} />
      </DetailModal>

      <DetailModal
        open={openStage === "listing"}
        title="Listing Queue"
        subtitle="Daily schedule — which keyword, how many products, what day"
        badge={<CountBadge n={scheduledTotal} />}
        onClose={() => setOpenStage(null)}
      >
        <ListingBreakdown plan={plan.data} />
      </DetailModal>
    </div>
  );
}

// ─────────────────────────────── drill-in bodies ────────────────────────────

function CountBadge({ n: c }: { n: number }) {
  return (
    <span className="rounded-full border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-1 text-xs font-semibold tabular-nums text-[var(--muted)]">
      {new Intl.NumberFormat("en-US").format(c)}
    </span>
  );
}

function Row({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3.5 py-2.5">
      {children}
    </div>
  );
}

function TrendList({ rows, store, onChange }: { rows: TrendRow[]; store: string; onChange: () => void }) {
  const sorted = [...rows].sort(
    (a, b) => Number(b.breakout) - Number(a.breakout) || (b.growth_month ?? 0) - (a.growth_month ?? 0),
  );
  if (!sorted.length) return <Empty>No trend signals yet. Run Trend Research to populate.</Empty>;
  return (
    <div className="flex flex-col gap-1.5">
      {sorted.slice(0, 80).map((t, i) => (
        <Row key={`${t.slug}-${t.keyword}-${i}`}>
          <div className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold">{t.keyword ?? "—"}</div>
              <div className="text-[11px] text-[var(--muted)]">
                {t.bucket} · {t.geo ?? "—"} · SV {n(t.sv)}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {t.breakout ? (
                <span className="rounded-md bg-[color-mix(in_srgb,var(--state-winner)_16%,transparent)] px-2 py-0.5 text-[10px] font-bold uppercase text-[var(--state-winner)]">
                  breakout
                </span>
              ) : null}
              {t.growth_month != null ? (
                <span className="text-xs font-semibold tabular-nums text-[var(--muted)]">
                  {t.growth_month > 0 ? "+" : ""}
                  {Math.round(t.growth_month * 100)}% mo
                </span>
              ) : null}
              <PromoteButton store={store} keyword={t.keyword} onDone={onChange} trend />
            </div>
          </div>
        </Row>
      ))}
    </div>
  );
}

function KeywordList({ rows, onChange }: { rows: KeywordCandidate[]; onChange: () => void }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const sorted = [...rows].sort((a, b) => (b.sv ?? 0) - (a.sv ?? 0));
  if (!sorted.length) return <Empty>No gate-cleared keywords yet. Run Keyword Research.</Empty>;
  return (
    <div className="flex flex-col gap-1.5">
      {sorted.map((c, i) => {
        const key = `${c.keyword}-${i}`;
        const isOpen = open.has(key);
        return (
          <Row key={key}>
            <div className="flex items-center justify-between gap-3">
              <button
                onClick={() =>
                  setOpen((prev) => {
                    const nx = new Set(prev);
                    if (nx.has(key)) nx.delete(key);
                    else nx.add(key);
                    return nx;
                  })
                }
                className="flex min-w-0 flex-1 items-center justify-between gap-3 text-left"
              >
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold">{c.keyword ?? "—"}</div>
                  <div className="text-[11px] text-[var(--muted)]">
                    SV {n(c.sv)} · {c.gate ?? "—"} · {n(c.n_segments)} sub-keywords · score {n(c.score)}
                  </div>
                </div>
                <span className="shrink-0 text-xs text-[var(--muted)]">{isOpen ? "▲" : `▾ ${n(c.n_segments)}`}</span>
              </button>
              <button
                type="button"
                onClick={async () => {
                  if (!c.keyword) return;
                  try {
                    await api.removeCandidate(c.store ?? "", c.keyword);
                    onChange();
                  } catch { /* keep the row on failure */ }
                }}
                title="Remove this keyword — take it out of the plan"
                aria-label="Remove keyword"
                className="shrink-0 rounded-md px-1.5 py-1 text-xs font-semibold text-[var(--muted)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--danger)]"
              >
                ✕
              </button>
              <PromoteButton store={c.store} keyword={c.keyword} gate={c.gate} onDone={onChange} />
            </div>
            {isOpen ? (
              <div className="mt-2 flex flex-wrap gap-1.5 border-t border-[var(--border)] pt-2">
                {(c.segments ?? []).length ? (
                  c.segments.map((seg, j) => (
                    <span
                      key={j}
                      className="inline-flex items-center gap-1 rounded-md bg-[var(--surface-2)] px-2 py-0.5 text-[11px]"
                    >
                      <span className="font-medium">{seg.term}</span>
                      <span className="text-[var(--muted)] tabular-nums">SV {n(seg.sv)}</span>
                      {seg.in_catalog ? <span className="text-[var(--state-winner)]">✓</span> : null}
                    </span>
                  ))
                ) : (
                  <span className="text-[11px] text-[var(--muted)]">No sub-keywords expanded yet.</span>
                )}
              </div>
            ) : null}
          </Row>
        );
      })}
    </div>
  );
}

function SkuList({ rows, onChange }: { rows: SkuPlanHead[]; onChange: () => void }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const sorted = [...rows].sort((a, b) => (b.sv ?? 0) - (a.sv ?? 0));
  if (!sorted.length) return <Empty>No SKU plan yet. Score the candidate queue (SKU Plan stage).</Empty>;
  return (
    <div className="flex flex-col gap-1.5">
      {sorted.map((h, i) => {
        const key = `${h.keyword}-${i}`;
        const isOpen = open.has(key);
        return (
          <Row key={key}>
            <div className="flex items-center justify-between gap-3">
              <button
                onClick={() =>
                  setOpen((prev) => {
                    const nx = new Set(prev);
                    if (nx.has(key)) nx.delete(key);
                    else nx.add(key);
                    return nx;
                  })
                }
                className="flex min-w-0 flex-1 items-center justify-between gap-3 text-left"
              >
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold">{h.keyword}</div>
                  <div className="text-[11px] text-[var(--muted)]">
                    SV {n(h.sv)} · {h.volume_tier_label} · {n(h.products_per_build)} to build ·{" "}
                    {n(h.counts?.built)} built
                  </div>
                </div>
                <span className="shrink-0 text-xs text-[var(--muted)]">{isOpen ? "▲" : `▾ ${n(h.segments?.length)}`}</span>
              </button>
              <FindProductsButton store={h.store ?? ""} keyword={h.keyword} onDone={onChange} />
            </div>
            {isOpen ? (
              <div className="mt-2 flex flex-col gap-1 border-t border-[var(--border)] pt-2">
                {(h.segments ?? []).slice(0, 40).map((seg, j) => (
                  <div key={j} className="flex items-center justify-between gap-2 text-[11px]">
                    <span className="truncate">{seg.term ?? "—"}</span>
                    <span className="flex shrink-0 items-center gap-2 text-[var(--muted)]">
                      <span className="tabular-nums">SV {n(seg.sv)}</span>
                      <span className="rounded bg-[var(--surface-2)] px-1.5 py-0.5">{seg.role}</span>
                      <span>{seg.selected}</span>
                      {seg.term ? (
                        <button
                          type="button"
                          onClick={async () => {
                            try {
                              await api.dismissSkuSegment(h.store ?? "", h.keyword, seg.term as string);
                              onChange();
                            } catch { /* keep the row on failure */ }
                          }}
                          title="Remove this sub-keyword from the plan"
                          aria-label="Remove sub-keyword"
                          className="rounded px-1 text-[var(--muted)] transition-colors hover:text-[var(--danger)]"
                        >
                          ✕
                        </button>
                      ) : null}
                    </span>
                  </div>
                ))}
              </div>
            ) : null}
          </Row>
        );
      })}
    </div>
  );
}

function FindList({ store, heads }: { store: string; heads: SkuPlanHead[] }) {
  const sorted = [...heads].sort((a, b) => (b.sv ?? 0) - (a.sv ?? 0));
  if (!sorted.length) return <Empty>No keywords planned yet — plan SKUs first, then products get located per keyword.</Empty>;
  return (
    <div className="flex flex-col gap-1.5">
      {sorted.map((h, i) => (
        <FoundRow key={`${h.keyword}-${i}`} store={store} keyword={h.keyword} />
      ))}
    </div>
  );
}

// Lazy per-keyword found-products loader — the innermost window. Loads only when expanded.
function FoundRow({ store, keyword }: { store: string; keyword: string }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<{ found: number; validated: number; products: LaneProduct[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && !data && !loading && store) {
      setLoading(true);
      setError(null);
      api
        .skuPlanFound(store, keyword)
        .then((d) => setData({ found: d.found, validated: d.validated, products: d.products }))
        .catch((e) => setError(e instanceof Error ? e.message : String(e)))
        .finally(() => setLoading(false));
    }
  }

  return (
    <Row>
      <button onClick={toggle} className="flex w-full items-center justify-between gap-3 text-left">
        <div className="min-w-0 truncate text-sm font-semibold">{keyword}</div>
        <span className="shrink-0 text-xs text-[var(--muted)]">
          {data ? `${n(data.found)} found · ${n(data.validated)} valid` : loading ? "loading…" : "▾ inspect"}
        </span>
      </button>
      {open ? (
        <div className="mt-2 border-t border-[var(--border)] pt-2">
          {error ? (
            <div className="text-[11px] text-[var(--state-killed)]">{error}</div>
          ) : loading ? (
            <div className="text-[11px] text-[var(--muted)]">Loading products…</div>
          ) : data && data.products.length ? (
            <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
              {data.products.slice(0, 24).map((p, j) => (
                <div key={j} className="flex items-center gap-2 rounded-md bg-[var(--surface-2)] p-1.5">
                  {p.image ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={p.image} alt="" className="h-10 w-10 shrink-0 rounded object-cover" />
                  ) : (
                    <div className="h-10 w-10 shrink-0 rounded bg-[var(--border)]" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[11px] font-medium">{p.title ?? "—"}</div>
                    <div className="truncate text-[10px] text-[var(--muted)]">
                      {eur(p.price)}
                      {p.store_name ? ` · ${p.store_name}` : ""}
                      {p.cogs != null ? ` · cogs ${eur(p.cogs)}` : ""}
                      {p.orders != null ? ` · ${n(p.orders)} ord` : ""}
                      {p.validated ? " · ✓" : ""}
                    </div>
                  </div>
                  {p.url ? (
                    <a
                      href={p.url}
                      target="_blank"
                      rel="noreferrer"
                      className="shrink-0 text-[10px] text-[var(--accent)]"
                    >
                      open
                    </a>
                  ) : null}
                  <button
                    type="button"
                    onClick={async () => {
                      try {
                        await api.dismissFoundProduct(store, keyword, p.title ?? p.url ?? "");
                        const r = await api.skuPlanFound(store, keyword);
                        setData({ found: r.found, validated: r.validated, products: r.products });
                      } catch { /* keep the card on failure */ }
                    }}
                    title="Remove — not a competitor to copy"
                    aria-label="Remove product"
                    className="shrink-0 rounded px-1 text-[10px] text-[var(--muted)] transition-colors hover:text-[var(--danger)]"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-[11px] text-[var(--muted)]">
              No products located yet for this keyword. Run Find Products (Winning Products / Marketplace) to populate.
            </div>
          )}
          {/* Final step: send this keyword to the daily listing queue with the General dropship
              template, at a chosen daily rate. No product needs to be located first — the daily
              build runs on the store's promoted categories. */}
          <DailyQueueBar store={store} keyword={keyword} />
        </div>
      ) : null}
    </Row>
  );
}

// Daily listing schedule as an OVERVIEW: one collapsible row per day showing how many listings
// + how many keywords, expanding to WHICH keyword × HOW MANY products that day. Keyword-level,
// not per-product (the Listing Queue tab owns per-product detail) — the granularity the operator
// asked for. Reads the daily listing plan (per_day × window); each item carries its keyword.
function ListingBreakdown({ plan }: { plan: ListingPlan | null }) {
  const days = useMemo(
    () => (plan?.days ?? []).filter((d) => (d.items?.length ?? 0) > 0),
    [plan],
  );
  // Group each day's items by keyword → count. Sorted heaviest-first so the day's main
  // categories read at a glance.
  const byDay = useMemo(
    () =>
      days.map((d) => {
        const counts = new Map<string, number>();
        for (const it of d.items ?? []) {
          const kw = (it.keyword ?? "—").trim() || "—";
          counts.set(kw, (counts.get(kw) ?? 0) + 1);
        }
        const keywords = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
        return { date: d.date, weekday: d.weekday, total: d.items?.length ?? 0, keywords };
      }),
    [days],
  );
  // First day open by default (plan is already loaded when this modal mounts — DetailModal
  // renders children only while open).
  const [open, setOpen] = useState<Set<string>>(() => new Set(days.length ? [days[0].date] : []));

  if (!plan) return <Empty>No listing schedule yet.</Empty>;
  if (days.length === 0) return <Empty>Nothing scheduled — promote keywords and add them to the daily queue.</Empty>;

  const total = plan.totals?.scheduled ?? byDay.reduce((a, d) => a + d.total, 0);
  const perDay = plan.params?.per_day ?? 0;

  function toggle(date: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(date)) next.delete(date);
      else next.add(date);
      return next;
    });
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between rounded-lg bg-[var(--surface)] px-3.5 py-2.5">
        <span className="text-sm text-[var(--muted)]">
          {perDay}/day{plan.params?.window ? ` · ${plan.params.window}` : ""}
        </span>
        <span className="text-sm font-semibold tabular-nums">{n(total)} listings scheduled</span>
      </div>
      <div className="flex flex-col gap-1.5">
        {byDay.map((d) => {
          const isOpen = open.has(d.date);
          return (
            <div key={d.date} className="rounded-lg border border-[var(--border)] bg-[var(--surface)]">
              <button
                type="button"
                onClick={() => toggle(d.date)}
                className="flex w-full items-center justify-between gap-3 px-3.5 py-2.5 text-left"
              >
                <div className="flex flex-col">
                  <span className="text-sm font-medium">{d.weekday}</span>
                  <span className="text-[11px] text-[var(--muted)] tabular-nums">
                    {d.date} · {n(d.keywords.length)} keyword{d.keywords.length === 1 ? "" : "s"}
                  </span>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <span className="text-lg font-bold tabular-nums">{n(d.total)}</span>
                  <span className="text-xs text-[var(--muted)]">{isOpen ? "▴" : "▾"}</span>
                </div>
              </button>
              {isOpen ? (
                <div className="flex flex-col gap-1 border-t border-[var(--border)] px-3.5 py-2">
                  {d.keywords.map(([kw, count]) => (
                    <div key={kw} className="flex items-center justify-between gap-3">
                      <span className="min-w-0 truncate text-[13px]">{kw}</span>
                      <span className="shrink-0 text-xs font-semibold tabular-nums text-[var(--muted)]">
                        {n(count)} product{count === 1 ? "" : "s"}
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─────────────────────────── pipeline action buttons ────────────────────────
// These turn the read-only drill-in lists into the running pipeline: Promote (stage 2 → 3),
// Find products (stage 3 → 4), and Add to daily queue (stage 4 → 5). Each is a small self-
// contained button with idle → busy → done state, reusing the exact control endpoints the
// dedicated Keyword/SKU-plan pages already use.

type ActionState = "idle" | "busy" | "done" | "err";

// Stage 2 → 3: promote a gate-cleared keyword into the SKU plan (adds the store category and
// auto-chains the SKU plan + product-find). Same write as the Keyword Research page's Promote.
function PromoteButton({
  store,
  keyword,
  gate,
  onDone,
  trend = false,
}: {
  store: string;
  keyword: string | null;
  gate?: string | null;
  onDone: () => void;
  // Trend rows aren't gate-cleared candidates yet — promoteTrend ingests them as a gated candidate
  // first, then chains the pipeline. So skip the gate check + use the trend endpoint.
  trend?: boolean;
}) {
  const [state, setState] = useState<ActionState>("idle");
  const [msg, setMsg] = useState<string | null>(null);
  if (!keyword) return null;
  if (!trend && !(gate ?? "").toUpperCase().includes("PASS"))
    return (
      <span className="shrink-0 text-[10px] font-semibold text-[var(--muted)]" title="Not gate-cleared">
        {gate ?? "—"}
      </span>
    );
  if (state === "done")
    return (
      <span
        className="shrink-0 rounded-lg px-2.5 py-1 text-[11px] font-semibold"
        style={{ color: "var(--state-winner)", background: "color-mix(in srgb, var(--state-winner) 14%, transparent)" }}
      >
        ✓ In SKU plan
      </span>
    );
  return (
    <button
      type="button"
      onClick={async () => {
        setState("busy");
        setMsg(null);
        try {
          await (trend ? api.promoteTrend(store, keyword) : api.promoteCandidate(store, keyword));
          setState("done");
          onDone();
        } catch (e) {
          setState("err");
          setMsg(e instanceof Error ? e.message : String(e));
        }
      }}
      disabled={state === "busy"}
      title={msg ?? "Promote into the SKU plan — auto-plans SKUs and starts finding products"}
      className="shrink-0 rounded-lg px-2.5 py-1 text-[11px] font-semibold disabled:opacity-60"
      style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
    >
      {state === "busy" ? "Promoting…" : state === "err" ? "Retry" : "Promote →"}
    </button>
  );
}

// Stage 3 → 4: run the product finder for one planned keyword (a background job that locates real
// supplier products across the weighted source mix). Same write as the SKU-plan page's Find products.
function FindProductsButton({ store, keyword, onDone }: { store: string; keyword: string; onDone: () => void }) {
  const [state, setState] = useState<"idle" | "scanning" | "done" | "err">("idle");
  const [msg, setMsg] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  async function run() {
    if (!store) return;
    setState("scanning");
    setMsg(null);
    try {
      await api.skuPlanResearch(store, keyword);
      onDone(); // the scan job now shows running on the stage card — refresh immediately
      // The scan runs ~30-60s server-side; poll the found count so the button reflects real progress
      // instead of silently "starting". Guarded so it stops if the modal closes.
      for (let i = 0; i < 12 && alive.current; i++) {
        await new Promise((r) => setTimeout(r, 6000));
        if (!alive.current) return;
        try {
          const r = await api.skuPlanFound(store, keyword);
          if (!alive.current) return;
          if (r.found > 0) {
            setState("done");
            setMsg(`${r.found} found`);
            onDone();
            return;
          }
        } catch { /* keep scanning */ }
      }
      if (alive.current) {
        setState("done");
        setMsg("still scanning");
        onDone();
      }
    } catch (e) {
      if (alive.current) {
        setState("err");
        setMsg(e instanceof Error ? e.message : String(e));
      }
    }
  }

  if (state === "scanning")
    return (
      <span
        className="inline-flex shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-1 text-[11px] font-semibold"
        style={{ color: "var(--state-testing)", background: "color-mix(in srgb, var(--state-testing) 14%, transparent)" }}
      >
        <Spinner /> Scanning Google Shopping…
      </span>
    );
  if (state === "done")
    return (
      <span
        className="shrink-0 rounded-lg px-2.5 py-1 text-[11px] font-semibold"
        style={{ color: "var(--state-winner)", background: "color-mix(in srgb, var(--state-winner) 14%, transparent)" }}
        title={msg ?? undefined}
      >
        ✓ {msg ?? "Finding…"}
      </span>
    );
  return (
    <button
      type="button"
      onClick={run}
      disabled={!store}
      title={msg ?? "Find real supplier products for this keyword (runs in the background)"}
      className="shrink-0 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-1 text-[11px] font-semibold text-[var(--text)] hover:bg-[var(--surface)] disabled:opacity-60"
    >
      {state === "err" ? "Retry" : "Find products →"}
    </button>
  );
}

// Stage 4 → 5: send a keyword to the DAILY listing queue with the General dropship template, at a
// chosen per-day rate. It (a) makes sure the keyword is a store category, (b) sets the store's
// default gameplan to the General template @ N/day, and (c) materializes today's drafts. The daily
// build then keeps drafting N General-template listings a day — draft-first, never auto-published.
function DailyQueueBar({ store, keyword }: { store: string; keyword: string }) {
  const [perDay, setPerDay] = useState(50); // standard daily cadence (high-SKU breadth); max 50
  const [state, setState] = useState<ActionState>("idle");
  const [msg, setMsg] = useState<string | null>(null);

  async function add() {
    if (!store) {
      setState("err");
      setMsg("Pick a store first (top-left).");
      return;
    }
    setState("busy");
    setMsg(null);
    try {
      await api.promoteCandidate(store, keyword).catch(() => {}); // idempotent; ignore "already a category"
      await api.createGameplan({
        store,
        name: `Daily · ${keyword}`,
        config: { window: "month", per_day: perDay, weights: {}, method: "source-import", compare_at: true },
        is_default: true,
      });
      await api.workerTick(store, ["daily-listings"], true);
      setState("done");
    } catch (e) {
      setState("err");
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="mt-2.5 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-2.5">
      {state === "done" ? (
        <div className="text-[11px] font-semibold text-[var(--state-live)]">
          ✓ In the daily listing queue — building {perDay} General-template draft{perDay === 1 ? "" : "s"}/day (draft-first).
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] font-semibold text-[var(--muted)]">Daily listing queue</span>
          <span
            className="rounded-md bg-[var(--surface)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--muted)]"
            title="Re-skins a supplier product onto your store — keyword title, template description, brand images, price-to-margin — saved as a draft. The classic Google-Shopping dropship listing."
          >
            General dropship template
          </span>
          <label className="flex items-center gap-1 text-[11px] text-[var(--muted)]">
            <input
              type="number"
              min={1}
              max={50}
              value={perDay}
              onChange={(e) => setPerDay(Math.max(1, Math.min(50, Number(e.target.value) || 1)))}
              className="w-14 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-right text-[11px] tabular-nums text-[var(--text)]"
            />
            per day
          </label>
          <button
            type="button"
            onClick={add}
            disabled={state === "busy"}
            className="rounded-lg px-2.5 py-1 text-[11px] font-semibold disabled:opacity-60"
            style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
          >
            {state === "busy" ? "Adding…" : "Add to daily queue"}
          </button>
          {state === "err" ? <span className="text-[11px] text-[var(--state-killed)]">{msg}</span> : null}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────── icons ─────────────────────────────────────

function PlayIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

function Spinner() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden className="animate-spin">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" opacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}
