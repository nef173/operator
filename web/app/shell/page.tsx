"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { OPERATOR_APPS, type OperatorApp } from "@/lib/apps";

// The Operation-System home screen — "the view before" the apps, redesigned to match the
// NN Operations launcher design language: a centered hero ("Hello — choose what you want to
// open"), a rotating business quote, a colored-dot KPI strip, and accent-glow app cards. It is
// operation-level, NOT per-store: the whole Google operation shows at a glance; store
// separation lives INSIDE each app. Settings + Log/Activity are reachable from the header.

const I = (d: React.ReactNode) => (
  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
    {d}
  </svg>
);

const APP_ICONS: Record<OperatorApp["icon"], React.ReactNode> = {
  research: I(<><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></>),
  feed: I(<><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18M9 21V9" /></>),
  multimarket: I(<><circle cx="12" cy="12" r="10" /><path d="M2 12h20" /><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" /></>),
  finance: I(<><path d="M3 3v17a1 1 0 0 0 1 1h17" /><rect x="7" y="13" width="3" height="5" rx="0.8" /><rect x="12" y="8" width="3" height="10" rx="0.8" /><rect x="17" y="10.5" width="3" height="7.5" rx="0.8" /></>),
  "company-pl": I(<><path d="M3 3v18h18" /><path d="M7 15l4-5 3 3 5-7" /></>),
  product: I(<><path d="M7.5 4.27 16.5 9.42" /><path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z" /><path d="m3.3 7 8.7 5 8.7-5" /><path d="M12 22V12" /></>),
  issues: I(<><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></>),
  tasks: I(<><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></>),
  store: I(<><path d="M3 9l1-5h16l1 5M4 9v11a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V9M3 9h18M9 21v-6h6v6" /></>),
  activity: I(<path d="M22 12h-4l-3 9L9 3l-3 9H2" />),
  settings: I(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></>),
  costs: I(<><line x1="12" y1="1" x2="12" y2="23" /><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></>),
};

// Per-app accent — mirrors the NN launcher palette; gives each card its own glow.
const APP_ACCENT: Record<OperatorApp["icon"], string> = {
  research: "#6366f1",
  feed: "#ec4899",
  multimarket: "#22d3ee",
  finance: "#34d399",
  "company-pl": "#10b981",
  product: "#8b5cf6",
  issues: "#f59e0b",
  tasks: "#0ea5e9",
  store: "#14b8a6",
  activity: "#f472b6",
  settings: "#94a3b8",
  costs: "#facc15",
};

function money(v: number | null | undefined) {
  if (v == null) return "—";
  return "€" + new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(v);
}

// Date-scope options for the KPI strip — keys into the P&L's per-window rollups
// (windowTotals + per-store window objects), so the numbers actually FOLLOW the
// selector: "Today" shows today, not the 30-day rollup the legacy `totals` holds.
// (No 90d option: the backend computes one 30-day pass and slices it.)
type WindowKey = "today" | "yesterday" | "w7" | "w14" | "w30";
const RANGES: { value: string; label: string }[] = [
  { value: "today", label: "Today" },
  { value: "yesterday", label: "Yesterday" },
  { value: "w7", label: "Last 7 days" },
  { value: "w14", label: "Last 14 days" },
  { value: "w30", label: "Last 30 days" },
];

export default function ShellPage() {
  const { data, error } = useApi(() => api.shell());
  // Per-app access (RBAC): any app the user isn't granted is hidden; a `rep` also loses the
  // P&L KPI strip.
  const access = useApi(() => api.access());
  const restricted = access.data?.restricted ?? [];
  const isRep = access.data?.role === "rep";
  const apps = OPERATOR_APPS.filter((a) => !restricted.includes(a.id));
  // Operation-wide P&L KPIs — real, from Finance. Date scope + store scope are operator-selectable;
  // defaults are ALWAYS today and all stores (aggregate). One 30-day fetch serves every window.
  const [win, setWin] = useState<WindowKey>("today");
  const [storeKey, setStoreKey] = useState("");
  const health = useApi(() => api.finStoreHealth(30));
  const healthStores = health.data?.ok ? health.data.stores : [];
  const ft = health.data?.ok
    ? storeKey
      ? (healthStores.find((s) => s.store === storeKey)?.[win] ?? null)
      : (health.data.windowTotals?.[win] ?? health.data.totals)
    : null;

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto bg-[var(--bg)]">
      {/* Ambient background — subtle radial glows, echoing the NN launcher. */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0"
        style={{
          background:
            "radial-gradient(60% 50% at 15% 0%, color-mix(in srgb, var(--accent) 12%, transparent), transparent 70%), radial-gradient(50% 45% at 90% 10%, color-mix(in srgb, #8b5cf6 10%, transparent), transparent 70%)",
        }}
      />

      <div className="relative z-10 flex min-h-full flex-col">
        {/* Header — brand left; operation utilities right. */}
        <header className="flex h-16 shrink-0 items-center justify-between gap-3 px-5 sm:px-8">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-[var(--accent)] text-sm font-bold text-[var(--accent-fg)]">
              GS
            </div>
            <span className="text-sm font-semibold leading-tight">Google Stores</span>
          </div>
          <div className="flex items-center gap-1.5">
            {access.data?.role ? (
              <span className="mr-1 hidden rounded-full border border-[var(--border)] px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-[var(--muted)] sm:inline">
                {access.data.role}
              </span>
            ) : null}
            <HeaderButton
              href="/activity"
              label="Log / Activity"
              icon={<path d="M22 12h-4l-3 9L9 3l-3 9H2" />}
            />
            <HeaderButton
              href="/settings"
              label="Settings"
              icon={
                <>
                  <circle cx="12" cy="12" r="3" />
                  <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                </>
              }
            />
          </div>
        </header>

        <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col px-5 py-7 sm:px-8 sm:py-9">
          <div className="my-auto w-full">
            <div
              className="text-[11px] font-semibold uppercase sm:text-[12px]"
              style={{ letterSpacing: ".28em", color: "var(--accent)" }}
            >
              Every field · one operation
            </div>
            <h1
              className="mt-3 text-4xl font-bold tracking-tight sm:text-6xl"
              style={{
                background: "linear-gradient(180deg, var(--text), var(--muted))",
                WebkitBackgroundClip: "text",
                backgroundClip: "text",
                color: "transparent",
                lineHeight: 1.05,
              }}
            >
              Operation System
            </h1>
            {error ? (
              <div className="mt-4 inline-flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-xs">
                <span className="font-semibold text-[var(--state-killed)]">Backend unreachable</span>
                <span className="text-[var(--muted)]">{error}</span>
              </div>
            ) : null}

            {/* KPI dot-strip — date + store scope are selectable; defaults are today + all stores.
                Hidden for reps; honest placeholder until Finance connects. */}
            {!isRep ? (
              <div className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-1.5 text-[13px]">
                <div className="flex items-center gap-2">
                  <Scope value={win} onChange={(v) => setWin(v as WindowKey)} options={RANGES} />
                  <Scope
                    value={storeKey}
                    onChange={setStoreKey}
                    options={[
                      { value: "", label: "All stores" },
                      ...healthStores.map((s) => ({ value: s.store, label: s.store })),
                    ]}
                  />
                </div>
                <Dot
                  color="#94a3b8"
                  label="stores"
                  value={storeKey ? "1" : data ? String(data.totals.stores) : "—"}
                />
                {ft ? (
                  <>
                    <Dot color="#34d399" label="revenue" value={money(ft.revenue)} />
                    <Dot
                      color={ft.profit >= 0 ? "#34d399" : "#fb7185"}
                      label="profit"
                      value={money(ft.profit)}
                    />
                    <Dot
                      color="#60a5fa"
                      label="margin"
                      value={ft.margin != null ? `${(ft.margin * 100).toFixed(0)}%` : "—"}
                    />
                    <Dot
                      color="#a78bfa"
                      label="ROAS"
                      value={ft.roas != null ? `${ft.roas.toFixed(1)}×` : "—"}
                    />
                  </>
                ) : (
                  <span className="inline-flex items-center gap-1.5 text-[var(--muted)]">
                    <span className="h-1.5 w-1.5 rounded-full bg-[var(--muted)]" />
                    Connect a store’s Shopify in Finance to populate revenue / profit / margin / ROAS.
                  </span>
                )}
              </div>
            ) : null}

            {/* App cards. */}
            <div className="mt-8 grid grid-cols-1 gap-5 sm:mt-10 sm:grid-cols-2 lg:grid-cols-3">
              {apps.map((app, i) => (
                <AppCard key={app.id} app={app} index={i} />
              ))}
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}

function HeaderButton({
  href,
  label,
  icon,
}: {
  href: string;
  label: string;
  icon: React.ReactNode;
}) {
  const router = useRouter();
  return (
    <button
      onClick={() => router.push(href)}
      title={label}
      className="flex h-9 w-9 items-center justify-center rounded-lg border border-[var(--border)] text-[var(--muted)] transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
    >
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
        {icon}
      </svg>
    </button>
  );
}

function Scope({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-lg border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)] outline-none transition-colors hover:text-[var(--text)] focus:border-[var(--accent)]"
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function Dot({ color, label, value }: { color: string; label: string; value: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      <span className="font-semibold text-[var(--text)]">{value}</span>
      <span className="text-[var(--muted)]">{label}</span>
    </span>
  );
}

const ARROW = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M5 12h14" />
    <path d="m12 5 7 7-7 7" />
  </svg>
);

function AppCard({ app, index }: { app: OperatorApp; index: number }) {
  const router = useRouter();
  const soon = app.status === "soon";
  const accent = APP_ACCENT[app.icon];
  return (
    <button
      disabled={soon}
      onClick={() => !soon && router.push(app.href)}
      style={{ animationDelay: `${index * 60}ms` }}
      className={`flex h-full flex-col rounded-2xl border p-6 text-left transition-all ${
        soon
          ? "cursor-not-allowed border-dashed border-[var(--border)] bg-transparent opacity-70"
          : "border-[var(--border)] bg-[var(--surface)] hover:-translate-y-0.5 hover:bg-[var(--surface-2)]"
      }`}
    >
      <div className="flex items-start justify-between">
        <span
          className="flex items-center justify-center"
          style={{
            width: 54,
            height: 54,
            borderRadius: 14,
            color: accent,
            background: `${accent}22`,
            boxShadow: `0 0 34px -8px ${accent}88, inset 0 0 0 1px ${accent}40`,
          }}
        >
          {APP_ICONS[app.icon]}
        </span>
        {soon ? (
          <span className="rounded-full border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-[var(--muted)]">
            Soon
          </span>
        ) : (
          <span className="flex items-center gap-1.5 text-sm font-semibold" style={{ color: accent }}>
            Open {ARROW}
          </span>
        )}
      </div>
      <div className={`mt-5 text-lg font-semibold ${soon ? "text-[var(--muted)]" : "text-[var(--text)]"}`}>
        {app.name}
      </div>
      <div className="mt-1 text-[13px] leading-relaxed text-[var(--muted)]">{app.tagline}</div>
    </button>
  );
}
