"use client";

import { useEffect, type ReactNode } from "react";

/**
 * Read-only detail popup. Where ConfirmDialog asks "do this write?", DetailModal just
 * shows the full info for one found item (a keyword / trend / product) so dense surfaces
 * stay a clean overview list and the detail lives in a popup — not squeezed inline.
 *
 * Closes on backdrop click and Escape. `title`/`subtitle`/`badge` make the header; the
 * body is whatever the caller renders (usually a <DetailGrid>).
 */
export function DetailModal({
  open,
  title,
  subtitle,
  badge,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: ReactNode;
  subtitle?: ReactNode;
  badge?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--surface)] shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3 border-b border-[var(--border)] p-5">
          <div className="min-w-0 flex-1">
            {badge ? <div className="mb-1.5">{badge}</div> : null}
            <h3 className="text-base font-semibold leading-snug">{title}</h3>
            {subtitle ? (
              <div className="mt-1 text-xs text-[var(--muted)]">{subtitle}</div>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="shrink-0 rounded-md px-2 py-1 text-sm text-[var(--muted)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
          >
            ✕
          </button>
        </div>

        <div className="p-5">{children}</div>

        {footer ? (
          <div className="flex flex-wrap items-center justify-end gap-2 border-t border-[var(--border)] p-4">
            {footer}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// A label/value grid for the popup body. Rows with a null/undefined value are skipped,
// so callers can pass everything and only the present fields render.
export function DetailGrid({ rows }: { rows: { label: string; value: ReactNode }[] }) {
  const present = rows.filter((r) => r.value !== null && r.value !== undefined && r.value !== "");
  if (present.length === 0) return null;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2.5 text-sm">
      {present.map((r) => (
        <div key={r.label} className="contents">
          <dt className="text-[var(--muted)]">{r.label}</dt>
          <dd className="text-right font-medium tabular-nums">{r.value}</dd>
        </div>
      ))}
    </dl>
  );
}
