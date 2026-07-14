"use client";

import { useEffect, useState } from "react";
import { api, type PricingRules, type PriceSuggest, type PricingEditable } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card } from "@/components/ui";

const STATUS_STYLE: Record<string, { label: string; color: string }> = {
  locked: { label: "locked", color: "var(--state-winner)" },
  draft: { label: "draft · to validate", color: "var(--state-testing)" },
  setting: { label: "setting · toggleable", color: "var(--accent)" },
};

/**
 * The listing price rules — undercut the cheapest competing Google listing, but never below
 * the marketplace-COGS markup floor (default 4×), all on a .99 charm ending. Drop it in
 * anywhere a price decision is made. Pass `rules` to render from already-fetched data
 * (e.g. the find-products payload), or omit to fetch standalone. `calculator` adds a live
 * COGS + competitor → suggested-price tool.
 */
export function PriceRulesCard({
  rules,
  calculator = false,
  editable = false,
  className = "",
}: {
  rules?: PricingRules;
  calculator?: boolean;
  editable?: boolean;
  className?: string;
}) {
  const fetched = useApi(() => api.pricingRules(), []);
  const data = rules ?? fetched.data;
  if (!data) return null;

  return (
    <Card className={`p-4 ${className}`}>
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold">Listing price rules</h3>
        <span className="text-[11px] text-[var(--muted)]">{data.charm} charm</span>
      </div>
      {editable ? <PriceRulesEditor onSaved={() => fetched.reload()} /> : null}
      <ul className="mt-3 flex flex-col gap-2.5">
        {data.rules
          .slice()
          .sort((a, b) => a.priority - b.priority)
          .map((r) => {
            const st = STATUS_STYLE[r.status] ?? STATUS_STYLE.locked;
            return (
              <li key={r.id} className="flex items-start gap-2">
                <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-[var(--surface-2)] text-[11px] font-bold tabular-nums">
                  {r.priority}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium">{r.name}</span>
                    <span
                      className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
                      style={{ color: st.color, background: `color-mix(in srgb, ${st.color} 14%, transparent)` }}
                    >
                      {st.label}
                    </span>
                  </div>
                  <div className="text-xs text-[var(--muted)]">{r.rule}</div>
                  {r.note ? <div className="mt-0.5 text-[11px] text-[var(--muted)]">{r.note}</div> : null}
                </div>
              </li>
            );
          })}
      </ul>
      {calculator ? <PriceCalculator markup={data.marketplace_markup} /> : null}
    </Card>
  );
}

// The editable knobs — this is what makes Settings the single source of truth. Changing a
// value here saves to the backend (one persisted override), and every price suggestion +
// every listing build reads it. Used on the Settings → Listing tab (`editable` prop).
function PriceRulesEditor({ onSaved }: { onSaved?: () => void }) {
  const loaded = useApi(() => api.pricingSettings(), []);
  const [draft, setDraft] = useState<PricingEditable | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (loaded.data) setDraft(loaded.data.settings);
  }, [loaded.data]);

  if (!draft) return null;
  const set = (patch: Partial<PricingEditable>) => {
    setDraft({ ...draft, ...patch });
    setSaved(false);
  };

  const save = async () => {
    setSaving(true);
    try {
      const res = await api.pricingSettingsSave(draft);
      setDraft(res.settings);
      setSaved(true);
      onSaved?.();
    } finally {
      setSaving(false);
    }
  };
  const reset = async () => {
    setSaving(true);
    try {
      const res = await api.pricingSettingsReset();
      setDraft(res.settings);
      setSaved(true);
      onSaved?.();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-3 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-3">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
        Edit the rules — saved here, used everywhere
      </div>
      <div className="mt-3 flex flex-col gap-3">
        {/* Undercut competitors */}
        <label className="flex cursor-pointer items-center justify-between gap-2 text-sm">
          <span>
            Undercut the cheapest competitor
            <span className="block text-[11px] text-[var(--muted)]">
              Price just below the cheapest competing Google listing we import.
            </span>
          </span>
          <input
            type="checkbox"
            checked={draft.undercut_enabled}
            onChange={(e) => set({ undercut_enabled: e.target.checked })}
            className="h-4 w-4 accent-[var(--accent)]"
          />
        </label>
        <label className="flex items-center justify-between gap-2 text-sm">
          <span>
            Come in this much under ($)
            <span className="block text-[11px] text-[var(--muted)]">Then snap to a .99 price.</span>
          </span>
          <input
            type="number"
            step="0.01"
            value={draft.undercut_amount}
            disabled={!draft.undercut_enabled}
            onChange={(e) => set({ undercut_amount: Number(e.target.value) })}
            className="w-24 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-sm text-[var(--text)] disabled:opacity-40"
          />
        </label>
        {/* Markup floor */}
        <label className="flex items-center justify-between gap-2 text-sm">
          <span>
            Lowest price = this × cost
            <span className="block text-[11px] text-[var(--muted)]">
              Never sell below this many times the supplier cost (protects margin).
            </span>
          </span>
          <input
            type="number"
            step="0.5"
            value={draft.marketplace_markup}
            onChange={(e) => set({ marketplace_markup: Number(e.target.value) })}
            className="w-24 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-sm text-[var(--text)]"
          />
        </label>
        {/* Compare-at sale */}
        <label className="flex cursor-pointer items-center justify-between gap-2 text-sm">
          <span>
            Show a “was” sale price
            <span className="block text-[11px] text-[var(--muted)]">
              Struck-through compare-at so each product reads as on sale.
            </span>
          </span>
          <input
            type="checkbox"
            checked={draft.compare_at_enabled}
            onChange={(e) => set({ compare_at_enabled: e.target.checked })}
            className="h-4 w-4 accent-[var(--accent)]"
          />
        </label>
        <div className="flex items-center justify-between gap-2 text-sm">
          <span>
            Discount sizes (% off)
            <span className="block text-[11px] text-[var(--muted)]">
              One drawn per product; comma-separated.
            </span>
          </span>
          <input
            type="text"
            value={draft.compare_at_tiers.join(", ")}
            disabled={!draft.compare_at_enabled}
            onChange={(e) =>
              set({
                compare_at_tiers: e.target.value
                  .split(",")
                  .map((s) => Number(s.trim()))
                  .filter((n) => Number.isFinite(n)),
              })
            }
            className="w-28 rounded-md border border-[var(--border)] bg-[var(--surface)] px-2 py-1 text-sm text-[var(--text)] disabled:opacity-40"
          />
        </div>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <button
          type="button"
          onClick={save}
          disabled={saving}
          className="rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-50"
          style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          {saving ? "Saving…" : saved ? "Saved ✓" : "Save rules"}
        </button>
        <button
          type="button"
          onClick={reset}
          disabled={saving}
          className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-semibold text-[var(--muted)] disabled:opacity-50"
        >
          Reset to default
        </button>
        <span className="text-[11px] text-[var(--muted)]">Charm (.99 endings) is fixed.</span>
      </div>
    </div>
  );
}

// Compare-at tier options the operator can pin (or "off" / "draw a random one"). The tier
// is per-PRODUCT — picked once, then every variant gets the same %, never re-rolled per row.
const COMPARE_AT_OPTS: { value: number | null | "draw"; label: string }[] = [
  { value: null, label: "Off" },
  { value: 30, label: "30% off" },
  { value: 40, label: "40% off" },
  { value: 50, label: "50% off" },
  { value: "draw", label: "Draw (30/40/50)" },
];

function PriceCalculator({ markup }: { markup: number }) {
  const [cogs, setCogs] = useState("");
  const [competitor, setCompetitor] = useState("");
  const [undercut, setUndercut] = useState(true);
  const [compareAt, setCompareAt] = useState<number | null | "draw">(null);
  const [result, setResult] = useState<PriceSuggest | null>(null);
  const [pending, setPending] = useState(false);

  const run = async () => {
    const c = cogs.trim() === "" ? null : Number(cogs);
    const comp = competitor.trim() === "" ? null : Number(competitor);
    if (c == null && comp == null) return;
    setPending(true);
    try {
      // "draw" → ask the backend for one weighted-random tier (the per-product pick).
      let pct: number | null = compareAt === "draw" ? null : compareAt;
      if (compareAt === "draw") pct = (await api.compareAtTier()).discount_pct;
      setResult(
        await api.priceSuggest({ cogs: c, competitor: comp, undercut, compareAtPct: pct }),
      );
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="mt-4 border-t border-[var(--border)] pt-4">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
        Suggest a price ({markup}× floor{undercut ? " + undercut" : ""}) · per variant
      </div>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-[11px] text-[var(--muted)]">
          Product cost
          <input
            type="number"
            value={cogs}
            onChange={(e) => setCogs(e.target.value)}
            placeholder="6.00"
            className="w-24 rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1 text-sm text-[var(--text)]"
          />
        </label>
        <label className="flex flex-col gap-1 text-[11px] text-[var(--muted)]">
          Competitor (imported)
          <input
            type="number"
            value={competitor}
            onChange={(e) => setCompetitor(e.target.value)}
            placeholder="24.99"
            disabled={!undercut}
            className="w-24 rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1 text-sm text-[var(--text)] disabled:opacity-40"
          />
        </label>
        <label className="flex flex-col gap-1 text-[11px] text-[var(--muted)]">
          Compare-at
          <select
            value={compareAt === null ? "off" : String(compareAt)}
            onChange={(e) =>
              setCompareAt(
                e.target.value === "off"
                  ? null
                  : e.target.value === "draw"
                    ? "draw"
                    : Number(e.target.value),
              )
            }
            className="rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1 text-sm text-[var(--text)]"
          >
            {COMPARE_AT_OPTS.map((o) => (
              <option key={o.label} value={o.value === null ? "off" : String(o.value)}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={run}
          disabled={pending}
          className="rounded-lg px-3 py-1.5 text-xs font-semibold disabled:opacity-50"
          style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          {pending ? "…" : "Suggest"}
        </button>
      </div>
      <label className="mt-2 flex w-fit cursor-pointer items-center gap-1.5 text-[11px] text-[var(--muted)]">
        <input
          type="checkbox"
          checked={undercut}
          onChange={(e) => setUndercut(e.target.checked)}
          className="accent-[var(--accent)]"
        />
        Undercut the imported competitor (setting)
      </label>
      {result ? (
        <div className="mt-3 rounded-lg bg-[var(--surface-2)] p-3 text-sm">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="text-[11px] uppercase tracking-wide text-[var(--muted)]">Recommended</span>
            {result.compare_at != null ? (
              <span className="text-xs text-[var(--muted)] line-through tabular-nums">
                ${result.compare_at.toFixed(2)}
              </span>
            ) : null}
            <span
              className="text-lg font-bold tabular-nums"
              style={{ color: result.conflict ? "var(--state-testing)" : "var(--state-winner)" }}
            >
              {result.recommended != null ? `$${result.recommended.toFixed(2)}` : "—"}
            </span>
            {result.compare_at_pct != null ? (
              <span
                className="rounded px-1.5 py-0.5 text-[10px] font-bold"
                style={{ color: "var(--accent)", background: "color-mix(in srgb, var(--accent) 14%, transparent)" }}
              >
                {result.compare_at_pct}% OFF
              </span>
            ) : null}
            {result.margin_pct != null ? (
              <span className="text-[11px] text-[var(--muted)]">{result.margin_pct}% margin</span>
            ) : null}
          </div>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-[var(--muted)]">
            {result.undercut != null ? <span>undercut ${result.undercut.toFixed(2)}</span> : null}
            {result.markup_floor != null ? <span>{markup}× floor ${result.markup_floor.toFixed(2)}</span> : null}
          </div>
          <div className="mt-1.5 text-[11px]" style={{ color: result.conflict ? "var(--state-testing)" : "var(--muted)" }}>
            {result.note}
          </div>
        </div>
      ) : null}
    </div>
  );
}
