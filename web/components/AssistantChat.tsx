"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  api,
  type AssistantAction,
  type AssistantMessage,
  type AutonomyMode,
  type Cadence,
  type JobRecord,
} from "@/lib/api";
import { useStore } from "@/components/StoreProvider";
import { Card, Pill } from "@/components/ui";
import { ConfirmDialog } from "@/components/ConfirmDialog";

// The Assistant — a read-only copilot. It reads pipeline state and PROPOSES actions;
// it NEVER runs anything on its own. Each proposed action renders as a button that opens
// a CONFIRM dialog; on confirm we call the SAME existing, logged control endpoint a manual
// button would use (createJob / setAutonomy / approveDecision / rejectDecision / workerTick
// / setWorkerEnabled) — or just navigate. The operator is always the decision node.
//
// Extracted from the old standalone Assistant page so it can live INSIDE the Decisions tab:
// rejecting a decision with a reason and telling the assistant "this keyword is bad, look
// further" are the same act — both steer the worker. They belong together.

type ChatTurn =
  | { kind: "user"; content: string }
  | { kind: "assistant"; content: string; actions: AssistantAction[] }
  | { kind: "system"; content: string }
  // The OUTPUT of a job the assistant ran, shown inline so the operator can read it and
  // approve / reject / refine — that verdict becomes a learning the assistant reads back.
  | { kind: "result"; spec: string; store: string | null; job: JobRecord; done: boolean };

const SUGGESTIONS = [
  "What needs my decision?",
  "What's trending right now?",
  "This keyword is weak — look further",
  "What's running right now?",
];

// A plain one-liner describing what an action will do (shown in the confirm dialog).
function actionDescription(a: AssistantAction): string {
  switch (a.type) {
    case "run_job":
      return `Start the "${a.spec}" step${a.store ? ` for ${a.store}` : ""}. Some steps run on their own; others hand you the exact thing to run.`;
    case "set_autonomy":
      return `Set step "${a.step}" to ${a.mode ?? "(unchanged)"}, ${a.cadence ?? "(unchanged)"}.`;
    case "approve_decision":
      return `Approve #${a.id}. Approving is the go-ahead — this runs it.`;
    case "reject_decision":
      return `Turn down #${a.id}. Nothing runs.`;
    case "worker_tick":
      return `Do one scheduled pass${a.store ? ` for ${a.store}` : ""}.`;
    case "set_worker_enabled":
      return `${a.enabled ? "Turn on" : "Turn off"} the background worker.`;
    case "navigate":
      return `Open ${a.href}.`;
    default:
      return "Run this.";
  }
}

export function AssistantChat() {
  const { store } = useStore();
  const router = useRouter();

  const activeStore = store || null;

  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // The action awaiting an explicit confirm click (null = no dialog open).
  const [confirming, setConfirming] = useState<AssistantAction | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // A run_job we just created, captured during confirm so onActionDone can poll it for output.
  const pendingJobRef = useRef<{ job: JobRecord; spec: string; store: string | null } | null>(null);

  function scrollToBottom() {
    requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
    });
  }

  async function send(text: string) {
    const content = text.trim();
    if (!content || sending) return;
    setErr(null);
    const nextTurns: ChatTurn[] = [...turns, { kind: "user", content }];
    setTurns(nextTurns);
    setInput("");
    setSending(true);
    scrollToBottom();

    // Build the wire conversation from the running turn log (user + assistant only).
    const wire: AssistantMessage[] = nextTurns
      .filter((t): t is ChatTurn & { kind: "user" | "assistant" } =>
        t.kind === "user" || t.kind === "assistant")
      .map((t) => ({ role: t.kind, content: t.content }));

    try {
      const res = await api.assistantChat(wire, activeStore);
      setTurns((prev) => [
        ...prev,
        { kind: "assistant", content: res.reply, actions: res.proposed_actions ?? [] },
      ]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
      scrollToBottom();
    }
  }

  // Execute a confirmed action by routing to the matching EXISTING api client method.
  // Never called without the operator's explicit confirm click.
  async function runAction(a: AssistantAction): Promise<void> {
    switch (a.type) {
      case "run_job": {
        if (!a.spec || !a.store) throw new Error("run_job needs a spec and store");
        const job = await api.createJob(a.spec, a.store, a.args);
        // Stash it so onActionDone can poll for the output and show it inline for review.
        pendingJobRef.current = { job, spec: a.spec, store: a.store };
        break;
      }
      case "set_autonomy":
        if (!a.step) throw new Error("set_autonomy needs a step");
        await api.setAutonomy(
          a.step,
          (a.mode ?? undefined) as AutonomyMode | undefined,
          (a.cadence ?? undefined) as Cadence | undefined,
        );
        break;
      case "approve_decision":
        if (a.id == null) throw new Error("approve_decision needs an id");
        await api.approveDecision(a.id);
        break;
      case "reject_decision":
        if (a.id == null) throw new Error("reject_decision needs an id");
        await api.rejectDecision(a.id);
        break;
      case "worker_tick":
        if (!a.store) throw new Error("worker_tick needs a store");
        await api.workerTick(a.store);
        break;
      case "set_worker_enabled":
        if (typeof a.enabled !== "boolean") throw new Error("set_worker_enabled needs enabled");
        await api.setWorkerEnabled(a.enabled);
        break;
      case "navigate":
        if (!a.href) throw new Error("navigate needs an href");
        router.push(a.href);
        break;
      default:
        throw new Error(`unknown action type: ${a.type}`);
    }
  }

  // Poll a created job until it reaches a terminal status, keeping the inline
  // result turn's job/output fresh so the operator can read it and act on it.
  async function pollJob(jobId: number) {
    const terminal = new Set(["done", "failed", "needs-operator"]);
    for (let i = 0; i < 150; i++) {
      let job: JobRecord;
      try {
        job = await api.job(jobId);
      } catch {
        break;
      }
      const isDone = terminal.has(job.status);
      setTurns((prev) =>
        prev.map((t) =>
          t.kind === "result" && t.job.id === jobId ? { ...t, job, done: isDone } : t,
        ),
      );
      scrollToBottom();
      if (isDone) return;
      await new Promise((r) => setTimeout(r, 2000));
    }
  }

  function onActionDone(a: AssistantAction) {
    // A run_job gets an inline result turn we poll for output + review controls.
    const pending = pendingJobRef.current;
    if (a.type === "run_job" && pending) {
      pendingJobRef.current = null;
      const terminal = new Set(["done", "failed", "needs-operator"]);
      const alreadyDone = terminal.has(pending.job.status);
      setTurns((prev) => [
        ...prev,
        {
          kind: "result",
          spec: pending.spec,
          store: pending.store,
          job: pending.job,
          done: alreadyDone,
        },
      ]);
      scrollToBottom();
      if (!alreadyDone) void pollJob(pending.job.id);
      return;
    }
    setTurns((prev) => [
      ...prev,
      { kind: "system", content: `Ran: ${a.label}` },
    ]);
    scrollToBottom();
  }

  // Record an operator verdict on a job result as a learning the assistant reads
  // back. "refine" also re-asks the assistant with the note so it continues with
  // context (the new learning is now in its snapshot).
  async function giveFeedback(
    verdict: "approve" | "reject" | "refine",
    spec: string,
    store: string | null,
    jobId: number,
    note: string,
  ) {
    const trimmed = note.trim();
    try {
      await api.assistantFeedback({
        verdict,
        note: trimmed || undefined,
        spec,
        store,
        job_id: jobId,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      return;
    }
    const label =
      verdict === "approve" ? "Approved" : verdict === "reject" ? "Turned down" : "Refining";
    setTurns((prev) => [
      ...prev,
      {
        kind: "system",
        content: `${label} "${spec}"${trimmed ? ` — ${trimmed}` : ""}`,
      },
    ]);
    scrollToBottom();
    // For refine, continue the conversation so the assistant acts on the note now.
    if (verdict === "refine" && trimmed) {
      void send(`About the "${spec}" result: ${trimmed}`);
    }
  }

  const empty = turns.length === 0;

  return (
    <div className="flex h-full min-h-[24rem] flex-col">
      {/* Message list */}
      <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto pb-4">
        {empty ? (
          <Card className="p-5">
            <div className="text-sm font-semibold">Talk to the assistant</div>
            <div className="mt-1 text-sm text-[var(--muted)]">
              Same as turning a suggestion down with a reason — tell it what&rsquo;s good, what&rsquo;s
              weak, or what to look into next, and it steers the work. It never runs anything until
              you say yes. Try:
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => send(s)}
                  className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium text-[var(--muted)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
                >
                  {s}
                </button>
              ))}
            </div>
          </Card>
        ) : (
          turns.map((t, i) => (
            <TurnRow key={i} turn={t} onPropose={setConfirming} onFeedback={giveFeedback} />
          ))
        )}
        {sending ? (
          <div className="flex justify-start">
            <div className="rounded-2xl bg-[var(--surface-2)] px-4 py-2 text-sm text-[var(--muted)]">
              Thinking…
            </div>
          </div>
        ) : null}
      </div>

      {err ? (
        <div className="mb-2 text-xs text-[var(--state-killed)]">{err}</div>
      ) : null}

      {/* Composer */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
        className="flex items-end gap-2 border-t border-[var(--border)] pt-3"
      >
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(input);
            }
          }}
          rows={1}
          placeholder="Tell it what to find, fix, or look into…"
          className="min-h-[2.5rem] flex-1 resize-none rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
        />
        <button
          type="submit"
          disabled={sending || !input.trim()}
          className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--accent-fg)] disabled:opacity-50"
        >
          Send
        </button>
      </form>

      {/* Confirm step — nothing runs without this explicit click. */}
      <ConfirmDialog
        open={confirming !== null}
        title={confirming ? confirming.label : ""}
        body={confirming ? actionDescription(confirming) : ""}
        confirmLabel={
          confirming?.type === "reject_decision" ? "Turn down" : "Yes, do it"
        }
        tone={confirming?.type === "reject_decision" ? "danger" : "accent"}
        onConfirm={async () => {
          if (confirming) await runAction(confirming);
        }}
        onDone={() => {
          if (confirming) onActionDone(confirming);
        }}
        onClose={() => setConfirming(null)}
      />
    </div>
  );
}

function TurnRow({
  turn,
  onPropose,
  onFeedback,
}: {
  turn: ChatTurn;
  onPropose: (a: AssistantAction) => void;
  onFeedback: (
    verdict: "approve" | "reject" | "refine",
    spec: string,
    store: string | null,
    jobId: number,
    note: string,
  ) => void;
}) {
  if (turn.kind === "result") {
    return <ResultRow turn={turn} onFeedback={onFeedback} />;
  }

  if (turn.kind === "system") {
    return (
      <div className="flex justify-center">
        <span className="rounded-full bg-[var(--surface-2)] px-3 py-1 text-xs text-[var(--muted)]">
          {turn.content}
        </span>
      </div>
    );
  }

  if (turn.kind === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl bg-[var(--accent)] px-4 py-2 text-sm text-[var(--accent-fg)]">
          {turn.content}
        </div>
      </div>
    );
  }

  // assistant
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%]">
        <Card className="px-4 py-3">
          <div className="whitespace-pre-wrap text-sm">{turn.content}</div>
        </Card>
        {turn.actions.length > 0 ? (
          <div className="mt-2 space-y-1.5">
            <div className="text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
              You confirm before anything runs
            </div>
            <div className="flex flex-wrap gap-2">
              {turn.actions.map((a, i) => (
                <button
                  key={i}
                  type="button"
                  onClick={() => onPropose(a)}
                  className="inline-flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--surface-2)]"
                >
                  {a.label}
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// The inline output of a job the assistant ran, with Approve / Reject / Refine
// controls. The operator's verdict (+ optional note) becomes a learning the
// assistant reads back on its next turn.
function ResultRow({
  turn,
  onFeedback,
}: {
  turn: Extract<ChatTurn, { kind: "result" }>;
  onFeedback: (
    verdict: "approve" | "reject" | "refine",
    spec: string,
    store: string | null,
    jobId: number,
    note: string,
  ) => void;
}) {
  const [note, setNote] = useState("");
  const [acted, setActed] = useState(false);
  const { job, spec, store, done } = turn;

  const statusTone =
    job.status === "done"
      ? "var(--state-live, #16a34a)"
      : job.status === "failed"
        ? "var(--state-killed)"
        : "var(--muted)";

  function act(verdict: "approve" | "reject" | "refine") {
    if (acted) return;
    setActed(true);
    onFeedback(verdict, spec, store, job.id, note);
  }

  return (
    <div className="flex justify-start">
      <div className="w-full max-w-[85%]">
        <Card className="px-4 py-3">
          <div className="flex items-center justify-between gap-2">
            <div className="text-sm font-semibold">
              {job.title ?? spec}
              {store ? <span className="text-[var(--muted)]"> · {store}</span> : null}
            </div>
            <span className="text-xs font-medium" style={{ color: statusTone }}>
              {done ? job.status : `${job.status}…`}
            </span>
          </div>

          {job.detail ? (
            <div className="mt-1 text-xs text-[var(--muted)]">{job.detail}</div>
          ) : null}

          {!done ? (
            <div className="mt-3 text-sm text-[var(--muted)]">Running…</div>
          ) : (
            <>
              {job.command ? (
                <pre className="mt-3 max-h-32 overflow-auto rounded-lg bg-[var(--surface-2)] px-3 py-2 text-xs whitespace-pre-wrap">
                  {job.command}
                </pre>
              ) : null}
              {job.output ? (
                <pre className="mt-2 max-h-72 overflow-auto rounded-lg bg-[var(--surface-2)] px-3 py-2 text-xs whitespace-pre-wrap">
                  {job.output}
                </pre>
              ) : (
                <div className="mt-3 text-sm text-[var(--muted)]">No output produced.</div>
              )}
            </>
          )}
        </Card>

        {done ? (
          acted ? (
            <div className="mt-2 text-xs text-[var(--muted)]">
              Got it — the assistant will remember this.
            </div>
          ) : (
            <div className="mt-2 space-y-2">
              <div className="text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                Your rating teaches the assistant
              </div>
              <textarea
                value={note}
                onChange={(e) => setNote(e.target.value)}
                rows={2}
                placeholder="Optional: what's good / weak / look more into… (sent with your rating)"
                className="w-full resize-none rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-[var(--accent)]"
              />
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => act("approve")}
                  className="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-[var(--accent-fg)]"
                >
                  Good
                </button>
                <button
                  type="button"
                  onClick={() => act("reject")}
                  className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium text-[var(--state-killed)] hover:bg-[var(--surface-2)]"
                >
                  Not good
                </button>
                <button
                  type="button"
                  onClick={() => act("refine")}
                  className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium hover:bg-[var(--surface-2)]"
                >
                  Look more into this
                </button>
              </div>
            </div>
          )
        ) : null}
      </div>
    </div>
  );
}
