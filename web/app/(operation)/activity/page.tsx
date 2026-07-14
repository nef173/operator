"use client";

import { api, type RunRecord, type JobRecord } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Pill, Stat } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";

// Unified timeline of control-layer writes (runs) + execution jobs.
// This is the operator's "what has the app actually done" log — read-only.

const STATUS_COLOR: Record<string, string> = {
  queued: "var(--muted)",
  running: "var(--state-testing)",
  done: "var(--state-winner)",
  failed: "var(--state-killed)",
  "needs-operator": "var(--state-drafted)",
};

type Entry =
  | { kind: "run"; ts: string; run: RunRecord }
  | { kind: "job"; ts: string; job: JobRecord };

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

function StatusDot({ status }: { status: string }) {
  const color = STATUS_COLOR[status] ?? "var(--muted)";
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs font-medium"
      style={{ color }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {status}
    </span>
  );
}

export default function ActivityPage() {
  const { data: runsData, error, loading, reload: reloadRuns } = useApi(() => api.runs(100));
  const { data: jobsData, reload: reloadJobs } = useApi(() => api.jobs(100));

  const runs = runsData?.runs ?? [];
  const jobs = jobsData?.jobs ?? [];
  const counts = runsData?.counts ?? { runs: 0, jobs: 0 };

  const entries: Entry[] = [
    ...runs.map((run): Entry => ({ kind: "run", ts: run.ts, run })),
    ...jobs.map((job): Entry => ({ kind: "job", ts: job.updated || job.ts, job })),
  ].sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0));

  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader
        title="Activity Log"
        subtitle="Every write the control layer made + every execution job it ran. Newest first."
        action={
          <button
            type="button"
            onClick={() => {
              reloadRuns();
              reloadJobs();
            }}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--surface-2)]"
          >
            Refresh
          </button>
        }
      />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {runsData ? (
        <>
          <div className="mb-6 grid gap-4 sm:grid-cols-2">
            <Stat label="Control writes" value={counts.runs} hint="State machine mutations" />
            <Stat label="Execution jobs" value={counts.jobs} hint="Auto + manual handoffs" />
          </div>

          {entries.length === 0 ? (
            <Empty>
              No activity yet. Promote a candidate, change an SKU state, or run a
              research job to see it logged here.
            </Empty>
          ) : (
            <Card className="divide-y divide-[var(--border)]">
              {entries.map((e) =>
                e.kind === "run" ? (
                  <RunRow key={`run-${e.run.id}`} run={e.run} />
                ) : (
                  <JobRow key={`job-${e.job.id}`} job={e.job} />
                ),
              )}
            </Card>
          )}
        </>
      ) : null}
    </div>
  );
}

function RunRow({ run }: { run: RunRecord }) {
  return (
    <div className="flex items-start gap-3 px-4 py-3">
      <Pill>run</Pill>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold">{run.action}</span>
          {run.target ? (
            <code className="font-mono text-xs text-[var(--muted)]">{run.target}</code>
          ) : null}
          {run.store ? <span className="text-xs text-[var(--muted)]">· {run.store}</span> : null}
        </div>
        {run.detail ? (
          <div className="mt-0.5 text-xs text-[var(--muted)]">{run.detail}</div>
        ) : null}
      </div>
      <div className="flex flex-col items-end gap-1">
        <StatusDot status={run.status} />
        <span className="text-[11px] text-[var(--muted)]">{fmtTs(run.ts)}</span>
      </div>
    </div>
  );
}

function JobRow({ job }: { job: JobRecord }) {
  return (
    <div className="flex items-start gap-3 px-4 py-3">
      <Pill>{job.mode}</Pill>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-semibold">{job.title ?? job.spec}</span>
          {job.store ? <span className="text-xs text-[var(--muted)]">· {job.store}</span> : null}
        </div>
        {job.command ? (
          <code className="mt-0.5 block break-all font-mono text-[11px] text-[var(--muted)]">
            {job.command}
          </code>
        ) : null}
        {job.detail ? (
          <div className="mt-0.5 text-xs text-[var(--state-killed)]">{job.detail}</div>
        ) : null}
      </div>
      <div className="flex flex-col items-end gap-1">
        <StatusDot status={job.status} />
        <span className="text-[11px] text-[var(--muted)]">{fmtTs(job.updated || job.ts)}</span>
      </div>
    </div>
  );
}
