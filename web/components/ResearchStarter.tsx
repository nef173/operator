"use client";

import { useEffect, useState, type ReactNode } from "react";
import { api, type ResearchMethod, type ResearchSurface } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { useStore } from "@/components/StoreProvider";
import { useQueryParam } from "@/lib/useQueryParam";
import { Card } from "@/components/ui";
import { JobRunner } from "@/components/JobRunner";

// "Ways to research" / get-started selector for a research surface
// (keyword / niche / product). Plain-language cards → pick one → short step
// overview → run (a keyword is optional; it runs without one — the AI finds what's hot).
export function ResearchStarter({
  surface,
  title = "Ways to research",
  initialKeyword,
  extra,
  gridCols = 2,
  selected: selectedProp,
  onSelectedChange,
}: {
  surface: ResearchSurface;
  title?: string;
  initialKeyword?: string;
  // An extra card rendered alongside the method cards in the main grid (e.g. the
  // News Radar on the trend surface, which isn't a backend research method).
  extra?: ReactNode;
  // Columns for the method grid. Trend surface uses 3 (Trend Radar + News Radar +
  // Events Calendar all on one line); other surfaces default to 2.
  gridCols?: 2 | 3;
  // Controlled selection (optional) — lets a page coordinate this grid with its extra
  // cards as one accordion (opening an extra clears the method selection and vice versa).
  selected?: string | null;
  onSelectedChange?: (id: string | null) => void;
}) {
  const { data } = useApi(() => api.researchMethods(surface), [surface]);
  const { store, setStore, stores } = useStore();
  const qpKeyword = useQueryParam("keyword");
  const seed = (initialKeyword ?? qpKeyword ?? "").trim();
  const [selectedLocal, setSelectedLocal] = useState<string | null>(null);
  const selected = selectedProp !== undefined ? selectedProp : selectedLocal;
  const setSelected = (id: string | null) => {
    if (onSelectedChange) onSelectedChange(id);
    else setSelectedLocal(id);
  };

  const methods = data?.research_methods ?? [];

  // Arriving with a keyword handed off from another stage: auto-open the first
  // method so the operator lands straight on the runnable, pre-seeded detail.
  useEffect(() => {
    if (seed && methods.length > 0 && selected == null) {
      setSelected(methods[0].id);
    }
  }, [seed, methods, selected]);

  if (methods.length === 0) return null;

  const activeStore = store || null;
  const active = methods.find((m) => m.id === selected) ?? null;

  // The "support" methods (Google Shopping scan, keyword-planner expansion) still run in the
  // BACKEND automatically on every idea — they just no longer get a wall of explainer text here.
  const findMethods = methods.filter((m) => m.role !== "support");

  return (
    <Card className="mb-4 p-3.5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="text-sm font-semibold">{title}</div>
        {stores.length > 1 ? (
          <select
            aria-label="Store"
            value={activeStore ?? ""}
            onChange={(e) => setStore(e.target.value)}
            className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1 text-xs font-medium"
          >
            {stores.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        ) : null}
      </div>

      {seed ? (
        <div
          className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-sm"
          style={{
            borderColor: "color-mix(in srgb, var(--accent) 40%, transparent)",
            background: "color-mix(in srgb, var(--accent) 8%, transparent)",
          }}
        >
          <span className="text-[var(--muted)]">From research →</span>
          <span className="font-semibold">{seed}</span>
          <span className="text-[var(--muted)]">— ready to run.</span>
        </div>
      ) : null}

      <div className={`mt-3 grid gap-2.5 ${gridCols === 3 ? "md:grid-cols-3" : "md:grid-cols-2"}`}>
        {findMethods.map((m) => (
          <MethodCard
            key={m.id}
            method={m}
            selected={selected === m.id}
            onSelect={() => setSelected(selected === m.id ? null : m.id)}
            store={activeStore}
            seedKeyword={seed}
          />
        ))}
        {extra}
      </div>

      {active ? <MethodDetail key={active.id} method={active} /> : null}
    </Card>
  );
}

function MethodCard({
  method,
  selected,
  onSelect,
  store,
  seedKeyword,
}: {
  method: ResearchMethod;
  selected: boolean;
  onSelect: () => void;
  store: string | null;
  seedKeyword?: string;
}) {
  // The keyword + Run live INSIDE the card so the operator can run without opening the detail.
  const [keyword, setKeyword] = useState(seedKeyword ?? "");
  return (
    <Card
      className={`flex h-full flex-col p-3 transition-colors ${
        selected
          ? "border-[var(--accent)] ring-1 ring-[var(--ring)]"
          : "hover:border-[var(--accent)]"
      }`}
    >
      <button onClick={onSelect} className="text-left" title="Show the steps">
        <div className="flex items-center justify-between gap-2">
          <div className="text-sm font-semibold">{method.name}</div>
          <span
            className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border ${
              selected
                ? "border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-fg)]"
                : "border-[var(--border)]"
            }`}
          >
            {selected ? (
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                <path d="M20 6 9 17l-5-5" />
              </svg>
            ) : null}
          </span>
        </div>
        <p className="mt-1 text-[13px] leading-snug text-[var(--muted)]">{method.tagline}</p>
      </button>

      {/* Inline run — a keyword is OPTIONAL: type one to research it, or leave it blank and the
          AI finds what's already taking off. */}
      {method.job_spec ? (
        <div className="mt-auto pt-2.5">
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="Add a keyword (optional)"
              className="min-w-0 flex-1 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-2.5 py-1.5 text-[13px]"
            />
            <JobRunner
              spec={method.job_spec}
              store={store}
              args={keyword ? { keyword } : undefined}
              label="Run"
            />
          </div>
          <div className="mt-1 text-[11px] text-[var(--muted)]">
            {keyword.trim()
              ? `Researches “${keyword.trim()}”.`
              : "Leave blank → the AI finds what’s hot right now."}
          </div>
        </div>
      ) : null}
    </Card>
  );
}

function MethodDetail({ method }: { method: ResearchMethod }) {
  // The card already carries the keyword + Run; the detail is just the step overview on select.
  return (
    <div className="mt-3 rounded-xl border border-[var(--border)] bg-[var(--surface-2)] p-3.5">
      <h3 className="text-sm font-semibold">{method.name}</h3>
      <p className="mt-0.5 text-[13px] text-[var(--muted)]">{method.best_for}</p>

      <div className="mt-3">
        <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          Steps
        </div>
        <ol className="space-y-1 text-[13px]">
          {method.steps.map((step, i) => (
            <li key={step} className="flex gap-2">
              <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-[var(--surface)] text-[10px] font-semibold tabular-nums">
                {i + 1}
              </span>
              <span>{step}</span>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
