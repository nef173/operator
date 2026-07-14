"use client";

// Horizontal low → median → high price-range bar with a median marker.
function fmtMoney(n: number) {
  if (!Number.isFinite(n)) return "—";
  return `$${Math.round(n).toLocaleString()}`;
}

export function PriceBand({
  low,
  med,
  high,
}: {
  low: number;
  med: number;
  high: number;
}) {
  const span = high - low || 1;
  const medPct = Math.max(0, Math.min(100, ((med - low) / span) * 100));
  return (
    <div className="w-full">
      <div className="relative h-1.5 w-full rounded-full bg-[var(--surface-2)]">
        <div
          className="absolute inset-y-0 left-0 rounded-full"
          style={{
            width: "100%",
            background:
              "linear-gradient(90deg, color-mix(in srgb, var(--accent) 30%, transparent), var(--accent))",
            opacity: 0.55,
          }}
        />
        <div
          className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-[var(--surface)]"
          style={{ left: `${medPct}%`, background: "var(--accent)" }}
          title={`Median ${fmtMoney(med)}`}
        />
      </div>
      <div className="mt-1.5 flex items-center justify-between text-xs tabular-nums">
        <span className="text-[var(--muted)]">{fmtMoney(low)}</span>
        <span className="font-semibold">{fmtMoney(med)}</span>
        <span className="text-[var(--muted)]">{fmtMoney(high)}</span>
      </div>
    </div>
  );
}
