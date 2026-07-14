"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type JobRecord } from "@/lib/api";

const TERMINAL = new Set(["done", "failed", "needs-operator"]);

const STATUS_COLOR: Record<string, string> = {
  queued: "var(--muted)",
  running: "var(--state-testing)",
  done: "var(--state-winner)",
  failed: "var(--state-killed)",
  "needs-operator": "var(--state-drafted)",
};

/**
 * One reusable Run control for a heavy pipeline step (Phase 3 hybrid jobs).
 * Creates a job from a spec, then polls every 2s until terminal. Auto jobs run
 * server-side; manual jobs come back `needs-operator` carrying the exact command the
 * operator pastes into their own Claude Code (which holds the keys/MCP/AdsPower).
 */
export function JobRunner({
  spec,
  store,
  args,
  label,
  disabled,
  onTerminal,
}: {
  spec: string;
  store: string | null;
  args?: Record<string, unknown>;
  label: string;
  disabled?: boolean;
  onTerminal?: (job: JobRecord) => void;
}) {
  const [job, setJob] = useState<JobRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = useCallback(() => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(() => () => clearTimer(), [clearTimer]);

  const poll = useCallback(
    (id: number) => {
      timer.current = setTimeout(async () => {
        try {
          const next = await api.job(id);
          setJob(next);
          if (!TERMINAL.has(next.status)) {
            poll(id);
          } else {
            setBusy(false);
            onTerminal?.(next);
          }
        } catch {
          setBusy(false);
        }
      }, 2000);
    },
    [onTerminal],
  );

  async function run() {
    if (!store) {
      setError("No store selected");
      return;
    }
    setBusy(true);
    setError(null);
    setJob(null);
    try {
      const created = await api.createJob(spec, store, args);
      setJob(created);
      if (TERMINAL.has(created.status)) {
        setBusy(false);
        onTerminal?.(created);
      } else {
        poll(created.id);
      }
    } catch (e) {
      setBusy(false);
      setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Failed");
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={run}
          disabled={busy || disabled || !store}
          className="rounded-lg px-4 py-2 text-sm font-semibold disabled:opacity-50"
          style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          {busy ? "Running…" : label}
        </button>
        {job ? (
          <span
            className="inline-flex items-center gap-1.5 text-xs font-medium"
            style={{ color: STATUS_COLOR[job.status] ?? "var(--muted)" }}
          >
            <span
              className="h-1.5 w-1.5 rounded-full"
              style={{ background: STATUS_COLOR[job.status] ?? "var(--muted)" }}
            />
            {job.status}
          </span>
        ) : null}
        {error ? <span className="text-xs text-[var(--state-killed)]">{error}</span> : null}
      </div>

      {job?.status === "needs-operator" && job.command ? (
        <div className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-3">
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--state-drafted)]">
              Copy and run this command yourself
            </span>
            <button
              type="button"
              onClick={() => {
                navigator.clipboard?.writeText(job.command ?? "");
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
              className="rounded border border-[var(--border)] px-2 py-0.5 text-[11px] font-medium hover:bg-[var(--surface)]"
            >
              {copied ? "Copied ✓" : "Copy"}
            </button>
          </div>
          <code className="block break-all font-mono text-xs text-[var(--text)]">
            {job.command}
          </code>
          <p className="mt-2 text-[11px] text-[var(--muted)]">
            This step needs tools this app can&apos;t reach, so you run it yourself in
            your own terminal. When it finishes, the result shows up here.
          </p>
        </div>
      ) : null}

      {job?.status === "done" && job.output ? (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--surface-2)] p-3 font-mono text-xs">
          {job.output}
        </pre>
      ) : null}

      {job?.status === "failed" ? (
        <div className="rounded-lg px-3 py-2 text-xs" style={{ color: "var(--state-killed)", background: "color-mix(in srgb, var(--state-killed) 12%, transparent)" }}>
          {job.detail ?? "Job failed"}
        </div>
      ) : null}
    </div>
  );
}
