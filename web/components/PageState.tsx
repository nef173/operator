import type { ReactNode } from "react";
import { Card } from "@/components/ui";

export function PageHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-4 flex items-start justify-between gap-4">
      <div>
        <h1>{title}</h1>
        {subtitle ? (
          <p className="mt-0.5 text-[13px] leading-snug text-[var(--muted)]">{subtitle}</p>
        ) : null}
      </div>
      {action}
    </div>
  );
}

export function Loading() {
  return (
    <Card className="p-8 text-sm text-[var(--muted)]">Loading…</Card>
  );
}

export function ErrorState({ message }: { message: string }) {
  // On the live deploy the usual cause is a ~30s api restart (every code deploy). useApi
  // silently retries every few seconds, so this card self-clears — say that, instead of
  // scaring the operator with dev instructions. The uvicorn hint only helps in local dev.
  const isLocalDev =
    typeof window !== "undefined" && window.location.hostname === "localhost";
  return (
    <Card className="p-6">
      <div className="flex items-center gap-2 text-sm font-semibold text-[var(--state-killed)]">
        <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-[var(--state-killed)] border-t-transparent" />
        Backend not answering — reconnecting automatically…
      </div>
      <div className="mt-1 text-sm text-[var(--muted)]">{message}</div>
      <div className="mt-3 text-xs text-[var(--muted)]">
        {isLocalDev ? (
          <>
            Local dev: start it with{" "}
            <code className="rounded bg-[var(--surface-2)] px-1.5 py-0.5 font-mono">
              uvicorn app.main:app --port 8077
            </code>{" "}
            in <code className="font-mono">operator-app/api</code>.
          </>
        ) : (
          <>
            This usually means the api is restarting (e.g. a deploy) and clears by itself in
            under a minute — no action needed. If it keeps showing, check the api service on Railway.
          </>
        )}
      </div>
    </Card>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <Card className="p-8 text-center text-sm text-[var(--muted)]">{children}</Card>
  );
}

// A clean "needs setup / not connected" notice — distinct from ErrorState (which implies a
// crashed/unreachable backend). Use when the backend is fine but a credential is missing.
export function ConnectNotice({
  title = "Not connected",
  message,
  cta = "Connect in Settings · Connections",
  href = "/settings",
  note,
}: {
  title?: string;
  message: string;
  cta?: string;
  href?: string;
  note?: string;
}) {
  return (
    <Card className="p-6">
      <div className="mb-1 flex items-center gap-2">
        <span className="h-2 w-2 rounded-full bg-[var(--muted)]" />
        <span className="text-sm font-semibold">{title}</span>
      </div>
      <p className="text-sm text-[var(--muted)]">{message}</p>
      <a
        href={href}
        className="mt-4 inline-flex items-center rounded-lg bg-[var(--accent)] px-3.5 py-2 text-sm font-semibold text-[var(--accent-fg)]"
      >
        {cta}
      </a>
      {note ? <p className="mt-3 text-xs text-[var(--muted)]">{note}</p> : null}
    </Card>
  );
}
