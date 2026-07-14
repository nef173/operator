"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useStore } from "@/components/StoreProvider";
import {
  api,
  API_BASE,
  type ConnApiField,
  type ConnShopifyStore,
  type ConnIntegration,
  type BrightDataStatus,
  type MarkifactStatus,
  type GoogleStatus,
  type GoogleAccounts,
  type GmcAccount,
  type McpTool,
  type JobSpec,
  type SkuPlanSettings,
  type AppRole,
  type Person,
  type PersonInput,
  type StoreProfile,
  type StoreVerifyResult,
  type StoreSyncResult,
  type DeployCheck,
  type Learning,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState } from "@/components/PageState";
import { PriceRulesCard } from "@/components/PriceRulesCard";
import { useAuth } from "@/components/AuthGate";
import { ALL_APPS } from "@/lib/apps";

// Backend / system view — paths, registered stores, per-step auto/manual map,
// health, DB location. Read-only this pass (the "see a bit of the backend" surface).

function Row({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-4 px-4 py-3">
      <div className="text-sm font-medium text-[var(--muted)]">{label}</div>
      <div className={`text-right text-sm ${mono ? "break-all font-mono text-xs" : ""}`}>
        {value}
      </div>
    </div>
  );
}

// Bytes → human-readable. Null/undefined (backend couldn't read it) renders as an em-dash so the
// storage card never shows a misleading "0 B".
function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
}

// What the system learned — durable steering memory (every rejection reason / assistant
// steer becomes a rule). Moved here from the Decisions page so that page stays pure
// steering; this is the reference view of everything the worker has been taught.
function SystemLearningsCard() {
  const { data, loading, error } = useApi(() => api.learnings());
  const learnings: Learning[] = data?.learnings ?? [];

  const fmtLearnTs = (ts: string): string => {
    const d = new Date(ts.endsWith("Z") || ts.includes("+") ? ts : `${ts}Z`);
    if (isNaN(d.getTime())) return ts;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          What the system learned
        </span>
        {learnings.length > 0 ? (
          <span className="text-[11px] text-[var(--muted)]">{learnings.length}</span>
        ) : null}
      </div>
      <p className="border-b border-[var(--border)] px-4 py-2.5 text-xs text-[var(--muted)]">
        Each reason you give on Decisions (or tell the assistant) becomes a rule, attached to
        similar future suggestions so the worker stops bringing back what you already turned down.
      </p>
      {loading && !data ? (
        <p className="px-4 py-3 text-xs text-[var(--muted)]">Loading…</p>
      ) : error ? (
        <p className="px-4 py-3 text-xs" style={{ color: "var(--state-killed)" }}>{error}</p>
      ) : learnings.length === 0 ? (
        <p className="px-4 py-3 text-xs text-[var(--muted)]">
          Nothing yet. Turn a suggestion down on Decisions and say why — e.g. &ldquo;bad keyword,
          look further&rdquo; — and it lands here.
        </p>
      ) : (
        <div className="max-h-[50vh] divide-y divide-[var(--border)] overflow-y-auto">
          {learnings.map((l) => (
            <div key={l.id} className="flex items-start gap-3 px-4 py-3">
              <Pill>{l.kind}</Pill>
              <div className="min-w-0 flex-1">
                <div className="text-sm">{l.reason}</div>
                {l.action ? (
                  <div className="mt-0.5 text-xs text-[var(--state-testing)]">→ {l.action}</div>
                ) : null}
                <div className="mt-0.5 text-[11px] text-[var(--muted)]">
                  {l.signal ? `${l.signal} · ` : ""}
                  {l.store ? `${l.store} · ` : ""}
                  {fmtLearnTs(l.ts)}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// Editable business-name row. The tenant label is display-only (it doesn't change data
// isolation), but the operator can rename it here — persisted server-side so it survives a
// redeploy. Click "Rename" → inline input → Save (PUT /api/settings/tenant) → reload.
function TenantRow({ tenant, onSaved }: { tenant: string; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(tenant);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    setSaving(true);
    setErr(null);
    try {
      await api.setTenant(val.trim());
      setEditing(false);
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex items-start justify-between gap-4 px-4 py-3">
      <div className="text-sm font-medium text-[var(--muted)]">Business (tenant)</div>
      {editing ? (
        <div className="flex flex-col items-end gap-1">
          <div className="flex items-center gap-2">
            <input
              autoFocus
              value={val}
              onChange={(e) => setVal(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") { setEditing(false); setVal(tenant); } }}
              maxLength={80}
              placeholder="business name"
              className="w-44 rounded-md border border-[var(--border)] bg-[var(--bg)] px-2 py-1 text-right text-sm"
            />
            <button
              type="button"
              onClick={save}
              disabled={saving}
              className="rounded-md px-2 py-1 text-xs font-semibold disabled:opacity-40"
              style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
            >
              {saving ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              onClick={() => { setEditing(false); setVal(tenant); setErr(null); }}
              className="rounded-md px-2 py-1 text-xs font-semibold text-[var(--muted)] hover:text-[var(--text)]"
            >
              Cancel
            </button>
          </div>
          {err ? <span className="text-[11px] text-[var(--state-warn, #d97706)]">{err}</span> : null}
        </div>
      ) : (
        <div className="flex items-center gap-2 text-right text-sm">
          <span className="font-semibold">{tenant}</span>
          <button
            type="button"
            onClick={() => { setVal(tenant); setEditing(true); }}
            className="rounded-md border border-[var(--border)] px-2 py-0.5 text-xs font-semibold text-[var(--muted)] hover:text-[var(--text)]"
          >
            Rename
          </button>
        </div>
      )}
    </div>
  );
}

// One deployment setup check (Database / Data volume). Green when the host env var is wired,
// amber with the exact fix when it isn't. These are the only two values not entered in the app.
function DeployCheckRow({ check }: { check: DeployCheck }) {
  return (
    <div className="flex items-start justify-between gap-4 px-4 py-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span
            className="h-1.5 w-1.5 shrink-0 rounded-full"
            style={{ background: check.ok ? "var(--state-winner, #16a34a)" : "var(--state-warn, #d97706)" }}
          />
          <span className="text-sm font-medium">{check.label}</span>
          <code className="rounded bg-[var(--border)] px-1 py-0.5 text-[10px] text-[var(--muted)]">
            {check.env}
          </code>
        </div>
        <div className="mt-1 pl-3.5 text-[11px] leading-relaxed text-[var(--muted)]">
          {check.ok ? check.status_ok : (
            <>
              {check.status_warn} <span className="text-[var(--fg)]">{check.fix}</span>
            </>
          )}
        </div>
      </div>
      <span
        className="shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium"
        style={{
          background: check.ok ? "var(--state-winner-bg, rgba(34,197,94,0.12))" : "var(--state-warn-bg, rgba(217,119,6,0.12))",
          color: check.ok ? "var(--state-winner, #16a34a)" : "var(--state-warn, #d97706)",
        }}
      >
        {check.ok ? "ready" : "set on host"}
      </span>
    </div>
  );
}

// The settings concerns, surfaced as a LEFT vertical nav (NN "Master Settings" style):
// a FLAT list where every important area is its own icon + title + subtitle row, each one
// click away instead of a long scroll. Areas that hold several related rule-sets (Rules)
// don't spawn multiple sidebar rows — they carry an in-window sub-selector (see RULES_SUBS)
// so the nav stays short and uncluttered.
const SETTINGS_TABS = [
  { id: "connections", label: "Connections", sub: "Stores & API keys" },
  { id: "merchant", label: "Merchant Center", sub: "Google Merchant Center accounts" },
  { id: "access", label: "Users & Access", sub: "People & permissions" },
  { id: "rules", label: "Rules", sub: "Listing & research defaults" },
  { id: "system", label: "System", sub: "Paths, health & execution" },
  { id: "api", label: "API", sub: "Backend connection" },
] as const;
type SettingsTab = (typeof SETTINGS_TABS)[number]["id"];

// Rule-sets that live UNDER the single "Rules" nav item, shown as an in-window horizontal
// selector (the "Store / Google Ads / ParcelPanel" tab-bar pattern) so they don't each need
// their own sidebar row. Add a new rule-set here + a render branch below — nav stays clean.
const RULES_SUBS = [
  { id: "listing", label: "Listing", hint: "pricing rules — compare-at on/off is set per store in Daily Listings" },
  { id: "research", label: "Research", hint: "SKU-plan weight split — how find-budget divides by keyword & source" },
] as const;
type RulesSub = (typeof RULES_SUBS)[number]["id"];

// Compact line icons for the left nav (NN Master Settings uses one glyph per area). Inherit
// `currentColor` so the active row can tint them with the accent.
function NavIcon({ id }: { id: SettingsTab }) {
  const common = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  switch (id) {
    case "connections":
      return (<svg {...common}><path d="M9 7 4.5 11.5a3.5 3.5 0 0 0 5 5L14 12" /><path d="M15 17l4.5-4.5a3.5 3.5 0 0 0-5-5L10 12" /></svg>);
    case "merchant":
      return (<svg {...common}><circle cx="9" cy="21" r="1" /><circle cx="20" cy="21" r="1" /><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6" /></svg>);
    case "access":
      return (<svg {...common}><circle cx="9" cy="8" r="3" /><path d="M3 20a6 6 0 0 1 12 0" /><path d="M16 5a3 3 0 0 1 0 6" /><path d="M18 20a6 6 0 0 0-3-5.2" /></svg>);
    case "rules":
      return (<svg {...common}><path d="M4 6h11" /><path d="M4 12h7" /><path d="M4 18h13" /><circle cx="18" cy="6" r="2" /><circle cx="14" cy="12" r="2" /><circle cx="20" cy="18" r="2" /></svg>);
    case "system":
      return (<svg {...common}><rect x="3" y="4" width="18" height="6" rx="1.5" /><rect x="3" y="14" width="18" height="6" rx="1.5" /><path d="M7 7h.01M7 17h.01" /></svg>);
    case "api":
      return (<svg {...common}><path d="M8 9l-4 3 4 3" /><path d="M16 9l4 3-4 3" /><path d="M13 6l-2 12" /></svg>);
    default:
      return null;
  }
}

// Global "sync everything, all stores" — forces the same engine the 24/7 worker runs daily
// (profile · finance · orders · issues · Product Performance snapshot with ad reconciliation) for
// every store right now, so nothing shows stale or 0 ad spend.
function SyncAllStoresCard() {
  const { data, reload } = useApi(() => api.storesSyncAllStatus(), []);
  const [starting, setStarting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const busy = starting || !!data?.running;

  // Clear the status poll on unmount — navigating away mid-sync otherwise leaks the interval and
  // fires setState on an unmounted component.
  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

  async function syncAll() {
    setStarting(true);
    setMsg(null);
    try {
      const r = await api.storesSyncAll();
      setMsg(`Syncing ${r.stores.length} store${r.stores.length === 1 ? "" : "s"}… this runs in the background (a few minutes for large catalogs).`);
      reload();
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        const s = await api.storesSyncAllStatus().catch(() => null);
        if (s && !s.running) {
          if (pollRef.current) clearInterval(pollRef.current);
          setMsg("All stores synced.");
          reload();
        }
      }, 5000);
    } catch {
      setMsg("Couldn't start the sync.");
    } finally {
      setStarting(false);
    }
  }

  const dot = (st: string) =>
    st === "done" ? { c: "var(--state-live, #16a34a)", t: "synced" }
      : st === "failed" ? { c: "var(--state-killed)", t: "failed" }
        : st === "skipped" ? { c: "var(--muted)", t: "not connected" }
          : { c: "var(--muted)", t: "never" };

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">Data sync — all stores</span>
        <button
          type="button"
          onClick={syncAll}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-lg bg-[var(--accent)] px-3 py-1.5 text-xs font-semibold text-[var(--accent-fg)] disabled:opacity-50"
        >
          {busy ? (
            <>
              <svg className="animate-spin" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M21 12a9 9 0 1 1-6.2-8.5" /></svg>
              Syncing…
            </>
          ) : "Sync all stores now"}
        </button>
      </div>
      <div className="px-4 py-3 text-[11px] leading-relaxed text-[var(--muted)]">
        Pulls every connected store&apos;s Shopify data — profile, finance, orders, issues, and the{" "}
        <span className="font-medium text-[var(--text)]">Product Performance snapshot</span> (revenue ×
        windows + ad-spend reconciliation) — so no store shows stale or €0 ad spend. The 24/7 worker
        runs this daily; this button forces it for every store right now.
        {msg ? <span className="ml-1 font-medium text-[var(--text)]">{msg}</span> : null}
      </div>
      {data?.stores?.length ? (
        <div className="divide-y divide-[var(--border)] border-t border-[var(--border)]">
          {data.stores.map((s) => {
            const d = dot(s.status);
            return (
              <div key={s.store} className="flex items-center justify-between px-4 py-1.5 text-xs">
                <span className="font-medium text-[var(--text)]">{s.store}</span>
                <span className="inline-flex items-center gap-1.5 text-[var(--muted)]">
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: d.c }} />
                  {d.t}{s.at ? ` · ${new Date(s.at).toLocaleString()}` : ""}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
    </Card>
  );
}

// Real-time Shopify webhooks — registers order/refund webhook subscriptions per store so new orders
// push into the app the moment they happen (no waiting for the daily/manual sync).
function WebhooksCard() {
  const [busy, setBusy] = useState(false);
  const [rows, setRows] = useState<{ store: string; ok: boolean; created?: string[]; existing?: string[]; error?: string }[] | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  async function enableAll() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.storesWebhooksRegisterAll();
      setRows(r.stores || []);
      const ok = (r.stores || []).filter((s) => s.ok).length;
      setMsg(`Registered on ${ok} of ${(r.stores || []).length} stores.`);
    } catch {
      setMsg("Couldn't register webhooks.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">Real-time — Shopify webhooks</span>
        <button
          type="button"
          onClick={enableAll}
          disabled={busy}
          className="rounded-lg border border-[var(--accent)] px-3 py-1.5 text-xs font-semibold text-[var(--accent)] hover:bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] disabled:opacity-50"
        >
          {busy ? "Registering…" : "Enable on all stores"}
        </button>
      </div>
      <div className="px-4 py-3 text-[11px] leading-relaxed text-[var(--muted)]">
        Subscribes to each store&apos;s <span className="font-medium text-[var(--text)]">order created / updated /
        cancelled + refund</span> events, so a new order pushes into the app within ~2 min — no waiting for the
        daily sync. Each event is HMAC-verified with the store&apos;s Client Secret and triggers a debounced
        refresh (orders · finance · issues · Product Performance snapshot).
        {msg ? <span className="ml-1 font-medium text-[var(--text)]">{msg}</span> : null}
      </div>
      {rows?.length ? (
        <div className="divide-y divide-[var(--border)] border-t border-[var(--border)]">
          {rows.map((s) => (
            <div key={s.store} className="flex items-center justify-between px-4 py-1.5 text-xs">
              <span className="font-medium text-[var(--text)]">{s.store}</span>
              <span className="inline-flex items-center gap-1.5 text-[var(--muted)]">
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: s.ok ? "var(--state-live, #16a34a)" : "var(--state-killed)" }} />
                {s.ok
                  ? `${(s.created?.length ?? 0)} new · ${(s.existing?.length ?? 0)} already active`
                  : s.error || "failed"}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  );
}

export default function SettingsPage() {
  const { data, error, loading, reload } = useApi(() => api.settings());
  const [tab, setTab] = useState<SettingsTab>("connections");
  const [rulesSub, setRulesSub] = useState<RulesSub>("listing");
  const active = SETTINGS_TABS.find((t) => t.id === tab) ?? SETTINGS_TABS[0];

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="Settings"
        subtitle="The control center. Set the rules once here and the whole app uses them — pricing for listings, how research splits its budget, your stores, and system health."
      />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <div className="flex flex-col gap-6 md:flex-row md:items-start">
          {/* ── LEFT NAV ── flat NN Master-Settings list: one icon + title + subtitle row per
              area, each its own proper settings pane. On mobile it collapses to a wrapping
              row above the content. */}
          <aside className="md:w-64 md:shrink-0">
            <nav className="flex flex-wrap gap-1 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-2 md:sticky md:top-4 md:flex-col md:gap-0.5">
              {SETTINGS_TABS.map((t) => {
                const on = t.id === tab;
                return (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => setTab(t.id)}
                    className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-left transition-colors ${
                      on ? "bg-[var(--surface-2)]" : "hover:bg-[var(--surface-2)]"
                    }`}
                    style={on ? { boxShadow: "inset 2px 0 0 var(--accent)" } : undefined}
                  >
                    <span className={on ? "text-[var(--accent)]" : "text-[var(--muted)]"}>
                      <NavIcon id={t.id} />
                    </span>
                    <span className="min-w-0">
                      <span className={`block text-sm font-semibold ${on ? "text-[var(--text)]" : "text-[var(--text)]"}`}>
                        {t.label}
                      </span>
                      <span className="block truncate text-xs text-[var(--muted)]">{t.sub}</span>
                    </span>
                  </button>
                );
              })}
            </nav>
          </aside>

          {/* ── CONTENT PANEL ── the selected section, with its own heading + hint. */}
          <div className="min-w-0 flex-1">
            <div className="mb-4">
              <h2 className="text-lg font-semibold text-[var(--text)]">{active.label}</h2>
              <p className="mt-0.5 text-xs text-[var(--muted)]">{active.sub}</p>
            </div>

          {/* ── CONNECTIONS ── per-store Shopify creds + global API keys (masked). */}
          {tab === "connections" ? <ConnectionsCard /> : null}

          {/* ── MERCHANT CENTER ── multi-account GMC connection registry (per-account OAuth). */}
          {tab === "merchant" ? <MerchantCenterCard /> : null}

          {/* ── RULES ── one nav row, several rule-sets behind an in-window horizontal selector
              (the "Store / Google Ads / ParcelPanel" tab-bar pattern) so the sidebar stays short. */}
          {tab === "rules" ? (
            <div>
              <div className="mb-4 flex flex-wrap items-center gap-5 border-b border-[var(--border)]">
                {RULES_SUBS.map((s) => {
                  const on = s.id === rulesSub;
                  return (
                    <button
                      key={s.id}
                      type="button"
                      onClick={() => setRulesSub(s.id)}
                      className={`-mb-px border-b-2 px-0.5 pb-2 text-sm font-medium transition-colors ${
                        on
                          ? "border-[var(--accent)] text-[var(--text)]"
                          : "border-transparent text-[var(--muted)] hover:text-[var(--text)]"
                      }`}
                    >
                      {s.label}
                    </button>
                  );
                })}
              </div>
              <p className="mb-4 -mt-1 text-xs text-[var(--muted)]">
                {RULES_SUBS.find((s) => s.id === rulesSub)?.hint}
              </p>
              {rulesSub === "listing" ? <PriceRulesCard editable /> : null}
              {rulesSub === "research" ? <SkuPlanWeightsCard /> : null}
            </div>
          ) : null}

          {/* ── ACCESS ── operation view-mode role gate (Owner sees P&L, Rep doesn't). */}
          {tab === "access" ? <AccessCard /> : null}

          {/* ── SYSTEM ── paths, DB, per-step auto/manual map, local deps, counts. */}
          {tab === "system" ? (
            <div className="space-y-4">
              <SyncAllStoresCard />
              <WebhooksCard />
              <Card>
                <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
                  <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                    Deployment
                  </span>
                  <span
                    className="rounded-full px-2 py-0.5 text-[11px] font-medium"
                    style={{
                      background: data.data_root_isolated ? "var(--state-winner-bg, rgba(34,197,94,0.12))" : "var(--border)",
                      color: data.data_root_isolated ? "var(--state-winner, #16a34a)" : "var(--muted)",
                    }}
                  >
                    {data.data_root_isolated ? "isolated data volume" : "shared (laptop default)"}
                  </span>
                </div>
                <div className="divide-y divide-[var(--border)]">
                  <TenantRow tenant={data.tenant} onSaved={reload} />
                  <Row label="Data root (this business)" value={data.data_root} mono />
                  <Row label="Code root (shared)" value={data.repo_root} mono />
                </div>
                <div className="border-t border-[var(--border)] px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
                  Setup checks
                </div>
                <div className="divide-y divide-[var(--border)]">
                  {data.deploy_checks.map((c) => (
                    <DeployCheckRow key={c.id} check={c} />
                  ))}
                </div>
                <div className="px-4 py-2.5 text-[11px] leading-relaxed text-[var(--muted)]">
                  Every <span className="font-medium">credential and setting</span> for this business
                  is entered inside the app (Settings → Connections + the other tabs). The two checks
                  above are the only exceptions — they&apos;re the deployment&apos;s own database
                  connection and its data volume, which by nature must exist <em>before</em> the app
                  starts, so they&apos;re set once on the host. The app confirms them here.
                </div>
              </Card>

              {/* Per-store health — "is everything syncing?" at a glance. Red rows mean the
                  operator must act (connect the store in Connections, or read the sync error). */}
              {data.stores_health?.length ? (
                <Card>
                  <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
                    <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                      Stores · connection &amp; data sync
                    </span>
                    <span className="text-[11px] text-[var(--muted)]">
                      {data.stores_health.filter((s) => s.shopify_auth).length} of {data.stores_health.length} connected
                    </span>
                  </div>
                  <div className="divide-y divide-[var(--border)]">
                    {data.stores_health.map((s) => {
                      const syncFailed = s.last_sync?.status === "failed";
                      const tone = !s.shopify_auth
                        ? "var(--state-drafted)"
                        : syncFailed
                          ? "var(--state-killed)"
                          : s.last_sync
                            ? "var(--state-winner)"
                            : "var(--muted)";
                      const text = !s.shopify_auth
                        ? "not connected — add its Shopify domain in Connections"
                        : syncFailed
                          ? `sync FAILED — ${s.last_sync?.detail ?? ""}`
                          : s.last_sync
                            ? `synced ${s.last_sync.at}${s.last_sync.status === "skipped" ? " (skipped)" : ""}`
                            : "connected — first sync runs on the next worker pass";
                      return (
                        <div key={s.store} className="flex items-center gap-3 px-4 py-2.5">
                          <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: tone }} />
                          <span className="min-w-0 flex-1">
                            <span className="flex items-center gap-2 text-sm font-medium">
                              {data.store_labels?.[s.store] ?? s.store}
                              {s.mode === "fashion" || s.mode === "both" ? (
                                <span className="rounded-md px-1.5 py-0.5 text-[10px] font-semibold capitalize"
                                  style={{ color: "var(--accent)", background: "color-mix(in srgb, var(--accent) 14%, transparent)" }}>
                                  {s.mode}
                                </span>
                              ) : null}
                            </span>
                            <span className="block truncate text-[11px] text-[var(--muted)]" title={s.last_sync?.detail ?? undefined}>
                              {text}
                            </span>
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </Card>
              ) : null}

              <SystemLearningsCard />

              <Card>
                <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                  Paths &amp; storage
                </div>
                <div className="divide-y divide-[var(--border)]">
                  <Row label="Repo root" value={data.repo_root} mono />
                  <Row label="General stores dir" value={data.general_stores_dir} mono />
                  <Row label="Run-log DB" value={data.db_path} mono />
                  <Row label="DB backend" value={data.db_backend} mono />
                </div>
              </Card>

              <LocalDepsCard />

              <Card>
                <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
                  <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                    Pipeline steps · execution mode
                  </span>
                  <span className="text-[11px] text-[var(--muted)]">
                    {data.job_specs.filter((j) => j.mode === "auto").length} auto ·{" "}
                    {data.job_specs.filter((j) => j.mode === "manual").length} manual
                  </span>
                </div>
                <div className="divide-y divide-[var(--border)]">
                  {data.job_specs.map((spec) => (
                    <SpecRow key={spec.id} spec={spec} />
                  ))}
                </div>
              </Card>

              <Card>
                <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                  Activity counts
                </div>
                <div className="divide-y divide-[var(--border)]">
                  <Row label="Control writes (runs)" value={data.counts.runs} />
                  <Row label="Execution jobs" value={data.counts.jobs} />
                  <Row label="SKU states" value={data.sku_states.join(" → ")} mono />
                  <Row label="Capture buckets" value={data.capture_buckets.join(" · ")} mono />
                </div>
              </Card>

              {/* Storage footprint — the two numbers that drive the Railway bill at scale, so
                  growth is visible before it costs money. Volume used % is the one to watch:
                  staged product images belong on the Shopify CDN, not this disk. */}
              {data.storage ? (
                <Card>
                  <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                    Storage footprint
                  </div>
                  <div className="divide-y divide-[var(--border)]">
                    <Row label="Database size" value={fmtBytes(data.storage.db_bytes)} />
                    <Row label="Data volume used" value={
                      `${fmtBytes(data.storage.volume_used_bytes)} / ${fmtBytes(data.storage.volume_total_bytes)}` +
                      (data.storage.volume_used_pct != null ? ` (${data.storage.volume_used_pct}%)` : "")
                    } />
                  </div>
                  <p className="px-4 py-2.5 text-xs text-[var(--muted)]">
                    Railway bills used volume GB. The only unbounded grower is staged product
                    images — those belong on the Shopify CDN, not this disk. Watch the % here.
                  </p>
                </Card>
              ) : null}

              <p className="px-1 text-xs text-[var(--muted)]">
                <span className="font-semibold text-[var(--state-winner)]">Auto</span> steps run
                inside the app on their own.{" "}
                <span className="font-semibold text-[var(--state-drafted)]">Manual</span> steps
                need tools the app can&apos;t reach, so the app hands you the exact command to
                copy and run yourself.
              </p>
            </div>
          ) : null}

          {/* ── API ── the backend connection this frontend talks to (readonly). */}
          {tab === "api" ? (
            <Card>
              <div className="divide-y divide-[var(--border)]">
                <Row label="API version" value={<span className="inline-flex items-center gap-2"><span className="h-1.5 w-1.5 rounded-full" style={{ background: "var(--state-winner)" }} />v{data.api_version}</span>} />
                <Row label="API base" value={API_BASE} mono />
                <Row label="CORS origins" value={data.cors_origins.join(", ") || "—"} mono />
              </div>
            </Card>
          ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// Local dependencies an auto-local job drives. The Google Shopping competitor scan now runs
// server-side from DataForSEO (no browser / local profile) — reachable when the DataForSEO
// creds are set, so the scan runs automatically on this backend rather than a manual handoff.
function LocalDepsCard() {
  const { data, error, loading, reload } = useApi(() => api.preflight("shopping_scan"));
  const up = data?.reachable === true;
  const color = up ? "var(--state-winner)" : "var(--state-drafted)";
  return (
    <Card>
      <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          Local dependencies · auto-local jobs
        </span>
        <button
          onClick={reload}
          className="text-[11px] text-[var(--muted)] hover:text-[var(--text)]"
        >
          recheck
        </button>
      </div>
      <div className="px-4 py-3">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold">Google Shopping scan</span>
              <span className="font-mono text-[11px] text-[var(--muted)]">
                server-side · DataForSEO
              </span>
            </div>
            <div className="mt-0.5 text-xs text-[var(--muted)]">
              {loading
                ? "Checking…"
                : error
                  ? error
                  : up
                    ? "Reachable — the Google Shopping competitor scan runs automatically on this backend."
                    : (data?.detail ??
                      "Not reachable — set the DataForSEO credentials in Data above.")}
            </div>
          </div>
          <span
            className="shrink-0 rounded-md px-2 py-0.5 text-xs font-semibold"
            style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
          >
            {loading ? "…" : up ? "up · auto" : "down · manual"}
          </span>
        </div>
      </div>
    </Card>
  );
}

// The persisted SKU-plan weight split editor. Loads the saved override (DEFAULTS ← saved),
// lets the operator edit each knob, and PUTs it back as the new default. Server clamps every
// value, so the inputs are forgiving — the API is the source of truth on bounds. Reset drops
// the override back to hard-coded defaults.
function num(v: string, fallback: number): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

// Operation view-mode role gate. Owner sees the Finance / P&L app + its overview KPIs;
// Rep does not. Persisted operation-wide (POST /api/access) — becomes a per-user role the
// moment real auth lands. This is the "reps never see P&L" control, wired from Settings
// downstream: the shell and the Finance layout both read GET /api/access.
// The app registry, id → friendly name, so the per-app access grid reads in plain English.
const APP_LABEL: Record<string, string> = Object.fromEntries(
  ALL_APPS.map((a) => [a.id, a.name]),
);
const appLabel = (id: string) => APP_LABEL[id] ?? id;

function initials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return (parts[0][0] + (parts[1]?.[0] ?? "")).toUpperCase();
}

// Users & Access — NN Operations' per-person, per-app RBAC ported natively. Each PERSON has a
// name / position / photo / password plus a map of which apps they can open and their ROLE inside
// each (Owner / Admin / Representative). The "logged-in" user is simulated by an active-user
// switcher until real auth lands; every downstream gate (shell app list, FinanceGuard) reflects it.
function AccessCard() {
  const { data, loading, error, reload } = useApi(() => api.users());
  const { user, signOut } = useAuth();
  const [editing, setEditing] = useState<Person | null>(null);
  const [inviting, setInviting] = useState(false);

  if (loading) return <Card className="p-4 text-sm text-[var(--muted)]">Loading people…</Card>;
  if (error) return <Card className="p-4 text-sm"><span style={{ color: "var(--state-killed)" }}>{error}</span></Card>;
  if (!data) return null;

  const { people, roles, role_labels, app_ids } = data;

  return (
    <div className="space-y-4">
      {user ? (
        <Card className="flex items-center justify-between px-4 py-3">
          <div className="min-w-0 text-sm">
            <span className="text-[var(--muted)]">Signed in as </span>
            <span className="font-semibold">{user.name}</span>
            {user.position ? <span className="text-[var(--muted)]"> · {user.position}</span> : null}
          </div>
          <button
            type="button"
            onClick={() => signOut()}
            className="shrink-0 rounded-lg border border-[var(--border)] px-2.5 py-1 text-xs font-semibold text-[var(--muted)] hover:text-[var(--text)]"
          >
            Sign out
          </button>
        </Card>
      ) : null}

      <Card>
        <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
          <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
            People · {people.length}
          </span>
          <button
            type="button"
            onClick={() => setInviting(true)}
            className="rounded-lg bg-[var(--accent)] px-2.5 py-1 text-xs font-semibold text-[var(--accent-fg)]"
          >
            + Invite person
          </button>
        </div>
        <div className="divide-y divide-[var(--border)]">
          {people.map((p) => {
            const isYou = !!user && p.id === user.id;
            const grantCount = Object.keys(p.access).length;
            return (
              <div key={p.id} className="flex items-center gap-3 px-4 py-3">
                {p.photo ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={p.photo}
                    alt={p.name}
                    className="h-9 w-9 shrink-0 rounded-full object-cover"
                  />
                ) : (
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[var(--surface-2)] text-xs font-semibold text-[var(--muted)]">
                    {initials(p.name)}
                  </span>
                )}
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 text-sm font-semibold">
                    <span className="truncate">{p.name}</span>
                    {isYou ? <Pill tone="ok">you</Pill> : null}
                    {p.has_password ? (
                      isYou ? null : <Pill tone="ok">active</Pill>
                    ) : (
                      <Pill>invite pending</Pill>
                    )}
                  </div>
                  <div className="truncate text-xs text-[var(--muted)]">
                    {p.position || "—"} · {grantCount} app{grantCount === 1 ? "" : "s"}
                    {p.has_password ? " · can sign in" : " · no password set"}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setEditing(p)}
                  className="rounded-lg border border-[var(--border)] px-2.5 py-1 text-xs font-semibold hover:bg-[var(--surface-2)]"
                >
                  Edit
                </button>
              </div>
            );
          })}
          {people.length === 0 ? (
            <div className="px-4 py-6 text-center text-sm text-[var(--muted)]">
              No people yet — invite the first teammate.
            </div>
          ) : null}
        </div>
      </Card>

      <p className="px-1 text-xs leading-relaxed text-[var(--muted)]">
        Each person is granted a set of apps and a <span className="font-medium">role</span> inside
        each — <span className="font-medium">Owner</span> / <span className="font-medium">Admin</span>{" "}
        / <span className="font-medium">Representative</span>. Anyone without the Finance app never
        sees profit &amp; loss. Everyone with a password is an{" "}
        <span className="font-medium">active</span> account — each person signs in with their own
        name and password, and the app runs with that person&apos;s access.
      </p>

      {inviting ? (
        <PersonModal
          title="Invite person"
          appIds={app_ids}
          roles={roles}
          roleLabels={role_labels}
          onClose={() => setInviting(false)}
          onSaved={() => {
            setInviting(false);
            reload();
          }}
        />
      ) : null}

      {editing ? (
        <PersonModal
          title="Edit person"
          person={editing}
          appIds={app_ids}
          roles={roles}
          roleLabels={role_labels}
          canDelete={people.length > 1}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            reload();
          }}
        />
      ) : null}
    </div>
  );
}

// Create / edit a person — name, position, password (blank keeps existing on edit), photo, and the
// per-app access grid (checkbox to grant + role dropdown). Delete lives here on edit.
function PersonModal({
  title,
  person,
  appIds,
  roles,
  roleLabels,
  canDelete,
  onClose,
  onSaved,
}: {
  title: string;
  person?: Person;
  appIds: string[];
  roles: AppRole[];
  roleLabels: Record<AppRole, string>;
  canDelete?: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(person?.name ?? "");
  const [position, setPosition] = useState(person?.position ?? "");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [revealed, setRevealed] = useState<string | null>(null);
  const [revealBusy, setRevealBusy] = useState(false);
  const [revealErr, setRevealErr] = useState<string | null>(null);
  const [photo, setPhoto] = useState<string | null>(person?.photo ?? null);
  const [access, setAccess] = useState<Record<string, AppRole>>({ ...(person?.access ?? {}) });
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function toggleApp(id: string) {
    setAccess((prev) => {
      const next = { ...prev };
      if (next[id]) delete next[id];
      else next[id] = "rep";
      return next;
    });
  }
  function setRole(id: string, role: AppRole) {
    setAccess((prev) => ({ ...prev, [id]: role }));
  }
  function grantAll(role: AppRole) {
    setAccess(Object.fromEntries(appIds.map((id) => [id, role])));
  }
  function clearAll() {
    setAccess({});
  }
  const showingSomething = showPassword && (!!password || revealed !== null);
  async function toggleShow() {
    setRevealErr(null);
    if (showingSomething) {
      setShowPassword(false);
      return;
    }
    // A newly typed password reveals instantly.
    if (password) {
      setShowPassword(true);
      return;
    }
    // Otherwise fetch the owner-viewable recoverable copy of the existing password.
    if (person?.can_reveal) {
      setRevealBusy(true);
      try {
        const r = await api.userRevealPassword(person.id);
        if (r.password == null) {
          setRevealErr("This password can't be shown. Type a new one to reset it, then Show.");
        } else {
          setRevealed(r.password);
          setShowPassword(true);
        }
      } catch (e) {
        setRevealErr(e instanceof Error ? e.message : "Could not reveal password");
      } finally {
        setRevealBusy(false);
      }
      return;
    }
    setRevealErr("No password is set yet. Type one and press Show.");
  }
  const isFullOwner = appIds.length > 0 && appIds.every((id) => access[id] === "owner");
  function onPhotoFile(file: File | undefined) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setPhoto(typeof reader.result === "string" ? reader.result : null);
    reader.readAsDataURL(file);
  }

  async function save() {
    if (!name.trim()) {
      setErr("Name is required");
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      const input: PersonInput = {
        name: name.trim(),
        position: position.trim(),
        access,
      };
      if (password) input.password = password;
      if (person) {
        // On edit, only send photo when it changed (backend keys off its presence).
        if (photo !== (person.photo ?? null)) input.photo = photo;
        await api.userUpdate(person.id, input);
      } else {
        input.photo = photo;
        await api.userCreate(input);
      }
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
      setSaving(false);
    }
  }

  async function remove() {
    if (!person) return;
    setDeleting(true);
    setErr(null);
    try {
      await api.userDelete(person.id);
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Delete failed");
      setDeleting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:p-8"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-2xl border border-[var(--border)] bg-[var(--surface)] shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-[var(--border)] px-5 py-3">
          <span className="text-sm font-semibold">{title}</span>
          <button
            type="button"
            onClick={onClose}
            className="text-[var(--muted)] hover:text-[var(--text)]"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="space-y-4 px-5 py-4">
          {/* identity */}
          <div className="flex items-center gap-3">
            {photo ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={photo} alt="" className="h-14 w-14 shrink-0 rounded-full object-cover" />
            ) : (
              <span className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-[var(--surface-2)] text-base font-semibold text-[var(--muted)]">
                {initials(name || "?")}
              </span>
            )}
            <div className="flex flex-col gap-1">
              <label className="cursor-pointer rounded-lg border border-[var(--border)] px-2.5 py-1 text-xs font-semibold hover:bg-[var(--surface-2)]">
                {photo ? "Change photo" : "Add photo"}
                <input
                  type="file"
                  accept="image/*"
                  className="hidden"
                  onChange={(e) => onPhotoFile(e.target.files?.[0])}
                />
              </label>
              {photo ? (
                <button
                  type="button"
                  onClick={() => setPhoto(null)}
                  className="text-left text-xs text-[var(--muted)] hover:text-[var(--danger)]"
                >
                  Remove photo
                </button>
              ) : null}
            </div>
          </div>

          <Field label="Name (used to sign in)">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. John Doe"
              autoComplete="off"
              className="w-full rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            />
          </Field>
          <Field label="Position (optional)">
            <input
              value={position}
              onChange={(e) => setPosition(e.target.value)}
              placeholder="e.g. Operations Rep"
              className="w-full rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
            />
          </Field>
          <Field label={person ? "Password (leave blank to keep)" : "Password"}>
            {(() => {
              const displayingExisting = !password && revealed !== null;
              return (
                <div className="relative">
                  <input
                    type={showPassword ? "text" : "password"}
                    value={displayingExisting ? revealed! : password}
                    readOnly={displayingExisting}
                    onChange={(e) => {
                      setRevealed(null);
                      setPassword(e.target.value);
                    }}
                    placeholder={person?.has_password ? "•••••• (unchanged)" : "Set a password"}
                    autoComplete="new-password"
                    className="w-full rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2 pr-16 text-sm outline-none focus:border-[var(--accent)]"
                  />
                  <button
                    type="button"
                    onClick={toggleShow}
                    disabled={revealBusy}
                    className="absolute inset-y-0 right-0 flex items-center px-3 text-xs font-semibold text-[var(--muted)] hover:text-[var(--text)] disabled:opacity-50"
                    aria-label={showingSomething ? "Hide password" : "Show password"}
                  >
                    {revealBusy ? "…" : showingSomething ? "Hide" : "Show"}
                  </button>
                </div>
              );
            })()}
            {revealErr ? (
              <p className="mt-1 text-xs text-[var(--danger)]">{revealErr}</p>
            ) : (
              <p className="mt-1 text-xs text-[var(--muted)]">
                {person?.can_reveal
                  ? "Press Show to view this person's current password and share it. Type a new one to reset it."
                  : person?.has_password
                    ? "A password is set but predates password-viewing — type a new one to reset it, then Show to view and share it."
                    : "Type a password and press Show to view it, then share it with this person."}
              </p>
            )}
          </Field>

          {/* per-app access grid */}
          <div>
            <div className="mb-1.5 flex items-center justify-between gap-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                App access &amp; role
              </span>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => grantAll("owner")}
                  className={`rounded-lg border px-2 py-1 text-xs font-semibold ${
                    isFullOwner
                      ? "border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-fg)]"
                      : "border-[var(--border)] hover:bg-[var(--surface-2)]"
                  }`}
                >
                  {isFullOwner ? "✓ Full owner" : "Make full owner"}
                </button>
                <button
                  type="button"
                  onClick={clearAll}
                  className="rounded-lg border border-[var(--border)] px-2 py-1 text-xs font-semibold text-[var(--muted)] hover:bg-[var(--surface-2)]"
                >
                  Clear all
                </button>
              </div>
            </div>
            <div className="divide-y divide-[var(--border)] rounded-lg border border-[var(--border)]">
              {appIds.map((id) => {
                const granted = id in access;
                return (
                  <div key={id} className="flex items-center gap-3 px-3 py-2">
                    <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2.5">
                      <input
                        type="checkbox"
                        checked={granted}
                        onChange={() => toggleApp(id)}
                        className="h-4 w-4 accent-[var(--accent)]"
                      />
                      <span className={`truncate text-sm ${granted ? "font-medium" : "text-[var(--muted)]"}`}>
                        {appLabel(id)}
                      </span>
                    </label>
                    <select
                      value={access[id] ?? ""}
                      disabled={!granted}
                      onChange={(e) => setRole(id, e.target.value as AppRole)}
                      className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1 text-xs font-semibold disabled:opacity-40"
                    >
                      {roles.map((r) => (
                        <option key={r} value={r}>
                          {roleLabels[r]}
                        </option>
                      ))}
                    </select>
                  </div>
                );
              })}
            </div>
          </div>

          {err ? <div className="text-xs text-[var(--danger)]">{err}</div> : null}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-[var(--border)] px-5 py-3">
          <div>
            {person && canDelete ? (
              <button
                type="button"
                onClick={remove}
                disabled={deleting || saving}
                className="rounded-lg border border-[var(--danger)] px-3 py-1.5 text-xs font-semibold text-[var(--danger)] hover:bg-[var(--danger)] hover:text-white disabled:opacity-50"
              >
                {deleting ? "Deleting…" : "Delete person"}
              </button>
            ) : null}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-[var(--border)] px-3.5 py-1.5 text-sm font-semibold text-[var(--muted)] hover:text-[var(--text)]"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={save}
              disabled={saving || deleting}
              className="rounded-lg bg-[var(--accent)] px-3.5 py-1.5 text-sm font-semibold text-[var(--accent-fg)] disabled:opacity-50"
            >
              {saving ? "Saving…" : person ? "Save changes" : "Create person"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
        {label}
      </span>
      {children}
    </label>
  );
}

function SkuPlanWeightsCard() {
  const { data, error, loading, reload } = useApi(() => api.skuPlanSettings());
  const [draft, setDraft] = useState<SkuPlanSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  useEffect(() => {
    if (data) setDraft(data.settings);
  }, [data]);

  if (loading) return <Card className="p-4 text-sm text-[var(--muted)]">Loading weight split…</Card>;
  if (error) return <Card className="p-4 text-sm"><span style={{ color: "var(--state-killed)" }}>{error}</span></Card>;
  if (!draft || !data) return null;

  const sw = draft.source_weights;
  const swTotal = sw.google_shopping + sw.competitor_catalog + sw.marketplace + sw.amazon + sw.meta;
  const pct = (v: number) => (swTotal > 0 ? Math.round((v / swTotal) * 100) : 0);

  const setTop = (k: "anchor_pct" | "coverage_pct" | "products_per_build" | "dedup_cap", v: number) =>
    setDraft((d) => (d ? { ...d, [k]: v } : d));
  const setSource = (
    k: "google_shopping" | "competitor_catalog" | "marketplace" | "amazon" | "meta",
    v: number,
  ) => setDraft((d) => (d ? { ...d, source_weights: { ...d.source_weights, [k]: v } } : d));
  const setRole = (k: "standalone_sv_floor" | "standalone_head_frac" | "supporting_sv_ceiling", v: number) =>
    setDraft((d) => (d ? { ...d, role: { ...d.role, [k]: v } } : d));

  const dirty = JSON.stringify(draft) !== JSON.stringify(data.settings);

  async function save() {
    if (!draft) return;
    setSaving(true);
    setSaveErr(null);
    try {
      await api.skuPlanSettingsSave(draft);
      setSavedAt(Date.now());
      reload();
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function reset() {
    setSaving(true);
    setSaveErr(null);
    try {
      const res = await api.skuPlanSettingsReset();
      setDraft(res.settings);
      setSavedAt(Date.now());
      reload();
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : "Reset failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
        How many products to look for
      </div>
      <div className="grid gap-4 px-4 py-4 sm:grid-cols-2">
        <NumberField label="Focus on main keyword" hint="how much of the search effort goes to the main keyword" suffix="%"
          value={draft.anchor_pct} min={10} max={90} step={1} onChange={(v) => setTop("anchor_pct", v)} />
        <NumberField label="Cover related searches" hint="how much of the related-search demand to also cover" suffix="%"
          value={draft.coverage_pct} min={10} max={100} step={1} onChange={(v) => setTop("coverage_pct", v)} />
        <NumberField label="Products to find per keyword" hint="how many candidate products to look for, per keyword"
          value={draft.products_per_build} min={1} max={50} step={1} onChange={(v) => setTop("products_per_build", v)} />
        <NumberField label="Max copies of the same product" hint="never list one physical product more than this many times (in different styles)" suffix="×"
          value={draft.dedup_cap} min={1} max={10} step={1} onChange={(v) => setTop("dedup_cap", v)} />
      </div>

      <div className="border-y border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
        Where to look for products
      </div>
      <div className="grid gap-4 px-4 py-4 sm:grid-cols-2 lg:grid-cols-3">
        <NumberField label="GoogleShopping Search" hint={`${pct(sw.google_shopping)}% — find NEW dropshippers on Google Shopping, scan their store`} value={sw.google_shopping} min={0} max={100} step={1}
          onChange={(v) => setSource("google_shopping", v)} color="var(--state-live)" />
        <NumberField label="Google Competitor Catalog" hint={`${pct(sw.competitor_catalog)}% — our tracked competitors' best-selling SKUs for the keyword (+ catalog)`} value={sw.competitor_catalog} min={0} max={100} step={1}
          onChange={(v) => setSource("competitor_catalog", v)} color="var(--state-live)" />
        <NumberField label="Marketplace" hint={`${pct(sw.marketplace)}% — Temu / AliExpress`} value={sw.marketplace} min={0} max={100} step={1}
          onChange={(v) => setSource("marketplace", v)} color="var(--state-testing)" />
        <NumberField label="Amazon" hint={`${pct(sw.amazon)}% — best-sellers`} value={sw.amazon} min={0} max={100} step={1}
          onChange={(v) => setSource("amazon", v)} color="var(--state-winner)" />
        <NumberField label="Meta" hint={`${pct(sw.meta)}% — dropship ad advertisers`} value={sw.meta} min={0} max={100} step={1}
          onChange={(v) => setSource("meta", v)} color="var(--accent)" />
      </div>
      <p className="px-4 pb-3 text-xs text-[var(--muted)]">
        These shares decide where products come from — Meta&apos;s share is simply its number above.
        If Amazon or Meta run out of new products, their share automatically flows to Google competitor
        and Marketplace so the daily count is still met.
      </p>

      <div className="border-y border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
        Which related keywords get their own product
      </div>
      <p className="px-4 pt-3 text-xs text-[var(--muted)]">
        When research finds keywords related to the one you&apos;re building (e.g. main keyword
        <em> &quot;dog stroller&quot;</em> also surfaces <em>&quot;jogging dog stroller&quot;</em>), these
        thresholds decide the fate of each related keyword: <b>list it as its own product</b>, or just
        <b> mention it inside an existing product&apos;s copy</b>. Bigger keywords earn their own listing;
        small ones only add supporting text.
      </p>
      <div className="grid gap-4 px-4 py-4 sm:grid-cols-3">
        <NumberField label="Own product if searches ≥" hint="a related keyword with at least this many monthly searches becomes its own listing"
          value={draft.role.standalone_sv_floor} min={0} max={1000000} step={100}
          onChange={(v) => setRole("standalone_sv_floor", v)} />
        <NumberField label="…or if it's this big vs the main keyword" hint="e.g. 0.30 = a related keyword also gets its own product once it reaches 30% of the main keyword's searches"
          value={draft.role.standalone_head_frac} min={0} max={1} step={0.05}
          onChange={(v) => setRole("standalone_head_frac", v)} />
        <NumberField label="Just a mention if searches ≤" hint="below this it never gets its own product — it only feeds text into an existing listing"
          value={draft.role.supporting_sv_ceiling} min={0} max={1000000} step={50}
          onChange={(v) => setRole("supporting_sv_ceiling", v)} />
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-[var(--border)] px-4 py-3">
        <button
          type="button"
          onClick={save}
          disabled={saving || !dirty}
          className="rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
          style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          {saving ? "Saving…" : "Save weight split"}
        </button>
        <button
          type="button"
          onClick={reset}
          disabled={saving}
          className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-semibold text-[var(--muted)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)] disabled:opacity-40"
        >
          Reset to defaults
        </button>
        {dirty ? <span className="text-xs text-[var(--state-drafted)]">unsaved changes</span> : null}
        {!dirty && savedAt ? <span className="text-xs" style={{ color: "var(--state-winner)" }}>saved ✓</span> : null}
        {saveErr ? <span className="text-xs" style={{ color: "var(--state-killed)" }}>{saveErr}</span> : null}
        <span className="ml-auto text-[11px] text-[var(--muted)]">
          values are clamped server-side · the SKU-plan tab can still nudge a one-off plan
        </span>
      </div>
    </Card>
  );
}

function NumberField({
  label, hint, value, min, max, step, suffix, color, onChange,
}: {
  label: string; hint: string; value: number; min: number; max: number; step: number;
  suffix?: string; color?: string; onChange: (v: number) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="flex items-baseline gap-1.5 text-sm font-medium">
        {color ? <span className="h-2 w-2 rounded-full" style={{ background: color }} /> : null}
        {label}
      </span>
      <div className="flex items-center gap-1.5">
        <input
          type="number"
          value={value}
          min={min}
          max={max}
          step={step}
          onChange={(e) => onChange(num(e.target.value, value))}
          className="w-28 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-sm tabular-nums focus:border-[var(--accent)] focus:outline-none"
        />
        {suffix ? <span className="text-xs text-[var(--muted)]">{suffix}</span> : null}
      </div>
      <span className="text-[11px] text-[var(--muted)]">{hint}</span>
    </label>
  );
}

// Connect the app to the outside world. Per-store Shopify (just the myshopify domain — the 24h
// Admin token is minted from the app-wide Client ID/Secret) + the global API keys (DataForSEO,
// Bright Data, the LLM/vision keys). Secrets load masked (••••last4); typing a new value overwrites,
// leaving a field blank keeps the stored one. Only edited fields are sent on save.
function maskedField(label: string, secret: boolean, configured: boolean, masked: string,
  placeholder: string, value: string | undefined, onChange: (v: string) => void,
  multiline = false) {
  const ph = configured ? `${masked} — leave blank to keep` : (placeholder || "not set");
  return (
    <label className="flex h-full flex-col gap-1" key={label}>
      <span className="flex min-h-[2.5rem] items-center gap-1.5 text-sm font-medium leading-tight">
        {label}
        {configured ? (
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: "var(--state-winner)" }} title="set" />
        ) : null}
      </span>
      {multiline ? (
        // A service-account JSON key is a multi-line document — a textarea is the only sane paste
        // target. (It can't be a masked password input; the value still loads masked on reload.)
        <textarea
          autoComplete="off"
          rows={4}
          value={value ?? ""}
          placeholder={ph}
          onChange={(e) => onChange(e.target.value)}
          className="w-full resize-y rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 font-mono text-xs focus:border-[var(--accent)] focus:outline-none"
        />
      ) : (
        <input
          type={secret ? "password" : "text"}
          autoComplete="off"
          value={value ?? ""}
          placeholder={ph}
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 text-sm focus:border-[var(--accent)] focus:outline-none"
        />
      )}
    </label>
  );
}

// A real date picker for a data cut-off field (e.g. "Data start date"). BLANK = all history is
// the default — the "All history" button clears it back to that. Non-secret, so the saved value
// arrives in `masked` and pre-fills the picker until the operator edits it.
function dateField(label: string, configured: boolean, masked: string,
  value: string | undefined, onChange: (v: string) => void) {
  const shown = value !== undefined ? value : (configured ? masked : "");
  return (
    <label className="flex h-full flex-col gap-1" key={label}>
      <span className="flex min-h-[2.5rem] items-center gap-1.5 text-sm font-medium leading-tight">
        {label}
        {configured && masked ? (
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: "var(--state-winner)" }} title="set" />
        ) : null}
      </span>
      <div className="flex items-center gap-2">
        <input
          type="date"
          value={shown}
          onChange={(e) => onChange(e.target.value)}
          className="flex-1 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 text-sm focus:border-[var(--accent)] focus:outline-none"
        />
        {shown ? (
          <button
            type="button"
            onClick={() => onChange("")}
            className="shrink-0 rounded-md border border-[var(--border)] px-2.5 py-1.5 text-[11px] font-semibold text-[var(--muted)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
          >
            All history
          </button>
        ) : (
          <span className="shrink-0 text-[11px] text-[var(--muted)]">all history</span>
        )}
      </div>
    </label>
  );
}

// A small "?" help button that opens a step-by-step popover. Reusable across Settings — pass a
// title + ordered steps. Lightweight modal (backdrop + centered card), matches ConfirmDialog.
function HelpPopover({ title, intro, steps, footnote }: {
  title: string;
  intro?: React.ReactNode;
  steps: React.ReactNode[];
  footnote?: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={(e) => { e.preventDefault(); setOpen(true); }}
        title={`How to: ${title}`}
        aria-label={`Help: ${title}`}
        className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-[var(--border)] text-[11px] font-bold text-[var(--muted)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)]"
      >
        ?
      </button>
      {open ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={() => setOpen(false)}
        >
          <div
            className="w-full max-w-lg rounded-xl border border-[var(--border)] bg-[var(--surface)] p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-4">
              <h3 className="text-base font-semibold">{title}</h3>
              <button
                type="button"
                onClick={() => setOpen(false)}
                aria-label="Close"
                className="-mr-1 -mt-1 rounded-md px-1.5 text-lg leading-none text-[var(--muted)] hover:text-[var(--text)]"
              >
                ×
              </button>
            </div>
            {intro ? <div className="mt-2 text-sm text-[var(--muted)]">{intro}</div> : null}
            <ol className="mt-3 space-y-2.5">
              {steps.map((s, i) => (
                <li key={i} className="flex gap-2.5 text-sm">
                  <span
                    className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-bold"
                    style={{ background: "color-mix(in srgb, var(--accent) 16%, transparent)", color: "var(--accent)" }}
                  >
                    {i + 1}
                  </span>
                  <div className="text-[var(--text)]">{s}</div>
                </li>
              ))}
            </ol>
            {footnote ? <div className="mt-4 border-t border-[var(--border)] pt-3 text-xs text-[var(--muted)]">{footnote}</div> : null}
            <div className="mt-5 flex justify-end">
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded-lg px-3 py-1.5 text-sm font-semibold"
                style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
              >
                Got it
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}

// Per-group help content for the "API keys · <group>" cards. Keyed by group label so the ?
// only appears where there's a real setup guide (e.g. the Merchant Center service-account path).
const API_GROUP_HELP: Record<string, { title: string; intro: React.ReactNode; steps: React.ReactNode[]; footnote?: React.ReactNode }> = {
  "Data": {
    title: "Set up your data providers",
    intro: (
      <>
        <span className="font-semibold">DataForSEO</span> is the one required data key (keyword volume,
        SERP, Trends, Shopping). Bright Data + the rest are optional scrapers — only fill them if you use
        those features.
      </>
    ),
    steps: [
      <>Sign up at <span className="font-mono text-xs">dataforseo.com</span> → open the Dashboard.</>,
      <>Copy your <span className="font-medium">API username</span> (your login email) and <span className="font-medium">API password</span> (shown in the dashboard — not your website password).</>,
      <>Paste them into <span className="font-medium">DataForSEO Username</span> + <span className="font-medium">Password</span>. Save.</>,
      <>(Optional) <span className="font-medium">Bright Data</span>: sign up at <span className="font-mono text-xs">brightdata.com</span>, copy the API token, paste into <span className="font-medium">Bright Data Token</span> — powers AliExpress finding + the Google-Shopping fallback (used when DataForSEO runs out of credit).</>,
      <>(Optional) <span className="font-medium">Apify</span> — for <span className="font-medium">Temu</span> product-finding: sign up at <span className="font-mono text-xs">apify.com</span> → Settings → Integrations, copy the API token, paste into <span className="font-medium">Apify Token</span>. Bright Data can&apos;t scrape Temu, so the Temu lane runs on the Apify <span className="font-mono text-xs">crw/temu-products-scraper</span> actor.</>,
    ],
    footnote: (
      <>DataForSEO is pay-as-you-go (~$0.001–0.002 per task). A full discovery cycle costs a few cents.</>
    ),
  },
  "AI": {
    title: "Add your AI keys",
    intro: (
      <>
        <span className="font-semibold">One key is enough.</span> Gemini is the locked model for vision
        (image QA), image generation and the in-app assistant — but you don&apos;t need a separate Gemini
        key: set <span className="font-semibold">OpenRouter</span> and every Gemini call is routed through
        it (model <span className="font-mono text-xs">google/gemini-2.5-flash</span>).
      </>
    ),
    steps: [
      <><span className="font-medium">OpenRouter</span> (recommended, serves Gemini): sign up at <span className="font-mono text-xs">openrouter.ai</span> → Keys → create a key → paste into <span className="font-medium">OpenRouter API Key</span>. Save.</>,
      <>(Optional) <span className="font-medium">Gemini direct</span>: <span className="font-mono text-xs">aistudio.google.com</span> → Get API key → paste into <span className="font-medium">Gemini API Key</span>. Only needed to hit Gemini directly (slightly cheaper + the direct image-gen transport).</>,
    ],
    footnote: (
      <>Vision + image gen are locked to the Gemini model. With only OpenRouter set, that model is served through OpenRouter — no second key to paste.</>
    ),
  },
  "Google Ads": {
    title: "Connect Google Ads (read-only)",
    intro: (
      <>
        These keys let the app <span className="font-semibold">read</span> your Google Ads + Merchant
        Center health. Ad execution is a separate step. For Merchant Center only, the service-account
        guide on the Google Merchant Center card below is easier.
      </>
    ),
    steps: [
      <>In <span className="font-mono text-xs">console.cloud.google.com</span>, create/pick a project and <span className="font-medium">enable the Google Ads API</span>.</>,
      <>In your Google Ads <span className="font-medium">Manager (MCC)</span> account → API Center, apply for a <span className="font-medium">Developer Token</span>.</>,
      <>In Google Cloud → <span className="font-medium">APIs &amp; Services → Credentials</span>, create an <span className="font-medium">OAuth client</span>. Copy its Client ID + Secret.</>,
      <>Paste the Developer Token, OAuth Client ID + Secret, and your <span className="font-medium">Manager (MCC) ID</span> here.</>,
      <>Use <span className="font-medium">Connect Google</span> (Integrations below) to authorize — that fills the refresh token automatically.</>,
    ],
    footnote: (
      <>The per-store <span className="font-medium">Google Ads Customer ID</span> only picks WHICH account each store acts on — set it in Per-store setup.</>
    ),
  },
  "Sourcing": {
    title: "CJ Dropshipping (fallback supplier)",
    intro: (
      <>
        Optional. CJ is only a fallback for SKUs your private agent / 1688 can&apos;t carry.
      </>
    ),
    steps: [
      <>Log in at <span className="font-mono text-xs">cjdropshipping.com</span> → <span className="font-medium">Authorization / API</span>.</>,
      <>Copy your account email and generate an <span className="font-medium">API key</span>.</>,
      <>Paste them into <span className="font-medium">CJ Email</span> + <span className="font-medium">CJ API Key</span>. Save.</>,
    ],
  },
  "Google Merchant Center": {
    title: "Connect Merchant Center (the easy way)",
    intro: (
      <>
        For stores <span className="font-semibold">you own / admin</span>, a service account is far
        simpler than the full Google OAuth flow — no consent popup, no refresh tokens. You add one
        robot email to your Merchant Center and paste its key. ~5 minutes, one-time per store.
      </>
    ),
    steps: [
      <>Go to <span className="font-mono text-xs">console.cloud.google.com</span> → create (or pick) a project.</>,
      <>In that project, open <span className="font-medium">APIs &amp; Services → Library</span> and <span className="font-medium">enable the &quot;Merchant API&quot;</span>.</>,
      <><span className="font-medium">IAM &amp; Admin → Service Accounts → Create service account</span>. Give it any name; you don&apos;t need to grant it project roles.</>,
      <>Open the new service account → <span className="font-medium">Keys → Add key → Create new key → JSON</span>. A <span className="font-mono text-xs">.json</span> file downloads — keep it safe.</>,
      <>Copy the service account&apos;s email (looks like <span className="font-mono text-xs">name@project.iam.gserviceaccount.com</span>).</>,
      <>In <span className="font-medium">Merchant Center → Settings → Account access → People &amp; access → Add</span>, paste that email and give it <span className="font-medium">Standard</span> (or Admin) access.</>,
      <>Open the downloaded JSON file, copy <span className="font-medium">all of it</span>, and paste it into the <span className="font-medium">Service Account JSON key</span> box here. Save.</>,
      <>Set this store&apos;s <span className="font-medium">Merchant Center ID</span> in its Per-store setup (the number top-right in Merchant Center) so jobs know which account to read.</>,
    ],
    footnote: (
      <>
        This robot key lets the app <span className="font-semibold">read</span> your GMC health —
        account suspensions, product disapprovals, feed errors and reports. Repeat step 6 (add the
        same email) for each store&apos;s Merchant Center. Prefer the full <span className="font-medium">Connect
        Google</span> OAuth below only if you need to manage a store you don&apos;t admin.
      </>
    ),
  },
};

// A per-store Google account picker. When the Google integration is connected, the operator
// chooses WHICH account this store acts on from the discovered accounts instead of typing an
// id. "— enter manually —" reveals a text box for an account the API didn't enumerate.
function accountSelectField(
  label: string,
  configured: boolean,
  masked: string,
  options: { id: string; label: string }[],
  value: string | undefined,
  onChange: (v: string) => void,
) {
  const known = options.some((o) => o.id === value);
  const manual = value !== undefined && value !== "" && !known;
  return (
    <label className="flex h-full flex-col gap-1" key={label}>
      <span className="flex min-h-[2.5rem] items-center gap-1.5 text-sm font-medium leading-tight">
        {label}
        {configured ? (
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: "var(--state-winner)" }} title="set" />
        ) : null}
      </span>
      <select
        value={manual ? "__manual__" : value ?? ""}
        onChange={(e) => onChange(e.target.value === "__manual__" ? " " : e.target.value)}
        className="w-full rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 text-sm focus:border-[var(--accent)] focus:outline-none"
      >
        <option value="">{configured ? `${masked} — keep / change` : "— choose account —"}</option>
        {options.map((o) => (
          <option key={o.id} value={o.id}>
            {o.label}
          </option>
        ))}
        <option value="__manual__">— enter manually —</option>
      </select>
      {manual ? (
        <input
          type="text"
          autoComplete="off"
          value={value?.trim() ?? ""}
          placeholder="account id"
          onChange={(e) => onChange(e.target.value)}
          className="w-full rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 text-sm focus:border-[var(--accent)] focus:outline-none"
        />
      ) : null}
    </label>
  );
}

// The ONE place to set the working TEXT/agent model — the in-app Assistant copilot today, and
// the server-side optimization jobs later. Mirrors the same control on the Costs page (both call
// PUT /api/costs/assumptions {agent_model}), so the selection stays in sync wherever it's shown.
// Gemini stays the LOCKED vision + image model (powered by the one Gemini key in the AI group);
// this selector only picks the text model, and changing it also re-prices the LLM cost line.
function AgentModelSettingsCard() {
  const { data, loading, error, reload } = useApi(() => api.costs());
  const [saving, setSaving] = useState<string | null>(null);
  if (loading) return <Card className="p-4 text-sm text-[var(--muted)]">Loading agent model…</Card>;
  if (error || !data) return null;
  const pick = async (id: string) => {
    if (id === data.agent.selected) return;
    setSaving(id);
    try {
      await api.saveCostAssumptions({ agent_model: id });
      reload();
    } finally {
      setSaving(null);
    }
  };
  return (
    <Card>
      <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
        Agent model · in-app assistant + backend jobs
      </div>
      <p className="px-4 pt-3 text-xs text-[var(--muted)]">
        The working text/agent model for the in-app Assistant copilot and future server-side
        optimization jobs.{" "}
        <span className="font-semibold text-[var(--text)]">
          Gemini stays the locked vision &amp; image model
        </span>{" "}
        (powered by the one Gemini key above) — this picks the text model only. Same setting as the
        Costs page; changing it also re-prices the LLM cost line.
      </p>
      <div className="grid grid-cols-1 gap-2 p-4 sm:grid-cols-2 lg:grid-cols-3">
        {data.agent.models.map((m) => {
          const active = m.id === data.agent.selected;
          return (
            <button
              key={m.id}
              onClick={() => pick(m.id)}
              disabled={!!saving}
              className={`rounded-lg border px-3 py-2.5 text-left transition-colors disabled:opacity-60 ${
                active
                  ? "border-[var(--accent)] bg-[var(--accent)]/10"
                  : "border-[var(--border)] hover:border-[var(--accent)]/50"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold">{m.label}</span>
                {active ? (
                  <span className="text-[10px] font-semibold text-[var(--accent)]">selected</span>
                ) : saving === m.id ? (
                  <span className="text-[10px] text-[var(--muted)]">…</span>
                ) : null}
              </div>
              <div className="mt-0.5 font-mono text-[11px] text-[var(--muted)]">
                ${m.input}/${m.output} per Mtok
              </div>
            </button>
          );
        })}
      </div>
    </Card>
  );
}

// A collapsible "connector" section — one per credential group. Header shows a status badge
// ("✓ Connected" / "N of M set" / "not set") so the operator can see at a glance which
// connectors are done without expanding every one. Optional ? help popover on the right.
function ConnectorSection({
  title,
  subtitle,
  configured,
  total,
  badgeOverride,
  defaultOpen = false,
  help,
  children,
}: {
  title: string;
  subtitle?: React.ReactNode;
  configured: number;
  total: number;
  badgeOverride?: { text: string; done: boolean };
  defaultOpen?: boolean;
  help?: { title: string; intro?: React.ReactNode; steps: React.ReactNode[]; footnote?: React.ReactNode };
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const allSet = total > 0 && configured >= total;
  const someSet = configured > 0;
  const done = badgeOverride ? badgeOverride.done : allSet;
  const badgeColor = done
    ? "var(--state-winner)"
    : someSet
      ? "var(--state-drafted)"
      : "var(--muted)";
  const badgeText = badgeOverride
    ? badgeOverride.text
    : allSet
      ? "✓ Connected"
      : someSet
        ? `${configured} of ${total} set`
        : "not set";
  return (
    <Card>
      <div
        className="flex cursor-pointer items-center justify-between gap-2 px-4 py-3 select-none"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="flex min-w-0 items-center gap-2.5">
          <span
            className="text-[var(--muted)] transition-transform"
            style={{ transform: open ? "rotate(90deg)" : "none" }}
          >
            ▸
          </span>
          <span className="min-w-0">
            <span className="text-sm font-semibold">{title}</span>
            {subtitle ? <span className="ml-2 text-xs text-[var(--muted)]">{subtitle}</span> : null}
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-2">
          <span
            className="rounded-md px-2 py-0.5 text-[11px] font-semibold"
            style={{ color: badgeColor, background: `color-mix(in srgb, ${badgeColor} 14%, transparent)` }}
          >
            {badgeText}
          </span>
          {help ? (
            <span onClick={(e) => e.stopPropagation()}>
              <HelpPopover {...help} />
            </span>
          ) : null}
        </span>
      </div>
      {open ? <div className="border-t border-[var(--border)]">{children}</div> : null}
    </Card>
  );
}

// The Admin API scopes this app needs on each Shopify store — one per feature area, so the copied
// set is enough for EVERY part of the app (build half + the ported operation "run" half). Grouped
// by which app surface consumes each scope. One-click copy into the Shopify custom/dev app.
const SHOPIFY_ADMIN_SCOPES = [
  // ── Listing / catalog build (Research & Listing, Product Feed, Product Mgmt cost push) ──
  "read_products",
  "write_products",
  "read_inventory",
  "write_inventory", // Product Mgmt pushes per-variant cost + inventory levels
  "read_price_rules",
  "read_discounts", // modern replacement for price_rules (compare-at / promo reads)
  "read_product_listings",
  // ── Publish live to the Google & YouTube sales channel (going-live from draft) ──
  "read_publications",
  "write_publications",
  // ── Store profile + multi-market localization (Multimarket, incl. its WRITE paths:
  //    enable languages, create markets, push shipping profiles + templated legal policies) ──
  "read_locales",
  "write_locales",
  "read_markets",
  "write_markets",
  "read_translations",
  "write_translations",
  "read_shipping",
  "write_shipping",
  "read_legal_policies",
  "write_legal_policies",
  // ── Images / theme content (image push, pages, policies) ──
  "read_files",
  "write_files",
  "read_content",
  "write_content",
  // ── Finance / P&L (revenue, refunds, fees, payouts, disputes) ──
  "read_orders",
  "read_shopify_payments_payouts", // payout fees line in Finance
  "read_shopify_payments_disputes", // chargebacks / disputes
  // ── Orders & Issues (parcel tracking, dispute resolution, customer contact) ──
  "read_customers",
  "read_fulfillments",
  "write_fulfillments", // attach tracking / resolve issues
  "read_merchant_managed_fulfillment_orders",
  "read_assigned_fulfillment_orders",
];

// Numbered onboarding for a NOT-yet-connected store — the exact steps to give it a Shopify
// app with every scope the operator app needs, so every future store add "just works".
// Shown only while the store can't authenticate; once connected the verify panel takes over.
function StoreSetupGuide({ storeName }: { storeName: string }) {
  const steps: React.ReactNode[] = [
    <>
      In the Shopify <span className="font-medium">dev dashboard</span> (dev.shopify.com) create an
      app for this store — e.g. <span className="font-mono">{storeName} Operation</span>.
    </>,
    <>
      In the app&apos;s <span className="font-medium">Access scopes</span>, paste the FULL scope list
      (button below — all 31, covering listing, publishing, localization, finance and issues).
    </>,
    <>
      <span className="font-medium">Release</span> the app version, then{" "}
      <span className="font-medium">install</span> the app on this store.
    </>,
    <>
      Copy the app&apos;s <span className="font-medium">Client ID</span> and{" "}
      <span className="font-medium">Secret</span> (app Settings) and paste them below, together with
      the store&apos;s <span className="font-mono">.myshopify.com</span> domain — then Save.
      Connection check + first data sync run automatically.
    </>,
  ];
  return (
    <div className="rounded-lg border border-[var(--accent)] bg-[var(--surface-2)] px-3 py-2.5"
      style={{ borderColor: "color-mix(in srgb, var(--accent) 45%, transparent)" }}>
      <div className="mb-1.5 text-xs font-semibold">Connect this store — 4 steps</div>
      <ol className="space-y-1.5">
        {steps.map((s, i) => (
          <li key={i} className="flex gap-2 text-[11px] leading-relaxed text-[var(--muted)]">
            <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[10px] font-bold"
              style={{ background: "var(--accent)", color: "var(--accent-fg)" }}>
              {i + 1}
            </span>
            <span>{s}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

// Last automatic data-sync verdict + a "Sync now" trigger. The worker syncs every connected
// store daily (profile + finance + orders + issues) and a save of creds fires an immediate
// background sync — this panel makes that visible and gives the operator a manual refresh.
function StoreSyncPanel({ store, lastSync }: {
  store: string;
  lastSync: ConnShopifyStore["last_sync"];
}) {
  const [running, setRunning] = useState(false);
  const [live, setLive] = useState<StoreSyncResult | null>(null);

  async function syncNow() {
    setRunning(true);
    try {
      setLive(await api.syncStore(store));
    } catch (e) {
      setLive({ ok: false, parts: {}, failures: [e instanceof Error ? e.message : "sync failed"] });
    } finally {
      setRunning(false);
    }
  }

  const shown = live
    ? {
        status: live.skipped ? "skipped" : live.ok ? "done" : "failed",
        detail: live.skipped ? (live.reason ?? "") : live.failures.join(" · ") || "all parts synced",
        at: "just now",
      }
    : lastSync
      ? { status: lastSync.status, detail: lastSync.detail ?? "", at: lastSync.at }
      : null;
  const tone = !shown
    ? "var(--muted)"
    : shown.status === "done"
      ? "var(--state-winner)"
      : shown.status === "failed"
        ? "var(--state-killed)"
        : "var(--state-drafted)";

  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2">
          <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: tone }} />
          <span className="min-w-0">
            <span className="block text-xs font-semibold">
              {running
                ? "Syncing store data…"
                : !shown
                  ? "Data sync — not run yet (worker runs it daily once connected)"
                  : shown.status === "done"
                    ? "Data sync healthy"
                    : shown.status === "failed"
                      ? "Data sync FAILED"
                      : "Data sync skipped"}
            </span>
            {shown ? (
              <span className="block truncate text-[11px] text-[var(--muted)]" title={shown.detail}>
                {shown.at} — {shown.detail}
              </span>
            ) : null}
          </span>
        </span>
        <button
          type="button"
          onClick={syncNow}
          disabled={running}
          className="shrink-0 rounded-md border border-[var(--border)] px-2 py-1 text-[11px] font-semibold hover:bg-[var(--surface)] disabled:opacity-40"
        >
          {running ? "…" : "Sync now"}
        </button>
      </div>
    </div>
  );
}

// Live connection + scope check for one store. Auto-runs when mounted (Store tab open) and again
// after a save, so "did this store connect and does it have every scope?" is answered automatically
// instead of only surfacing as an error deep inside some sub-app later.
function StoreVerifyPanel({ store, refreshKey }: { store: string; refreshKey: number }) {
  const [res, setRes] = useState<StoreVerifyResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  function copyMissing() {
    if (!res?.missing?.length) return;
    navigator.clipboard?.writeText(res.missing.join(",")).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1600);
      },
      () => {},
    );
  }

  const run = useCallback(() => {
    setLoading(true);
    api.verifyStore(store).then(
      (r) => {
        setRes(r);
        setLoading(false);
      },
      () => setLoading(false),
    );
  }, [store]);

  useEffect(() => {
    run();
  }, [run, refreshKey]);

  const tone = res?.ok
    ? "var(--state-winner)"
    : res?.connected
      ? "var(--state-drafted)"
      : "var(--state-killed)";
  const headline = loading
    ? "Checking connection & scopes…"
    : !res
      ? "Not checked yet"
      : res.ok
        ? "Connected — all required scopes granted"
        : res.connected
          ? `Connected, but missing ${res.missing.length} scope${res.missing.length === 1 ? "" : "s"}`
          : res.auth_error || "Not connected";

  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-2">
          <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: tone }} />
          <span className="text-xs font-semibold">{headline}</span>
        </span>
        <button
          type="button"
          onClick={run}
          disabled={loading}
          className="rounded-md border border-[var(--border)] px-2 py-1 text-[11px] font-semibold hover:bg-[var(--surface)] disabled:opacity-40"
        >
          {loading ? "…" : "Re-check"}
        </button>
      </div>
      {res && !res.ok && res.connected && res.missing.length > 0 ? (
        <div className="mt-2 text-[11px] leading-relaxed text-[var(--muted)]">
          <div className="flex items-center justify-between gap-2">
            <span>Add these scopes to the Shopify app, then re-install &amp; re-check:</span>
            <button
              type="button"
              onClick={copyMissing}
              className="shrink-0 rounded-md px-2 py-0.5 text-[11px] font-semibold"
              style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
            >
              {copied ? "Copied ✓" : "Copy missing"}
            </button>
          </div>
          <code className="mt-1 block break-words rounded bg-[var(--surface)] px-2 py-1.5 font-mono text-[11px] text-[var(--state-killed)]">
            {res.missing.join(", ")}
          </code>
        </div>
      ) : null}
      {res && !res.connected && res.auth_error ? (
        <p className="mt-2 text-[11px] leading-relaxed text-[var(--muted)]">{res.auth_error}</p>
      ) : null}
    </div>
  );
}

function ShopifyScopeCopy() {
  const [copied, setCopied] = useState(false);
  // Prefer the backend single-source list (can never drift from what the code needs); fall back
  // to the local constant if the fetch fails.
  const [scopeList, setScopeList] = useState<string[]>(SHOPIFY_ADMIN_SCOPES);
  useEffect(() => {
    let live = true;
    api.shopifyScopes().then(
      (r) => live && r.scopes?.length && setScopeList(r.scopes),
      () => {},
    );
    return () => {
      live = false;
    };
  }, []);
  const scopes = scopeList.join(",");
  function copy() {
    navigator.clipboard?.writeText(scopes).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1600);
      },
      () => {},
    );
  }
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2.5">
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
          Admin API access scopes
        </span>
        <button
          type="button"
          onClick={copy}
          className="rounded-md px-2.5 py-1 text-[11px] font-semibold"
          style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          {copied ? "Copied ✓" : "Copy all scopes"}
        </button>
      </div>
      <p className="mb-2 text-[11px] leading-relaxed text-[var(--muted)]">
        In your Shopify custom / dev app →{" "}
        <span className="font-medium">Configuration → Admin API integration → Admin API access scopes</span>,
        paste these so the app can import products, pull each store&apos;s profile (markets + languages)
        and write per-market translations. Then <span className="font-medium">Install</span> the app on the store.
      </p>
      <code className="block break-words rounded bg-[var(--surface)] px-2 py-1.5 font-mono text-[11px] leading-relaxed text-[var(--text)]">
        {scopes}
      </code>
    </div>
  );
}

// Per-store edit surface, NN "Master Settings" style: a store opens into a modal with its own
// tabs (Store / Google Ads / Finance / Issues), so each concern is its own small settings pane
// instead of everything piled inline. The tab order is fixed; only groups the store actually has
// are shown. `Store` tab also pulls the Shopify profile; `Google` tab carries the ad-spend key.
const STORE_TAB_ORDER = ["Store", "Google", "Finance", "Orders & Issues"] as const;
const STORE_TAB_LABEL: Record<string, string> = {
  Store: "Store",
  Google: "Google Ads",
  Finance: "Finance",
  "Orders & Issues": "Issues",
};

function storeField(s: ConnShopifyStore, key: string) {
  return s.fields.find((f) => f.key === key);
}
function storeDisplayName(s: ConnShopifyStore): string {
  const dn = storeField(s, "display_name");
  return dn?.configured && dn.masked ? dn.masked : s.store;
}
function storeDomain(s: ConnShopifyStore): string {
  const d = storeField(s, "shop_domain");
  return d?.configured ? d.masked : "";
}

function StoreEditModal({
  store,
  googleAccts,
  googleConnected,
  onClose,
  onSaved,
}: {
  store: ConnShopifyStore;
  googleAccts: GoogleAccounts | null;
  googleConnected: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const groups: Record<string, ConnShopifyStore["fields"]> = {};
  for (const f of store.fields) (groups[f.group] ??= []).push(f);
  const tabs = STORE_TAB_ORDER.filter((g) => groups[g]?.length);
  const [tab, setTab] = useState<string>(tabs[0] ?? "Store");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [delConfirm, setDelConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // Bumped after every save so the verify panel re-checks the connection + scopes automatically.
  const [verifyKey, setVerifyKey] = useState(0);
  // Catalog path (general | fashion) — saved immediately on change (it lives in the store's
  // listing queue, not in the credential edits payload).
  const [mode, setMode] = useState<"general" | "fashion" | "both">(store.mode ?? "general");
  const [modeSaving, setModeSaving] = useState(false);
  const { reload: reloadStores } = useStore(); // refresh the global sidebar/badges after a path change
  const dirty = Object.keys(edits).length > 0;
  const set = (key: string, v: string) => setEdits((e) => ({ ...e, [key]: v }));

  async function changeMode(next: "general" | "fashion" | "both") {
    if (next === mode || modeSaving) return;
    const prev = mode;
    setMode(next);
    setModeSaving(true);
    try {
      await api.setStoreMode(store.store, next);
      onSaved();
      reloadStores(); // update the sidebar dropdown "· Fashion/· Both" badge everywhere, no reload
    } catch (e) {
      setMode(prev);
      setErr(e instanceof Error ? e.message : "Mode change failed");
    } finally {
      setModeSaving(false);
    }
  }

  async function save() {
    setSaving(true);
    setErr(null);
    try {
      await api.connectionsSave({ shopify: { [store.store]: edits } });
      setEdits({});
      onSaved();
      // Stay open and auto re-verify so the operator immediately sees whether the store
      // connected and has every scope — instead of closing blind.
      setVerifyKey((k) => k + 1);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }
  async function remove() {
    setDeleting(true);
    setErr(null);
    try {
      await api.deleteStore(store.store);
      onSaved();
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Delete failed");
      setDeleting(false);
    }
  }

  const fields = groups[tab] ?? [];

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:p-8"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl rounded-2xl border border-[var(--border)] bg-[var(--surface)] shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header — store identity (left) + primary actions (right): Save is the top-right anchor,
            Delete sits next to it in small, then the close ✕. */}
        <div className="flex items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-3">
          <span className="flex min-w-0 items-center gap-2.5">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ background: store.connected ? "var(--state-winner)" : "var(--state-drafted)" }}
              title={store.connected ? "connected" : "not connected"}
            />
            <span className="min-w-0">
              <span className="block truncate text-sm font-semibold">{storeDisplayName(store)}</span>
              <span className="block truncate text-[11px] text-[var(--muted)]">
                {storeDomain(store) || <span className="italic">no domain yet</span>}
                <span className="ml-1.5 font-mono opacity-60">{store.store}</span>
              </span>
            </span>
          </span>
          <span className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={save}
              disabled={saving || !dirty}
              className="rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
              style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
            >
              {saving ? "Saving…" : "Save"}
            </button>
            {delConfirm ? (
              <>
                <button
                  type="button"
                  onClick={remove}
                  disabled={deleting}
                  className="rounded-md px-2 py-1 text-[11px] font-semibold disabled:opacity-40"
                  style={{ background: "var(--state-killed)", color: "#fff" }}
                >
                  {deleting ? "Removing…" : "Confirm"}
                </button>
                <button
                  type="button"
                  onClick={() => setDelConfirm(false)}
                  className="rounded-md border border-[var(--border)] px-2 py-1 text-[11px] font-semibold hover:bg-[var(--surface-2)]"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => setDelConfirm(true)}
                className="rounded-md border border-[var(--border)] px-2 py-1 text-[11px] font-semibold text-[var(--state-killed)] hover:bg-[var(--surface-2)]"
                title="Delete store"
              >
                Delete
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="ml-0.5 text-[var(--muted)] hover:text-[var(--text)]"
              aria-label="Close"
            >
              ✕
            </button>
          </span>
        </div>

        {/* tabs */}
        <div className="flex gap-1 border-b border-[var(--border)] px-3 pt-2">
          {tabs.map((g) => {
            const on = g === tab;
            return (
              <button
                key={g}
                type="button"
                onClick={() => setTab(g)}
                className={`rounded-t-lg px-3 py-1.5 text-xs font-semibold transition-colors ${
                  on ? "bg-[var(--surface-2)] text-[var(--text)]" : "text-[var(--muted)] hover:text-[var(--text)]"
                }`}
                style={on ? { boxShadow: "inset 0 -2px 0 var(--accent)" } : undefined}
              >
                {STORE_TAB_LABEL[g] ?? g}
              </button>
            );
          })}
        </div>

        {/* active tab body */}
        <div className="space-y-3 px-5 py-4">
          {tab === "Google" ? (
            <p className="text-[11px] text-[var(--muted)]">
              {googleConnected
                ? "Google is connected — pick which account this store acts on."
                : "Connect Google (Integrations, below) first, then pick an account here — or type an id manually."}
            </p>
          ) : null}
          <div className="grid items-start gap-x-4 gap-y-3 sm:grid-cols-2">
            {fields.map((f) => {
              const opts =
                f.key === "google_ads_customer_id"
                  ? googleAccts?.ads_accounts
                  : f.key === "merchant_center_id"
                    ? googleAccts?.merchant_accounts
                    : null;
              if (tab === "Google" && opts && opts.length > 0) {
                return accountSelectField(f.label, f.configured, f.masked, opts, edits[f.key], (v) => set(f.key, v));
              }
              if (f.key === "data_start_date") {
                return dateField(f.label, f.configured, f.masked, edits[f.key], (v) => set(f.key, v));
              }
              return maskedField(f.label, f.secret, f.configured, f.masked, f.placeholder, edits[f.key], (v) => set(f.key, v));
            })}
          </div>
          {tab === "Store" ? (
            <div className="flex items-center justify-between gap-3 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2.5">
              <span className="min-w-0">
                <span className="block text-xs font-semibold">Catalog path</span>
                <span className="block text-[11px] leading-relaxed text-[var(--muted)]">
                  {mode === "fashion"
                    ? "Fashion — research, competitor finding and listing all work ON apparel for this store."
                    : mode === "both"
                      ? "Both — general breadth AND apparel together; nothing is excluded, discovery spans both."
                      : "General — broad multi-category catalog; apparel stays excluded."}
                </span>
              </span>
              <span className="flex shrink-0 items-center gap-1 rounded-lg border border-[var(--border)] p-0.5">
                {(["general", "fashion", "both"] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => changeMode(m)}
                    disabled={modeSaving}
                    className={`rounded-md px-2.5 py-1 text-[11px] font-semibold capitalize transition-colors ${
                      mode === m ? "" : "text-[var(--muted)] hover:text-[var(--text)]"
                    }`}
                    style={mode === m ? { background: "var(--accent)", color: "var(--accent-fg)" } : undefined}
                  >
                    {m}
                  </button>
                ))}
              </span>
            </div>
          ) : null}
          {tab === "Store" && !store.connected ? <StoreSetupGuide storeName={storeDisplayName(store)} /> : null}
          {tab === "Store" ? <StoreVerifyPanel store={store.store} refreshKey={verifyKey} /> : null}
          {tab === "Store" ? <StoreSyncPanel store={store.store} lastSync={store.last_sync ?? null} /> : null}
          {tab === "Store" ? <ShopifyScopeCopy /> : null}
          {tab === "Store" ? (
            <StoreProfilePanel store={store.store} profile={store.profile ?? null} connected={store.connected} onPulled={onSaved} />
          ) : null}
          {tab === "Google" ? <AdSpendScriptPanel store={store.store} /> : null}
        </div>

        {/* footer — status only (Save + Delete live in the header top-right now) */}
        {dirty || err ? (
          <div className="flex flex-wrap items-center gap-3 border-t border-[var(--border)] px-5 py-2.5">
            {dirty ? <span className="text-xs text-[var(--state-drafted)]">unsaved changes</span> : null}
            {err ? <span className="text-xs" style={{ color: "var(--state-killed)" }}>{err}</span> : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// Live connector-API health — makes an out-of-credit / auth-failed / no-creds failure VISIBLE
// (e.g. DataForSEO negative balance → every Google-Shopping Merchant scan 402s → the lane returns 0)
// so the operator can see WHY a source isn't working and fix it, right where the keys are configured.
function ConnectorHealthPanel() {
  const { data, loading, reload } = useApi(() => api.connectionsHealth());
  if (loading || !data) return null;
  const problems = data.problems ?? [];
  return (
    <Card className={`p-4${problems.length ? " ring-1 ring-[var(--state-killed)]" : ""}`}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          Connector health{problems.length ? ` — ${problems.length} need${problems.length === 1 ? "s" : ""} attention` : " — all OK"}
        </span>
        <button onClick={reload} className="text-xs text-[var(--muted)] hover:underline">Re-check</button>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {data.checks.map((c) => (
          <div key={c.key} className="flex items-start gap-2 rounded-md border border-[var(--border)] px-3 py-2">
            <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full"
              style={{ background: c.ok ? "var(--state-live, #16a34a)" : "var(--state-killed)" }} />
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-sm font-medium">
                {c.name}
                {!c.ok ? (
                  <span className="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase"
                    style={{ background: "color-mix(in srgb, var(--state-killed) 14%, transparent)", color: "var(--state-killed)" }}>
                    {c.status.replace(/_/g, " ")}
                  </span>
                ) : null}
              </div>
              <div className="text-xs text-[var(--muted)]">{c.message}</div>
              {c.fix ? (
                <div className="mt-1 text-xs font-medium" style={{ color: c.ok ? "var(--muted)" : "var(--state-killed)" }}>
                  → {c.fix}
                </div>
              ) : null}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ConnectionsCard() {
  const { data, error, loading, reload } = useApi(() => api.connections());
  // edits: api keyed by env; shopify keyed by `${store}::${key}`. Only present keys are sent.
  const [apiEdits, setApiEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  // add-store
  const [newStore, setNewStore] = useState("");
  const [newMode, setNewMode] = useState<"general" | "fashion" | "both">("general");
  const [adding, setAdding] = useState(false);
  const [addErr, setAddErr] = useState<string | null>(null);
  // which store's edit modal is open (store key)
  const [editing, setEditing] = useState<string | null>(null);
  // Google account picker — only meaningful once the Google integration is connected.
  const [googleAccts, setGoogleAccts] = useState<GoogleAccounts | null>(null);
  const googleConnected = !!data?.integrations?.find((i) => i.id === "google")?.connected;

  useEffect(() => {
    if (!googleConnected) {
      setGoogleAccts(null);
      return;
    }
    let live = true;
    api.googleAccounts().then(
      (a) => live && setGoogleAccts(a),
      () => live && setGoogleAccts(null),
    );
    return () => {
      live = false;
    };
  }, [googleConnected]);

  if (loading) return <Card className="p-4 text-sm text-[var(--muted)]">Loading connections…</Card>;
  if (error) return <Card className="p-4 text-sm"><span style={{ color: "var(--state-killed)" }}>{error}</span></Card>;
  if (!data) return null;

  async function addStore() {
    // One field: the operator types the store NAME. The store key is DERIVED from it
    // (slugified) — no separate key field. The typed name is also saved as the display name.
    const name = newStore.trim();
    const key = name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
    if (!key) return;
    setAdding(true);
    setAddErr(null);
    try {
      await api.addStore(key, newMode);
      // Persist the human name as the store's display name (key ≠ name once slugified).
      if (name && name !== key) {
        try {
          await api.connectionsSave({ shopify: { [key]: { display_name: name } } });
        } catch {
          /* display-name is best-effort; the store still exists under its key */
        }
      }
      setNewStore("");
      reload();
      // Land the operator straight in the new store's setup guide (Store tab of its modal):
      // create the app → paste all scopes → release+install → paste creds. Verify + first
      // sync then run automatically on save — future store adds work end-to-end by default.
      setEditing(key);
    } catch (e) {
      setAddErr(e instanceof Error ? e.message : "Add failed");
    } finally {
      setAdding(false);
    }
  }

  // Account-wide API keys save here; per-store creds are saved from each store's edit modal.
  const dirty = Object.keys(apiEdits).length > 0;

  async function save() {
    setSaving(true);
    setSaveErr(null);
    try {
      await api.connectionsSave({ api: apiEdits });
      setApiEdits({});
      setSavedAt(Date.now());
      reload();
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  // group API fields by their `group` label (Data / AI).
  const groups: Record<string, ConnApiField[]> = {};
  for (const f of data.api) (groups[f.group] ??= []).push(f);

  return (
    <div className="space-y-4">
      {/* Live connector-API health — shows out-of-credit / auth failures right where keys are set. */}
      <ConnectorHealthPanel />

      <p className="px-1 text-xs text-[var(--muted)]">
        Secrets are stored masked and never shown again — a set field reads{" "}
        <span className="font-mono">••••1234</span>. Type a new value to overwrite, or leave
        a field blank to keep what&apos;s saved. These persist in the backend database (Neon
        on the live deploy), so a redeploy won&apos;t lose them.
      </p>

      {/* ── stores ── a clean list; each store opens into its own tabbed settings modal ── */}
      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] px-4 py-3">
          <span className="text-sm font-semibold">Stores</span>
          <span className="text-xs text-[var(--muted)]">
            {data.shopify.filter((s) => s.connected).length} of {data.shopify.length} connected
          </span>
          <span className="ml-auto flex items-center gap-2">
            <input
              value={newStore}
              onChange={(e) => setNewStore(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") addStore();
              }}
              placeholder="Store name"
              title="Type the store name — the store key is derived from it automatically"
              className="w-44 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-xs focus:border-[var(--accent)] focus:outline-none"
            />
            <select
              value={newMode}
              onChange={(e) =>
                setNewMode(
                  e.target.value === "fashion" ? "fashion" : e.target.value === "both" ? "both" : "general",
                )
              }
              title="Catalog path: general (broad, apparel excluded), fashion (apparel end-to-end), or both"
              className="rounded-md border border-[var(--border)] bg-[var(--surface)] px-1.5 py-1 text-xs focus:border-[var(--accent)] focus:outline-none"
            >
              <option value="general">General</option>
              <option value="fashion">Fashion</option>
              <option value="both">Both</option>
            </select>
            <button
              type="button"
              onClick={addStore}
              disabled={adding || !newStore.trim()}
              className="rounded-lg px-2.5 py-1 text-xs font-semibold disabled:opacity-40"
              style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
            >
              {adding ? "Adding…" : "+ Add store"}
            </button>
          </span>
        </div>
        {newStore.trim() ? (
          <div className="px-4 pt-2 text-[11px] text-[var(--muted)]">
            key:{" "}
            <span className="font-mono text-[var(--text)]">
              {newStore.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "")}
            </span>
          </div>
        ) : null}
        {addErr ? (
          <div className="px-4 pt-2 text-xs" style={{ color: "var(--state-killed)" }}>{addErr}</div>
        ) : null}
        {data.shopify.length === 0 ? (
          <div className="px-4 py-4 text-sm text-[var(--muted)]">
            No stores yet. Add one by its name, then open it to add its <span className="font-mono">.myshopify.com</span> domain.
          </div>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {data.shopify.map((s: ConnShopifyStore) => (
              <button
                key={s.store}
                type="button"
                onClick={() => setEditing(s.store)}
                className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-[var(--surface-2)]"
              >
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ background: s.connected ? "var(--state-winner)" : "var(--state-drafted)" }}
                  title={s.connected ? "connected" : "not connected"}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-semibold">{storeDisplayName(s)}</span>
                  <span className="block truncate text-[11px] text-[var(--muted)]">
                    {storeDomain(s) || <span className="italic">no myshopify domain yet</span>}
                  </span>
                </span>
                {s.mode === "fashion" || s.mode === "both" ? (
                  <span className="hidden shrink-0 rounded-md px-2 py-0.5 text-[10px] font-semibold capitalize sm:inline"
                    style={{ color: "var(--accent)", background: "color-mix(in srgb, var(--accent) 14%, transparent)" }}>
                    {s.mode}
                  </span>
                ) : null}
                {s.last_sync?.status === "failed" ? (
                  <span className="shrink-0 rounded-md px-2 py-0.5 text-[10px] font-semibold"
                    title={s.last_sync?.detail ?? undefined}
                    style={{ color: "var(--state-killed)", background: "color-mix(in srgb, var(--state-killed) 14%, transparent)" }}>
                    Sync failed
                  </span>
                ) : null}
                {s.ads_ready ? (
                  <span className="hidden shrink-0 rounded-md px-2 py-0.5 text-[10px] font-semibold sm:inline"
                    style={{ color: "var(--state-winner)", background: "color-mix(in srgb, var(--state-winner) 14%, transparent)" }}>
                    Google linked
                  </span>
                ) : null}
                <span className="shrink-0 text-[var(--muted)]" aria-hidden>
                  {/* pencil */}
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
                  </svg>
                </span>
              </button>
            ))}
          </div>
        )}
      </Card>

      {editing ? (() => {
        const s = data.shopify.find((x) => x.store === editing);
        if (!s) return null;
        return (
          <StoreEditModal
            store={s}
            googleAccts={googleAccts}
            googleConnected={googleConnected}
            onClose={() => setEditing(null)}
            onSaved={reload}
          />
        );
      })() : null}

      {/* ── global API keys, grouped — one collapsible section per connector ── */}
      {Object.entries(groups).map(([group, fields]) => {
        const help = API_GROUP_HELP[group];
        const configured = fields.filter((f) => f.configured).length;
        return (
          <ConnectorSection
            key={group}
            title={group}
            subtitle="API keys"
            configured={configured}
            total={fields.length}
            defaultOpen={configured < fields.length}
            help={help}
          >
            <div className={`grid gap-3 px-4 py-3 ${fields.some((f) => f.multiline) ? "" : "sm:grid-cols-2"}`}>
              {fields.map((f) =>
                maskedField(
                  f.label, f.secret, f.configured, f.masked, f.placeholder ?? "",
                  apiEdits[f.env],
                  (v) => setApiEdits((e) => ({ ...e, [f.env]: v })),
                  f.multiline ?? false,
                ),
              )}
            </div>
            {group === "Data" ? <BrightDataSetup onChange={reload} /> : null}
          </ConnectorSection>
        );
      })}

      {/* ── working text/agent model (one place to set it; vision stays locked to Gemini) ── */}
      <AgentModelSettingsCard />

      {/* ── OAuth / MCP integrations (no key to paste) ── */}
      {data.integrations && data.integrations.length > 0 ? (
        <ConnectorSection
          title="Integrations"
          subtitle="OAuth / MCP"
          configured={data.integrations.filter((i) => (i.id === "markifact" || i.id === "google" ? !!i.connected : i.status === "keys")).length}
          total={data.integrations.length}
        >
          <div className="divide-y divide-[var(--border)]">
            {data.integrations.map((it: ConnIntegration) => {
              const oauthRow = it.id === "markifact" || it.id === "google";
              const connected = oauthRow ? !!it.connected : it.status === "keys";
              const badge =
                it.id === "markifact"
                  ? connected
                    ? "connected"
                    : it.status === "registered"
                      ? "client registered"
                      : "not connected"
                  : it.id === "google"
                    ? connected
                      ? "connected"
                      : it.status === "client ready"
                        ? "client ready · connect"
                        : "needs OAuth client"
                    : connected
                      ? "via API keys above"
                      : "planned · manual";
              return (
                <div key={it.id} className="px-4 py-3">
                  <div className="mb-1 flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold">{it.name}</span>
                    <span className="font-mono text-[11px] text-[var(--muted)]">{it.kind}</span>
                    <span
                      className="rounded-md px-2 py-0.5 text-[11px] font-semibold"
                      style={{
                        color: connected ? "var(--state-winner)" : "var(--state-drafted)",
                        background: `color-mix(in srgb, ${connected ? "var(--state-winner)" : "var(--state-drafted)"} 14%, transparent)`,
                      }}
                    >
                      {badge}
                    </span>
                  </div>
                  <div className="text-xs text-[var(--muted)]">{it.purpose}</div>
                  <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-[var(--muted)]">
                    <span className="font-mono">{it.endpoint}</span>
                    <span>{it.auth}</span>
                  </div>
                  {it.id === "markifact" ? (
                    <MarkifactControls connected={connected} detail={it.detail as MarkifactStatus | undefined} onChange={reload} />
                  ) : null}
                  {it.id === "google" ? (
                    <GoogleControls detail={it.detail as GoogleStatus | undefined} onChange={reload} />
                  ) : null}
                </div>
              );
            })}
          </div>
        </ConnectorSection>
      ) : null}

      <div className="flex flex-wrap items-center gap-3 px-1">
        <button
          type="button"
          onClick={save}
          disabled={saving || !dirty}
          className="rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
          style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          {saving ? "Saving…" : "Save connections"}
        </button>
        {dirty ? <span className="text-xs text-[var(--state-drafted)]">unsaved changes</span> : null}
        {!dirty && savedAt ? <span className="text-xs" style={{ color: "var(--state-winner)" }}>saved ✓</span> : null}
        {saveErr ? <span className="text-xs" style={{ color: "var(--state-killed)" }}>{saveErr}</span> : null}
      </div>
    </div>
  );
}

// Per-store store profile pulled from Shopify (native language, currency, timezone, markets,
// catalog size). This is the SINGLE SOURCE for those facts — it flows downstream (VisionScan
// gallery-language check, pricing currency, scheduling timezone). "Pull from Shopify" hits the
// store's admin token and saves the facts into Connections; nothing here is editable by hand.
// Google Ads ad-spend ingest — the per-store key + endpoint the operator pastes into their Google
// Ads Apps Script so daily spend streams into Finance. Lives HERE in Connections (Google Ads), not
// as a standalone Finance tab: ad spend is a connection, not a manual input.
// The complete, ready-to-paste Google Ads Script for one store — same model as the
// pl-dashboard's (script runs INSIDE the Google Ads account, no third party) but pushing
// to THIS app's ingest endpoint with its payload shape. Copy → Google Ads → Tools →
// Scripts → paste → schedule hourly. Spend then streams into Finance · P&L daily.
function buildAdsScript(store: string, key: string, lookbackDays: number): string {
  const endpoint = `${API_BASE}/api/finance/ad-spend/ingest`;
  const checkpointEndpoint = `${API_BASE}/api/finance/ad-spend/checkpoint`;
  return `/**
 * Google Ads Script — ${store}
 * Pushes daily ad spend to the PrimeCore Operation app (Finance · P&L).
 * Schedule: Hourly
 *
 * Built around Google's two hard limits on Ads Scripts:
 *   1. ~20,000 urlfetch calls per account per DAY  → the script first asks the app what
 *      it already has (checkpoint), then pushes only FRESH days + a capped slice of the
 *      still-missing history per run, in 1000-row batches. History completes over a few
 *      runs; steady state is a handful of calls per hour.
 *   2. 30 minutes per RUN → a time budget stops pushing gracefully before the kill.
 * Every push upserts by (store, date[, product]) so re-sends are always harmless.
 */
var CONFIG = {
  ENDPOINT:            "${endpoint}",
  CHECKPOINT_ENDPOINT: "${checkpointEndpoint}",
  STORE_KEY:           "${store}",
  SCRIPT_KEY:          "${key || "ROTATE_KEY_FIRST"}",
  LOOKBACK_DAYS:       ${lookbackDays},
  FRESH_DAYS:          30,    // always re-push this recent window (conversions update ~30d back)
  BATCH_SIZE:          1000,
  MAX_FETCHES_PER_RUN: 400,   // 24 runs/day x 400 stays far under the ~20k/day quota
  TIME_BUDGET_MIN:     24     // stop before Google's 30-min hard kill
};

var START_MS = 0;
var FETCHES = 0;
var QUOTA_HIT = false;
var ACCOUNT = ""; // Ads account id (set in main) — stamped on every push for mislabel detection

function canPush() {
  if (QUOTA_HIT) return false;
  if (FETCHES >= CONFIG.MAX_FETCHES_PER_RUN) return false;
  return (new Date().getTime() - START_MS) < CONFIG.TIME_BUDGET_MIN * 60000;
}

function main() {
  START_MS  = new Date().getTime();
  var tz    = AdsApp.currentAccount().getTimeZone();
  var today = new Date();
  var floor = new Date(today.getTime() - CONFIG.LOOKBACK_DAYS * 86400000);
  var currency = AdsApp.currentAccount().getCurrencyCode();
  // Which Ads account this script runs in — sent on every push so the app can detect a script
  // pasted with the WRONG store key (account A posting as store B overwrites B's real spend).
  ACCOUNT = AdsApp.currentAccount().getCustomerId();
  var fmt = function (d) { return Utilities.formatDate(d, tz, "yyyy-MM-dd"); };

  // ── 0. Checkpoint: which product dates is the app still missing? ───────────────
  var ckpt = null;
  try {
    var cResp = UrlFetchApp.fetch(CONFIG.CHECKPOINT_ENDPOINT, {
      method: "post", contentType: "application/json", muteHttpExceptions: true,
      payload: JSON.stringify({ store: CONFIG.STORE_KEY, key: CONFIG.SCRIPT_KEY, floor: fmt(floor) })
    });
    FETCHES++;
    ckpt = JSON.parse(cResp.getContentText());
    if (!ckpt.ok) { Logger.log("Checkpoint rejected: " + cResp.getContentText()); ckpt = null; }
  } catch (e) {
    Logger.log("Checkpoint unavailable (" + e + ") — pushing fresh window only.");
  }

  // ── 1. Account-level daily spend (cheap: whole lookback is 1-2 calls) ──────────
  var query =
    "SELECT segments.date, metrics.cost_micros " +
    "FROM campaign " +
    "WHERE segments.date BETWEEN '" + fmt(floor) + "' AND '" + fmt(today) + "'";
  var results = AdsApp.search(query);
  var byDate  = {};
  while (results.hasNext()) {
    var row  = results.next();
    var date = getNested(row, "segments", "date");
    var cost = getNested(row, "metrics", "costMicros");
    if (!date) continue;
    if (!byDate[date]) byDate[date] = { date: date, amount: 0, currency: currency, source: "google" };
    byDate[date].amount += (parseInt(cost) || 0) / 1000000;
  }
  var allRows = objValues(byDate);
  allRows.sort(function (a, b) { return a.date < b.date ? 1 : -1; }); // newest first
  Logger.log("Collected " + allRows.length + " spend days (" + fmt(floor) + " -> " + fmt(today) + ")");
  for (var i = 0; i < allRows.length && canPush(); i += CONFIG.BATCH_SIZE) {
    postBatch({ store: CONFIG.STORE_KEY, key: CONFIG.SCRIPT_KEY, rows: allRows.slice(i, i + CONFIG.BATCH_SIZE) });
  }

  // ── 2. Fresh per-product window (always) ───────────────────────────────────────
  var freshStart = new Date(Math.max(today.getTime() - CONFIG.FRESH_DAYS * 86400000, floor.getTime()));
  var pushed = pushProductRange(freshStart, today, tz, currency);

  // ── 3. Gap fill: the checkpoint returns the date ranges the app is actually
  //       MISSING (newest first, capped) — killed runs leave holes anywhere, so this
  //       self-heals regardless of how past runs died. Resumes each run until clean. ──
  var gaps = (ckpt && ckpt.product_missing) || [];
  if (!ckpt) gaps = [{ from: fmt(floor), to: fmt(new Date(freshStart.getTime() - 86400000)) }];
  for (var g = 0; g < gaps.length && canPush(); g++) {
    var gapFrom = new Date(gaps[g].from + "T12:00:00Z");
    var gapTo   = new Date(gaps[g].to   + "T12:00:00Z");
    if (isNaN(gapFrom.getTime()) || isNaN(gapTo.getTime())) continue;
    // chunk big gaps into ~90-day report windows, newest side first
    var winEnd = gapTo;
    while (winEnd.getTime() >= gapFrom.getTime() && canPush()) {
      var winStart = new Date(Math.max(winEnd.getTime() - 89 * 86400000, gapFrom.getTime()));
      pushed += pushProductRange(winStart, winEnd, tz, currency);
      winEnd = new Date(winStart.getTime() - 86400000);
    }
  }
  if (gaps.length > 0 && !canPush()) {
    Logger.log("Run budget reached — remaining gaps resume next run.");
  }
  Logger.log("Pushed " + pushed + " product-day rows in " + FETCHES + " calls this run (" + gaps.length + " gaps reported)." + (QUOTA_HIT ? " (stopped: daily urlfetch quota)" : ""));
}

// Pull shopping_performance_view for [start, end] and push it in batches. Returns rows pushed.
function pushProductRange(start, end, tz, currency) {
  if (end.getTime() < start.getTime() || !canPush()) return 0;
  var pFrom = Utilities.formatDate(start, tz, "yyyy-MM-dd");
  var pTo   = Utilities.formatDate(end,   tz, "yyyy-MM-dd");
  var pQuery =
    "SELECT segments.product_item_id, segments.date, metrics.cost_micros, metrics.clicks, " +
    "metrics.impressions, metrics.conversions, metrics.conversions_value " +
    "FROM shopping_performance_view " +
    "WHERE segments.date BETWEEN '" + pFrom + "' AND '" + pTo + "'";
  var byKey = {};
  var pResults = AdsApp.search(pQuery);
  while (pResults.hasNext()) {
    var pr  = pResults.next();
    var pid = getNested(pr, "segments", "productItemId");
    var pdt = getNested(pr, "segments", "date");
    if (!pid || !pdt) continue;
    var kk = pdt + "|" + pid;
    if (!byKey[kk]) byKey[kk] = { date: pdt, item_id: pid, cost: 0, clicks: 0, impressions: 0, conversions: 0, conv_value: 0, currency: currency };
    byKey[kk].cost        += (parseInt(getNested(pr, "metrics", "costMicros")) || 0) / 1000000;
    byKey[kk].clicks      += parseInt(getNested(pr, "metrics", "clicks")) || 0;
    byKey[kk].impressions += parseInt(getNested(pr, "metrics", "impressions")) || 0;
    byKey[kk].conversions += parseFloat(getNested(pr, "metrics", "conversions")) || 0;
    byKey[kk].conv_value  += parseFloat(getNested(pr, "metrics", "conversionsValue")) || 0;
  }
  var pRows = objValues(byKey);
  var n = 0;
  for (var j = 0; j < pRows.length && canPush(); j += CONFIG.BATCH_SIZE) {
    postBatch({ store: CONFIG.STORE_KEY, key: CONFIG.SCRIPT_KEY, product_rows: pRows.slice(j, j + CONFIG.BATCH_SIZE) });
    n += Math.min(CONFIG.BATCH_SIZE, pRows.length - j);
  }
  return n;
}

function postBatch(payload) {
  try {
    payload.account = ACCOUNT; // who is pushing — lets the app catch wrong-store scripts
    var resp = UrlFetchApp.fetch(CONFIG.ENDPOINT, {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });
    FETCHES++;
    Logger.log("Push: " + resp.getContentText());
  } catch (e) {
    // Daily urlfetch quota exhausted — stop pushing, finish the run cleanly. Whatever
    // is missing gets picked up by the next run's checkpoint.
    QUOTA_HIT = true;
    Logger.log("Push stopped (" + e + ") — resuming on the next run.");
  }
}

function getNested(obj, key1, key2) {
  try {
    if (obj[key1] && obj[key1][key2] !== undefined) return String(obj[key1][key2]);
    var dotKey = key1 + "." + key2;
    if (obj[dotKey] !== undefined) return String(obj[dotKey]);
  } catch (e) {}
  return null;
}

function objValues(o) {
  var out = [];
  for (var k in o) out.push(o[k]);
  return out;
}
`;
}

function AdSpendScriptPanel({ store }: { store: string }) {
  const { data, reload } = useApi(() => api.finAdScript(store), [store]);
  const [revealed, setRevealed] = useState(false);
  const [busy, setBusy] = useState(false);
  // Default = MAX lookback (lifetime backfill). Hourly re-pushes upsert per day, so a big
  // window costs nothing extra and the history can never have holes.
  const [lookback, setLookback] = useState(3650);
  const [copied, setCopied] = useState(false);

  async function rotate() {
    setBusy(true);
    try {
      await api.finAdScriptRotate(store);
      await reload();
      setRevealed(true);
    } finally {
      setBusy(false);
    }
  }

  const key = data?.script_key ?? "";
  const shown = revealed ? key : key ? `${key.slice(0, 8)}••••••••` : "—";
  const script = buildAdsScript(store, key, lookback);

  function copyScript() {
    navigator.clipboard?.writeText(script).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1600);
      },
      () => {},
    );
  }

  return (
    <div className="mt-3 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2.5">
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
          Google Ads · ad-spend script
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setRevealed((r) => !r)}
            className="rounded-md border border-[var(--border)] px-2.5 py-1 text-[11px] font-semibold hover:bg-[var(--surface)]"
          >
            {revealed ? "Hide key" : "Show key"}
          </button>
          <button
            type="button"
            onClick={rotate}
            disabled={busy}
            className="rounded-md border border-[var(--border)] px-2.5 py-1 text-[11px] font-semibold hover:bg-[var(--surface)] disabled:opacity-40"
          >
            {busy ? "Rotating…" : "Rotate"}
          </button>
        </div>
      </div>
      <p className="mb-1.5 text-[11px] leading-relaxed text-[var(--muted)]">
        No third party needed — this script runs <span className="font-medium">inside your Google Ads account</span> and
        pushes daily spend straight into Finance · P&amp;L. Copy → Google Ads → Tools → Scripts → paste →
        schedule hourly. Lookback defaults to the maximum (3650 = lifetime) so history can never
        have holes — each run upserts per day, so the big window costs nothing.
      </p>
      <div className="mb-1.5 flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-1.5 text-[11px] text-[var(--muted)]">
          Lookback days
          <input
            type="number"
            min={1}
            max={3650}
            value={lookback}
            onChange={(e) => setLookback(Math.max(1, Math.min(3650, Number(e.target.value) || 3650)))}
            className="w-16 rounded-md border border-[var(--border)] bg-[var(--surface)] px-1.5 py-0.5 text-[11px] focus:border-[var(--accent)] focus:outline-none"
          />
        </label>
        <span className="text-[11px] text-[var(--muted)]">
          Script key: <span className="font-mono">{shown}</span>
        </span>
        <button
          type="button"
          onClick={copyScript}
          disabled={!key}
          className="ml-auto rounded-md px-2.5 py-1 text-[11px] font-semibold disabled:opacity-40"
          style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          {copied ? "Copied ✓" : "Copy script"}
        </button>
      </div>
      <textarea
        readOnly
        value={script}
        rows={6}
        className="w-full resize-y rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 font-mono text-[10px] leading-relaxed text-[var(--muted)] focus:outline-none"
      />
    </div>
  );
}

function StoreProfilePanel({
  store,
  profile,
  connected,
  onPulled,
}: {
  store: string;
  profile: StoreProfile | null;
  connected: boolean;
  onPulled: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function pull() {
    setBusy(true);
    setErr(null);
    try {
      const res = await api.shopifyProfilePull(store);
      if (!res.ok) setErr(res.error || "Pull failed");
      else if (res.warning) setErr(res.warning);
      onPulled();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Pull failed");
    } finally {
      setBusy(false);
    }
  }

  const facts: [string, string][] = profile
    ? ([
        ["Language", profile.language || "—"],
        ["Currency", profile.currency || "—"],
        ["Timezone", profile.timezone || "—"],
        ["Country", profile.country || "—"],
        ["Markets", (profile.markets && profile.markets.length)
          ? profile.markets.map((m) => `${m.name}${m.currency ? ` (${m.currency})` : ""}${m.primary ? " ★" : ""}`).join(", ")
          : "—"],
        ["Catalog", profile.catalog_count != null ? `${profile.catalog_count} products` : "—"],
      ] as [string, string][])
    : [];

  return (
    <div className="mt-3 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2.5">
      <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
          Store profile · pulled from Shopify
        </div>
        <button
          type="button"
          onClick={pull}
          disabled={busy || !connected}
          title={connected ? "Pull language / currency / timezone / markets / catalog from Shopify" : "Set the app-wide Shopify Client ID/Secret + this store's myshopify domain first"}
          className="rounded-md border border-[var(--border)] px-2.5 py-1 text-[11px] font-semibold hover:bg-[var(--surface)] disabled:opacity-40"
        >
          {busy ? "Pulling…" : profile ? "Re-pull" : "Pull from Shopify"}
        </button>
      </div>
      {!connected ? (
        <p className="text-[11px] text-[var(--muted)]">
          Connect this store (app-wide Client ID/Secret + the myshopify domain above) to pull its language, currency, timezone, markets and catalog.
        </p>
      ) : profile ? (
        <div className="grid gap-x-4 gap-y-1 sm:grid-cols-2">
          {facts.map(([k, v]) => (
            <div key={k} className="flex items-baseline justify-between gap-2 text-[11px]">
              <span className="text-[var(--muted)]">{k}</span>
              <span className="truncate text-right font-medium" title={v}>{v}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-[11px] text-[var(--muted)]">
          Not pulled yet. These facts flow into VisionScan (gallery language), pricing (currency) and scheduling (timezone).
        </p>
      )}
      {err ? <p className="mt-1.5 text-[11px]" style={{ color: "var(--state-killed)" }}>{err}</p> : null}
    </div>
  );
}

// One-click Bright Data setup. The operator pastes ONE Bright Data token above; this button
// calls the account Zone API to create/adopt the SERP + CN zones inside their Bright Data
// account, reads the passwords, and saves the resolved creds back — so they never hand-build a
// zone. Creating a zone is billable, so it only runs on click, never on token paste.
function BrightDataSetup({ onChange }: { onChange: () => void }) {
  const [st, setSt] = useState<BrightDataStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; msg: string; needs?: string[] } | null>(null);

  useEffect(() => {
    let live = true;
    api.brightdataStatus().then((s) => live && setSt(s)).catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  async function run() {
    setBusy(true);
    setResult(null);
    try {
      const r = await api.brightdataProvision();
      const created = (r.created ?? []).length ? `created ${r.created!.join(", ")}` : "zones already existed";
      setResult({
        ok: r.ok,
        msg: r.ok ? `Bright Data set up — ${created}.` : r.error || "Setup failed.",
        needs: r.needs,
      });
      const s = await api.brightdataStatus();
      setSt(s);
      onChange();
    } catch (e) {
      setResult({ ok: false, msg: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  const done = st?.serp_zone && st?.serp_password_set;
  return (
    <div className="border-t border-[var(--border)] px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="text-xs font-semibold">Auto-set-up Bright Data from the one token</div>
          <p className="mt-0.5 text-[11px] text-[var(--muted)]">
            Don&apos;t hand-create zones. With the token above set, this makes the SERP zone (Google
            Lens) + the CN residential zone (1688) inside your Bright Data account and fills the
            fields for you. The only thing it can&apos;t fetch is your Customer ID — paste that once.
          </p>
        </div>
        <button
          type="button"
          onClick={run}
          disabled={busy || !st?.has_token}
          className="shrink-0 rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
          style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          {busy ? "Setting up…" : done ? "Re-run setup" : "Set up Bright Data"}
        </button>
      </div>
      {!st?.has_token ? (
        <p className="mt-1.5 text-[11px]" style={{ color: "var(--state-drafted)" }}>
          Set the Bright Data Token above and save first, then this button lights up.
        </p>
      ) : null}
      {st?.has_token ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          <SetupChip label="SERP zone" ok={!!st.serp_zone} />
          <SetupChip label="SERP password" ok={st.serp_password_set} />
          <SetupChip label="Customer ID" ok={st.customer_id_set} />
          <SetupChip label="CN proxy" ok={st.cn_proxy_set} />
        </div>
      ) : null}
      {result ? (
        <p
          className="mt-2 text-[11px]"
          style={{ color: result.ok ? "var(--state-winner)" : "var(--state-killed)" }}
        >
          {result.msg}
        </p>
      ) : null}
      {result?.needs && result.needs.length > 0 ? (
        <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11px] text-[var(--muted)]">
          {result.needs.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function SetupChip({ label, ok }: { label: string; ok: boolean }) {
  return (
    <span
      className="rounded-md px-2 py-0.5 text-[11px] font-semibold"
      style={{
        color: ok ? "var(--state-winner)" : "var(--state-drafted)",
        background: `color-mix(in srgb, ${ok ? "var(--state-winner)" : "var(--state-drafted)"} 14%, transparent)`,
      }}
    >
      {ok ? "✓ " : "• "}
      {label}
    </span>
  );
}

// Connect / disconnect / inspect the Markifact OAuth-MCP write layer. The OAuth consent
// happens in a popup window the operator drives; the backend callback posts a
// 'markifact-connected' message back here so we can refresh the masked status.
function MarkifactControls({
  connected,
  detail,
  onChange,
}: {
  connected: boolean;
  detail?: MarkifactStatus;
  onChange: () => void;
}) {
  const [busy, setBusy] = useState<"connect" | "disconnect" | "tools" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tools, setTools] = useState<McpTool[] | null>(null);

  async function connect() {
    setBusy("connect");
    setErr(null);
    try {
      const { authorization_url } = await api.markifactConnect();
      const popup = window.open(authorization_url, "markifact-oauth", "width=560,height=720");
      const onMsg = (e: MessageEvent) => {
        if (e.data === "markifact-connected") {
          window.removeEventListener("message", onMsg);
          try {
            popup?.close();
          } catch {
            /* cross-origin close may throw — ignore */
          }
          setBusy(null);
          onChange();
        }
      };
      window.addEventListener("message", onMsg);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  }

  async function disconnect() {
    setBusy("disconnect");
    setErr(null);
    try {
      await api.markifactDisconnect();
      setTools(null);
      onChange();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function loadTools() {
    setBusy("tools");
    setErr(null);
    try {
      const { tools } = await api.markifactTools();
      setTools(tools);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const expiresIn = detail?.expires_in ?? null;

  return (
    <div className="mt-2 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {connected ? (
          <button
            type="button"
            onClick={disconnect}
            disabled={busy !== null}
            className="rounded-lg border px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
            style={{ borderColor: "var(--border)", color: "var(--state-killed)" }}
          >
            {busy === "disconnect" ? "Disconnecting…" : "Disconnect"}
          </button>
        ) : (
          <button
            type="button"
            onClick={connect}
            disabled={busy !== null}
            className="rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
            style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
          >
            {busy === "connect" ? "Waiting for consent…" : "Connect Markifact"}
          </button>
        )}
        {connected ? (
          <button
            type="button"
            onClick={loadTools}
            disabled={busy !== null}
            className="rounded-lg border px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
            style={{ borderColor: "var(--border)" }}
          >
            {busy === "tools" ? "Loading…" : "View tools"}
          </button>
        ) : null}
        {connected && expiresIn != null ? (
          <span className="text-[11px] text-[var(--muted)]">
            token valid ~{Math.max(0, Math.round(expiresIn / 60))}m
          </span>
        ) : null}
      </div>
      {err ? (
        <div className="text-[11px]" style={{ color: "var(--state-killed)" }}>
          {err}
        </div>
      ) : null}
      {detail?.last_error && !err ? (
        <div className="text-[11px]" style={{ color: "var(--state-drafted)" }}>
          last error: {detail.last_error}
        </div>
      ) : null}
      {tools ? (
        <div className="rounded-lg border border-[var(--border)] p-2">
          <div className="mb-1 text-[11px] font-semibold text-[var(--muted)]">
            {tools.length} tool{tools.length === 1 ? "" : "s"} exposed
          </div>
          <div className="flex flex-wrap gap-1">
            {tools.map((t) => (
              <span
                key={t.name}
                title={t.description ?? ""}
                className="rounded-md bg-[var(--surface-2)] px-1.5 py-0.5 font-mono text-[10px]"
              >
                {t.name}
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// Connect / disconnect Google (Ads + Merchant Center) over OAuth 2.0. Same popup-consent
// pattern as Markifact: the backend returns an authorization_url, the callback posts
// 'google-connected' back here. Once connected, "View accounts" lists the Ads customers +
// Merchant accounts this login can reach — that's what the per-store id pickers read from.
// ── MERCHANT CENTER ── the multi-account GMC connection registry (native port of NN
// Operations' Settings · Merchant Center tab). Each account brings its OWN Google Cloud OAuth
// client + refresh token, keyed by its numeric Merchant ID — because a multi-brand operator
// owns several Merchant Center accounts under different Google logins, unreachable from one
// app-wide consent. Distinct from the Google (Ads + Merchant) OAuth in Connections.
const GMC_CART = (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
    <circle cx="9" cy="21" r="1" /><circle cx="20" cy="21" r="1" />
    <path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6" />
  </svg>
);

function MerchantCenterCard() {
  const { data, loading, error, reload } = useApi<{ accounts: GmcAccount[] }>(() => api.gmcAccounts(), []);
  const accounts = data?.accounts ?? [];
  const [show, setShow] = useState(false);
  const [guide, setGuide] = useState(false);
  const [f, setF] = useState({ name: "", merchantId: "", clientId: "", clientSecret: "" });
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [registerId, setRegisterId] = useState<string | null>(null);
  const [regEmail, setRegEmail] = useState("");
  const [verified, setVerified] = useState<Set<string>>(new Set());
  const redirectUri = `${API_BASE}/api/gmc/oauth/callback`;
  const upd = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setF((s) => ({ ...s, [k]: e.target.value }));

  function connect() {
    const name = f.name.trim(), merchantId = f.merchantId.trim();
    const clientId = f.clientId.trim(), clientSecret = f.clientSecret.trim();
    if (!name || !merchantId || !clientId || !clientSecret) {
      setMsg({ ok: false, text: "Name, Merchant ID, Client ID and Client secret are all required." });
      return;
    }
    setMsg(null);
    const url = api.gmcOauthStartUrl({ name, merchantId, clientId, clientSecret });
    const popup = window.open(url, "gmc-oauth", "width=560,height=720");
    const onMsg = (e: MessageEvent) => {
      if (e.data === "gmc-account-connected" || e.data === "gmc-account-failed") {
        window.removeEventListener("message", onMsg);
        try { popup?.close(); } catch { /* cross-origin close may throw */ }
        if (e.data === "gmc-account-connected") {
          setMsg({ ok: true, text: `Connected "${name}" (merchant ${merchantId}).` });
          setF({ name: "", merchantId: "", clientId: "", clientSecret: "" });
          setShow(false);
          reload();
        } else {
          setMsg({ ok: false, text: "Connection failed — see the tab that opened, then try again." });
        }
      }
    };
    window.addEventListener("message", onMsg);
  }

  async function test(a: GmcAccount) {
    setBusy(a.id); setMsg(null);
    try {
      const r = await api.gmcAccountTest(a.id);
      if (r.ok) {
        setVerified((v) => new Set(v).add(a.id));
        setMsg({ ok: true, text: `${a.name} connected — ${r.services ?? 0} shipping service(s).` });
      } else {
        setVerified((v) => { const n = new Set(v); n.delete(a.id); return n; });
        setMsg({ ok: false, text: r.error || "Test failed." });
      }
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function register(a: GmcAccount) {
    const email = regEmail.trim();
    if (!email) { setMsg({ ok: false, text: "Enter the GMC owner email to register." }); return; }
    setBusy(a.id); setMsg(null);
    try {
      await api.gmcAccountRegister(a.id, email);
      setRegisterId(null); setRegEmail("");
      setMsg({ ok: true, text: `Registered "${a.name}". Wait ~5 min, then click Test — it turns green once Google confirms.` });
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(null);
    }
  }

  async function remove(a: GmcAccount) {
    setBusy(a.id); setMsg(null); setConfirmId(null);
    try {
      await api.gmcAccountRemove(a.id);
      reload();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--border)] px-4 py-3">
          <div className="min-w-0">
            <div className="text-sm font-semibold text-[var(--text)]">Merchant Center accounts</div>
            <p className="mt-0.5 text-xs text-[var(--muted)]">
              Connect a Google Merchant Center account per brand. Each authorises with its own Google
              login — push shipping &amp; return policies from the Merchant Center page.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setGuide(true)}
              className="rounded-lg border px-3 py-1.5 text-xs font-semibold"
              style={{ borderColor: "var(--border)" }}
            >
              How to set up
            </button>
            <button
              type="button"
              onClick={() => { setShow((v) => !v); setMsg(null); }}
              className="rounded-lg px-3 py-1.5 text-xs font-semibold"
              style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
            >
              + Connect account
            </button>
          </div>
        </div>

        <div className="p-3">
          {loading ? <div className="px-1 py-6 text-center text-sm text-[var(--muted)]">Loading…</div> : null}
          {error ? <div className="px-1 py-2 text-xs" style={{ color: "var(--state-killed)" }}>{error}</div> : null}

          {msg ? (
            <div
              className="mb-3 rounded-lg border px-3 py-2 text-xs"
              style={msg.ok
                ? { borderColor: "var(--border)", background: "var(--surface-2)", color: "var(--text)" }
                : { borderColor: "color-mix(in srgb, var(--state-killed) 30%, transparent)", background: "color-mix(in srgb, var(--state-killed) 10%, transparent)", color: "var(--state-killed)" }}
            >
              {msg.ok ? "✓ " : "✗ "}{msg.text}
            </div>
          ) : null}

          {!loading && accounts.length === 0 ? (
            <div className="px-1 py-8 text-center text-sm text-[var(--muted)]">
              No Merchant Center accounts yet — connect one.
            </div>
          ) : null}

          <div className="space-y-2">
            {accounts.map((a) => (
              <div key={a.id} className="flex items-center gap-3 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-3">
                <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-[var(--surface-2)] text-[var(--muted)]">
                  {GMC_CART}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-semibold text-[var(--text)]">{a.name}</div>
                  <div className="truncate text-xs text-[var(--muted)]">
                    Merchant ID {a.merchantId} · {a.authType === "oauth" ? "OAuth" : "Service account"}
                  </div>
                </div>
                {confirmId === a.id ? (
                  <div className="flex shrink-0 items-center gap-1">
                    <span className="mr-1 text-xs text-[var(--muted)]">Remove?</span>
                    <button
                      type="button"
                      onClick={() => remove(a)}
                      disabled={busy === a.id}
                      className="rounded-md px-2.5 py-1 text-xs font-semibold disabled:opacity-40"
                      style={{ background: "var(--state-killed)", color: "var(--accent-fg)" }}
                    >
                      Yes
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmId(null)}
                      className="rounded-md border px-2.5 py-1 text-xs font-semibold"
                      style={{ borderColor: "var(--border)" }}
                    >
                      No
                    </button>
                  </div>
                ) : registerId === a.id ? (
                  <div className="flex shrink-0 items-center gap-1">
                    <input
                      type="email"
                      autoFocus
                      value={regEmail}
                      onChange={(e) => setRegEmail(e.target.value)}
                      placeholder="GMC owner email"
                      className="w-44 rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1 text-xs text-[var(--text)]"
                    />
                    <button
                      type="button"
                      onClick={() => register(a)}
                      disabled={busy === a.id}
                      className="rounded-md px-2.5 py-1 text-xs font-semibold disabled:opacity-40"
                      style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
                    >
                      {busy === a.id ? "…" : "Register"}
                    </button>
                    <button
                      type="button"
                      onClick={() => { setRegisterId(null); setRegEmail(""); }}
                      className="rounded-md border px-2.5 py-1 text-xs font-semibold"
                      style={{ borderColor: "var(--border)" }}
                    >
                      Cancel
                    </button>
                  </div>
                ) : (
                  <div className="flex shrink-0 items-center gap-1">
                    {verified.has(a.id) ? (
                      <span
                        className="mr-1 inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold"
                        style={{ background: "color-mix(in srgb, var(--state-winner, #16a34a) 14%, transparent)", color: "var(--state-winner, #16a34a)" }}
                      >
                        ✓ Registered
                      </span>
                    ) : null}
                    <button
                      type="button"
                      onClick={() => { setRegisterId(a.id); setRegEmail(""); setMsg(null); }}
                      disabled={busy === a.id}
                      title="Register the Cloud project as a developer on this Merchant account (one time, required before publishing)"
                      className="rounded-lg border px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
                      style={{ borderColor: "var(--border)" }}
                    >
                      Register
                    </button>
                    <button
                      type="button"
                      onClick={() => test(a)}
                      disabled={busy === a.id}
                      className="rounded-lg border px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
                      style={{ borderColor: "var(--border)" }}
                    >
                      {busy === a.id ? "Testing…" : "Test"}
                    </button>
                    <button
                      type="button"
                      onClick={() => { setConfirmId(a.id); setMsg(null); }}
                      title="Remove"
                      className="rounded-md p-2 text-[var(--muted)] hover:text-[var(--state-killed)]"
                    >
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9} strokeLinecap="round" strokeLinejoin="round">
                        <polyline points="3 6 5 6 21 6" />
                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                      </svg>
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>

          {show ? (
            <div className="mt-3 rounded-xl border border-[var(--border)] bg-[var(--surface-2)] p-4">
              <div className="mb-1 text-sm font-semibold text-[var(--text)]">Connect via Google</div>
              <p className="mb-3 text-xs text-[var(--muted)]">
                Enter the account details, then authorise with Google — you&apos;ll be redirected to
                the consent screen and back. New here?{" "}
                <button type="button" onClick={() => setGuide(true)} className="underline underline-offset-2">
                  How to set up
                </button>.
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-[var(--muted)]">Display name</span>
                  <input className="w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--text)]" placeholder="e.g. My Store" value={f.name} onChange={upd("name")} />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-[var(--muted)]">Merchant ID</span>
                  <input className="w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--text)]" placeholder="123456789" value={f.merchantId} onChange={upd("merchantId")} />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-[var(--muted)]">Google OAuth Client ID</span>
                  <input className="w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--text)]" placeholder="…apps.googleusercontent.com" value={f.clientId} onChange={upd("clientId")} />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-[var(--muted)]">Client secret</span>
                  <input type="password" className="w-full rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm text-[var(--text)]" placeholder="GOCSPX-…" value={f.clientSecret} onChange={upd("clientSecret")} />
                </label>
              </div>
              <div className="mt-3 flex items-center gap-2">
                <button type="button" onClick={connect} className="rounded-lg px-3 py-1.5 text-xs font-semibold" style={{ background: "var(--accent)", color: "var(--accent-fg)" }}>
                  Connect with Google
                </button>
                <button type="button" onClick={() => setShow(false)} className="rounded-lg px-3 py-1.5 text-xs font-semibold text-[var(--muted)]">
                  Cancel
                </button>
              </div>
              <p className="mt-3 text-[11px] text-[var(--muted)]">
                Add this exact <b>Authorised redirect URI</b> on your Google OAuth client:
                <code className="ml-1 rounded bg-[var(--surface)] px-1.5 py-0.5 font-mono text-[10px] text-[var(--text)]">{redirectUri}</code>
              </p>
            </div>
          ) : null}
        </div>
      </Card>

      {guide ? <MerchantCenterGuide redirectUri={redirectUri} onClose={() => setGuide(false)} /> : null}
    </div>
  );
}

// The "How to connect a Google Merchant Center account" walkthrough — ported from NN
// Operations' setup guide. Rendered as an overlay so it can sit beside the connect form.
function MerchantCenterGuide({ redirectUri, onClose }: { redirectUri: string; onClose: () => void }) {
  const Num = ({ n }: { n: number }) => (
    <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full text-xs font-bold" style={{ background: "var(--accent)", color: "var(--accent-fg)" }}>{n}</span>
  );
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-1 flex items-start justify-between gap-4">
          <h3 className="text-lg font-bold text-[var(--text)]">How to connect a Google Merchant Center account</h3>
          <button type="button" onClick={onClose} className="rounded-md p-1 text-[var(--muted)] hover:text-[var(--text)]" aria-label="Close">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
          </button>
        </div>
        <p className="mb-5 text-sm text-[var(--muted)]">
          Do every step logged into the store&apos;s Google (GMC) account. Keep this open beside the
          connect form.
        </p>

        <ol className="space-y-5 text-sm text-[var(--text)]">
          <li className="flex gap-3">
            <Num n={1} />
            <div>
              <div className="font-semibold">Sign in</div>
              <p className="mt-1 text-[var(--muted)]">Log in to the browser/proxy where this store&apos;s GMC email is signed in — do everything from there.</p>
            </div>
          </li>
          <li className="flex gap-3">
            <Num n={2} />
            <div>
              <div className="font-semibold">Create a Google Cloud account <span className="font-normal text-[var(--muted)]">(first time only)</span></div>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-[var(--muted)]">
                <li>Go to <span className="font-mono text-xs text-[var(--text)]">cloud.google.com/free</span> and sign up.</li>
                <li>Add the shop&apos;s company details.</li>
                <li>Add a credit card (Google requires it to verify — the Merchant API is free, you won&apos;t be charged).</li>
              </ul>
            </div>
          </li>
          <li className="flex gap-3">
            <Num n={3} />
            <div>
              <div className="font-semibold">Enable the Merchant API</div>
              <p className="mt-1 text-[var(--muted)]">In the Cloud dashboard: search bar → type <b className="text-[var(--text)]">Merchant API</b> → open → <b className="text-[var(--text)]">Enable</b>.</p>
            </div>
          </li>
          <li className="flex gap-3">
            <Num n={4} />
            <div>
              <div className="font-semibold">Consent screen</div>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-[var(--muted)]">
                <li>Search <b className="text-[var(--text)]">OAuth consent screen</b> → <b className="text-[var(--text)]">Get started</b>.</li>
                <li>App name — any name (e.g. &ldquo;Merchant Ops&rdquo;); support email = the GMC email.</li>
                <li>Audience <b className="text-[var(--text)]">External</b> → Save.</li>
                <li>Audience → <b className="text-[var(--text)]">Test users</b> → Add users → add the GMC email.</li>
              </ul>
            </div>
          </li>
          <li className="flex gap-3">
            <Num n={5} />
            <div>
              <div className="font-semibold">Create the OAuth client</div>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-[var(--muted)]">
                <li>Go to <b className="text-[var(--text)]">Clients</b> → <b className="text-[var(--text)]">Create client</b>.</li>
                <li>Application type <b className="text-[var(--text)]">Web application</b>. Name it (e.g. &ldquo;GMC API&rdquo;).</li>
                <li>Under <b className="text-[var(--text)]">Authorised redirect URIs</b> → <b className="text-[var(--text)]">Add URI</b> → paste exactly:</li>
              </ul>
              <code className="mt-2 block break-all rounded-lg bg-[var(--surface-2)] px-3 py-2 font-mono text-xs text-[var(--text)]">{redirectUri}</code>
              <ul className="mt-2 list-disc space-y-1 pl-5 text-[var(--muted)]">
                <li>Create → copy the <b className="text-[var(--text)]">Client ID</b> and <b className="text-[var(--text)]">Client secret</b>.</li>
              </ul>
              <p className="mt-2 rounded-lg px-3 py-2 text-xs" style={{ background: "color-mix(in srgb, var(--warning, #d97706) 12%, transparent)", color: "var(--warning, #b45309)" }}>
                ⚠ The Client secret is shown only once — copy and save it right away.
              </p>
            </div>
          </li>
          <li className="flex gap-3">
            <Num n={6} />
            <div>
              <div className="font-semibold">Connect here</div>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-[var(--muted)]">
                <li>Open <b className="text-[var(--text)]">+ Connect account</b> above: fill in <b className="text-[var(--text)]">Display name</b>, the <b className="text-[var(--text)]">Merchant ID</b> (the store&apos;s, e.g. 5621336355), <b className="text-[var(--text)]">Client ID</b> and <b className="text-[var(--text)]">Client secret</b> → <b className="text-[var(--text)]">Connect with Google</b>.</li>
                <li>Choose the store&apos;s Google GMC account.</li>
                <li>Google warns <span className="italic">&ldquo;Google hasn&apos;t verified this app&rdquo;</span> → <b className="text-[var(--text)]">Continue</b> / Proceed → <b className="text-[var(--text)]">Allow</b>. (Normal — it doesn&apos;t affect your Merchant Center.)</li>
              </ul>
            </div>
          </li>
          <li className="flex gap-3">
            <Num n={7} />
            <div>
              <div className="font-semibold">Register <span className="font-normal text-[var(--muted)]">(required, one time)</span></div>
              <p className="mt-1 text-[var(--muted)]">Done here in the app — not on Google — on the account&apos;s row above.</p>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-[var(--muted)]">
                <li>On the account you just connected, click <b className="text-[var(--text)]">Register</b> → enter the <b className="text-[var(--text)]">GMC owner email</b> → Register.</li>
                <li>Wait ~5 minutes, then click <b className="text-[var(--text)]">Test</b>. Once Google confirms, it turns green (<span style={{ color: "var(--state-winner, #16a34a)" }}>✓ Registered</span>) — now you can push shipping &amp; returns.</li>
              </ul>
              <p className="mt-2 text-xs text-[var(--muted)]">
                Without Register, publishing fails with <span className="font-mono text-[var(--text)]">&ldquo;GCP project … is not registered with the merchant account&rdquo;</span>.
              </p>
            </div>
          </li>
        </ol>

        <div className="mt-6 flex justify-end">
          <button type="button" onClick={onClose} className="rounded-lg px-4 py-2 text-sm font-semibold" style={{ background: "var(--accent)", color: "var(--accent-fg)" }}>
            Got it
          </button>
        </div>
      </div>
    </div>
  );
}

function GoogleControls({
  detail,
  onChange,
}: {
  detail?: GoogleStatus;
  onChange: () => void;
}) {
  const [busy, setBusy] = useState<"connect" | "disconnect" | "accounts" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [accts, setAccts] = useState<GoogleAccounts | null>(null);
  const connected = !!detail?.connected;
  const clientReady = !!detail?.client_ready;

  async function connect() {
    setBusy("connect");
    setErr(null);
    try {
      const { authorization_url } = await api.googleConnect();
      const popup = window.open(authorization_url, "google-oauth", "width=560,height=720");
      const onMsg = (e: MessageEvent) => {
        if (e.data === "google-connected") {
          window.removeEventListener("message", onMsg);
          try {
            popup?.close();
          } catch {
            /* cross-origin close may throw — ignore */
          }
          setBusy(null);
          onChange();
        }
      };
      window.addEventListener("message", onMsg);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  }

  async function disconnect() {
    setBusy("disconnect");
    setErr(null);
    try {
      await api.googleDisconnect();
      setAccts(null);
      onChange();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function loadAccounts() {
    setBusy("accounts");
    setErr(null);
    try {
      setAccts(await api.googleAccounts());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const expiresIn = detail?.expires_in ?? null;

  return (
    <div className="mt-2 space-y-2">
      {!clientReady && !connected ? (
        <div className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2.5 py-2 text-[11px] text-[var(--muted)]">
          Set the <span className="font-medium">Google Ads OAuth Client ID + Client Secret</span> in
          API keys · Google Ads first — Google has no dynamic client registration, so a Google Cloud
          OAuth client is required before you can connect.
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        {connected ? (
          <button
            type="button"
            onClick={disconnect}
            disabled={busy !== null}
            className="rounded-lg border px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
            style={{ borderColor: "var(--border)", color: "var(--state-killed)" }}
          >
            {busy === "disconnect" ? "Disconnecting…" : "Disconnect"}
          </button>
        ) : (
          <button
            type="button"
            onClick={connect}
            disabled={busy !== null || !clientReady}
            className="rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
            style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
          >
            {busy === "connect" ? "Waiting for consent…" : "Connect Google"}
          </button>
        )}
        {connected ? (
          <button
            type="button"
            onClick={loadAccounts}
            disabled={busy !== null}
            className="rounded-lg border px-3 py-1.5 text-xs font-semibold disabled:opacity-40"
            style={{ borderColor: "var(--border)" }}
          >
            {busy === "accounts" ? "Loading…" : "View accounts"}
          </button>
        ) : null}
        {connected && expiresIn != null ? (
          <span className="text-[11px] text-[var(--muted)]">
            token valid ~{Math.max(0, Math.round(expiresIn / 60))}m
          </span>
        ) : null}
        {connected && !detail?.developer_token_set ? (
          <span className="text-[11px]" style={{ color: "var(--state-drafted)" }}>
            set the Developer Token to list Ads accounts
          </span>
        ) : null}
      </div>
      {err ? (
        <div className="text-[11px]" style={{ color: "var(--state-killed)" }}>
          {err}
        </div>
      ) : null}
      {detail?.last_error && !err ? (
        <div className="text-[11px]" style={{ color: "var(--state-drafted)" }}>
          last error: {detail.last_error}
        </div>
      ) : null}
      {accts ? (
        <div className="grid gap-2 sm:grid-cols-2">
          <div className="rounded-lg border border-[var(--border)] p-2">
            <div className="mb-1 text-[11px] font-semibold text-[var(--muted)]">
              Ads accounts {accts.ads_accounts.length ? `(${accts.ads_accounts.length})` : ""}
            </div>
            {accts.ads_accounts.length ? (
              <div className="flex flex-wrap gap-1">
                {accts.ads_accounts.map((a) => (
                  <span key={a.id} className="rounded-md bg-[var(--surface-2)] px-1.5 py-0.5 font-mono text-[10px]">
                    {a.label}
                  </span>
                ))}
              </div>
            ) : (
              <div className="text-[11px] text-[var(--muted)]">{accts.ads_error ?? "none"}</div>
            )}
          </div>
          <div className="rounded-lg border border-[var(--border)] p-2">
            <div className="mb-1 text-[11px] font-semibold text-[var(--muted)]">
              Merchant accounts {accts.merchant_accounts.length ? `(${accts.merchant_accounts.length})` : ""}
            </div>
            {accts.merchant_accounts.length ? (
              <div className="flex flex-wrap gap-1">
                {accts.merchant_accounts.map((a) => (
                  <span key={a.id} className="rounded-md bg-[var(--surface-2)] px-1.5 py-0.5 font-mono text-[10px]">
                    {a.label}
                  </span>
                ))}
              </div>
            ) : (
              <div className="text-[11px] text-[var(--muted)]">{accts.merchant_error ?? "none"}</div>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function SpecRow({ spec }: { spec: JobSpec }) {
  const auto = spec.mode === "auto";
  const local = spec.mode === "auto-local";
  const color = auto
    ? "var(--state-winner)"
    : local
      ? "var(--state-testing)"
      : "var(--state-drafted)";
  return (
    <div className="flex items-start justify-between gap-4 px-4 py-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold">{spec.title}</span>
          <span className="font-mono text-[11px] text-[var(--muted)]">{spec.surface}</span>
        </div>
        <div className="mt-0.5 text-xs text-[var(--muted)]">{spec.summary}</div>
      </div>
      <span
        className="shrink-0 rounded-md px-2 py-0.5 text-xs font-semibold"
        style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
      >
        {spec.mode}
      </span>
    </div>
  );
}
