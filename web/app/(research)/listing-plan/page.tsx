"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import Link from "next/link";
import {
  api,
  type PlanItem,
  type PlanDay,
  type PlanWindow,
  type PlanSource,
  type PlanCategoryId,
  type Gameplan,
  type GameplanConfig,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card } from "@/components/ui";
import { PageHeader, Loading, ErrorState } from "@/components/PageState";
import { DetailModal, DetailGrid } from "@/components/DetailModal";

// Each research SOURCE has a stable colour + a link back to its research page. The
// plan's source mix is broken into the same fine-grained CATEGORIES the operator
// sees elsewhere (keyword list-now / build-ahead, the trend momentum horizons,
// winning products, and the marketplace + ad-winner discovery lanes).
const SOURCE_COLOR: Record<PlanSource, string> = {
  keyword: "var(--state-testing)",
  trend: "var(--state-live)",
  winning: "var(--state-winner)",
  marketplace: "var(--accent)",
  amazon: "var(--state-drafted)",
  meta: "var(--state-candidate)",
};

const SOURCE_META: Record<PlanSource, { label: string; what: string; href: string }> = {
  keyword: { label: "Keyword", what: "Keyword", href: "/keyword-discovery" },
  trend: { label: "Trending", what: "Rising momentum", href: "/trends" },
  winning: { label: "Google competitor", what: "Best-seller", href: "/product-research" },
  marketplace: { label: "Market", what: "Ali · Temu · 1688", href: "/product-research" },
  amazon: { label: "Amazon", what: "Ad winner", href: "/product-research" },
  meta: { label: "Meta", what: "Ad winner", href: "/product-research" },
};

// Mirrors the backend PLAN_CATEGORIES default mix (sums to 100). The operator can
// re-weight live; this is the starting point sent on the first fetch.
const DEFAULT_WEIGHTS: Record<PlanCategoryId, number> = {
  trending_now: 15,
  keyword_now: 25,
  keyword_ahead: 15,
  winning: 20,
  marketplace: 10,
  amazon: 10,
  meta: 5,
};
const ALL_CATS = Object.keys(DEFAULT_WEIGHTS) as PlanCategoryId[];

// The mix maps to WHERE the operator does the research. The keyword lanes are EXCLUSIVE —
// a keyword lives in exactly one: TRENDING NOW (rising-momentum keywords, listed with
// urgency while the spike is live), LIST NOW (steady in-season demand) or BUILD AHEAD
// (1–3 mo before season). That exclusivity is what makes Trending its own weight instead
// of the old reorder dial. PRODUCT research (winning competitor products + marketplace /
// Amazon / Meta lanes) feeds the other bucket.
const MIX_GROUPS: { id: string; name: string; hint: string; cats: PlanCategoryId[] }[] = [
  {
    id: "research",
    name: "Keyword & Trend research",
    hint: "Trending now = rising-momentum keywords (own lane) · list now = in season · build ahead = 1–3 mo. Lists from the products already found for each keyword in the SKU Plan.",
    cats: ["trending_now", "keyword_now", "keyword_ahead"],
  },
  {
    id: "product",
    name: "Product research",
    hint: "Winning competitor products + marketplace / Amazon / Meta lanes",
    cats: ["winning", "marketplace", "amazon", "meta"],
  },
];

const PER_DAY_MIN = 1;
const PER_DAY_MAX = 50;
const WEIGHT_MAX = 40;

// Default plan window / cadence — the rest of a fresh gameplan's settings.
const DEFAULT_WINDOW: PlanWindow = "month";
const DEFAULT_PER_DAY = 50;
// Compare-at ("was" price) is ON by default for a fresh gameplan. Older saved plans
// that predate the field read as undefined → treated as ON.
const DEFAULT_COMPARE_AT = true;

// Build the full settings bundle a gameplan persists. (The deprecated trend_bias dial is no
// longer written — trending is a weighted lane; old configs carrying it are read-tolerated.)
function buildConfig(
  window: PlanWindow,
  perDay: number,
  weights: Record<PlanCategoryId, number>,
  method: string,
  compareAt: boolean,
): GameplanConfig {
  return {
    window,
    per_day: perDay,
    weights: { ...weights },
    method,
    compare_at: compareAt,
  };
}

// True when two configs differ in any setting (window, cadence, mix, method, compare-at).
function configsEqual(a: GameplanConfig, b: GameplanConfig): boolean {
  if (a.window !== b.window || a.per_day !== b.per_day || (a.method || "") !== (b.method || ""))
    return false;
  if ((a.compare_at ?? DEFAULT_COMPARE_AT) !== (b.compare_at ?? DEFAULT_COMPARE_AT)) return false;
  return ALL_CATS.every((c) => (a.weights[c] ?? 0) === (b.weights[c] ?? 0));
}

function fmtNum(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

// "2026-06-19" → "Thu 19 Jun"
function fmtDate(iso: string | undefined, withWeekday = true): string {
  if (!iso) return "";
  const d = new Date(`${iso}T00:00:00`);
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()];
  const mo = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getMonth()];
  return `${withWeekday ? `${wd} ` : ""}${d.getDate()} ${mo}`;
}

// Internal pool / dossier slugs (e.g. "_candidate-pool-2026-06-02") are plumbing,
// not a real store — never surface them as a label.
function cleanStore(s: string | null | undefined): string | null {
  if (!s) return null;
  if (s.startsWith("_")) return null;
  return s;
}

export default function ListingPlanPage() {
  // Levers: the WINDOW (week / month), the daily CADENCE (per_day, up to 50), the
  // STORE the plan lists into, and the per-category WEIGHT mix. Together they define
  // an automatable daily schedule the operator can hand to bulk execution later.
  const [window, setWindow] = useState<PlanWindow>(DEFAULT_WINDOW);
  const [perDay, setPerDay] = useState(DEFAULT_PER_DAY);
  const [store, setStore] = useState<string | null>(null);
  const [weights, setWeights] = useState<Record<PlanCategoryId, number>>(DEFAULT_WEIGHTS);
  // Plan-level "which way to list" — seeds every card's method unless overridden per-item.
  // "" means use each item's backend default.
  const [planMethod, setPlanMethod] = useState<string>("");
  const [methods, setMethods] = useState<Record<string, string>>({});
  // Plan-level compare-at: when ON, each product draws one 30/40/50%-off tier and shows a
  // struck-through "was" price (uniform across its variants). When OFF, list at the flat
  // recommended price with no sale comparison. Persists into the gameplan config.
  const [compareAt, setCompareAt] = useState(DEFAULT_COMPARE_AT);

  // Saved gameplans across ALL stores (the overview groups them by store). activeGp
  // tracks the one currently loaded into the builder; planName/makeDefault are the
  // editable save fields.
  const [activeGp, setActiveGp] = useState<Gameplan | null>(null);
  const [planName, setPlanName] = useState("");
  const [makeDefault, setMakeDefault] = useState(false);
  const { data: gpData, reload: reloadGameplans } = useApi(() => api.gameplans(), []);
  const allGameplans = useMemo(() => gpData?.gameplans ?? [], [gpData]);
  const gameplans = useMemo(
    () => (store ? allGameplans.filter((g) => g.store === store) : []),
    [allGameplans, store],
  );

  const { data, error, loading } = useApi(
    () => api.listingPlan({ window, per_day: perDay, store, weights }),
    [window, perDay, store, weights],
  );

  const itemKey = (it: PlanItem) => `${it.source}::${it.store ?? ""}::${it.keyword ?? ""}`;
  const methodOf = (it: PlanItem) => methods[itemKey(it)] ?? (planMethod || it.method);
  const setMethod = (it: PlanItem, m: string) =>
    setMethods((prev) => ({ ...prev, [itemKey(it)]: m }));

  const setWeight = (id: PlanCategoryId, v: number) =>
    setWeights((prev) => ({ ...prev, [id]: Math.max(0, Math.min(WEIGHT_MAX, v)) }));
  const resetWeights = () => {
    setWeights(DEFAULT_WEIGHTS);
  };

  // ---- gameplan apply / save / dirty ----
  const currentConfig = useMemo(
    () => buildConfig(window, perDay, weights, planMethod, compareAt),
    [window, perDay, weights, planMethod, compareAt],
  );

  const applyConfig = (cfg: GameplanConfig) => {
    setWindow(cfg.window ?? DEFAULT_WINDOW);
    setPerDay(cfg.per_day ?? DEFAULT_PER_DAY);
    const merged = { ...DEFAULT_WEIGHTS } as Record<PlanCategoryId, number>;
    for (const c of ALL_CATS) {
      const v = cfg.weights?.[c];
      if (typeof v === "number") merged[c] = Math.max(0, Math.min(WEIGHT_MAX, v));
    }
    setWeights(merged);
    setPlanMethod(cfg.method ?? "");
    setCompareAt(cfg.compare_at ?? DEFAULT_COMPARE_AT);
    setMethods({});
  };

  const applyGameplan = (gp: Gameplan) => {
    setActiveGp(gp);
    setPlanName(gp.name);
    setMakeDefault(gp.is_default);
    applyConfig(gp.config);
  };

  // Edit from the overview: switch to that gameplan's store, then load it.
  const editGameplan = (gp: Gameplan) => {
    setStore(gp.store);
    applyGameplan(gp);
  };

  // Start a fresh gameplan (clear the active one but keep the current settings as a draft).
  const startNew = () => {
    setActiveGp(null);
    setPlanName("");
    setMakeDefault(false);
  };

  // When the store changes, keep the active gameplan only if it belongs to the new
  // store (so editGameplan survives); otherwise reset the builder to a fresh draft.
  useEffect(() => {
    if (activeGp && activeGp.store === store) return;
    setActiveGp(null);
    setPlanName("");
    setMakeDefault(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store]);

  // Auto-load a store's default gameplan when nothing is loaded yet.
  useEffect(() => {
    if (activeGp || !store) return;
    const def = gameplans.find((g) => g.is_default);
    if (def) applyGameplan(def);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gameplans, store]);

  const dirty = useMemo(
    () =>
      activeGp
        ? !configsEqual(currentConfig, activeGp.config) ||
          planName.trim() !== activeGp.name ||
          makeDefault !== activeGp.is_default
        : false,
    [activeGp, currentConfig, planName, makeDefault],
  );

  const saveGameplan = async () => {
    if (!store) return;
    const name = planName.trim();
    if (!name) return;
    const res = await api.createGameplan({
      store,
      name,
      config: currentConfig,
      is_default: makeDefault,
    });
    applyGameplan(res.gameplan);
    await reloadGameplans();
  };
  const updateActiveGameplan = async () => {
    if (!activeGp) return;
    const res = await api.updateGameplan(activeGp.id, {
      name: planName.trim() || activeGp.name,
      config: currentConfig,
      is_default: makeDefault,
    });
    applyGameplan(res.gameplan);
    await reloadGameplans();
  };
  const setGameplanDefault = async (gp: Gameplan) => {
    await api.updateGameplan(gp.id, { is_default: !gp.is_default });
    if (activeGp?.id === gp.id) setMakeDefault(!gp.is_default);
    await reloadGameplans();
  };
  const deleteGameplan = async (gp: Gameplan) => {
    await api.deleteGameplan(gp.id);
    if (activeGp?.id === gp.id) startNew();
    await reloadGameplans();
  };

  // ---- Run today: materialize day 1 of the plan into real jobs (the scheduler bridge) ----
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<
    { created: number; skipped: number; date: string | null } | null
  >(null);
  const runToday = async () => {
    if (!store) return;
    setRunning(true);
    setRunError(null);
    setRunResult(null);
    try {
      const res = await api.executeListingPlan({
        store,
        window,
        per_day: perDay,
        weights,
        day: 1,
      });
      setRunResult({ created: res.counts.created, skipped: res.counts.skipped, date: res.date });
    } catch (e) {
      setRunError(e instanceof Error ? e.message : "Failed to queue today's listings");
    } finally {
      setRunning(false);
    }
  };

  const start = data?.start_date;
  const end = data?.days?.length ? data.days[data.days.length - 1].date : undefined;
  const stores = data?.stores ?? [];
  const totalW = ALL_CATS.reduce((a, c) => a + (weights[c] ?? 0), 0) || 1;

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader
        title="Daily Listings"
        subtitle="An automatable daily listing schedule. Pick the store, window and daily cadence, then dial the source mix — the highest-priority products from every research method fill each day. Each card shows where it came from and what to list."
      />

      {/* ── BUILDER ── one window: store → window/cadence/method → source mix → save.
          Everything that defines a gameplan lives here so it reads top-to-bottom. */}
      <Card className="mb-6 p-5">
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <h2 className="text-base font-semibold">
            {activeGp ? "Edit listing gameplan" : "Create a listing gameplan"}
          </h2>
          {activeGp ? (
            <button
              type="button"
              onClick={startNew}
              className="rounded-md border border-[var(--border)] px-2.5 py-1 text-xs font-semibold text-[var(--muted)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)]"
            >
              + New gameplan
            </button>
          ) : null}
        </div>

        {/* Step 1 — store selector (the entry point). */}
        <div className="mb-5">
          <Label>1 · Store</Label>
          <div className="mt-2 inline-flex flex-wrap gap-1 rounded-lg border border-[var(--border)] bg-[var(--surface)] p-0.5">
            <SegBtn active={store === null} onClick={() => setStore(null)}>
              All stores
            </SegBtn>
            {stores.map((s) => (
              <SegBtn key={s} active={store === s} onClick={() => setStore(s)}>
                {s}
              </SegBtn>
            ))}
          </div>
          <div className="mt-2 text-xs text-[var(--muted)]">
            {store
              ? `Building a gameplan for ${store}. Settings below save into it.`
              : "Pick a store to create a saveable gameplan. “All stores” previews the schedule but can’t be saved."}
          </div>
        </div>

        {/* Step 2 — window · cadence · method. */}
        <div className="mb-5 border-t border-[var(--border)] pt-5">
          <Label>2 · Schedule &amp; method</Label>
          <div className="mt-3 flex flex-wrap items-start gap-x-8 gap-y-4">
            <div>
              <MiniLabel>Plan window</MiniLabel>
              <div className="mt-1.5 inline-flex rounded-lg border border-[var(--border)] bg-[var(--surface)] p-0.5">
                {(data?.windows ?? [
                  { id: "week", label: "This week", days: 7 },
                  { id: "month", label: "This month", days: 30 },
                ]).map((w) => (
                  <SegBtn key={w.id} active={window === w.id} onClick={() => setWindow(w.id as PlanWindow)}>
                    {w.label}
                  </SegBtn>
                ))}
              </div>
              {start ? (
                <div className="mt-1.5 text-xs text-[var(--muted)]">
                  {fmtDate(start)} → {fmtDate(end)}
                  <span className="ml-1.5 tabular-nums">· {data?.params.days ?? "—"} days</span>
                </div>
              ) : null}
            </div>

            <div>
              <MiniLabel>Listings per day</MiniLabel>
              <div className="mt-1.5 flex items-center gap-3">
                <Stepper value={perDay} min={PER_DAY_MIN} max={PER_DAY_MAX} onChange={setPerDay} />
                <span className="text-sm text-[var(--muted)]">
                  {data ? (
                    <>
                      ≈{" "}
                      <span className="font-semibold tabular-nums text-[var(--text)]">
                        {data.params.capacity}
                      </span>{" "}
                      over the {window === "week" ? "week" : "month"}
                    </>
                  ) : (
                    "products each day"
                  )}
                </span>
              </div>
            </div>

            <div>
              <MiniLabel>Which way to list</MiniLabel>
              <div className="mt-1.5 inline-flex rounded-lg border border-[var(--border)] bg-[var(--surface)] p-0.5">
                <SegBtn active={planMethod === ""} onClick={() => setPlanMethod("")}>
                  Per item
                </SegBtn>
                {(data?.methods ?? []).map((m) => (
                  <SegBtn key={m.id} active={planMethod === m.id} onClick={() => setPlanMethod(m.id)}>
                    {m.id === "source-import" ? "General Listing" : "Branded Listing"}
                  </SegBtn>
                ))}
              </div>
              <div className="mt-1.5 max-w-[14rem] text-xs text-[var(--muted)]">
                Default import method for the batch. Each card can override.
              </div>
            </div>

            <div>
              <MiniLabel>Compare-at price</MiniLabel>
              <div className="mt-1.5 inline-flex rounded-lg border border-[var(--border)] bg-[var(--surface)] p-0.5">
                <SegBtn active={compareAt} onClick={() => setCompareAt(true)}>
                  On
                </SegBtn>
                <SegBtn active={!compareAt} onClick={() => setCompareAt(false)}>
                  Off
                </SegBtn>
              </div>
              <div className="mt-1.5 max-w-[14rem] text-xs text-[var(--muted)]">
                {compareAt
                  ? "Each product draws one 30/40/50% “was” discount, applied evenly to every variant."
                  : "List at the flat recommended price — no struck-through “was” price."}
              </div>
            </div>
          </div>
        </div>

        {/* Step 3 — source mix. */}
        {data ? (
          <div className="border-t border-[var(--border)] pt-5">
            <div className="mb-3 flex items-center justify-between gap-3">
              <Label>3 · Source mix</Label>
              <button
                type="button"
                onClick={resetWeights}
                className="text-[11px] font-semibold text-[var(--muted)] hover:text-[var(--accent)]"
              >
                Reset to default
              </button>
            </div>

            <div className="mb-5 flex h-2.5 w-full overflow-hidden rounded-full bg-[var(--surface-2)]">
              {ALL_CATS.map((id) => {
                const cat = data.categories.find((c) => c.id === id);
                const pct = ((weights[id] ?? 0) / totalW) * 100;
                if (pct <= 0) return null;
                return (
                  <div
                    key={id}
                    style={{ width: `${pct}%`, background: SOURCE_COLOR[cat?.source ?? "keyword"] }}
                    title={`${cat?.name ?? id} · ${Math.round(pct)}%`}
                  />
                );
              })}
            </div>

            <div className="flex flex-col gap-5">
              {MIX_GROUPS.map((g) => {
                const cats = g.cats
                  .map((id) => data.categories.find((c) => c.id === id))
                  .filter((c): c is NonNullable<typeof c> => Boolean(c));
                const groupShare = Math.round(
                  (g.cats.reduce((a, id) => a + (weights[id] ?? 0), 0) / totalW) * 100,
                );
                return (
                  <div key={g.id}>
                    <div className="mb-2.5 flex items-baseline gap-2 border-b border-[var(--border)] pb-1.5">
                      <span className="text-xs font-bold uppercase tracking-wide text-[var(--text)]">
                        {g.name}
                      </span>
                      <span className="text-[11px] text-[var(--muted)]">{g.hint}</span>
                      <span className="ml-auto text-xs font-bold tabular-nums">{groupShare}%</span>
                    </div>
                    <div className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
                      {cats.map((cat) => (
                        <MixRow
                          key={cat.id}
                          name={cat.name}
                          color={SOURCE_COLOR[cat.source]}
                          href={SOURCE_META[cat.source].href}
                          available={cat.available}
                          allocated={Math.round(((weights[cat.id] ?? 0) / totalW) * perDay)}
                          total={perDay}
                          weight={weights[cat.id] ?? 0}
                          share={Math.round(((weights[cat.id] ?? 0) / totalW) * 100)}
                          onChange={(v) => setWeight(cat.id, v)}
                        />
                      ))}
                    </div>
                    {g.id === "research" ? (
                      <p className="mt-3 rounded-lg border border-dashed border-[var(--border)] bg-[var(--surface-2)] px-3 py-2 text-[11px] leading-snug text-[var(--muted)]">
                        These lanes list from the products <span className="font-medium text-[var(--text)]">already found in the SKU Plan</span> for
                        each keyword (Find products → found products) — so every scheduled slot starts
                        from a product we already have, not a fresh search.
                        {(() => {
                          const n = data.sources.find((s) => s.id === "trend")?.count ?? 0;
                          return n > 0 ? ` ${n} keyword${n === 1 ? " is" : "s are"} trending right now.` : " Nothing is trending right now — the Trending lane back-fills from the other lanes until something spikes.";
                        })()}
                      </p>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        ) : null}

        {/* Step 4 — save / update this gameplan. */}
        <div className="mt-5 border-t border-[var(--border)] pt-4">
          {store ? (
            <div className="flex flex-wrap items-center gap-3">
              <input
                value={planName}
                onChange={(e) => setPlanName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") (activeGp ? updateActiveGameplan() : saveGameplan());
                }}
                placeholder={`Gameplan name for ${store}…`}
                className="min-w-[14rem] flex-1 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm outline-none focus:border-[var(--accent)]"
              />
              <label className="flex cursor-pointer items-center gap-1.5 text-xs text-[var(--muted)]">
                <input
                  type="checkbox"
                  checked={makeDefault}
                  onChange={(e) => setMakeDefault(e.target.checked)}
                  className="accent-[var(--accent)]"
                />
                Default for {store}
              </label>
              {activeGp ? (
                <>
                  <SaveBtn onClick={updateActiveGameplan} disabled={!dirty}>
                    {dirty ? "Save changes" : "Saved ✓"}
                  </SaveBtn>
                  <button
                    type="button"
                    onClick={saveGameplan}
                    disabled={!planName.trim()}
                    className="rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-semibold text-[var(--muted)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)] disabled:opacity-40"
                  >
                    Save as new
                  </button>
                </>
              ) : (
                <SaveBtn onClick={saveGameplan} disabled={!planName.trim()}>
                  Save gameplan
                </SaveBtn>
              )}
            </div>
          ) : (
            <p className="text-xs text-[var(--muted)]">
              Pick a specific store above to name and save this setup as a gameplan.
            </p>
          )}
        </div>
      </Card>

      {/* ── OVERVIEW ── a column per store of saved gameplans; click one to edit. */}
      <GameplanOverview
        stores={stores}
        gameplans={allGameplans}
        activeId={activeGp?.id ?? null}
        onEdit={editGameplan}
        onSetDefault={setGameplanDefault}
        onDelete={deleteGameplan}
      />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data && data.totals.pool > 0 ? (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[var(--border)] bg-[var(--surface)] px-4 py-3">
          <div className="text-sm">
            <span className="font-semibold text-[var(--text)]">Run today’s listings</span>
            <span className="ml-2 text-xs text-[var(--muted)]">
              Queues day 1 ({perDay} product{perDay === 1 ? "" : "s"}) as jobs
              {store ? ` for ${store}` : " — pick a store first"}. Auto steps run; manual
              steps emit the exact command in Activity.
            </span>
            {runResult ? (
              <span className="ml-2 text-xs font-semibold text-[var(--accent)]">
                ✓ Queued {runResult.created} job{runResult.created === 1 ? "" : "s"}
                {runResult.skipped ? ` (${runResult.skipped} skipped)` : ""} — see{" "}
                <a href="/activity" className="underline">Activity</a>.
              </span>
            ) : null}
            {runError ? (
              <span className="ml-2 text-xs font-semibold text-red-500">{runError}</span>
            ) : null}
          </div>
          <button
            type="button"
            onClick={runToday}
            disabled={!store || running}
            className="shrink-0 rounded-lg bg-[var(--accent)] px-3.5 py-1.5 text-sm font-semibold text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {running ? "Queuing…" : "Run today →"}
          </button>
        </div>
      ) : null}

      {data ? (
        data.totals.pool === 0 ? (
          <EmptyPlan />
        ) : (
          <div className="flex flex-col gap-2.5">
            {data.days.map((d) => (
              <DayRow
                key={d.date}
                day={d}
                perDay={data.params.per_day}
                methods={data.methods}
                methodOf={methodOf}
                setMethod={setMethod}
              />
            ))}
          </div>
        )
      ) : null}
    </div>
  );
}

// After a gameplan is saved it shows up here: a column PER STORE of that store's
// saved setups. Each card is clickable to load it back into the builder for editing,
// with a default ★ toggle and delete. This is the "overview" the operator asked for.
function GameplanOverview({
  stores,
  gameplans,
  activeId,
  onEdit,
  onSetDefault,
  onDelete,
}: {
  stores: string[];
  gameplans: Gameplan[];
  activeId: number | null;
  onEdit: (gp: Gameplan) => void;
  onSetDefault: (gp: Gameplan) => Promise<void>;
  onDelete: (gp: Gameplan) => Promise<void>;
}) {
  // Group saved gameplans by store. Include any store that has gameplans even if it
  // dropped out of the current plan's `stores` list, so nothing saved goes hidden.
  const byStore = useMemo(() => {
    const m = new Map<string, Gameplan[]>();
    for (const s of stores) m.set(s, []);
    for (const gp of gameplans) {
      if (!m.has(gp.store)) m.set(gp.store, []);
      m.get(gp.store)!.push(gp);
    }
    return [...m.entries()].filter(([, list]) => list.length > 0);
  }, [stores, gameplans]);

  if (gameplans.length === 0) {
    return (
      <Card className="mb-6 p-4 text-xs text-[var(--muted)]">
        <span className="font-semibold text-[var(--text)]">No saved gameplans yet.</span> Build one
        above for a store and save it — it’ll appear here as a per-store overview you can click to
        edit.
      </Card>
    );
  }

  return (
    <div className="mb-6">
      <div className="mb-2 flex items-baseline gap-2">
        <h2 className="text-base font-semibold">Saved gameplans</h2>
        <span className="text-xs text-[var(--muted)]">Click any to edit · ★ = store default</span>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {byStore.map(([s, list]) => (
          <Card key={s} className="p-3">
            <div className="mb-2 flex items-center gap-2 border-b border-[var(--border)] pb-2">
              <span className="text-sm font-bold">{s}</span>
              <span className="ml-auto text-[11px] tabular-nums text-[var(--muted)]">
                {list.length} plan{list.length === 1 ? "" : "s"}
              </span>
            </div>
            <div className="flex flex-col gap-1.5">
              {list.map((gp) => (
                <GameplanOverviewCard
                  key={gp.id}
                  gp={gp}
                  active={gp.id === activeId}
                  onEdit={onEdit}
                  onSetDefault={onSetDefault}
                  onDelete={onDelete}
                />
              ))}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

// One saved gameplan inside its store column: click body to edit; hover reveals the
// default ★ toggle + delete ✕.
function GameplanOverviewCard({
  gp,
  active,
  onEdit,
  onSetDefault,
  onDelete,
}: {
  gp: Gameplan;
  active: boolean;
  onEdit: (gp: Gameplan) => void;
  onSetDefault: (gp: Gameplan) => Promise<void>;
  onDelete: (gp: Gameplan) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const wrap = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } catch {
      /* surfaced elsewhere; keep the card usable */
    } finally {
      setBusy(false);
    }
  };
  const methodLabel = gp.config.method
    ? gp.config.method === "source-import"
      ? "General Listing"
      : "Branded Listing"
    : "Per-item";
  return (
    <div
      className={`group flex items-center gap-2 rounded-lg border px-2.5 py-2 transition-colors ${
        active
          ? "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)]"
          : "border-[var(--border)] hover:border-[var(--accent)]"
      }`}
    >
      <button
        type="button"
        onClick={() => onEdit(gp)}
        className="min-w-0 flex-1 text-left"
        title="Edit this gameplan"
      >
        <div className="flex items-center gap-1.5">
          {gp.is_default ? <span className="text-[var(--accent)]">★</span> : null}
          <span className="truncate text-sm font-semibold">{gp.name}</span>
        </div>
        <div className="mt-0.5 text-[11px] text-[var(--muted)]">
          {methodLabel} · {gp.config.window} · {gp.config.per_day}/day
        </div>
      </button>
      <button
        type="button"
        onClick={() => wrap(() => onSetDefault(gp))}
        disabled={busy}
        title={gp.is_default ? "Unset default" : "Set as default for this store"}
        className={`shrink-0 text-sm transition-opacity hover:text-[var(--accent)] ${
          gp.is_default ? "text-[var(--accent)]" : "text-[var(--muted)] opacity-0 group-hover:opacity-100"
        }`}
      >
        {gp.is_default ? "★" : "☆"}
      </button>
      <button
        type="button"
        onClick={() => wrap(() => onDelete(gp))}
        disabled={busy}
        title="Delete gameplan"
        className="shrink-0 text-sm text-[var(--muted)] opacity-0 transition-opacity hover:text-[var(--state-killed)] group-hover:opacity-100"
      >
        ✕
      </button>
    </div>
  );
}

function Label({ children }: { children: ReactNode }) {
  return (
    <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--muted)]">
      {children}
    </span>
  );
}

// A smaller field label for the individual controls inside a step.
function MiniLabel({ children }: { children: ReactNode }) {
  return (
    <span className="text-[11px] font-medium text-[var(--muted)]">{children}</span>
  );
}

// The primary accent button used to save / update a gameplan.
function SaveBtn({
  onClick,
  disabled,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-lg px-4 py-2 text-sm font-semibold transition-opacity disabled:opacity-40"
      style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
    >
      {children}
    </button>
  );
}

// One category in the source-mix control: colour dot + name + pool count + a slider
// and its live % share of the calendar.
function MixRow({
  name,
  color,
  href,
  available,
  allocated,
  total,
  weight,
  share,
  onChange,
}: {
  name: string;
  color: string;
  href: string;
  available: number;
  allocated: number;
  total: number;
  weight: number;
  share: number;
  onChange: (n: number) => void;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: color }} />
        <Link href={href} className="truncate text-sm font-medium hover:text-[var(--accent)]" title={name}>
          {name}
        </Link>
        <span
          className="ml-auto shrink-0 text-[11px] tabular-nums text-[var(--muted)]"
          title={`${allocated} of ${total} listings go to this lane · ${available} candidate${available === 1 ? "" : "s"} in pool`}
        >
          {allocated}/{total}
        </span>
        <span className="w-9 shrink-0 text-right text-xs font-bold tabular-nums">{share}%</span>
      </div>
      <input
        type="range"
        min={0}
        max={WEIGHT_MAX}
        value={weight}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full cursor-pointer"
        style={{ accentColor: color }}
        aria-label={`${name} weight`}
      />
    </div>
  );
}

function SegBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md px-3 py-1.5 text-sm font-semibold transition-colors ${
        active
          ? "bg-[var(--accent)] text-[var(--accent-fg)]"
          : "text-[var(--muted)] hover:text-[var(--text)]"
      }`}
    >
      {children}
    </button>
  );
}

// A proper setting selector — a numeric stepper for the daily cadence.
function Stepper({
  value,
  min,
  max,
  onChange,
}: {
  value: number;
  min: number;
  max: number;
  onChange: (n: number) => void;
}) {
  const clamp = (n: number) => Math.min(max, Math.max(min, n));
  return (
    <div className="inline-flex items-center rounded-lg border border-[var(--border)] bg-[var(--surface)]">
      <StepBtn disabled={value <= min} onClick={() => onChange(clamp(value - 1))}>
        −
      </StepBtn>
      <span className="w-12 text-center text-lg font-bold tabular-nums">{value}</span>
      <StepBtn disabled={value >= max} onClick={() => onChange(clamp(value + 1))}>
        +
      </StepBtn>
    </div>
  );
}

function StepBtn({
  disabled,
  onClick,
  children,
}: {
  disabled: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="flex h-9 w-9 items-center justify-center text-xl font-bold text-[var(--muted)] transition-colors hover:text-[var(--text)] disabled:cursor-not-allowed disabled:opacity-30"
    >
      {children}
    </button>
  );
}

function DayRow({
  day,
  perDay,
  methods,
  methodOf,
  setMethod,
}: {
  day: PlanDay;
  perDay: number;
  methods: { id: string; name: string }[];
  methodOf: (it: PlanItem) => string;
  setMethod: (it: PlanItem, m: string) => void;
}) {
  const empty = day.items.length === 0;
  const dd = /^\d{4}-\d{2}-(\d{2})$/.exec(day.date)?.[1];
  return (
    <Card className={`p-4 ${empty ? "opacity-50" : ""}`}>
      <div className="mb-3 flex items-center gap-3">
        <div className="flex h-11 w-11 shrink-0 flex-col items-center justify-center rounded-lg bg-[var(--surface-2)] leading-none">
          <span className="text-[10px] font-semibold uppercase text-[var(--muted)]">{day.weekday}</span>
          <span className="text-base font-bold tabular-nums">{dd ?? day.day}</span>
        </div>
        <div>
          <div className="text-sm font-semibold">{fmtDate(day.date)}</div>
          <div className="text-xs text-[var(--muted)]">
            {empty
              ? `Open — up to ${perDay} slot${perDay === 1 ? "" : "s"}`
              : `${day.items.length} listing${day.items.length === 1 ? "" : "s"}`}
          </div>
        </div>
      </div>

      {empty ? (
        <p className="rounded-lg border border-dashed border-[var(--border)] px-3 py-3 text-center text-xs text-[var(--muted)]">
          No more pooled products for this day. Run discovery, raise a category’s weight, or lower the cadence.
        </p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {day.items.map((it, i) => (
            <PlanCard
              key={`${it.source}-${it.store}-${it.keyword}-${i}`}
              it={it}
              methods={methods}
              method={methodOf(it)}
              onMethod={(m) => setMethod(it, m)}
            />
          ))}
        </div>
      )}
    </Card>
  );
}

// Minimal by design: research-method badge + the keyword/trend/product name + the
// import-method toggle. Clicking the card opens the full detail in a popup, so the
// card stays a clean overview row and the analytics don't squeeze the grid.
function PlanCard({
  it,
  methods,
  method,
  onMethod,
}: {
  it: PlanItem;
  methods: { id: string; name: string }[];
  method: string;
  onMethod: (m: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const src = SOURCE_META[it.source];
  const color = SOURCE_COLOR[it.source];
  // Trends are cross-pipeline (dossier slug, not a store) — hide the slug there.
  const store = it.source === "trend" ? null : cleanStore(it.store);
  return (
    <>
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen(true);
          }
        }}
        className="flex cursor-pointer flex-col gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-3 text-left transition-colors hover:border-[var(--accent)]"
      >
        <div className="flex items-center gap-2">
          <span
            className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
            style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
          >
            {src.label}
          </span>
          <span className="text-[10px] uppercase tracking-wide text-[var(--muted)]">{src.what}</span>
          {(it.found_products ?? 0) > 0 ? (
            <span
              className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
              style={{
                color: "var(--state-winner)",
                background: "color-mix(in srgb, var(--state-winner) 14%, transparent)",
              }}
              title="Products already found for this keyword in the SKU Plan — listing starts from these, not a fresh search"
            >
              {it.found_products} found
            </span>
          ) : null}
          <span
            className={`text-[10px] uppercase tracking-wide text-[var(--muted)]${it.sv != null ? "" : " ml-auto"}`}
            title="When to list this — its momentum horizon"
          >
            {it.horizon === "now" ? "List now" : "Build ahead"}
          </span>
          {it.sv != null ? (
            <span className="ml-auto text-[10px] tabular-nums text-[var(--muted)]">
              {fmtNum(it.sv)} SV
            </span>
          ) : null}
        </div>

        <div className="font-medium leading-snug line-clamp-2" title={it.keyword ?? "—"}>
          {it.keyword ?? "—"}
        </div>

        {store ? (
          <div className="truncate text-[11px] text-[var(--muted)]" title={store}>
            {store}
          </div>
        ) : null}

        <div
          className="mt-auto flex items-center justify-between gap-2 pt-1"
          onClick={(e) => e.stopPropagation()}
        >
          <MethodToggle methods={methods} value={method} onChange={onMethod} />
          <span className="text-[11px] font-semibold text-[var(--muted)]">Details →</span>
        </div>
      </div>

      <PlanItemModal
        open={open}
        it={it}
        method={method}
        store={store}
        onClose={() => setOpen(false)}
      />
    </>
  );
}

// The popup: the full found-item overview (one keyword / trend / product) — every
// analytic the card deliberately omits, plus the image and source links.
function PlanItemModal({
  open,
  it,
  method,
  store,
  onClose,
}: {
  open: boolean;
  it: PlanItem;
  method: string;
  store: string | null;
  onClose: () => void;
}) {
  const src = SOURCE_META[it.source];
  const color = SOURCE_COLOR[it.source];
  const methodLabel = method
    ? method === "source-import"
      ? "General Listing"
      : "Branded Listing"
    : "Per-item default";
  return (
    <DetailModal
      open={open}
      onClose={onClose}
      badge={
        <span className="inline-flex items-center gap-1.5">
          <span
            className="rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide"
            style={{ color, background: `color-mix(in srgb, ${color} 14%, transparent)` }}
          >
            {src.label}
          </span>
          <span className="text-[10px] uppercase tracking-wide text-[var(--muted)]">{src.what}</span>
        </span>
      }
      title={it.keyword ?? "—"}
      subtitle={store ?? undefined}
      footer={
        <>
          {it.url ? (
            <a
              href={it.url}
              target="_blank"
              rel="noopener noreferrer"
              className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-xs font-semibold text-[var(--muted)] hover:border-[var(--accent)] hover:text-[var(--text)]"
            >
              Source ↗
            </a>
          ) : null}
          <Link
            href={`/product-research?keyword=${encodeURIComponent(it.keyword ?? "")}`}
            className="rounded-lg px-3 py-1.5 text-xs font-semibold"
            style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
          >
            Open research →
          </Link>
        </>
      }
    >
      {it.image ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={it.image}
          alt={it.keyword ?? ""}
          className="mb-4 h-40 w-full rounded-lg border border-[var(--border)] object-cover"
        />
      ) : null}
      <DetailGrid
        rows={[
          { label: "Research source", value: src.what },
          { label: "Store", value: store },
          { label: "Search volume", value: it.sv != null ? `${fmtNum(it.sv)}/mo` : null },
          { label: "Gate", value: it.gate ?? null },
          { label: "Capture bucket", value: it.capture_bucket ?? null },
          { label: "Trend bucket", value: it.trend_bucket ?? null },
          { label: "Rising trend", value: it.is_rising ? "yes" : null },
          { label: "Momentum", value: it.momentum != null ? String(it.momentum) : null },
          { label: "Score", value: it.score != null ? it.score : null },
          { label: "Validation lanes", value: it.n_validation != null ? it.n_validation : null },
          { label: "Horizon", value: it.horizon },
          { label: "Price basis", value: it.price != null ? String(it.price) : null },
          { label: "Listing method", value: methodLabel },
        ]}
      />
    </DetailModal>
  );
}

function MethodToggle({
  methods,
  value,
  onChange,
}: {
  methods: { id: string; name: string }[];
  value: string;
  onChange: (m: string) => void;
}) {
  return (
    <div className="inline-flex rounded-md border border-[var(--border)] p-0.5">
      {methods.map((m) => (
        <button
          key={m.id}
          type="button"
          onClick={() => onChange(m.id)}
          className={`rounded px-2 py-0.5 text-[10px] font-semibold transition-colors ${
            value === m.id
              ? "bg-[var(--accent)] text-[var(--accent-fg)]"
              : "text-[var(--muted)] hover:text-[var(--text)]"
          }`}
          title={m.name}
        >
          {m.id === "source-import" ? "General Listing" : "Branded Listing"}
        </button>
      ))}
    </div>
  );
}

function EmptyPlan() {
  return (
    <Card className="p-8 text-center text-sm text-[var(--muted)]">
      <p className="font-medium text-[var(--text)]">Nothing to schedule yet.</p>
      <p className="mt-2">
        The calendar pools products from every research method — keyword candidates, the trend
        momentum horizons, winning competitor products, and the marketplace + ad-winner lanes. Fill
        the pool by running discovery (
        <code className="rounded bg-[var(--surface-2)] px-1.5 py-0.5 font-mono">/general-store-research</code>) or
        the Best-Seller Spy, then promote from{" "}
        <Link href="/keyword-discovery" className="font-semibold text-[var(--accent)]">
          Keyword Research
        </Link>
        ,{" "}
        <Link href="/trends" className="font-semibold text-[var(--accent)]">
          Trend Research
        </Link>
        , or{" "}
        <Link href="/product-research" className="font-semibold text-[var(--accent)]">
          Winning Products
        </Link>
        .
      </p>
    </Card>
  );
}
