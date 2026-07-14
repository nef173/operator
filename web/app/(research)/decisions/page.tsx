"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, type Decision } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";
import { AssistantChat } from "@/components/AssistantChat";

// The operator's one steering desk. Two ways to point the work, side by side because
// they're the same act:
//   1) DECISIONS — approve a suggestion forward, or turn it down with a reason ("bad
//      keyword, look further") that becomes a durable rule.
//   2) ASSISTANT — just say it in plain words ("this trend's weak, do this SKU plan
//      instead"); it proposes the move and you confirm.
// Both teach the system. What they've taught lives in Settings → System ("What the
// system learned"), keeping this page pure steering.
//
// Deliberately NOT a worker cockpit. Worker arm/run-now lives in Autonomy; in-flight jobs
// and manual handoffs live in Activity. This page is only steering and what it teaches.

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

export default function DecisionsPage() {
  const decisions = useApi(() => api.decisions("pending"));
  // The header badge counts pending decisions + manual handoffs; the handoffs live in
  // Activity, so surface them here too or the badge number won't add up on this page.
  const workerApi = useApi(() => api.worker());

  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Light auto-refresh so new proposals from a tick surface without a manual reload.
  useEffect(() => {
    const id = setInterval(() => {
      decisions.reload();
      workerApi.reload();
    }, 8000);
    return () => clearInterval(id);
  }, [decisions.reload, workerApi.reload]);

  const reloadAll = () => {
    decisions.reload();
    workerApi.reload();
  };

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setErr(null);
    try {
      await fn();
      reloadAll();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const pending = decisions.data?.decisions ?? [];
  const handoffs = workerApi.data?.needs_operator_jobs ?? [];

  return (
    <div className="mx-auto flex h-[calc(100vh-6rem)] max-w-4xl flex-col">
      <div className="shrink-0">
        <PageHeader
          title="Decisions"
          subtitle="Where you steer the work. Say yes to a suggestion, or turn it down and say why — or just tell the assistant below what to find, drop, or look into. Both teach the system."
          action={
            <button
              type="button"
              onClick={reloadAll}
              className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--surface-2)]"
            >
              Refresh
            </button>
          }
        />

        {decisions.loading && !decisions.data ? <Loading /> : null}
        {decisions.error ? <ErrorState message={decisions.error} /> : null}
        {err ? (
          <Card className="mb-4 p-3 text-xs text-[var(--state-killed)]">{err}</Card>
        ) : null}

        {/* Manual handoffs waiting in Activity — counted in the "needs you" badge, so name
            them here or the badge number won't add up on the page it links to. */}
        {handoffs.length > 0 ? (
          <Link
            href="/activity"
            className="mb-3 flex items-center justify-between rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-4 py-2.5 text-sm hover:bg-[var(--surface)]"
          >
            <span>
              <span className="font-semibold">{handoffs.length}</span> manual{" "}
              {handoffs.length === 1 ? "handoff" : "handoffs"} waiting — steps that need you to
              run a command
            </span>
            <span className="text-[var(--muted)]">Open Activity →</span>
          </Link>
        ) : null}

        {/* Count header so the operator SEES how many are waiting — otherwise the capped
            scroll region below (which shows ~3 at a time) reads as "only 3" when there are
            many more, and the badge number looks wrong. */}
        {pending.length > 0 ? (
          <div className="mb-2 flex items-center justify-between">
            <div className="text-sm">
              <span className="font-semibold">{pending.length}</span>{" "}
              {pending.length === 1 ? "decision" : "decisions"} waiting on you
              {pending.length > 3 ? (
                <span className="ml-2 text-xs text-[var(--muted)]">— scroll the list to see them all</span>
              ) : null}
            </div>
            {(() => {
              const runSteps = pending.filter((d) => d.kind === "run-step");
              return runSteps.length > 1 ? (
                <button
                  type="button"
                  disabled={busy === "approve-all"}
                  onClick={() =>
                    act("approve-all", async () => {
                      // Approve every pending scheduled run-step in one go (they're routine
                      // weekly-cadence approvals; the tedious part is clicking 20 of them).
                      for (const d of runSteps) await api.approveDecision(d.id);
                    })
                  }
                  className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-medium hover:bg-[var(--surface-2)] disabled:opacity-50"
                  title="Approve all pending scheduled run-steps at once"
                >
                  {busy === "approve-all" ? "Approving…" : `Approve all ${runSteps.length} run-steps`}
                </button>
              ) : null;
            })()}
          </div>
        ) : null}

        {/* The decision inbox — suggestions waiting on a yes/no. Taller so more are visible at
            once; still capped + scrollable so the assistant below stays reachable. */}
        <section className="mb-4 max-h-[58vh] overflow-y-auto">
          {decisions.loading && !decisions.data ? null : pending.length === 0 ? (
            <Empty>
              Nothing waiting on you. When a step is set to Suggest in Autonomy, what it finds
              (a keyword, a supplier match, a product idea) lands here for your call.
            </Empty>
          ) : (
            <div className="space-y-3">
              {pending.map((d) => (
                <DecisionCard
                  key={d.id}
                  d={d}
                  busy={busy === `approve-${d.id}` || busy === `reject-${d.id}`}
                  onApprove={() => act(`approve-${d.id}`, () => api.approveDecision(d.id))}
                  onReject={(reason, action) =>
                    act(`reject-${d.id}`, () => api.rejectDecision(d.id, reason, action))
                  }
                />
              ))}
            </div>
          )}
        </section>
      </div>

      {/* Assistant — the free-text way to steer. Fills the rest of the screen so its
          composer is pinned at the bottom of the first view, always visible. */}
      <section className="flex min-h-0 flex-1 flex-col">
        <div className="mb-2 shrink-0">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-[var(--muted)]">
            Or just tell the assistant
          </h2>
        </div>
        <div className="min-h-0 flex-1">
          <AssistantChat />
        </div>
      </section>

    </div>
  );
}

// Quick-pick reasons — the common "why this proposal is wrong" buckets. Picking one is enough
// to create a learning; the free-text "what to do instead" is optional extra direction.
const REJECT_REASONS = [
  "Bad keyword / weak signal",
  "Off-strategy / wrong niche",
  "Already tried / didn't work",
  "Too expensive / margin too thin",
  "Bad timing / seasonality",
  "Duplicate of existing work",
];

function DecisionCard({
  d,
  busy,
  onApprove,
  onReject,
}: {
  d: Decision;
  busy: boolean;
  onApprove: () => void;
  onReject: (reason?: string, action?: string) => void;
}) {
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [action, setAction] = useState("");
  const related = d.related_learnings ?? [];

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Pill>{d.kind}</Pill>
            <span className="text-sm font-semibold">{d.title}</span>
          </div>
          {d.summary ? (
            <div className="mt-1 text-xs text-[var(--muted)]">{d.summary}</div>
          ) : null}
          <div className="mt-1 text-[11px] text-[var(--muted)]">
            {d.store ? `${d.store} · ` : ""}
            {d.source ? `${d.source} · ` : ""}
            {fmtTs(d.ts)}
          </div>
        </div>
        {!rejecting ? (
          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => setRejecting(true)}
              className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--surface-2)] disabled:opacity-50"
            >
              No, not this
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={onApprove}
              title="Saying yes starts this off — it runs the next step for you."
              className="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-[var(--accent-fg)] disabled:opacity-50"
            >
              {busy ? "…" : "Yes, go ahead"}
            </button>
          </div>
        ) : null}
      </div>

      {/* Prior rejections of the same kind/signal — so the operator sees the accumulated memory */}
      {related.length > 0 ? (
        <div className="mt-3 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-2.5">
          <div className="text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
            You&rsquo;ve turned down similar before
          </div>
          <ul className="mt-1 space-y-1">
            {related.map((l) => (
              <li key={l.id} className="text-xs">
                {l.reason}
                {l.action ? (
                  <span className="text-[var(--state-testing)]"> → {l.action}</span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Reject-with-reason form — capturing this is what makes the system smarter */}
      {rejecting ? (
        <div className="mt-3 border-t border-[var(--border)] pt-3">
          <div className="text-xs font-medium">
            Why not this one? Your reason becomes a rule the system remembers.
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {REJECT_REASONS.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => setReason(r)}
                className={`rounded-full border px-2.5 py-1 text-xs ${
                  reason === r
                    ? "border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-fg)]"
                    : "border-[var(--border)] hover:bg-[var(--surface-2)]"
                }`}
              >
                {r}
              </button>
            ))}
          </div>
          <input
            type="text"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="…or type your own reason"
            className="mt-2 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-sm"
          />
          <input
            type="text"
            value={action}
            onChange={(e) => setAction(e.target.value)}
            placeholder="What to do/change instead? e.g. search further, different angle (optional)"
            className="mt-2 w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-sm"
          />
          <div className="mt-3 flex items-center justify-end gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setRejecting(false);
                setReason("");
                setAction("");
              }}
              className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--surface-2)] disabled:opacity-50"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={busy || !reason.trim()}
              onClick={() => onReject(reason.trim(), action.trim() || undefined)}
              className="rounded-lg bg-[var(--state-killed)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {busy ? "…" : "Turn down & steer"}
            </button>
          </div>
        </div>
      ) : null}
    </Card>
  );
}
