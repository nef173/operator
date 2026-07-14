"use client";

import { useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api";

/**
 * Minimal confirm modal used by every write action (the locked "confirm each write"
 * rule). Owns its own pending + inline-error state: it runs `onConfirm` (an async
 * write), shows a spinner while it's in flight, surfaces a 4xx `detail` inline (e.g. a
 * promote rejected by the gate), and only closes via `onClose` on success.
 */
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "Confirm",
  tone = "accent",
  onConfirm,
  onClose,
  onDone,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  tone?: "accent" | "danger";
  onConfirm: () => Promise<void>;
  onClose: () => void;
  onDone?: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) return null;

  const color = tone === "danger" ? "var(--state-killed)" : "var(--accent)";

  async function run() {
    setPending(true);
    setError(null);
    try {
      await onConfirm();
      onDone?.();
      onClose();
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.message
          : e instanceof Error
            ? e.message
            : "Something went wrong";
      setError(msg);
    } finally {
      setPending(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={() => (pending ? null : onClose())}
    >
      <div
        className="w-full max-w-md rounded-xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold">{title}</h3>
        <div className="mt-2 text-sm text-[var(--muted)]">{body}</div>

        {error ? (
          <div
            className="mt-3 rounded-lg px-3 py-2 text-xs"
            style={{
              color: "var(--state-killed)",
              background: "color-mix(in srgb, var(--state-killed) 12%, transparent)",
            }}
          >
            {error}
          </div>
        ) : null}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={pending}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium text-[var(--muted)] hover:bg-[var(--surface-2)] disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={run}
            disabled={pending}
            className="rounded-lg px-3 py-1.5 text-sm font-semibold disabled:opacity-60"
            style={{ background: color, color: "var(--accent-fg)" }}
          >
            {pending ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
