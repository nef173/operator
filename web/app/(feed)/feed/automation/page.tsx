"use client";

import { useState } from "react";
import { api, type AutomationRule, type AutomationCondition, type AutomationEval } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { useStore } from "@/components/StoreProvider";
import { Card, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";

// Automation — rules over the RECONCILED Product Performance data. "When <conditions> on a
// window/scope → <action>". Analysis-first: the engine reports which products WOULD trigger; the
// operator reviews + applies. Safe because the numbers underneath tie to the P&L, split per market,
// with PM's per-market COGS.

const METRICS: { key: string; label: string; unit?: "money" | "pct" | "x" }[] = [
  { key: "roas", label: "ROAS", unit: "x" },
  { key: "ad_spend", label: "Ad spend", unit: "money" },
  { key: "sales", label: "Sales (CV)", unit: "money" },
  { key: "profit", label: "Profit", unit: "money" },
  { key: "margin", label: "Margin %", unit: "pct" },
  { key: "qty", label: "Units" },
  { key: "refunds", label: "Refunds", unit: "money" },
  { key: "orders", label: "Conversions" },
];
const OPS: { key: string; label: string }[] = [
  { key: "lt", label: "<" }, { key: "lte", label: "≤" },
  { key: "gt", label: ">" }, { key: "gte", label: "≥" }, { key: "eq", label: "=" },
];
const WINDOWS: { key: string; label: string }[] = [
  { key: "7", label: "Last 7 days" }, { key: "14", label: "Last 14 days" },
  { key: "30", label: "Last 30 days" }, { key: "all", label: "All-time" },
];
const SCOPES: { key: string; label: string }[] = [
  { key: "total", label: "product total" },
  { key: "any_market", label: "any market" },
  { key: "all_markets", label: "all markets" },
];
const ACTIONS: { key: string; label: string }[] = [
  { key: "draft", label: "Set to Draft" },
  { key: "lower_price", label: "Lower price" },
  { key: "optimize_title", label: "Optimize title" },
  { key: "optimize_product", label: "Optimize product (PDP)" },
  { key: "exclude", label: "Exclude" },
  { key: "flag", label: "Flag / note" },
];
const lbl = (arr: { key: string; label: string }[], k: string) => arr.find((x) => x.key === k)?.label || k;
const metricUnit = (k: string) => METRICS.find((m) => m.key === k)?.unit;

function fmtVal(metric: string, v: number | null, cur: string | null): string {
  if (v == null) return "—";
  const u = metricUnit(metric);
  if (u === "money") return `${Math.round(v).toLocaleString()} ${cur === "EUR" || !cur ? "€" : cur}`;
  if (u === "pct") return `${v}%`;
  if (u === "x") return `${v}x`;
  return v.toLocaleString();
}

const BLANK_RULE: AutomationRule = {
  name: "", enabled: true, window: "30", scope: "total",
  conditions: [{ metric: "roas", op: "lt", value: 1.5 }], action: "draft",
};

// One-click starting points for the rules described: draft / lower price / optimize product /
// optimize title, keyed off ad spend + ROAS + margin, checking markets or the total.
const PRESETS: { label: string; rule: AutomationRule }[] = [
  { label: "Draft losing markets", rule: { name: "Draft losing markets", enabled: true, window: "30", scope: "any_market", action: "draft", conditions: [{ metric: "ad_spend", op: "gt", value: 50 }, { metric: "roas", op: "lt", value: 1 }] } },
  { label: "High spend, low ROAS → lower price", rule: { name: "High spend, low ROAS → lower price", enabled: true, window: "30", scope: "total", action: "lower_price", conditions: [{ metric: "ad_spend", op: "gt", value: 100 }, { metric: "roas", op: "lt", value: 2 }] } },
  { label: "Negative margin → optimize product", rule: { name: "Negative margin → optimize product", enabled: true, window: "30", scope: "any_market", action: "optimize_product", conditions: [{ metric: "margin", op: "lt", value: 0 }] } },
  { label: "ROAS ≈ 1.7 → optimize title", rule: { name: "ROAS around 1.7 → optimize title", enabled: true, window: "30", scope: "total", action: "optimize_title", conditions: [{ metric: "ad_spend", op: "gt", value: 50 }, { metric: "roas", op: "lt", value: 1.7 }] } },
];

export default function AutomationPage() {
  const { store, ready } = useStore();
  const rulesApi = useApi(() => api.automationRules(store), [store]);
  const [draft, setDraft] = useState<AutomationRule | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const evalApi = useApi<AutomationEval | null>(
    () => (store ? api.automationEvaluate(store) : Promise.resolve(null as unknown as AutomationEval)),
    [store],
  );
  const logApi = useApi<{ ok: boolean; entries: import("@/lib/api").AutomationLogEntry[] } | null>(
    () => (store ? api.automationLog(store) : Promise.resolve(null as unknown as { ok: boolean; entries: [] })),
    [store],
  );
  const [applying, setApplying] = useState<string | null>(null);

  async function applyMatch(m: import("@/lib/api").AutomationMatch) {
    if (!m.product_id) return;
    setApplying(`${m.rule_id}:${m.product_id}:${m.market}`);
    try {
      await api.automationApply(store, {
        product_id: m.product_id, action: m.action, rule_name: m.rule_name,
        product_title: m.title, market: m.market, vals: m.values,
      });
      evalApi.reload();
      logApi.reload();
    } finally {
      setApplying(null);
    }
  }

  if (!ready) return <Loading />;
  if (!store) return <Empty>No store selected. Pick a store in the sidebar.</Empty>;

  const rules = rulesApi.data?.rules ?? [];
  const enabled = rulesApi.data?.automation_enabled ?? true;

  async function setEnabled(v: boolean) {
    await api.automationSetEnabled(store, v);
    rulesApi.reload();
    evalApi.reload();
  }

  async function saveDraft() {
    if (!draft) return;
    setBusy(true); setErr(null);
    try {
      const res = await api.automationSaveRule(store, draft);
      if (!res.ok) throw new Error(res.error || "Save failed");
      setDraft(null);
      rulesApi.reload();
      evalApi.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function toggleRule(r: AutomationRule) {
    await api.automationSaveRule(store, { ...r, enabled: !r.enabled });
    rulesApi.reload(); evalApi.reload();
  }
  async function removeRule(id: number) {
    await api.automationDeleteRule(store, id);
    rulesApi.reload(); evalApi.reload();
  }

  return (
    <div className="mx-auto max-w-[1200px]">
      <PageHeader
        title="Automation"
        subtitle="Rules over the reconciled Product Performance data. The engine reports which products WOULD trigger each action — analysis-first; you review and apply."
        action={
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setEnabled(!enabled)}
              title="Global on/off for all automation on this store"
              className={`inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 text-sm font-semibold ${enabled ? "border-[var(--accent)] text-[var(--accent)]" : "border-[var(--border)] text-[var(--muted)]"}`}
            >
              <span className={`h-4 w-7 rounded-full p-0.5 transition-colors ${enabled ? "bg-[var(--accent)]" : "bg-[var(--surface-2)]"}`}>
                <span className={`block h-3 w-3 rounded-full bg-white transition-transform ${enabled ? "translate-x-3" : ""}`} />
              </span>
              Automation {enabled ? "ON" : "OFF"}
            </button>
            <button
              onClick={() => setDraft({ ...BLANK_RULE })}
              className="rounded-lg bg-[var(--accent)] px-3.5 py-2 text-sm font-semibold text-[var(--accent-fg)]"
            >
              + New rule
            </button>
          </div>
        }
      />

      {!enabled ? (
        <div className="mb-4 rounded-lg border px-4 py-2.5 text-sm" style={{ borderColor: "var(--warning, #d97706)", background: "color-mix(in srgb, var(--warning, #d97706) 10%, transparent)" }}>
          Automation is <b>OFF</b> — no rules evaluate and nothing triggers. Turn it on (top right) to resume.
        </div>
      ) : null}

      {err ? <div className="mb-3"><ErrorState message={err} /></div> : null}

      {/* Quick templates — pre-fill the builder with the common rules. */}
      {!draft ? (
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">Quick templates:</span>
          {PRESETS.map((p) => (
            <button
              key={p.label}
              onClick={() => setDraft({ ...p.rule, conditions: p.rule.conditions.map((c) => ({ ...c })) })}
              className="rounded-md border border-[var(--border)] px-2.5 py-1 text-xs font-medium hover:border-[var(--accent)] hover:text-[var(--accent)]"
            >
              + {p.label}
            </button>
          ))}
        </div>
      ) : null}

      {/* Rule builder */}
      {draft ? (
        <Card className="mb-5 p-4">
          <RuleBuilder rule={draft} onChange={setDraft} />
          <div className="mt-4 flex items-center justify-end gap-2">
            <button onClick={() => { setDraft(null); setErr(null); }} className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm text-[var(--muted)] hover:text-[var(--text)]">Cancel</button>
            <button onClick={saveDraft} disabled={busy} className="rounded-lg bg-[var(--accent)] px-4 py-1.5 text-sm font-semibold text-[var(--accent-fg)] disabled:opacity-60">{busy ? "Saving…" : "Save rule"}</button>
          </div>
        </Card>
      ) : null}

      {/* Rules list */}
      <Card className="mb-5 p-0">
        <div className="border-b border-[var(--border)] px-4 py-3 text-sm font-semibold">Rules</div>
        {rulesApi.loading ? (
          <Loading />
        ) : rules.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-[var(--muted)]">
            No rules yet. A rule is “when <b>conditions</b> on a window → an action”, e.g. <i>ROAS &lt; 1.5 and ad spend &gt; €100 on last 30 days (any market) → Set to Draft</i>.
          </div>
        ) : (
          <div className="divide-y divide-[var(--border)]">
            {rules.map((r) => (
              <div key={r.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
                <button
                  onClick={() => toggleRule(r)}
                  title={r.enabled ? "Enabled — click to pause" : "Paused — click to enable"}
                  className={`h-5 w-9 shrink-0 rounded-full p-0.5 transition-colors ${r.enabled ? "bg-[var(--accent)]" : "bg-[var(--surface-2)]"}`}
                >
                  <span className={`block h-4 w-4 rounded-full bg-white transition-transform ${r.enabled ? "translate-x-4" : ""}`} />
                </button>
                <div className="min-w-0 flex-1">
                  <div className="font-semibold">{r.name}</div>
                  <div className="text-xs text-[var(--muted)]">
                    when {r.conditions.map((c, i) => (
                      <span key={i}>{i > 0 ? " and " : ""}<b>{lbl(METRICS, c.metric)} {lbl(OPS, c.op)} {c.value}</b></span>
                    ))} on {lbl(WINDOWS, r.window)} · {lbl(SCOPES, r.scope)} → <span className="text-[var(--accent)]">{lbl(ACTIONS, r.action)}</span>
                  </div>
                </div>
                <button onClick={() => setDraft(r)} className="rounded-md border border-[var(--border)] px-2.5 py-1 text-xs hover:bg-[var(--surface-2)]">Edit</button>
                <button onClick={() => r.id && removeRule(r.id)} className="rounded-md px-2 py-1 text-xs text-[var(--muted)] hover:text-[var(--state-killed)]">Delete</button>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Matches preview */}
      <MatchesCard evalApi={evalApi} onApply={applyMatch} applying={applying} />

      {/* Activity log — what the automation actually did. */}
      <ActivityLog logApi={logApi} />
    </div>
  );
}

function ActivityLog({ logApi }: { logApi: ReturnType<typeof useApi<{ ok: boolean; entries: import("@/lib/api").AutomationLogEntry[] } | null>> }) {
  const entries = logApi.data?.entries ?? [];
  const resultTone = (r: string) => (r === "applied" ? "ok" : r === "failed" ? "danger" : "warn") as "ok" | "warn" | "danger";
  return (
    <Card className="mt-5 p-0">
      <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
        <div>
          <div className="text-sm font-semibold">Activity log</div>
          <div className="text-xs text-[var(--muted)]">What the automation did — product · market · action · result</div>
        </div>
        <button onClick={() => logApi.reload()} className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-semibold hover:bg-[var(--surface-2)]">Refresh</button>
      </div>
      {logApi.loading ? (
        <Loading />
      ) : entries.length === 0 ? (
        <div className="px-4 py-6 text-center text-sm text-[var(--muted)]">Nothing yet. Apply a match above and it shows here.</div>
      ) : (
        <div className="divide-y divide-[var(--border)]">
          {entries.map((e, i) => (
            <div key={i} className="flex flex-wrap items-center gap-2 px-4 py-2.5 text-sm">
              <span className="w-32 shrink-0 text-xs text-[var(--muted)]">{e.at?.replace("T", " ").slice(0, 16)}</span>
              <Pill tone={resultTone(e.result)}>{e.result}</Pill>
              <span className="font-medium">{e.action_label}</span>
              <span className="min-w-0 flex-1 truncate">{e.product_title || e.product_id}{e.market && e.market !== "all" ? ` · ${e.market}` : ""}</span>
              <span className="text-xs text-[var(--muted)]">{e.rule_name}</span>
              {e.vals?.roas != null ? <span className="text-xs text-[var(--muted)]">ROAS {e.vals.roas}x</span> : null}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function RuleBuilder({ rule, onChange }: { rule: AutomationRule; onChange: (r: AutomationRule) => void }) {
  const set = (patch: Partial<AutomationRule>) => onChange({ ...rule, ...patch });
  const setCond = (i: number, patch: Partial<AutomationCondition>) =>
    set({ conditions: rule.conditions.map((c, j) => (j === i ? { ...c, ...patch } : c)) });
  const selCls = "rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1.5 text-sm";
  return (
    <div className="space-y-3">
      <input
        value={rule.name}
        onChange={(e) => set({ name: e.target.value })}
        placeholder="Rule name (e.g. Draft losing products)"
        className="w-full rounded-md border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
      />
      <div className="space-y-2">
        <div className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">When (all conditions)</div>
        {rule.conditions.map((c, i) => (
          <div key={i} className="flex items-center gap-2">
            <select value={c.metric} onChange={(e) => setCond(i, { metric: e.target.value })} className={selCls}>
              {METRICS.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
            </select>
            <select value={c.op} onChange={(e) => setCond(i, { op: e.target.value })} className={selCls}>
              {OPS.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
            </select>
            <input type="number" value={c.value} onChange={(e) => setCond(i, { value: Number(e.target.value) })} className={`${selCls} w-28 tabular-nums`} />
            {rule.conditions.length > 1 ? (
              <button onClick={() => set({ conditions: rule.conditions.filter((_, j) => j !== i) })} className="px-1.5 text-[var(--muted)] hover:text-[var(--state-killed)]">✕</button>
            ) : null}
          </div>
        ))}
        <button onClick={() => set({ conditions: [...rule.conditions, { metric: "ad_spend", op: "gt", value: 100 }] })} className="text-xs font-semibold text-[var(--accent)]">+ Add condition</button>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">On</span>
        <select value={rule.window} onChange={(e) => set({ window: e.target.value })} className={selCls}>
          {WINDOWS.map((w) => <option key={w.key} value={w.key}>{w.label}</option>)}
        </select>
        <span className="text-[var(--muted)]">·</span>
        <select value={rule.scope} onChange={(e) => set({ scope: e.target.value })} className={selCls}>
          {SCOPES.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
        </select>
        <span className="text-[var(--muted)]">→</span>
        <select value={rule.action} onChange={(e) => set({ action: e.target.value })} className={`${selCls} font-semibold text-[var(--accent)]`}>
          {ACTIONS.map((a) => <option key={a.key} value={a.key}>{a.label}</option>)}
        </select>
      </div>
    </div>
  );
}

function MatchesCard({ evalApi, onApply, applying }: {
  evalApi: ReturnType<typeof useApi<AutomationEval | null>>;
  onApply: (m: import("@/lib/api").AutomationMatch) => void;
  applying: string | null;
}) {
  const d = evalApi.data;
  const cur = d?.currency ?? null;
  return (
    <Card className="p-0">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] px-4 py-3">
        <div>
          <div className="text-sm font-semibold">What would trigger now</div>
          <div className="text-xs text-[var(--muted)]">
            {d ? `${d.match_count} match${d.match_count === 1 ? "" : "es"} across ${d.rule_count} enabled rule${d.rule_count === 1 ? "" : "s"} · ${d.product_count} products evaluated` : "—"}
            {" · review, then Apply (Draft/Exclude execute; others flag for you)"}
          </div>
        </div>
        <button onClick={() => evalApi.reload()} className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-semibold hover:bg-[var(--surface-2)]">Re-evaluate</button>
      </div>
      {evalApi.loading ? (
        <Loading />
      ) : evalApi.error ? (
        <ErrorState message={evalApi.error} />
      ) : !d || d.matches.length === 0 ? (
        <div className="px-4 py-6 text-center text-sm text-[var(--muted)]">
          {d?.paused ? "Automation is OFF — evaluation is paused." : "Nothing triggers under the current rules. 🎉"}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-[var(--border)] text-left text-[11px] font-bold uppercase tracking-wide text-[var(--muted)]">
                <th className="px-4 py-2">Product</th>
                <th className="px-3 py-2">Rule</th>
                <th className="px-3 py-2">Action</th>
                <th className="px-3 py-2">Market</th>
                <th className="px-3 py-2 text-right">ROAS</th>
                <th className="px-3 py-2 text-right">Ad spend</th>
                <th className="px-3 py-2 text-right">Profit</th>
                <th className="px-3 py-2 text-right">Margin</th>
                <th className="px-3 py-2 text-right">Apply</th>
              </tr>
            </thead>
            <tbody>
              {d.matches.map((m, i) => {
                const key = `${m.rule_id}:${m.product_id}:${m.market}`;
                return (
                <tr key={i} className="border-b border-[var(--border)] last:border-0 hover:bg-[var(--surface-2)]">
                  <td className="px-4 py-2"><span className="line-clamp-1 max-w-[260px] font-medium">{m.title}</span></td>
                  <td className="px-3 py-2 text-xs text-[var(--muted)]">{m.rule_name}</td>
                  <td className="px-3 py-2"><Pill tone="warn">{m.action_label}</Pill></td>
                  <td className="px-3 py-2 text-xs">{m.market ? (m.market === "all" ? "all markets" : m.market) : "total"}</td>
                  <td className="px-3 py-2 text-right tabular-nums" style={{ color: m.values.roas != null && m.values.roas < 1 ? "var(--state-killed)" : undefined }}>{fmtVal("roas", m.values.roas, cur)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{fmtVal("ad_spend", m.values.ad_spend, cur)}</td>
                  <td className="px-3 py-2 text-right tabular-nums" style={{ color: m.values.profit != null && m.values.profit < 0 ? "var(--state-killed)" : "var(--accent)" }}>{fmtVal("profit", m.values.profit, cur)}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{fmtVal("margin", m.values.margin, cur)}</td>
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => onApply(m)}
                      disabled={applying === key || evalApi.loading}
                      title={evalApi.loading ? "Re-evaluating…" : undefined}
                      className="rounded-md bg-[var(--accent)] px-2.5 py-1 text-xs font-semibold text-[var(--accent-fg)] disabled:opacity-50"
                    >
                      {applying === key ? "…" : "Apply"}
                    </button>
                  </td>
                </tr>
              ); })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
