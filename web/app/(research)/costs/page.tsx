"use client";

import { useState } from "react";
import { api, type CostOverview, type CostInfraOption } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Stat } from "@/components/ui";
import { PageHeader, Loading, ErrorState } from "@/components/PageState";

// Operation costs — an honest ESTIMATE built from editable unit-cost assumptions ×
// typical per-run recipes (nothing is metered server-side yet; the heavy steps run as
// manual jobs in the operator's own Claude Code). Lives under System, kept clean.

const usd = (n: number) =>
  n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(n < 0.01 ? 4 : 3)}`;

export default function CostsPage() {
  const [store, setStore] = useState<string | undefined>(undefined);
  const [lpm, setLpm] = useState(30);
  const [stores, setStores] = useState(1);
  const settings = useApi(() => api.settings());
  const { data, error, loading, reload } = useApi(
    () => api.costs(store, lpm, stores),
    [store, lpm, stores],
  );

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Costs"
        subtitle="Operation expenses — an estimate from editable unit costs × typical per-run recipes."
      />

      {/* Controls: scope to a store + set a monthly listing cadence for the projection. */}
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          Store
        </span>
        <SegBtn active={!store} onClick={() => setStore(undefined)}>
          All
        </SegBtn>
        {(settings.data?.stores ?? []).map((s) => (
          <SegBtn key={s} active={store === s} onClick={() => setStore(s)}>
            {s}
          </SegBtn>
        ))}
        <div className="ml-auto flex items-center gap-4">
          <div className="flex items-center gap-2">
            <span className="text-xs text-[var(--muted)]">Stores</span>
            <input
              type="number"
              min={1}
              max={100}
              value={stores}
              onChange={(e) => setStores(Math.max(1, Math.min(100, Number(e.target.value) || 1)))}
              className="w-16 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-sm"
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-[var(--muted)]">Listings / mo · store</span>
            <input
              type="number"
              min={0}
              max={50000}
              value={lpm}
              onChange={(e) => setLpm(Math.max(0, Math.min(50000, Number(e.target.value) || 0)))}
              className="w-24 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-sm"
            />
          </div>
        </div>
      </div>

      {loading && !data ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? <CostBody data={data} onSaved={reload} /> : null}
    </div>
  );
}

function CostBody({ data, onSaved }: { data: CostOverview; onSaved: () => void }) {
  return (
    <div className="space-y-6">
      {/* Headline projection */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat
          label="Projected / month"
          value={usd(data.projection.projected_monthly)}
          hint={`${data.projection.total_listings.toLocaleString()} listings · ${data.projection.variable_share}% variable`}
        />
        <Stat
          label="Per listing"
          value={
            data.per_listing.plain && data.per_listing.branded
              ? `${usd(data.per_listing.plain.est_cost)}–${usd(data.per_listing.branded.est_cost)}`
              : usd(data.per_listing.est_cost)
          }
          hint="plain (no images) → branded (AI gallery)"
        />
        <Stat
          label="Fixed infra / month"
          value={usd(data.fixed_monthly.total)}
          hint={`${data.fixed_monthly.selected_worker} + ${data.fixed_monthly.selected_frontend}`}
        />
        <Stat
          label="Data storage / month"
          value={usd(data.projection.storage_monthly)}
          hint={`${data.storage.db_gb} GB DB @ ${data.storage.months_retained}mo`}
        />
      </div>

      <p className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-4 py-3 text-xs text-[var(--muted)]">
        {data.note}
      </p>

      <PerListingCard data={data} />
      <ScaleCard data={data} />
      <AgentModelCard data={data} onSaved={onSaved} />
      <UnitCostsCard data={data} onSaved={onSaved} />
      <FixedInfraCard data={data} />
      <PerSpecCard data={data} />
      <SpendCard data={data} />
    </div>
  );
}

function PerListingCard({ data }: { data: CostOverview }) {
  // Image gen is NOT every listing. A plain supplier-photo import is basically just the
  // (cheap) LLM copy; a branded build adds a full AI gallery (~95% of its cost). Show both
  // so the single per-listing number isn't read as "every listing costs the branded price".
  const types = data.per_listing.types;
  return (
    <Card>
      <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
        Cost per listing · by build method
      </div>
      <div className="border-b border-[var(--border)] bg-[var(--bg)] px-4 py-2 text-xs text-[var(--muted)]">
        {data.per_listing.note}
      </div>
      <div className="grid gap-px bg-[var(--border)] sm:grid-cols-2">
        {types.map((t) => (
          <div key={t.id} className="bg-[var(--surface)] px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm font-semibold">{t.label}</span>
              <span className="font-mono text-sm">{usd(t.est_cost)}</span>
            </div>
            <div className="mt-0.5 text-[11px] text-[var(--muted)]">
              {t.images > 0 ? `${t.images} AI images` : "supplier photos · no image gen"}
            </div>
            <div className="mt-2 space-y-1">
              {t.breakdown.map((b) => (
                <div key={b.driver} className="flex items-center justify-between gap-3 text-[11px]">
                  <span className="text-[var(--muted)]">
                    {b.qty}× {b.label}
                  </span>
                  <span className="font-mono text-[var(--muted)]">{usd(b.cost)}</span>
                </div>
              ))}
            </div>
            <p className="mt-2 text-[11px] leading-snug text-[var(--muted)]">{t.note}</p>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ScaleCard({ data }: { data: CostOverview }) {
  return (
    <Card>
      <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
        Scale curve · hobby → production
      </div>
      <div className="border-b border-[var(--border)] bg-[var(--bg)] px-4 py-2 text-xs text-[var(--muted)]">
        Past hobby scale the per-listing <strong>variable</strong> spend (image-gen + LLM) dominates —
        fixed infra + data storage stay a rounding error. The real lever is the per-listing cost
        (cheaper image model / fewer images / cheaper LLM), not the host.
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--border)] text-left text-[11px] uppercase tracking-wide text-[var(--muted)]">
              <th className="px-4 py-2 font-semibold">Scenario</th>
              <th className="px-4 py-2 text-right font-semibold">Listings/mo</th>
              <th className="px-4 py-2 text-right font-semibold">Variable</th>
              <th className="px-4 py-2 text-right font-semibold">Storage</th>
              <th className="px-4 py-2 text-right font-semibold">Fixed</th>
              <th className="px-4 py-2 text-right font-semibold">Total/mo</th>
              <th className="px-4 py-2 text-right font-semibold">% var</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[var(--border)]">
            {data.scenarios.map((s) => (
              <tr key={s.label}>
                <td className="px-4 py-2 font-medium">{s.label}</td>
                <td className="px-4 py-2 text-right font-mono text-[var(--muted)]">
                  {s.total_listings.toLocaleString()}
                </td>
                <td className="px-4 py-2 text-right font-mono">{usd(s.variable)}</td>
                <td className="px-4 py-2 text-right font-mono text-[var(--muted)]">{usd(s.storage)}</td>
                <td className="px-4 py-2 text-right font-mono text-[var(--muted)]">{usd(s.fixed)}</td>
                <td className="px-4 py-2 text-right font-mono font-semibold">{usd(s.monthly)}</td>
                <td className="px-4 py-2 text-right font-mono text-[var(--muted)]">{s.variable_share}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="border-t border-[var(--border)] px-4 py-2 text-xs text-[var(--muted)]">
        {data.storage.note}
      </div>
    </Card>
  );
}

function AgentModelCard({ data, onSaved }: { data: CostOverview; onSaved: () => void }) {
  const [saving, setSaving] = useState<string | null>(null);
  const pick = async (id: string) => {
    if (id === data.agent.selected) return;
    setSaving(id);
    try {
      await api.saveCostAssumptions({ agent_model: id });
      onSaved();
    } finally {
      setSaving(null);
    }
  };
  return (
    <Card>
      <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
        Agent model · drives the LLM cost line
      </div>
      <p className="px-4 pt-3 text-xs text-[var(--muted)]">{data.agent.note}</p>
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

function UnitCostsCard({ data, onSaved }: { data: CostOverview; onSaved: () => void }) {
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const dirty = Object.keys(edits).length > 0;

  const save = async () => {
    setSaving(true);
    setSaveErr(null);
    try {
      const payload: Record<string, number> = {};
      for (const [k, v] of Object.entries(edits)) {
        const n = Number(v);
        if (!Number.isNaN(n)) payload[k] = n;
      }
      await api.saveCostAssumptions({ unit_costs: payload });
      setEdits({});
      onSaved();
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          Unit costs · editable assumptions
        </span>
        {dirty ? (
          <div className="flex items-center gap-2">
            {saveErr ? <span className="text-xs text-[var(--state-killed)]">{saveErr}</span> : null}
            <button
              onClick={() => setEdits({})}
              className="rounded-md px-2 py-1 text-xs text-[var(--muted)] hover:text-[var(--text)]"
            >
              Reset
            </button>
            <button
              onClick={save}
              disabled={saving}
              className="rounded-md bg-[var(--accent)] px-3 py-1 text-xs font-semibold text-[var(--accent-fg)] disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        ) : null}
      </div>
      <div className="divide-y divide-[var(--border)]">
        {Object.entries(data.unit_costs).map(([key, u]) => (
          <div key={key} className="flex items-start justify-between gap-4 px-4 py-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold">{u.label}</span>
                {u.edited ? (
                  <span className="rounded bg-[var(--state-drafted)]/15 px-1.5 py-0.5 text-[10px] font-semibold text-[var(--state-drafted)]">
                    edited
                  </span>
                ) : null}
              </div>
              <div className="mt-0.5 text-xs text-[var(--muted)]">{u.source}</div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <span className="text-xs text-[var(--muted)]">$</span>
              <input
                type="number"
                step="0.0001"
                value={edits[key] ?? u.value}
                onChange={(e) => setEdits((p) => ({ ...p, [key]: e.target.value }))}
                className="w-24 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-right font-mono text-xs"
              />
              <span className="w-10 text-left text-[11px] text-[var(--muted)]">/{u.unit}</span>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function InfraRow({ o }: { o: CostInfraOption }) {
  return (
    <div className="flex items-start justify-between gap-4 px-4 py-3">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold">{o.label}</span>
          {o.resource ? (
            <span className="font-mono text-[11px] text-[var(--muted)]">{o.resource}</span>
          ) : null}
          {o.selected ? (
            <span className="rounded bg-[var(--accent)]/15 px-1.5 py-0.5 text-[10px] font-semibold text-[var(--accent)]">
              recommended
            </span>
          ) : null}
        </div>
        <div className="mt-0.5 text-xs text-[var(--muted)]">{o.note}</div>
      </div>
      <span className="shrink-0 font-mono text-sm">{o.value === 0 ? "free" : `${usd(o.value)}/mo`}</span>
    </div>
  );
}

function FixedInfraCard({ data }: { data: CostOverview }) {
  const fm = data.fixed_monthly;
  return (
    <Card>
      <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
        Fixed monthly infra · run the pipeline 24/7
      </div>
      <div className="border-b border-[var(--border)] bg-[var(--bg)] px-4 py-2 text-xs text-[var(--muted)]">
        {fm.note}
      </div>

      <div className="px-4 pt-3 text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
        Worker + API host · the always-on process (load-bearing)
      </div>
      <div className="divide-y divide-[var(--border)]">
        {fm.worker_options.map((o) => (
          <InfraRow key={o.id} o={o} />
        ))}
      </div>

      <div className="border-t border-[var(--border)] px-4 pt-3 text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
        Frontend host · serves the UI only (cannot run the worker)
      </div>
      <div className="divide-y divide-[var(--border)]">
        {fm.frontend_options.map((o) => (
          <InfraRow key={o.id} o={o} />
        ))}
      </div>

      <div className="border-t border-[var(--border)] px-4 pt-3 text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
        State + services
      </div>
      <div className="divide-y divide-[var(--border)]">
        {fm.services.map((s) => (
          <div key={s.id} className="flex items-start justify-between gap-4 px-4 py-3">
            <div className="min-w-0">
              <span className="text-sm font-medium text-[var(--muted)]">{s.label}</span>
              <div className="mt-0.5 text-xs text-[var(--muted)]">{s.note}</div>
            </div>
            <span className="shrink-0 font-mono text-sm text-[var(--muted)]">
              {s.value === 0 ? "free" : `${usd(s.value)}/mo`}
            </span>
          </div>
        ))}
      </div>
    </Card>
  );
}

function PerSpecRow({ s }: { s: CostOverview["per_spec"][number] }) {
  const color = s.mode === "auto" ? "var(--state-winner)" : "var(--state-drafted)";
  return (
    <div className="px-4 py-3">
      <div className="flex items-center justify-between gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold">{s.title}</span>
          <span
            className="rounded-md px-1.5 py-0.5 text-[10px] font-semibold"
            style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
          >
            {s.mode}
          </span>
        </div>
        <span className="shrink-0 font-mono text-sm">{usd(s.est_cost)}</span>
      </div>
      {s.breakdown.length ? (
        <div className="mt-1 text-[11px] text-[var(--muted)]">
          {s.breakdown.map((b) => `${b.qty}× ${b.label} (${usd(b.cost)})`).join("  ·  ")}
        </div>
      ) : (
        <div className="mt-1 text-[11px] text-[var(--muted)]">
          stdlib / on-disk — no external cost
        </div>
      )}
    </div>
  );
}

function PerSpecCard({ data }: { data: CostOverview }) {
  // Grouped by the operator's mental categories: listing per build method · research ·
  // trend research · vision scan (catalog dedup + 1688 match) · maintenance. (Operating /
  // fixed infra is its own flat layer in FixedInfraCard, not a per-run category.)
  return (
    <Card>
      <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
        Per-run cost by category
      </div>
      <div className="divide-y divide-[var(--border)]">
        {data.cost_groups.map((g) => (
          <div key={g.category}>
            <div className="flex items-center justify-between gap-3 bg-[var(--bg)] px-4 py-2">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
                {g.label}
              </span>
              <span className="font-mono text-[11px] text-[var(--muted)]">
                {g.subtotal > 0 ? `${usd(g.subtotal)} / run total` : "free"}
              </span>
            </div>
            <div className="divide-y divide-[var(--border)]">
              {g.specs.map((s) => (
                <PerSpecRow key={s.spec} s={s} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function SpendCard({ data }: { data: CostOverview }) {
  if (!data.spend_to_date.by_spec.length) {
    return (
      <Card>
        <div className="border-b border-[var(--border)] px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          Estimated spend to date
        </div>
        <div className="px-4 py-6 text-center text-sm text-[var(--muted)]">
          No jobs run yet{data.store ? ` for ${data.store}` : ""}.
        </div>
      </Card>
    );
  }
  return (
    <Card>
      <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-2.5">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          Estimated spend to date{data.store ? ` · ${data.store}` : ""}
        </span>
        <span className="font-mono text-sm">{usd(data.spend_to_date.total)}</span>
      </div>
      <div className="divide-y divide-[var(--border)]">
        {data.spend_to_date.by_spec.map((r) => (
          <div key={r.spec} className="flex items-center justify-between gap-4 px-4 py-2.5">
            <span className="font-mono text-xs">{r.spec}</span>
            <div className="flex items-center gap-4 text-xs">
              <span className="text-[var(--muted)]">
                {r.runs} × {usd(r.unit_cost)}
              </span>
              <span className="w-16 text-right font-mono">{usd(r.cost)}</span>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

function SegBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
        active
          ? "bg-[var(--accent)] text-[var(--accent-fg)]"
          : "border border-[var(--border)] text-[var(--muted)] hover:text-[var(--text)]"
      }`}
    >
      {children}
    </button>
  );
}
