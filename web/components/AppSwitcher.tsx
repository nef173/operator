"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { useRouter, usePathname } from "next/navigation";
import { OPERATOR_APPS, BACKEND_APPS, type OperatorApp } from "@/lib/apps";
import { useEmbedded } from "@/lib/embed";

// Fast app navigator — a popover off the sidebar logo. It mirrors the Operation-System
// shell IN MINIATURE: the operation has two faces (Frontend = the build apps; Backend =
// the operation backend — P&L, product management, issue management, tasks, store mgmt,
// plus the control plane), each a one-click jump. This lets the operator switch apps
// without round-tripping through /shell. Used by BOTH sidebars so the chrome is
// consistent across every app.

const I = (d: ReactNode) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    {d}
  </svg>
);

const APP_ICONS: Record<OperatorApp["icon"], ReactNode> = {
  research: I(<><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></>),
  feed: I(<><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18M9 21V9" /></>),
  multimarket: I(<><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18" /></>),
  finance: I(<><path d="M12 1v22M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></>),
  "company-pl": I(<><path d="M3 3v18h18" /><path d="M7 15l4-5 3 3 5-7" /></>),
  product: I(<><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /><path d="m3.3 7 8.7 5 8.7-5M12 22V12" /></>),
  issues: I(<><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /><path d="M12 7v4M12 15h.01" /></>),
  tasks: I(<><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></>),
  store: I(<><path d="M3 9l1-5h16l1 5M4 9v11a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V9M3 9h18M9 21v-6h6v6" /></>),
  activity: I(<path d="M22 12h-4l-3 9L9 3l-3 9H2" />),
  settings: I(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></>),
  costs: I(<><line x1="12" y1="1" x2="12" y2="23" /><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></>),
};

type Face = "frontend" | "backend";

export function AppSwitcher({
  logo,
  title,
  subtitle,
  activeAppId,
}: {
  logo: ReactNode; // the small icon/badge shown in the trigger
  title: string;
  subtitle: string;
  activeAppId: string; // which OPERATOR_APPS entry is the current app (highlighted)
}) {
  const router = useRouter();
  const pathname = usePathname();
  const embedded = useEmbedded();
  const [open, setOpen] = useState(false);
  const [face, setFace] = useState<Face>("frontend");
  const ref = useRef<HTMLDivElement>(null);
  // Hover-to-open: the switcher shows on hover (no click needed) and closes shortly after the
  // pointer leaves the whole area. The delay stops flicker when crossing the trigger↔popover gap.
  const closeTimer = useRef<number | null>(null);
  const cancelClose = () => {
    if (closeTimer.current != null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  };
  const hoverOpen = () => {
    cancelClose();
    setOpen(true);
  };
  const hoverClose = () => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => setOpen(false), 250);
  };

  // Close on outside click + on Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Close whenever the route changes (we navigated).
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  function go(href: string) {
    setOpen(false);
    router.push(href);
  }

  // Embedded in the NN dashboard: NN owns the cross-app navigation, so the switcher collapses
  // to a plain, non-interactive brand header — no popover, no "Operation System" menu, no way
  // back into the operator's standalone shell.
  if (embedded) {
    return (
      <div className="flex items-center gap-2.5 px-3 pb-4 pt-4">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[var(--accent)] text-[var(--accent-fg)] text-sm font-bold">
          {logo}
        </div>
        <div className="leading-tight">
          <div className="text-[15px] font-semibold">{title}</div>
          {subtitle ? <div className="text-xs text-[var(--muted)]">{subtitle}</div> : null}
        </div>
      </div>
    );
  }

  return (
    <div ref={ref} className="relative" onMouseEnter={cancelClose} onMouseLeave={hoverClose}>
      {/* Back navigation lives on its own slim row at the very top — a plain text link,
          not an icon jammed against the app logo. Keeps the logo header clean. */}
      <button
        onClick={() => router.back()}
        title="Back"
        aria-label="Back to previous page"
        className="flex w-full items-center gap-1 px-4 pb-0.5 pt-2.5 text-[11px] font-medium text-[var(--muted)] transition-colors hover:text-[var(--text)]"
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M15 18l-6-6 6-6" />
        </svg>
        Back
      </button>
      {/* Trigger — the logo header. Opens on hover (and still toggles on click/tap for touch). */}
      <button
        onClick={() => setOpen((v) => !v)}
        onMouseEnter={hoverOpen}
        title="Switch app"
        className="group flex w-full min-w-0 items-center gap-2.5 px-3 pb-4 pt-1 text-left hover:bg-[var(--surface-2)]"
      >
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[var(--accent)] text-[var(--accent-fg)] text-sm font-bold">
          {logo}
        </div>
        <div className="leading-tight">
          <div className="text-[15px] font-semibold">{title}</div>
          {subtitle ? <div className="text-xs text-[var(--muted)]">{subtitle}</div> : null}
        </div>
        <svg
          className={`ml-auto text-[var(--muted)] transition-transform ${open ? "rotate-180" : ""}`}
          width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      {open ? (
        <div className="absolute left-2 right-2 top-full z-50 mt-1.5 rounded-xl border border-[var(--border)] bg-[var(--bg)] p-2.5 shadow-2xl ring-1 ring-black/30">
          <div className="px-2 pb-1.5 pt-1 text-[10px] font-semibold uppercase tracking-wider text-[var(--muted)]">
            Operation System
          </div>

          {/* The two faces — Frontend (operational apps) and Backend (control plane).
              Each face lists ONLY its own apps (face-filtered from the registry). */}
          <div className="grid grid-cols-2 gap-1.5">
            <button
              onClick={() => setFace("frontend")}
              className={`rounded-lg border px-2.5 py-2 text-left text-xs font-semibold transition-colors ${
                face === "frontend"
                  ? "border-[var(--accent)] bg-[var(--surface-2)] text-[var(--text)]"
                  : "border-[var(--border)] text-[var(--muted)] hover:text-[var(--text)]"
              }`}
            >
              Frontend
              <span className="mt-0.5 block text-[10px] font-normal text-[var(--muted)]">The apps</span>
            </button>
            <button
              onClick={() => setFace("backend")}
              className={`rounded-lg border px-2.5 py-2 text-left text-xs font-semibold transition-colors ${
                face === "backend"
                  ? "border-[var(--accent)] bg-[var(--surface-2)] text-[var(--text)]"
                  : "border-[var(--border)] text-[var(--muted)] hover:text-[var(--text)]"
              }`}
            >
              Backend
              <span className="mt-0.5 block text-[10px] font-normal text-[var(--muted)]">Operation & control</span>
            </button>
          </div>

          {/* Apps for the selected face — frontend apps under Frontend, control-plane
              apps under Backend. Nothing crosses over. */}
          <div className="mt-1.5 space-y-0.5">
            {(face === "frontend" ? OPERATOR_APPS : BACKEND_APPS).map((app) => {
              const soon = app.status === "soon";
              const active = app.id === activeAppId;
              return (
                <button
                  key={app.id}
                  disabled={soon}
                  onClick={() => !soon && go(app.href)}
                  className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2.5 text-left transition-colors ${
                    active
                      ? "bg-[var(--surface-2)]"
                      : soon
                        ? "cursor-not-allowed opacity-60"
                        : "hover:bg-[var(--surface-2)]"
                  }`}
                >
                  <span style={{ color: active ? "var(--accent)" : "var(--muted)" }}>
                    {APP_ICONS[app.icon]}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5 text-sm font-medium">
                      {app.name}
                      {soon ? (
                        <span className="rounded bg-[var(--surface-2)] px-1 text-[9px] font-semibold uppercase text-[var(--muted)]">
                          Soon
                        </span>
                      ) : null}
                    </span>
                  </span>
                  {active ? (
                    <span className="text-[10px] font-semibold uppercase text-[var(--accent)]">Current</span>
                  ) : !soon ? (
                    <span className="text-[var(--muted)]">→</span>
                  ) : null}
                </button>
              );
            })}
          </div>

          {/* Full launcher. */}
          <div className="my-1.5 border-t border-[var(--border)]" />
          <button
            onClick={() => go("/dashboard")}
            className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left text-xs font-medium text-[var(--muted)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
          >
            Open full Operation System →
          </button>
        </div>
      ) : null}
    </div>
  );
}
