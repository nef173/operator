"use client";

// Hand-rolled inline-SVG sparkline / area chart. No external deps.
// Used for both keyword 12-mo SV series and long (~130pt) Trends interest series.
export function Sparkline({
  values,
  width = 240,
  height = 56,
  area = true,
  emphasizeLast = true,
  strokeWidth = 1.75,
}: {
  values: number[];
  width?: number;
  height?: number;
  area?: boolean;
  emphasizeLast?: boolean;
  strokeWidth?: number;
}) {
  const clean = values.filter((v) => typeof v === "number" && !Number.isNaN(v));
  if (clean.length < 2) {
    return (
      <div className="text-xs text-[var(--muted)]">Not enough data</div>
    );
  }

  const pad = 3;
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const span = max - min || 1;
  const innerW = width - pad * 2;
  const innerH = height - pad * 2;

  const points = clean.map((v, i) => {
    const x = pad + (i / (clean.length - 1)) * innerW;
    const y = pad + (1 - (v - min) / span) * innerH;
    return [x, y] as const;
  });

  const linePath = points
    .map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`)
    .join(" ");

  const areaPath =
    `M${points[0][0].toFixed(2)} ${(height - pad).toFixed(2)} ` +
    points.map(([x, y]) => `L${x.toFixed(2)} ${y.toFixed(2)}`).join(" ") +
    ` L${points[points.length - 1][0].toFixed(2)} ${(height - pad).toFixed(2)} Z`;

  const last = points[points.length - 1];

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width="100%"
      height={height}
      preserveAspectRatio="none"
      role="img"
      aria-hidden="true"
      style={{ display: "block", overflow: "visible" }}
    >
      {area ? (
        <path d={areaPath} fill="var(--accent)" opacity={0.1} stroke="none" />
      ) : null}
      <path
        d={linePath}
        fill="none"
        stroke="var(--accent)"
        strokeWidth={strokeWidth}
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
      {emphasizeLast ? (
        <circle
          cx={last[0]}
          cy={last[1]}
          r={2.75}
          fill="var(--accent)"
          stroke="var(--surface)"
          strokeWidth={1.5}
        />
      ) : null}
    </svg>
  );
}
