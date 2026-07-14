"use client";

// 0-100 competition index rendered as a compact bar + numeric label.
export function CompetitionMeter({
  index,
  label,
}: {
  index: number | null;
  label?: string | null;
}) {
  if (index == null) {
    return <span className="text-xs text-[var(--muted)]">—</span>;
  }
  const pct = Math.max(0, Math.min(100, index));
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-[var(--surface-2)]">
        <div
          className="h-full rounded-full"
          style={{
            width: `${pct}%`,
            background: "var(--accent)",
          }}
        />
      </div>
      <span className="text-xs tabular-nums text-[var(--muted)]">
        {label ? `${label} · ${pct}` : pct}
      </span>
    </div>
  );
}
