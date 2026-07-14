"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, type WorkerStatus, type AlertsDigest } from "@/lib/api";

// Always-visible heartbeat in the app chrome. The operator's "what is the worker
// doing right now, and does it need me" — on every page. Polls /api/worker every 5s.
// It NEVER acts; it only surfaces state and links to the Decisions cockpit, which is
// the single seam where a suggestion becomes real work (operator approval).

const WORKER_DOT: Record<string, string> = {
  idle: "var(--muted)",
  armed: "var(--state-testing)",
  running: "var(--state-testing)",
};

export function WorkerStrip() {
  const [data, setData] = useState<WorkerStatus | null>(null);
  const [offline, setOffline] = useState(false);
  const [alerts, setAlerts] = useState<AlertsDigest | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = () =>
      api
        .worker()
        .then((d) => {
          if (!alive) return;
          setData(d);
          setOffline(false);
        })
        .catch(() => alive && setOffline(true));
    poll();
    const id = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // Performance alerts — heavier (scans every store's snapshot), so poll slower: on mount + every 60s.
  useEffect(() => {
    let alive = true;
    const poll = () => api.alerts().then((d) => alive && setAlerts(d)).catch(() => {});
    poll();
    const id = setInterval(poll, 60000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (offline && !data) {
    return (
      <div className="flex items-center gap-2 text-xs text-[var(--muted)]">
        <span className="h-1.5 w-1.5 rounded-full bg-[var(--state-killed)]" />
        worker offline
      </div>
    );
  }
  if (!data) {
    return <div className="text-xs text-[var(--muted)]">worker…</div>;
  }

  const w = data.worker;
  const c = data.counts;
  const dot = WORKER_DOT[w.status] ?? "var(--muted)";
  const needsYou = c.pending_decisions + c.needs_operator;

  return (
    <div className="flex items-center gap-4 text-xs">
      <span className="inline-flex items-center gap-1.5 font-medium">
        <span
          className="h-2 w-2 rounded-full"
          style={{
            background: dot,
            boxShadow: w.status === "running" ? `0 0 0 3px color-mix(in srgb, ${dot} 25%, transparent)` : "none",
          }}
        />
        <span className="text-[var(--muted)]">worker</span>
        <span className="font-semibold">{w.enabled ? w.status : "manual"}</span>
      </span>

      {data.scheduler?.running ? (
        <span
          className="inline-flex items-center gap-1 rounded-full bg-[var(--surface-2)] px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-[var(--muted)]"
          title={`Always-on scheduler live — cadence-gated tick every ${Math.round(
            data.scheduler.poll_seconds / 60,
          )}m${
            data.scheduler.last_loop.at ? ` · last pass ${data.scheduler.last_loop.at}` : ""
          }${data.scheduler.last_loop.error ? ` · err: ${data.scheduler.last_loop.error}` : ""}`}
        >
          <span className="h-1.5 w-1.5 rounded-full bg-[var(--state-winner)]" />
          24/7
        </span>
      ) : null}

      <span className="text-[var(--muted)]">
        <span className="font-semibold text-[var(--text)]">{c.running}</span> running
      </span>
      <span className="text-[var(--muted)]">
        <span className="font-semibold text-[var(--text)]">{c.queued}</span> queued
      </span>

      <Link
        href="/decisions"
        className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-medium transition-colors ${
          needsYou > 0
            ? "bg-[color-mix(in_srgb,var(--state-drafted)_18%,transparent)] text-[var(--state-drafted)] hover:bg-[color-mix(in_srgb,var(--state-drafted)_28%,transparent)]"
            : "text-[var(--muted)] hover:bg-[var(--surface-2)]"
        }`}
      >
        {needsYou > 0 ? (
          <span className="h-1.5 w-1.5 rounded-full bg-[var(--state-drafted)]" />
        ) : null}
        {needsYou > 0 ? `${needsYou} needs you` : "all clear"}
      </Link>

      {alerts && alerts.needs_attention > 0 ? (
        <Link
          href="/alerts"
          title={`${alerts.counts.high} high · ${alerts.counts.medium} medium · ${alerts.counts.positive} winners`}
          className="inline-flex items-center gap-1.5 rounded-full bg-[color-mix(in_srgb,var(--state-killed)_16%,transparent)] px-2.5 py-1 font-medium text-[var(--state-killed)] transition-colors hover:bg-[color-mix(in_srgb,var(--state-killed)_26%,transparent)]"
        >
          <span className="h-1.5 w-1.5 rounded-full bg-[var(--state-killed)]" />
          {alerts.needs_attention} alert{alerts.needs_attention === 1 ? "" : "s"}
        </Link>
      ) : null}
    </div>
  );
}
