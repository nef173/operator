import type { ReactNode } from "react";
import type { SkuState } from "@/lib/api";

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`rounded-xl border border-[var(--border)] bg-[var(--surface)] ${className}`}
    >
      {children}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
  compact,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  compact?: boolean;
}) {
  return (
    <Card className={compact ? "px-3 py-2" : "p-4"}>
      <div className="text-xs font-medium text-[var(--muted)]">{label}</div>
      <div className={`font-bold tracking-tight ${compact ? "text-lg" : "mt-1 text-2xl"}`}>
        {value}
      </div>
      {hint ? (
        <div className={`text-xs text-[var(--muted)] ${compact ? "leading-tight" : "mt-0.5"}`}>
          {hint}
        </div>
      ) : null}
    </Card>
  );
}

export function StateBadge({ state }: { state: SkuState | string }) {
  const color = `var(--state-${state}, var(--muted))`;
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-medium"
      style={{
        color,
        background: `color-mix(in srgb, ${color} 14%, transparent)`,
      }}
    >
      <span
        className="h-1.5 w-1.5 rounded-full"
        style={{ background: color }}
      />
      {state}
    </span>
  );
}

export function Pill({
  children,
  tone,
}: {
  children: ReactNode;
  tone?: "ok" | "warn" | "danger" | "info";
}) {
  const toneCls =
    tone === "ok"
      ? "border-[color-mix(in_srgb,var(--accent)_40%,var(--border))] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] text-[var(--accent)]"
      : tone === "warn"
        ? "border-[color-mix(in_srgb,var(--warning)_40%,var(--border))] bg-[color-mix(in_srgb,var(--warning)_12%,transparent)] text-[var(--warning)]"
        : tone === "danger"
          ? "border-[color-mix(in_srgb,var(--danger)_40%,var(--border))] bg-[color-mix(in_srgb,var(--danger)_12%,transparent)] text-[var(--danger)]"
          : tone === "info"
            ? "border-[color-mix(in_srgb,var(--info,#3b82f6)_40%,var(--border))] bg-[color-mix(in_srgb,var(--info,#3b82f6)_12%,transparent)] text-[var(--info,#3b82f6)]"
            : "border-[var(--border)] bg-[var(--surface-2)] text-[var(--muted)]";
  return (
    <span
      className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium ${toneCls}`}
    >
      {children}
    </span>
  );
}
