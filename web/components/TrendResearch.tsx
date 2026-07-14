"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, type TrendRow, type TrendBucket, type TrendHorizon, type TrendMonth, type TrendRelatedQueries, type NewsSignal, type NewsTimelinePoint, type CalendarEvent, type EventHorizon } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Pill } from "@/components/ui";
import { Loading, ErrorState, Empty } from "@/components/PageState";
import { FindProductsModal } from "@/components/FindProductsModal";
import { useStore } from "@/components/StoreProvider";

const BUCKET_COLOR: Record<TrendBucket, string> = {
  rising: "var(--state-winner)",
  seasonal: "var(--state-testing)",
  evergreen: "var(--state-live)",
  declining: "var(--state-killed)",
  other: "var(--muted)",
};

const MONTHS = ["—", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// "2026-05-31" → "May 31, 2026" — the exact date the trend was last measured.
// "2026-05-17" → "17 May" — the day this row's data was last captured (the run date).
function runDate(iso: string | null): string | null {
  const m = iso ? /^(\d{4})-(\d{2})-(\d{2})/.exec(iso) : null;
  if (!m) return null;
  return `${Number(m[3])} ${MONTHS[Number(m[2])] ?? "—"}`;
}

// Year boundary ticks for the monthly chart's x-axis. The Trends series spans ~5 years,
// so instead of just the first/last month we mark each YEAR at the x-position of its first
// datapoint — e.g. ’21 ’22 ’23 ’24 ’25 ’26 spread across the axis at their true offsets.
// `pct` is the 0–100 horizontal position (same i/(n-1) mapping the chart uses).
function yearTicks(monthly: TrendMonth[]): { label: string; pct: number }[] {
  if (monthly.length < 2) return [];
  const n = monthly.length;
  const seen = new Set<string>();
  const ticks: { label: string; pct: number }[] = [];
  monthly.forEach((pt, i) => {
    const mm = /^(\d{4})-\d{2}/.exec(pt.m ?? "");
    if (!mm || seen.has(mm[1])) return;
    seen.add(mm[1]);
    ticks.push({ label: `\u2019${mm[1].slice(2)}`, pct: (i / (n - 1)) * 100 });
  });
  return ticks;
}

function BucketBadge({ bucket }: { bucket: TrendBucket }) {
  const color = BUCKET_COLOR[bucket];
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-semibold capitalize"
      style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {bucket}
    </span>
  );
}

// Monthly area+line chart — cleaner and more honest than the noisy weekly series.
// Falls back to the raw weekly series only if monthly aggregation is unavailable.
function MonthlyChart({ monthly, raw, color }: { monthly: TrendMonth[]; raw: number[]; color: string }) {
  const values = monthly.length >= 2 ? monthly.map((m) => m.v) : raw;
  if (values.length < 2) return <span className="text-xs text-[var(--muted)]">—</span>;
  const w = 100;
  const h = 36;
  const max = Math.max(...values, 1);
  const min = Math.min(...values);
  const span = max - min || 1;
  const xy = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - ((v - min) / span) * h;
    return [x, y] as const;
  });
  const line = xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `0,${h} ${line} ${w},${h}`;
  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="block">
      <polygon points={area} fill={color} opacity={0.1} />
      <polyline points={line} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// 22200 → "22.2k", 110000 → "110k" — compact search-volume label.
function fmtSV(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1_000)}k`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

// "2026-05" → "May" (month-only label for the volume strip).
function shortMonth(ym: string): string {
  const m = /-(\d{2})$/.exec(ym);
  return m ? MONTHS[Number(m[1])] ?? ym : ym;
}

// Build the planning window the operator asked for: previous month, current month,
// then the next 2 — anchored to TODAY, not to whenever the scrape happened. Each cell's
// volume is filled from the keyword's historical series for that *calendar* month, using
// the most recent year available (seasonal read). A cell is flagged `est` when there's no
// fresh actual for it yet (the series ends before that month) — honest "this is last
// year's same-month demand", so the months are never "way behind" again.
function planningWindow(
  series: { m: string; v: number | null }[],
): { m: string; v: number | null; est: boolean }[] {
  const byMonth = new Map<number, number | null>(); // calendar month (1-12) → most recent year's value
  for (const c of series) {
    const mm = Number(c.m.slice(-2));
    if (Number.isFinite(mm)) byMonth.set(mm, c.v); // series sorted ascending → last write = newest year
  }
  const latestKey = series.length ? series[series.length - 1].m : null;
  const now = new Date();
  const out: { m: string; v: number | null; est: boolean }[] = [];
  for (let off = -1; off <= 2; off++) {
    const d = new Date(now.getFullYear(), now.getMonth() + off, 1);
    const ym = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    const mm = d.getMonth() + 1;
    out.push({
      m: ym,
      v: byMonth.has(mm) ? byMonth.get(mm)! : null,
      est: latestKey == null ? true : ym > latestKey,
    });
  }
  return out;
}

// Monthly search-volume strip — the planning window (prev → current → +2 months),
// anchored to today. Volumes are real DataForSEO numbers, pulled seasonally per calendar
// month; cells with no fresh actual yet are marked "~" (last year's same-month demand).
// Falls back to the relative ×-momentum windows when no SV series is joined.
function MonthlyVolume({ row }: { row: TrendRow }) {
  const series = row.monthly_sv ?? [];
  if (series.length >= 2) {
    const win = planningWindow(series);
    const anyEst = win.some((c) => c.est);
    return (
      <div>
        <div className="mb-1 flex items-center justify-between text-[10px] uppercase tracking-wide text-[var(--muted)]">
          <span>Monthly search volume · planning window</span>
          {anyEst ? <span className="normal-case opacity-80">~ = seasonal (last yr)</span> : null}
        </div>
        <div className="grid grid-cols-4 gap-1.5">
          {win.map((c, i) => {
            const prev = i > 0 ? win[i - 1].v : null;
            const up = prev != null && c.v != null && c.v > prev;
            const down = prev != null && c.v != null && c.v < prev;
            const tint = up ? "var(--state-live)" : down ? "var(--state-killed)" : "var(--text)";
            return (
              <div
                key={c.m}
                className="rounded-lg border border-[var(--border)] px-1.5 py-1 text-center"
                style={{ background: "var(--surface)" }}
              >
                <div className="text-[10px] text-[var(--muted)]">{shortMonth(c.m)}</div>
                <div className="text-xs font-semibold tabular-nums" style={{ color: tint }}>
                  {c.est && c.v != null ? "~" : ""}{fmtSV(c.v)}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    );
  }
  // Fallback: relative momentum windows (no real SV series joined to this keyword).
  const cells: { label: string; v: number | null }[] = [
    { label: "2 wk", v: row.growth_week },
    { label: "1 mo", v: row.growth_month },
    { label: "1–3 mo", v: row.growth_quarter },
  ];
  return (
    <div className="grid grid-cols-3 gap-1.5">
      {cells.map((c) => {
        const rising = c.v != null && c.v >= 1.05;
        const falling = c.v != null && c.v <= 0.95;
        const tint = rising ? "var(--state-winner)" : falling ? "var(--state-killed)" : "var(--muted)";
        return (
          <div
            key={c.label}
            className="rounded-lg border border-[var(--border)] px-2 py-1 text-center"
            style={{ background: "var(--surface)" }}
          >
            <div className="text-[10px] uppercase tracking-wide text-[var(--muted)]">{c.label}</div>
            <div className="text-sm font-semibold tabular-nums" style={{ color: tint }}>
              {c.v != null ? (
                <>
                  {rising ? "↑" : falling ? "↓" : ""}{c.v.toFixed(2)}×
                </>
              ) : (
                "—"
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// Always-on at-a-glance SV under the keyword: last month → this month, anchored to today.
// Reuses the same seasonal planning window as the strip below; "~" = last-year same-month read
// (no fresh actual yet). Falls back to the keyword's single SV figure when no monthly series
// is joined, so the line is NEVER blank.
function KeywordSvLine({ row }: { row: TrendRow }) {
  const series = row.monthly_sv ?? [];
  let last: { v: number | null; est: boolean } | null = null;
  let cur: { v: number | null; est: boolean } | null = null;
  if (series.length >= 2) {
    const win = planningWindow(series);
    last = { v: win[0].v, est: win[0].est }; // off=-1 → previous (last) calendar month
    cur = { v: win[1].v, est: win[1].est };  // off=0  → current (this) calendar month
  } else if (row.sv != null) {
    cur = { v: row.sv, est: false };
  }
  if (!last && !cur) return null;
  const cell = (c: { v: number | null; est: boolean } | null) => (
    <span className="font-semibold tabular-nums text-[var(--text)]">
      {c ? `${c.est && c.v != null ? "~" : ""}${fmtSV(c.v)}` : "—"}
    </span>
  );
  return (
    <div className="mt-0.5 text-[11px] text-[var(--muted)]">
      <span className="uppercase tracking-wide">SV</span> last mo {cell(last)} · this mo {cell(cur)}
    </div>
  );
}

// The four tabs are rising-momentum horizons: what's surging right now (last 1–2 wk),
// what's risen over the past month, what's been climbing over the quarter, plus "All".
type Filter = "all" | "early" | "week" | "month" | "quarter";

const FILTER_TABS: {
  id: Filter;
  label: string;
  horizon: TrendHorizon | null;
  hint: string;
  blurb: string;
}[] = [
  { id: "all", label: "All keywords", horizon: null, hint: "Every tracked keyword",
    blurb: "Every keyword we track momentum for, sorted by long-run growth." },
  { id: "early", label: "Early signals", horizon: null, hint: "A related search is taking off before the main one does",
    blurb: "The earliest sign — a related search is taking off while the main keyword hasn't moved yet. The earliest, least-crowded way in: list and title for the rising related search now." },
  { id: "week", label: "Rising now", horizon: "week", hint: "Surging in the last 1–2 weeks",
    blurb: "Weekly breakouts — interest spiked in the last 1–2 weeks. Move fast." },
  { id: "month", label: "Rising this month", horizon: "month", hint: "Climbing over the last month",
    blurb: "Keywords that have risen over the past month — building, not yet peaked." },
  { id: "quarter", label: "Rising 1–3 months", horizon: "quarter", hint: "Climbing over the last 1–3 months",
    blurb: "Steady climbers over the last 1–3 months — durable momentum to plan a batch around." },
];

function horizonGrowth(r: TrendRow, h: TrendHorizon | null): number | null {
  if (h === "week") return r.growth_week;
  if (h === "month") return r.growth_month;
  if (h === "quarter") return r.growth_quarter;
  return r.growth_ratio;
}

const HORIZON_COLOR: Record<TrendHorizon, string> = {
  week: "var(--state-winner)",
  month: "var(--state-testing)",
  quarter: "var(--state-live)",
};

function FilterTab({
  tab,
  count,
  active,
  onClick,
}: {
  tab: { id: Filter; label: string; horizon: TrendHorizon | null; hint: string };
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  const color =
    tab.id === "early" ? "var(--state-testing)" : tab.horizon ? HORIZON_COLOR[tab.horizon] : "var(--accent)";
  return (
    <button
      type="button"
      onClick={onClick}
      title={tab.hint}
      className="flex items-center gap-2 rounded-xl border px-3.5 py-2 text-sm font-semibold transition-colors"
      style={{
        borderColor: active ? color : "var(--border)",
        background: active ? `color-mix(in srgb, ${color} 12%, transparent)` : "var(--surface)",
        color: active ? "var(--text)" : "var(--muted)",
      }}
    >
      {tab.horizon || tab.id === "early" ? <span className="h-2 w-2 rounded-full" style={{ background: color }} /> : null}
      {tab.label}
      <span className="tabular-nums" style={{ color: active ? color : "var(--muted)" }}>
        {count}
      </span>
    </button>
  );
}

const HORIZON_GROWTH_LABEL: Record<TrendHorizon, string> = {
  week: "vs last 2 wk",
  month: "vs last month",
  quarter: "vs 1–3 months ago",
};

// A rising-related-query change value → a compact badge label.
// "Breakout" (>5000% surge) is the strongest; otherwise a +N% increase.
function fmtChange(c: number | string | null): { label: string; breakout: boolean } {
  if (typeof c === "string" && /break/i.test(c)) return { label: "⚡ Breakout", breakout: true };
  if (typeof c === "number") return { label: `+${c}%`, breakout: c >= 5000 };
  if (c != null) return { label: String(c), breakout: false };
  return { label: "rising", breakout: false };
}

// Numeric magnitude of a rising-related-query change so values sort/compare uniformly.
// "Breakout" (>5000% surge) collapses to a large sentinel; "+643%" → 643; null → 0.
function changeMagnitude(c: number | string | null): number {
  if (typeof c === "string") return /break/i.test(c) ? 1e6 : parseFloat(c.replace(/[^\d.]/g, "")) || 0;
  if (typeof c === "number") return c;
  return 0;
}

// The single strongest rising sub-query under a keyword (by change magnitude), or null.
function topRising(
  rq: TrendRelatedQueries | undefined,
): { query: string; change: number | string | null; mag: number } | null {
  let best: { query: string; change: number | string | null; mag: number } | null = null;
  for (const r of rq?.rising ?? []) {
    if (!r.query) continue;
    const mag = changeMagnitude(r.change);
    if (!best || mag > best.mag) best = { query: r.query, change: r.change, mag };
  }
  return best;
}

// LEADING-INDICATOR signal (the AC-case early-warning): a rising sub-search is breaking out
// (≥1000% surge) while the HEAD term itself hasn't visibly spiked yet (not breakout-flagged).
// The rising bucket leads head-term SV — "portable" surged before "air conditioner" volume did.
// Returns the surging sub-query to list/title against now, or null.
const LEADING_THRESHOLD = 1000; // % change on a sub-query to count as an ahead-of-head breakout
function leadingSignal(row: TrendRow): { query: string; change: number | string | null } | null {
  const best = topRising(row.related_queries);
  if (!best || best.mag < LEADING_THRESHOLD) return null;
  if (row.breakout) return null; // head already spiking → not a *leading* (ahead-of-head) signal
  return { query: best.query, change: best.change };
}

// The "portable" panel: the related sub-searches surging under this head keyword.
// These are what map into the 5-variant SKU titles — each rising query is itself
// directly researchable (open Find Products seeded on the sub-query), because the
// winning move was to list/title against the rising sub-search, not just the head term.
function RisingSearches({
  rq,
  onResearch,
}: {
  rq: TrendRelatedQueries;
  onResearch: (query: string) => void;
}) {
  const rising = (rq.rising ?? []).filter((r) => r.query).slice(0, 4);
  const top = (rq.top ?? []).filter((r) => r.query).slice(0, 3);
  if (rising.length === 0 && top.length === 0) return null;
  return (
    <div className="rounded-lg border border-[var(--border)] p-2.5" style={{ background: "var(--surface)" }}>
      {rising.length > 0 ? (
        <>
          <div className="mb-1.5 text-[10px] uppercase tracking-wide text-[var(--muted)]">
            Rising related searches · title seeds
          </div>
          <div className="flex flex-wrap gap-1.5">
            {rising.map((r) => {
              const ch = fmtChange(r.change);
              const c = ch.breakout ? "var(--state-winner)" : "var(--state-live)";
              return (
                <button
                  key={r.query}
                  type="button"
                  onClick={() => onResearch(r.query)}
                  title={`Find products for "${r.query}" (${ch.label})`}
                  className="inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium transition-colors hover:border-[var(--accent)]"
                  style={{ borderColor: "var(--border)", color: "var(--text)" }}
                >
                  <span className="truncate max-w-[12rem]">{r.query}</span>
                  <span
                    className="rounded px-1 py-px text-[10px] font-bold"
                    style={{ color: c, background: `color-mix(in srgb, ${c} 16%, transparent)` }}
                  >
                    {ch.label}
                  </span>
                </button>
              );
            })}
          </div>
        </>
      ) : null}
      {top.length > 0 ? (
        <div className={`${rising.length > 0 ? "mt-2 " : ""}text-[11px] text-[var(--muted)]`}>
          <span className="uppercase tracking-wide">Top</span>{" "}
          {top.map((t) => t.query).join(" · ")}
        </div>
      ) : null}
    </div>
  );
}

// ───────────────────────── News Radar (earliest leading signal) ─────────────────────────
// News-volume velocity (GDELT) is one layer EARLIER than the rising-sub-query signal: a
// real-world event spikes news coverage hours-to-days before the public Google search
// breakout. A BREAKOUT here, while search is still flat, is the A-grade pre-list window
// (the air-conditioner heatwave play). Rendered at the top of the Early-signals tab.

const NEWS_STATE: Record<NewsSignal["state"], { label: string; color: string }> = {
  BREAKOUT: { label: "⚡ News breakout", color: "var(--state-winner)" },
  RISING: { label: "🔭 News rising", color: "var(--state-testing)" },
  FLAT: { label: "Flat", color: "var(--muted)" },
  NO_DATA: { label: "No data", color: "var(--muted)" },
};

// "20260621" → "21 Jun"
function newsDate(yyyymmdd: string | null | undefined): string | null {
  const m = yyyymmdd ? /^(\d{4})(\d{2})(\d{2})/.exec(yyyymmdd) : null;
  if (!m) return null;
  return `${Number(m[3])} ${MONTHS[Number(m[2])] ?? "—"}`;
}

// Compact "N min/h/d ago" from a seconds count.
function agoLabel(sec: number | null): string {
  if (sec == null) return "never synced";
  if (sec < 90) return "just now";
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return `${Math.round(sec / 86400)}d ago`;
}

// Tiny article-volume sparkline. The last point (today) is partial — drawn faint.
function NewsSparkline({ timeline, color }: { timeline: NewsTimelinePoint[]; color: string }) {
  const vals = timeline.map((p) => p.value);
  if (vals.length < 2) return <span className="text-xs text-[var(--muted)]">—</span>;
  const w = 100;
  const h = 28;
  const max = Math.max(...vals, 1);
  const xy = vals.map((v, i) => {
    const x = (i / (vals.length - 1)) * w;
    const y = h - (v / max) * h;
    return [x, y] as const;
  });
  const line = xy.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `0,${h} ${line} ${w},${h}`;
  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="block">
      <polygon points={area} fill={color} opacity={0.1} />
      <polyline points={line} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// Tiny per-state emoji for the "also in" market badges.
const NEWS_STATE_EMOJI: Record<NewsSignal["state"], string> = {
  BREAKOUT: "⚡",
  RISING: "🔭",
  FLAT: "",
  NO_DATA: "",
};

function NewsSignalCard({ signal, otherMarkets = [] }: { signal: NewsSignal; otherMarkets?: NewsSignal[] }) {
  const { store } = useStore();
  const [findOpen, setFindOpen] = useState(false);
  const [findKw, setFindKw] = useState("");
  const meta = NEWS_STATE[signal.state] ?? NEWS_STATE.FLAT;
  const openResearch = (kw: string) => {
    setFindKw(kw);
    setFindOpen(true);
  };
  const alert = newsDate(signal.alert_date);
  const peak = newsDate(signal.recent_peak_date ?? signal.peak_date);
  return (
    <Card className="flex flex-col gap-2.5 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold capitalize">{signal.theme}</span>
            {signal.discovered ? (
              <span
                title="Auto-discovered — a new story the radar found via demand triggers, not the seeded watchlist"
                className="inline-flex items-center gap-0.5 rounded-full border px-1.5 py-0.5 text-[10px] font-bold"
                style={{ borderColor: "var(--accent)", color: "var(--accent)" }}
              >
                🔎 Discovered
              </span>
            ) : null}
            {signal.geo ? <Pill>{signal.geo}</Pill> : null}
            {otherMarkets.map((o) => (
              <span
                key={`${o.geo}-${o.state}`}
                title={`Also ${(NEWS_STATE[o.state] ?? NEWS_STATE.FLAT).label.toLowerCase()} in ${o.geo}${o.surge_ratio != null ? ` (${o.surge_ratio}×)` : ""}`}
                className="inline-flex items-center gap-0.5 rounded-full border border-[var(--border)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--muted)]"
              >
                {NEWS_STATE_EMOJI[o.state]}
                {o.geo}
              </span>
            ))}
          </div>
          {alert ? (
            <div className="mt-0.5 text-[11px] text-[var(--muted)]">
              Alert fired {alert}
              {signal.distinct_outlets ? ` · ${signal.distinct_outlets} outlets` : ""}
            </div>
          ) : null}
        </div>
        <span
          className="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold"
          style={{ background: `color-mix(in srgb, ${meta.color} 18%, transparent)`, color: meta.color }}
        >
          {meta.label}
        </span>
      </div>

      <NewsSparkline timeline={signal.timeline ?? []} color={meta.color} />

      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-[var(--muted)]">News surge</div>
          <div className="text-lg font-semibold tabular-nums" style={{ color: meta.color }}>
            {signal.surge_ratio != null ? `${signal.surge_ratio}×` : "—"}
          </div>
        </div>
        <div className="text-right text-[11px] text-[var(--muted)]">
          {signal.baseline_per_day != null ? `${signal.baseline_per_day}/day base` : ""}
          {signal.recent_peak != null ? (
            <div>
              peak {signal.recent_peak}
              {peak ? ` · ${peak}` : ""}
            </div>
          ) : null}
        </div>
      </div>

      {signal.top_headlines && signal.top_headlines.length > 0 ? (
        <div className="rounded-lg border border-[var(--border)] p-2.5" style={{ background: "var(--surface)" }}>
          <div className="mb-1.5 text-[10px] uppercase tracking-wide text-[var(--muted)]">
            Why now · the actual story
          </div>
          <ul className="flex flex-col gap-1.5">
            {signal.top_headlines.slice(0, 3).map((h, i) => {
              const inner = (
                <>
                  <span className="leading-snug">{h.title}</span>
                  {h.outlet ? (
                    <span className="ml-1 whitespace-nowrap text-[10px] text-[var(--muted)]">— {h.outlet}</span>
                  ) : null}
                </>
              );
              return (
                <li key={`${h.url || h.title}-${i}`} className="text-[11px]">
                  {h.url ? (
                    <a
                      href={h.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-[var(--text)] hover:text-[var(--accent)] hover:underline"
                    >
                      {inner}
                    </a>
                  ) : (
                    <span className="text-[var(--text)]">{inner}</span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {signal.product_keywords.length > 0 ? (
        <div className="rounded-lg border border-[var(--border)] p-2.5" style={{ background: "var(--surface)" }}>
          <div className="mb-1.5 text-[10px] uppercase tracking-wide text-[var(--muted)]">
            Drives demand for · research now
          </div>
          <div className="flex flex-wrap gap-1.5">
            {signal.product_keywords.map((kw) => (
              <button
                key={kw}
                type="button"
                onClick={() => openResearch(kw)}
                title={`Find products for "${kw}" — pre-list ahead of the search breakout`}
                className="inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium transition-colors hover:border-[var(--accent)]"
                style={{ borderColor: "var(--border)", color: "var(--text)" }}
              >
                {kw}
              </button>
            ))}
          </div>
        </div>
      ) : signal.discovered && signal.candidate_topics && signal.candidate_topics.length > 0 ? (
        <div className="rounded-lg border border-dashed border-[var(--border)] p-2.5" style={{ background: "var(--surface)" }}>
          <div className="mb-1.5 text-[10px] uppercase tracking-wide text-[var(--muted)]">
            New story · pick what to research
          </div>
          <div className="flex flex-wrap gap-1.5">
            {signal.candidate_topics.map((kw) => (
              <button
                key={kw}
                type="button"
                onClick={() => openResearch(kw)}
                title={`Research "${kw}" — a recurring word in this story's headlines`}
                className="inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium transition-colors hover:border-[var(--accent)]"
                style={{ borderColor: "var(--border)", color: "var(--text)" }}
              >
                {kw}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      <FindProductsModal
        keyword={findKw}
        sv={null}
        source="trend"
        store={store}
        open={findOpen}
        onClose={() => setFindOpen(false)}
      />
    </Card>
  );
}

// Collapse the per-market signals (one per US/UK/AU/CA) into ONE entry per theme.
// The loudest market leads the card (breakouts first, then surge); the other markets
// that are still breaking/rising ride along as compact "also in" badges. This keeps the
// regional read but stops the same theme (heatwave, air conditioning…) repeating 4×.
const _NEWS_STATE_RANK: Record<NewsSignal["state"], number> = { BREAKOUT: 0, RISING: 1, FLAT: 2, NO_DATA: 3 };
function groupNewsByTheme(signals: NewsSignal[]): { primary: NewsSignal; others: NewsSignal[] }[] {
  const byTheme = new Map<string, NewsSignal[]>();
  for (const s of signals) {
    const arr = byTheme.get(s.theme) ?? [];
    arr.push(s);
    byTheme.set(s.theme, arr);
  }
  const rank = (s: NewsSignal) => _NEWS_STATE_RANK[s.state] ?? 9;
  const cmp = (a: NewsSignal, b: NewsSignal) =>
    rank(a) - rank(b) || (b.surge_ratio ?? 0) - (a.surge_ratio ?? 0);
  const groups = Array.from(byTheme.values()).map((arr) => {
    const sorted = [...arr].sort(cmp);
    const primary = sorted[0];
    // Only surface other markets that are themselves breaking/rising — a FLAT market is noise.
    const others = sorted.slice(1).filter((s) => s.state === "BREAKOUT" || s.state === "RISING");
    return { primary, others };
  });
  groups.sort((a, b) => cmp(a.primary, b.primary));
  return groups;
}

// News Radar — sits as a card alongside Trend Radar in "Ways to research trends".
// Collapsed by default; clicking it expands the live dashboard (signals + sync).
// No country picker: every sync scans ALL markets at once (GDELT sourcecountry-backed) and
// each signal card carries its own country badge, so the operator sees the whole world in one read.
// `open`/`onOpenChange` (optional) make it controllable so the trends page can run all the
// research panels as one accordion — opening one closes the others. Uncontrolled elsewhere.
export function NewsRadarCard({
  open: openProp,
  onOpenChange,
}: {
  open?: boolean;
  onOpenChange?: (v: boolean) => void;
} = {}) {
  const { data, error, loading, reload } = useApi(() => api.news());
  const [syncing, setSyncing] = useState(false);
  const [syncErr, setSyncErr] = useState<string | null>(null);
  const [openLocal, setOpenLocal] = useState(false);
  const open = openProp ?? openLocal;
  const setOpen = (v: boolean | ((p: boolean) => boolean)) => {
    const next = typeof v === "function" ? v(open) : v;
    if (onOpenChange) onOpenChange(next);
    else setOpenLocal(next);
  };

  const runSync = async () => {
    setSyncing(true);
    setSyncErr(null);
    try {
      const r = await api.newsSync("ALL"); // scan every market every sync
      if (!r.ok) setSyncErr(r.error ?? "sync failed");
      reload();
    } catch (e) {
      setSyncErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  };

  // ALWAYS show every signal the snapshot found (breakouts first via the backend's sort), not
  // just leading states — the operator wants the full read regardless of breakout/rising. Only
  // NO_DATA (GDELT returned nothing) is dropped, as there is nothing to show.
  const signals = (data?.signals ?? []).filter((s) => s.state !== "NO_DATA");
  // De-duped count: one entry per theme (markets collapse into a single card).
  const themeCount = groupNewsByTheme(signals).length;

  const summary = data?.has_snapshot
    ? `${data.totals.breakout} breakout · ${data.totals.rising} rising · ${themeCount} stories · synced ${agoLabel(
        data.synced_ago_seconds,
      )}${data.geo ? ` · ${data.geo}` : ""}`
    : "No news snapshot yet — open and sync to pull the GDELT velocity read across all markets.";

  // The compact card ALWAYS stays in the method grid (so the Trend Radar card next to it
  // never gets pushed away). Opening News Radar reveals the full dashboard as a separate
  // full-width panel BELOW the grid — it doesn't replace or displace the method cards.
  return (
    <>
      <Card
        className={`flex h-full flex-col p-3 transition-colors ${
          open ? "border-[var(--accent)] ring-1 ring-[var(--ring)]" : "hover:border-[var(--accent)]"
        }`}
      >
        <button onClick={() => setOpen((v) => !v)} className="w-full text-left">
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm font-semibold">News Radar</div>
            <span className="text-[11px] font-semibold text-[var(--muted)]">
              {open ? "Close ›" : "Open ›"}
            </span>
          </div>
          <p className="mt-1 text-[13px] leading-snug text-[var(--muted)]">
            The earliest layer — real-world news leads the search breakout.
          </p>
          <p className="mt-1.5 text-[11px] text-[var(--muted)]">{summary}</p>
        </button>
        {/* Run action on the card face — pull the GDELT read without opening first. */}
        <div className="mt-auto flex flex-wrap items-center gap-2 pt-2.5">
          <button
            type="button"
            onClick={runSync}
            disabled={syncing}
            className="rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-50"
            style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
          >
            {syncing ? "Syncing…" : "Sync now"}
          </button>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="text-xs font-medium text-[var(--muted)] hover:text-[var(--text)]"
          >
            {open ? "Close" : "Open live read"}
          </button>
          {syncErr ? <span className="text-[11px] text-[var(--state-killed)]">{syncErr}</span> : null}
        </div>
      </Card>

      {open ? (
        // Full-width dashboard, rendered below the method grid (both cards stay visible above).
        <div
          className="rounded-2xl border border-[var(--border)] p-4 md:col-span-3 md:order-last"
          style={{ background: "var(--surface)" }}
        >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-[11px] font-semibold text-[var(--muted)] hover:text-[var(--text)]"
              aria-label="Collapse News Radar"
            >
              ‹ Close
            </button>
            <span>News Radar</span>
            <span className="text-[11px] font-normal text-[var(--muted)]">
              the earliest layer — news leads the search breakout
            </span>
          </div>
          <div className="mt-0.5 text-[11px] text-[var(--muted)]">{summary}</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* No country picker — every sync scans ALL markets; each card shows its own country. */}
          <span className="text-[11px] text-[var(--muted)]">Scans all markets · US · UK · AU · CA</span>
          <button
            type="button"
            onClick={runSync}
            disabled={syncing}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-semibold text-[var(--muted)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)] disabled:opacity-50"
          >
            {syncing ? "Syncing all markets…" : "Sync news (all markets)"}
          </button>
        </div>
      </div>

      {syncErr ? (
        <div
          className="mb-3 rounded-lg border px-3 py-2 text-xs"
          style={{ borderColor: "var(--state-killed)", color: "var(--state-killed)" }}
        >
          {syncErr}
        </div>
      ) : null}

      {loading && !data ? (
        <Loading />
      ) : error ? (
        <ErrorState message={error} />
      ) : signals.length === 0 ? (
        <p className="text-sm text-[var(--muted)]">
          No news signals on the watchlist yet — hit “Sync news (all markets)” to pull the GDELT
          read. When a real-world event spikes coverage (a heatwave, a storm, a ban), the theme
          surfaces here before the Google search does.
        </p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {groupNewsByTheme(signals).map(({ primary, others }) => (
            <NewsSignalCard key={primary.theme} signal={primary} otherMarkets={others} />
          ))}
        </div>
      )}
        </div>
      ) : null}
    </>
  );
}

// ───────────────────────── World-events calendar (predictable, dated signal) ─────────────────
// The opposite class from News Radar: News catches UNPREDICTABLE breaking events (you react);
// this is the PREDICTABLE, dated calendar (holidays / seasonal / world events per country) —
// the only signal you can plan a build-ahead batch around with certainty. Computed live (no
// sync/fetch). Each event resolves its next date + days-until + a horizon that matches the
// listing-plan's timing language: "List now" (in the run-up) / "Build ahead" (1–3 mo) / "Upcoming".

const EVENT_HORIZON: Record<EventHorizon, { label: string; color: string }> = {
  now: { label: "List now", color: "var(--state-winner)" },
  build_ahead: { label: "Build ahead", color: "var(--state-testing)" },
  later: { label: "Upcoming", color: "var(--muted)" },
};

// ISO-2 → flag emoji (regional-indicator pair). GLOBAL → globe.
function countryFlag(code: string): string {
  if (!code || code === "GLOBAL") return "🌍";
  const cc = code.toUpperCase();
  if (cc.length !== 2) return "🏳️";
  return String.fromCodePoint(...[...cc].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

// "2026-10-31" → "31 Oct 2026"
function eventDate(iso: string): string {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

// keyword → a listing-queue slug (lowercase, hyphenated, ≤40 chars — the project convention).
function kwSlug(kw: string): string {
  return kw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

type PromoteState = "idle" | "busy" | "done" | "err";

function EventCard({ event }: { event: CalendarEvent }) {
  const [findOpen, setFindOpen] = useState(false);
  const [findKw, setFindKw] = useState("");
  const { store } = useStore();
  // Per-keyword promote status so each chip reflects its own pipeline write.
  const [promo, setPromo] = useState<Record<string, PromoteState>>({});
  const [promoErr, setPromoErr] = useState<string | null>(null);
  const meta = EVENT_HORIZON[event.horizon] ?? EVENT_HORIZON.later;
  const openResearch = (kw: string) => {
    setFindKw(kw);
    setFindOpen(true);
  };
  // Map the event's timing horizon → the listing-queue capture bucket, so a promoted
  // keyword lands in the pipeline already tagged with WHEN it should be built.
  const captureBucket =
    event.in_season || event.horizon === "now" ? "LIST-NOW"
    : event.horizon === "build_ahead" ? "BUILD-AHEAD"
    : "EVERGREEN";
  async function promote(kw: string) {
    if (!store) {
      setPromoErr("Pick a store first (top-left).");
      return;
    }
    setPromoErr(null);
    setPromo((p) => ({ ...p, [kw]: "busy" }));
    try {
      await api.addCategory(store, { slug: kwSlug(kw), keyword: kw, capture: captureBucket });
      setPromo((p) => ({ ...p, [kw]: "done" }));
    } catch (e) {
      setPromo((p) => ({ ...p, [kw]: "err" }));
      setPromoErr(e instanceof Error ? e.message : "Promote failed");
    }
  }
  const countdown =
    event.in_season
      ? event.season_end
        ? `in season · until ${eventDate(event.season_end)}`
        : "in season"
      : event.days_until <= 0
        ? "today"
        : event.days_until < 21
          ? `in ${event.days_until} days`
          : `in ${event.weeks_until} weeks`;
  return (
    <Card className="flex flex-col gap-2.5 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">{event.name}</span>
            <span
              title={event.country === "GLOBAL" ? "Applies across all markets" : `Searched in ${event.country}`}
              className="inline-flex items-center gap-1 rounded-full border border-[var(--border)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--muted)]"
            >
              {countryFlag(event.country)} {event.country}
            </span>
            {event.recurring ? null : (
              <span
                title="One-off scheduled event (not annually recurring)"
                className="rounded-full border border-[var(--border)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--muted)]"
              >
                one-off
              </span>
            )}
          </div>
          <div className="mt-0.5 text-[11px] text-[var(--muted)]">
            {event.is_season && event.season_start && !event.in_season
              ? `${eventDate(event.season_start)} → ${event.season_end ? eventDate(event.season_end) : ""}`
              : eventDate(event.next_date)}{" "}
            · {countdown}
            {event.category ? ` · ${event.category}` : ""}
          </div>
        </div>
        <span
          className="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold"
          style={{ background: `color-mix(in srgb, ${meta.color} 18%, transparent)`, color: meta.color }}
        >
          {meta.label}
        </span>
      </div>

      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-[var(--muted)]">
            {event.in_season ? "Status" : event.is_season ? "Season starts" : "Countdown"}
          </div>
          <div className="text-lg font-semibold tabular-nums" style={{ color: meta.color }}>
            {event.in_season ? "live" : event.days_until <= 0 ? "now" : `${event.days_until}d`}
          </div>
        </div>
        <div className="text-right text-[11px] text-[var(--muted)]">
          list ~{event.lead_weeks}w ahead
          <div>
            {event.in_season
              ? "in season — list now"
              : event.horizon === "build_ahead"
                ? "prep the batch now"
                : event.horizon === "now"
                  ? "run-up started — list today"
                  : "on the radar"}
          </div>
        </div>
      </div>

      {event.keywords.length > 0 ? (
        <div className="rounded-lg border border-[var(--border)] p-2.5" style={{ background: "var(--surface)" }}>
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="text-[10px] uppercase tracking-wide text-[var(--muted)]">
              Drives demand for · research now
            </span>
            <span className="text-[10px] text-[var(--muted)]">
              promote → {captureBucket.toLowerCase().replace("-", " ")}
            </span>
          </div>
          <div className="flex flex-col gap-1.5">
            {event.keywords.map((kw) => {
              const st = promo[kw] ?? "idle";
              return (
                <div key={kw} className="flex items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => openResearch(kw)}
                    title={`Find products for "${kw}" — pre-list ahead of ${event.name}`}
                    className="inline-flex flex-1 items-center rounded-md border px-2 py-0.5 text-xs font-medium transition-colors hover:border-[var(--accent)]"
                    style={{ borderColor: "var(--border)", color: "var(--text)" }}
                  >
                    {kw}
                  </button>
                  <button
                    type="button"
                    onClick={() => promote(kw)}
                    disabled={st === "busy" || st === "done"}
                    title={
                      st === "done"
                        ? `"${kw}" is in the ${store} pipeline`
                        : `Promote "${kw}" into the ${store || "store"} listing pipeline`
                    }
                    className="inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-semibold transition-colors disabled:opacity-60"
                    style={
                      st === "done"
                        ? { borderColor: "var(--accent)", color: "var(--accent)", background: "color-mix(in srgb, var(--accent) 12%, transparent)" }
                        : { borderColor: "var(--border)", color: "var(--muted)" }
                    }
                  >
                    {st === "busy" ? "…" : st === "done" ? "✓ in pipeline" : st === "err" ? "retry" : "↑ Promote"}
                  </button>
                </div>
              );
            })}
          </div>
          {promoErr ? (
            <div className="mt-1.5 text-[11px] text-[var(--danger)]">{promoErr}</div>
          ) : null}
        </div>
      ) : null}

      <FindProductsModal
        keyword={findKw}
        sv={null}
        source="event"
        store={store}
        open={findOpen}
        onClose={() => setFindOpen(false)}
      />
    </Card>
  );
}

// Events Calendar — sits as a card alongside News Radar / Trend Radar in "Ways to research
// trends". Collapsed by default; opening it reveals the full calendar (country + horizon
// filters + event cards) as a full-width panel below the method grid.
const EVENT_WINDOWS: { days: number; label: string }[] = [
  { days: 90, label: "3 mo" },
  { days: 180, label: "6 mo" },
  { days: 365, label: "1 yr" },
];

// `open`/`onOpenChange` (optional) — same controlled/accordion contract as NewsRadarCard.
export function EventsCalendarCard({
  open: openProp,
  onOpenChange,
}: {
  open?: boolean;
  onOpenChange?: (v: boolean) => void;
} = {}) {
  const [openLocal, setOpenLocal] = useState(false);
  const open = openProp ?? openLocal;
  const setOpen = (v: boolean | ((p: boolean) => boolean)) => {
    const next = typeof v === "function" ? v(open) : v;
    if (onOpenChange) onOpenChange(next);
    else setOpenLocal(next);
  };
  const [country, setCountry] = useState("ALL");
  const [withinDays, setWithinDays] = useState(180);
  const { data, error, loading, reload } = useApi(
    () => api.events(country, withinDays),
    [country, withinDays],
  );

  const events = data?.events ?? [];
  const summary = data
    ? `${data.totals.events} upcoming · ${data.totals.now} to list now · ${data.totals.build_ahead} to build ahead`
    : "Holidays, seasonal & world events per country — the predictable, plannable demand signal.";

  // Mirrors News Radar: the compact card ALWAYS stays in the method grid; opening reveals a
  // separate full-width (md:col-span-2) panel below, so both cards stay visible above.
  return (
    <>
      <Card
        className={`flex h-full flex-col p-3 transition-colors ${
          open ? "border-[var(--accent)] ring-1 ring-[var(--ring)]" : "hover:border-[var(--accent)]"
        }`}
      >
        <button onClick={() => setOpen((v) => !v)} className="w-full text-left">
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm font-semibold">Events Calendar</div>
            <span className="text-[11px] font-semibold text-[var(--muted)]">
              {open ? "Close ›" : "Open ›"}
            </span>
          </div>
          <p className="mt-1 text-[13px] leading-snug text-[var(--muted)]">
            The predictable layer — dated holidays, seasons & world events you can build a batch ahead of.
          </p>
          <p className="mt-1.5 text-[11px] text-[var(--muted)]">{summary}</p>
        </button>
        {/* Card-face action. The calendar is pure date-math (no external pull), so this
            recomputes it now — updating the "in N days" countdowns and to-list/build-ahead counts. */}
        <div className="mt-auto flex flex-wrap items-center gap-2 pt-2.5">
          <button
            type="button"
            onClick={() => reload()}
            disabled={loading}
            className="rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-50"
            style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="text-xs font-medium text-[var(--muted)] hover:text-[var(--text)]"
          >
            {open ? "Close" : "Open calendar"}
          </button>
        </div>
      </Card>

      {open ? (
        <div
          className="rounded-2xl border border-[var(--border)] p-4 md:col-span-3 md:order-last"
          style={{ background: "var(--surface)" }}
        >
          <div className="mb-3 flex flex-wrap items-center gap-2 border-b border-[var(--border)] pb-3">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-[11px] font-semibold text-[var(--muted)] hover:text-[var(--text)]"
              aria-label="Collapse Events Calendar"
            >
              ‹ Close
            </button>
            <span className="text-sm font-semibold">Events Calendar</span>
            <span className="text-[11px] font-normal text-[var(--muted)]">
              the predictable layer — dated demand you can plan a build-ahead batch around
            </span>
          </div>

          <div className="mb-3 flex flex-wrap items-center gap-3">
            <div className="flex flex-wrap items-center gap-1">
              <span className="mr-1 text-[10px] uppercase tracking-wide text-[var(--muted)]">Market</span>
              {["ALL", ...(data?.countries ?? [])].map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => setCountry(c)}
                  className={`rounded-md border px-2 py-0.5 text-xs font-medium transition-colors ${
                    country === c
                      ? "border-[var(--accent)] text-[var(--text)]"
                      : "border-[var(--border)] text-[var(--muted)] hover:text-[var(--text)]"
                  }`}
                >
                  {c === "ALL" ? "All" : `${countryFlag(c)} ${c}`}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1">
              <span className="mr-1 text-[10px] uppercase tracking-wide text-[var(--muted)]">Horizon</span>
              {EVENT_WINDOWS.map((w) => (
                <button
                  key={w.days}
                  type="button"
                  onClick={() => setWithinDays(w.days)}
                  className={`rounded-md border px-2 py-0.5 text-xs font-medium transition-colors ${
                    withinDays === w.days
                      ? "border-[var(--accent)] text-[var(--text)]"
                      : "border-[var(--border)] text-[var(--muted)] hover:text-[var(--text)]"
                  }`}
                >
                  {w.label}
                </button>
              ))}
            </div>
          </div>

          {loading ? (
            <Loading />
          ) : error ? (
            <ErrorState message={error} />
          ) : events.length === 0 ? (
            <p className="text-sm text-[var(--muted)]">
              No events in this window. Widen the horizon or pick a different market. Grow the
              calendar by editing events-calendar/events.json.
            </p>
          ) : (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {events.map((ev) => (
                <EventCard key={`${ev.country}-${ev.name}-${ev.next_date}`} event={ev} />
              ))}
            </div>
          )}
        </div>
      ) : null}
    </>
  );
}

// One trend as a self-contained grid cell.
function TrendCard({
  row,
  horizon,
  onDismiss,
}: {
  row: TrendRow;
  horizon: TrendHorizon | null;
  onDismiss?: () => void;
}) {
  const { store } = useStore();
  const [findOpen, setFindOpen] = useState(false);
  // Find Products can be opened on a rising sub-query (the "portable" lesson) — track which query
  // + its SV the modal should use.
  const [findKw, setFindKw] = useState<{ keyword: string; sv: number | null }>({
    keyword: row.keyword ?? "",
    sv: row.sv,
  });
  const openSubResearch = (query: string) => {
    setFindKw({ keyword: query, sv: null }); // sub-query SV not joined; let the finder resolve it
    setFindOpen(true);
  };
  // Promote the head keyword into the pipeline (SKU plan → product-find) — same meaning as Promote
  // on the Keyword Research table. Ingests it as a gated candidate first if it isn't one yet.
  const router = useRouter();
  const [promoting, setPromoting] = useState(false);
  const [promoteMsg, setPromoteMsg] = useState<{ ok: boolean; text: string } | null>(null);
  async function promote() {
    if (!row.keyword || !store || promoting) return;
    setPromoting(true);
    setPromoteMsg(null);
    try {
      await api.promoteTrend(store, row.keyword);
      setPromoteMsg({ ok: true, text: `Promoted to ${store} — opening the SKU Plan…` });
      router.push("/sku-plan"); // land the operator in the SKU Plan where it now appears
    } catch (e) {
      setPromoteMsg({ ok: false, text: e instanceof Error ? e.message : "Promote failed" });
    } finally {
      setPromoting(false);
    }
  }
  const color = BUCKET_COLOR[row.bucket];
  const lead = leadingSignal(row); // rising sub-search breaking out ahead of the head term
  const ran = runDate(row.last_date);
  const ticks = yearTicks(row.monthly);
  const growth =
    horizon === "week" ? row.growth_week
    : horizon === "month" ? row.growth_month
    : horizon === "quarter" ? row.growth_quarter
    : row.growth_ratio;
  const growthLabel = horizon ? HORIZON_GROWTH_LABEL[horizon] : "long-run";
  return (
    <Card className="flex flex-col gap-3 p-4">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">{row.keyword ?? "—"}</span>
            {row.geo ? <Pill>{row.geo}</Pill> : null}
            {row.breakout ? (
              <span
                className="rounded-full px-1.5 py-0.5 text-[10px] font-bold"
                style={{
                  background: "color-mix(in srgb, var(--state-winner) 18%, transparent)",
                  color: "var(--state-winner)",
                }}
                title="Sharp recent spike — promoted to Rising now to catch it ASAP"
              >
                ⚡ Breakout
              </span>
            ) : null}
            {lead ? (
              <span
                className="rounded-full px-1.5 py-0.5 text-[10px] font-bold"
                style={{
                  background: "color-mix(in srgb, var(--state-testing) 18%, transparent)",
                  color: "var(--state-testing)",
                }}
                title={`Leading signal — the rising sub-search "${lead.query}" (${fmtChange(lead.change).label}) is breaking out before this head term's own volume has. The rising bucket leads head-term SV: list / title against "${lead.query}" now, ahead of the crowd.`}
              >
                🔭 Leading: {lead.query}
              </span>
            ) : null}
          </div>
          {ran ? (
            <div
              className="mt-0.5 text-[11px]"
              style={{ color: row.stale ? "var(--state-testing)" : "var(--muted)" }}
              title={row.stale ? "Data is older than today — momentum is as of this run date, not now" : "Data is current"}
            >
              Ran {ran}
              {row.days_old != null ? ` · ${row.days_old}d ago` : ""}
            </div>
          ) : null}
          <KeywordSvLine row={row} />
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <BucketBadge bucket={row.bucket} />
          {onDismiss ? (
            <button
              type="button"
              onClick={onDismiss}
              aria-label={`Remove ${row.keyword ?? "this keyword"} from trend research`}
              title="Remove this keyword from trend research"
              className="flex h-6 w-6 items-center justify-center rounded-full border text-[var(--muted)] transition-colors hover:text-[var(--state-killed)]"
              style={{ borderColor: "var(--border)" }}
            >
              <svg width="12" height="12" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                <path
                  d="M1 1l12 12M13 1L1 13"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          ) : null}
        </div>
      </div>

      {/* Promote — the primary action, up top: this keyword goes down the pipeline from here
          (SKU plan → product-find), same meaning as Promote on the Keyword Research table. */}
      {row.keyword ? (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={promote}
            disabled={promoting || !store}
            title={store ? `Promote "${row.keyword}" into ${store}'s pipeline` : "Pick a store first"}
            className="inline-flex items-center rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-60"
            style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
          >
            {promoting ? "Promoting…" : "Promote →"}
          </button>
          {promoteMsg ? (
            <span
              className="text-[11px]"
              style={{ color: promoteMsg.ok ? "var(--state-winner)" : "var(--state-killed)" }}
            >
              {promoteMsg.text}
            </span>
          ) : null}
        </div>
      ) : null}

      {/* Monthly chart with month-axis ticks. */}
      <div>
        <MonthlyChart monthly={row.monthly} raw={row.raw_series} color={color} />
        {ticks.length > 0 ? (
          <div className="relative mt-0.5 h-3 text-[9px] text-[var(--muted)]">
            {ticks.map((t) => (
              <span
                key={t.label + t.pct}
                className="absolute top-0 whitespace-nowrap tabular-nums"
                style={{
                  left: `${t.pct}%`,
                  transform:
                    t.pct <= 2 ? "translateX(0)" : t.pct >= 98 ? "translateX(-100%)" : "translateX(-50%)",
                }}
              >
                {t.label}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      {/* Real monthly search volume (falls back to ×-momentum if no SV series). */}
      <MonthlyVolume row={row} />

      {/* Rising related searches — the "portable" signal → click to research the surging sub-query. */}
      {row.related_queries ? (
        <RisingSearches rq={row.related_queries} onResearch={openSubResearch} />
      ) : null}

      <div className="flex items-center justify-between gap-2">
        <div>
          <div className="text-[11px] uppercase tracking-wide text-[var(--muted)]">
            Growth <span className="normal-case text-[var(--muted)]">· {growthLabel}</span>
          </div>
          <div className="text-lg font-semibold tabular-nums" style={{ color }}>
            {growth != null ? `${growth.toFixed(2)}×` : "—"}
          </div>
        </div>
        <div className="text-right text-xs text-[var(--muted)]">
          {row.trend_verdict ?? row.evergreen_verdict ?? "—"}
          {row.peak_month ? (
            <div>peaks {MONTHS[row.peak_month] ?? row.peak_month}</div>
          ) : null}
        </div>
      </div>

      {/* Find-products preview is now reached by clicking a rising sub-search above (openSubResearch);
          the head-keyword action is Promote at the top. This modal serves those sub-query previews. */}
      {row.keyword ? (
        <FindProductsModal
          keyword={findKw.keyword || row.keyword}
          sv={findKw.sv}
          source="trend"
          store={store}
          open={findOpen}
          onClose={() => setFindOpen(false)}
        />
      ) : null}
    </Card>
  );
}

// The reusable Trend Research surface — momentum tabs + card grid + its own data fetch.
// Rendered both standalone at /trends and as the "Trend Research" tab under Research.
// Matches the backend _trend_key (slug | lowercased keyword | geo) so optimistic
// hides line up with what the server persists.
const trendKey = (r: TrendRow) =>
  `${r.slug ?? ""}|${(r.keyword ?? "").trim().toLowerCase()}|${r.geo ?? ""}`;

export function TrendResearch() {
  const { data, error, loading } = useApi(() => api.trends());
  const [filter, setFilter] = useState<Filter>("all");
  // Operator-curated hides — dropped optimistically here and persisted server-side so
  // they stay gone on reload. Tracks keys we've dismissed this session.
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const onDismiss = (r: TrendRow) => {
    const k = trendKey(r);
    setDismissed((prev) => new Set(prev).add(k));
    void api.dismissTrend(r.slug ?? "", r.keyword ?? "", r.geo).catch(() => {
      // Revert the optimistic hide if the server rejected it.
      setDismissed((prev) => {
        const next = new Set(prev);
        next.delete(k);
        return next;
      });
    });
  };
  const rows = (data?.trends ?? []).filter((r) => !dismissed.has(trendKey(r)));
  const t = data?.totals;

  // Counts derive from the post-dismiss rows so the tab badges drop in step with hides.
  const counts: Record<Filter, number> = {
    all: rows.length,
    early: rows.filter((r) => leadingSignal(r)).length,
    week: rows.filter((r) => r.horizon === "week").length,
    month: rows.filter((r) => r.horizon === "month").length,
    quarter: rows.filter((r) => r.horizon === "quarter").length,
  };
  const activeTab = FILTER_TABS.find((f) => f.id === filter) ?? FILTER_TABS[0];
  const activeHorizon = activeTab.horizon;
  const shown =
    filter === "early"
      ? // Leading indicators: rising sub-search breaking out ahead of the head term,
        // sorted by the sub-query's surge magnitude (strongest early signal first).
        rows
          .filter((r) => leadingSignal(r))
          .slice()
          .sort(
            (a, b) => (topRising(b.related_queries)?.mag ?? 0) - (topRising(a.related_queries)?.mag ?? 0),
          )
      : (activeHorizon ? rows.filter((r) => r.horizon === activeHorizon) : rows)
          .slice()
          .sort((a, b) => (horizonGrowth(b, activeHorizon) ?? 0) - (horizonGrowth(a, activeHorizon) ?? 0));

  return (
    <>
      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          {t && t.data_age_days != null && t.data_age_days > 10 ? (
            <div
              className="mb-4 rounded-xl border px-3.5 py-2.5 text-sm"
              style={{
                borderColor: "color-mix(in srgb, var(--state-testing) 40%, var(--border))",
                background: "color-mix(in srgb, var(--state-testing) 8%, transparent)",
                color: "var(--text)",
              }}
            >
              <span className="font-semibold" style={{ color: "var(--state-testing)" }}>
                Trend data is {t.data_age_days} days old.
              </span>{" "}
              Momentum below is anchored to each keyword&apos;s last run date (shown
              on every card), <strong>not today</strong> — re-run Trend Research or a
              discovery pipeline for a today-fresh read of what&apos;s rising now.
            </div>
          ) : null}
          <div className="mb-3 flex flex-wrap gap-2">
            {FILTER_TABS.map((tab) => (
              <FilterTab
                key={tab.id}
                tab={tab}
                count={counts[tab.id]}
                active={filter === tab.id}
                onClick={() => setFilter(tab.id)}
              />
            ))}
          </div>
          <p className="mb-6 text-sm text-[var(--muted)]">{activeTab.blurb}</p>

          {rows.length === 0 ? (
            <Empty>
              No trend signals on disk yet. Run Trend Research or a niche discovery
              pipeline to populate momentum reads here.
            </Empty>
          ) : shown.length === 0 ? (
            <Empty>Nothing rising on this horizon right now. Try another tab.</Empty>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {shown.map((r, i) => (
                <TrendCard
                  key={`${r.slug}-${r.keyword}-${r.geo}-${i}`}
                  row={r}
                  horizon={activeHorizon}
                  onDismiss={() => onDismiss(r)}
                />
              ))}
            </div>
          )}
        </>
      ) : null}
    </>
  );
}
