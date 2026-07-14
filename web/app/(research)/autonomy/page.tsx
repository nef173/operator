"use client";

import { useEffect, useState } from "react";
import {
  api,
  type AutonomyStep,
  type AutonomyMode,
  type Cadence,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { useStore } from "@/components/StoreProvider";
import { Card, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState } from "@/components/PageState";

function fmtTs(ts: string): string {
  const d = new Date(ts.endsWith("Z") || ts.includes("+") ? ts : `${ts}Z`);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// The global worker switch + manual tick. This is operational ("how much / how often the
// worker runs"), not a judgment call — so it lives here in Autonomy, not in Decisions.
function WorkerControl() {
  const worker = useApi(() => api.worker());
  const { store, setStore, stores } = useStore();
  const activeStore = store || null;
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const id = setInterval(() => worker.reload(), 8000);
    return () => clearInterval(id);
  }, [worker.reload]);

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setErr(null);
    try {
      await fn();
      worker.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const w = worker.data;
  if (!w) return null;
  return (
    <Card className="mb-6 p-5">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="text-sm font-semibold">
            Worker · {w.worker.enabled ? w.worker.status : "manual (you drive the tick)"}
          </div>
          <div className="mt-0.5 text-xs text-[var(--muted)]">
            {w.worker.last_tick
              ? `Last run ${fmtTs(w.worker.last_tick)} · ${w.worker.ticks} ticks`
              : "Not run yet"}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {stores.length > 0 ? (
            <select
              value={activeStore ?? ""}
              onChange={(e) => setStore(e.target.value)}
              className="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-sm"
            >
              {stores.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          ) : null}
          <button
            type="button"
            disabled={!activeStore || busy !== null}
            onClick={() => activeStore && act("tick", () => api.workerTick(activeStore))}
            className="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-[var(--accent-fg)] disabled:opacity-50"
          >
            {busy === "tick" ? "Running…" : "Run scheduled steps now"}
          </button>
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => act("enable", () => api.setWorkerEnabled(!w.worker.enabled))}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--surface-2)] disabled:opacity-50"
          >
            {w.worker.enabled ? "Disarm auto-tick" : "Arm auto-tick"}
          </button>
        </div>
      </div>
      {err ? <div className="mt-3 text-xs text-[var(--state-killed)]">{err}</div> : null}
    </Card>
  );
}

// The what / how / when control panel. The operator configures, per pipeline step:
//   HOW   = Manual (you trigger it) · Suggest (worker drops an approval into Decisions) · Auto (worker runs it on a tick)
//   WHEN  = on-demand · daily · every-3-days · weekly
// This is where the hybrid posture lives: keep KEYWORD + TREND research on Suggest so
// they stay your call; let the daily listing plan run Auto once the plan is set.

const MODES: { value: AutonomyMode; label: string; hint: string }[] = [
  { value: "manual", label: "Manual", hint: "You trigger it" },
  { value: "suggest", label: "Suggest", hint: "Worker asks first" },
  { value: "auto", label: "Auto", hint: "Worker runs it" },
];
const CADENCES: Cadence[] = ["on-demand", "daily", "every-3-days", "weekly"];

const MODE_COLOR: Record<AutonomyMode, string> = {
  manual: "var(--muted)",
  suggest: "var(--state-drafted)",
  auto: "var(--state-winner)",
};

// Group steps by surface so the panel reads as the pipeline, not a flat list.
const SURFACE_ORDER = ["listing", "keyword", "trend", "product", "niche", "marketplace"];
const SURFACE_LABEL: Record<string, string> = {
  listing: "Listing",
  keyword: "Keyword research",
  trend: "Trend research",
  product: "Product research",
  niche: "Niche research",
  marketplace: "Marketplace",
};

export default function AutonomyPage() {
  const { data, error, loading, reload } = useApi(() => api.autonomy());
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const steps = data?.steps ?? [];
  const groups = SURFACE_ORDER.map((surface) => ({
    surface,
    items: steps.filter((s) => s.surface === surface),
  })).filter((g) => g.items.length > 0);
  const ungrouped = steps.filter((s) => !SURFACE_ORDER.includes(s.surface));
  if (ungrouped.length > 0) groups.push({ surface: "other", items: ungrouped });

  async function update(
    step: string,
    patch: { mode?: AutonomyMode; cadence?: Cadence },
  ) {
    setBusy(step);
    setErr(null);
    try {
      await api.setAutonomy(step, patch.mode, patch.cadence);
      reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader
        title="Autonomy"
        subtitle="Per step: how much the worker may do on its own (Manual · Suggest · Auto) and how often. Suggest keeps a decision in your hands; Auto runs it for you."
      />

      <WorkerControl />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}
      {err ? (
        <Card className="mb-4 p-3 text-xs text-[var(--state-killed)]">{err}</Card>
      ) : null}

      {data ? (
        <div className="space-y-6">
          {groups.map((g) => (
            <section key={g.surface}>
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wider text-[var(--muted)]">
                {SURFACE_LABEL[g.surface] ?? g.surface}
              </h2>
              <Card className="divide-y divide-[var(--border)]">
                {g.items.map((s) => (
                  <StepRow
                    key={s.step}
                    s={s}
                    busy={busy === s.step}
                    onMode={(mode) => update(s.step, { mode })}
                    onCadence={(cadence) => update(s.step, { cadence })}
                  />
                ))}
              </Card>
            </section>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function StepRow({
  s,
  busy,
  onMode,
  onCadence,
}: {
  s: AutonomyStep;
  busy: boolean;
  onMode: (m: AutonomyMode) => void;
  onCadence: (c: Cadence) => void;
}) {
  // A step whose spec can only run with creds/MCP (spec_mode === "manual") can't truly
  // go Auto server-side — flag it so the operator knows Auto means "queue a handoff".
  const handoffOnly = s.kind === "job" && s.spec_mode === "manual";
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
      <div className="min-w-0 max-w-md">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{s.title}</span>
          {s.overridden ? <Pill>custom</Pill> : null}
          {handoffOnly ? (
            <span className="text-[11px] text-[var(--muted)]">needs creds — Auto queues a handoff</span>
          ) : null}
        </div>
        {s.description ? (
          <p className="mt-0.5 text-xs leading-snug text-[var(--muted)]">{s.description}</p>
        ) : null}
      </div>
      <div className="flex items-center gap-3">
        {/* HOW — mode segmented control */}
        <div className="inline-flex overflow-hidden rounded-lg border border-[var(--border)]">
          {MODES.map((m) => {
            const active = s.mode === m.value;
            return (
              <button
                key={m.value}
                type="button"
                disabled={busy || active}
                title={m.hint}
                onClick={() => onMode(m.value)}
                className="px-2.5 py-1 text-xs font-medium transition-colors disabled:cursor-default"
                style={{
                  background: active ? `color-mix(in srgb, ${MODE_COLOR[m.value]} 16%, transparent)` : "transparent",
                  color: active ? MODE_COLOR[m.value] : "var(--muted)",
                }}
              >
                {m.label}
              </button>
            );
          })}
        </div>
        {/* WHEN — cadence */}
        <select
          value={s.cadence}
          disabled={busy}
          onChange={(e) => onCadence(e.target.value as Cadence)}
          className="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1 text-xs disabled:opacity-50"
        >
          {CADENCES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
