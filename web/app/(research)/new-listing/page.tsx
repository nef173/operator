"use client";

import { useState } from "react";
import { api, ApiError, type ListingMethod } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { useStore } from "@/components/StoreProvider";
import { Card, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState } from "@/components/PageState";
import { JobRunner } from "@/components/JobRunner";
import { DetailModal } from "@/components/DetailModal";

// For Branded Listing the operator can start from a queue candidate/keyword
// (slug-driven build) OR paste one product link. The two paths map to different job
// specs / arg keys; General Listing is always a single URL.
type StartFrom = "slug" | "url";

export default function NewListingPage() {
  const { data, error, loading } = useApi(() => api.listingMethods());
  const { store, setStore, stores } = useStore();
  const [selected, setSelected] = useState<string | null>(null);
  const [argValue, setArgValue] = useState("");
  const [startFrom, setStartFrom] = useState<StartFrom>("slug");
  const [bulkOpen, setBulkOpen] = useState(false);

  const methods = data?.listing_methods ?? [];
  const active = methods.find((m) => m.id === selected) ?? null;
  const activeStore = store || null;

  // General Listing is always a single product URL. The research pipeline (Branded
  // General Import) can take a category slug OR a single product link — operator picks.
  const isSourceImport = active?.id === "source-import";
  const canPickStart = active?.id === "research-pipeline";
  const inputMode: StartFrom = isSourceImport ? "url" : startFrom;

  // Resolve the job spec + arg key for the SINGLE import given the chosen start mode.
  // General Listing → its own spec (url). Branded Listing → build-listing (slug)
  // by default, or general-import-url (url) when the operator pastes a link.
  const singleSpec =
    isSourceImport || inputMode === "url"
      ? isSourceImport
        ? active?.job_spec ?? "source-import"
        : "general-import-url"
      : active?.job_spec ?? "build-listing";
  const argKey: StartFrom = inputMode;
  const argPlaceholder =
    inputMode === "url"
      ? "Product link (AliExpress / Temu / Shopify / …)"
      : "Category slug (e.g. dog-cooling-mat)";

  // The bulk import always works off pasted links (one job per line), so it uses the
  // URL-driven spec for whichever method is active.
  const bulkSpec = isSourceImport ? "source-import" : "general-import-url";

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader title="New Listing" />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          <div className="grid gap-4 md:grid-cols-2">
            {methods.map((m) => (
              <MethodCard
                key={m.id}
                method={m}
                selected={selected === m.id}
                onSelect={() => {
                  setSelected(m.id);
                  setArgValue("");
                  setStartFrom(m.id === "research-pipeline" ? "slug" : "url");
                }}
              />
            ))}
          </div>

          {active ? (
            <Card className="mt-6 p-6">
              <div className="flex items-center gap-2">
                <h2>{active.name}</h2>
                <Pill>{active.engine}</Pill>
              </div>
              <p className="mt-1 text-sm text-[var(--muted)]">{active.best_for}</p>

              <div className="mt-5 grid gap-6 sm:grid-cols-2">
                <div>
                  <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                    You provide
                  </div>
                  <ul className="space-y-1.5 text-sm">
                    {active.inputs.map((inp) => (
                      <li key={inp} className="flex items-center gap-2">
                        <span className="h-1.5 w-1.5 rounded-full bg-[var(--accent)]" />
                        {inp}
                      </li>
                    ))}
                  </ul>
                </div>
                <div>
                  <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                    Steps
                  </div>
                  <ol className="space-y-1.5 text-sm">
                    {active.steps.map((step, i) => (
                      <li key={step} className="flex gap-2.5">
                        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-[var(--surface-2)] text-xs font-semibold tabular-nums">
                          {i + 1}
                        </span>
                        <span>{step}</span>
                      </li>
                    ))}
                  </ol>
                </div>
              </div>

              <div className="mt-6 flex flex-col gap-3">
                {/* "Normal listing" (General Listing) runs in the STANDALONE listing app — it owns
                    the per-step view incl. "fix bad images". Open it instead of the in-app runner. */}
                {active.external_app ? (
                  <ExternalAppPanel method={active} />
                ) : (
                <>
                {/* Branded Listing lets the operator choose the starting point. */}
                {canPickStart ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
                      Start from
                    </span>
                    <StartToggle
                      value={startFrom}
                      onChange={(v) => {
                        setStartFrom(v);
                        setArgValue("");
                      }}
                    />
                  </div>
                ) : null}

                <div className="flex flex-wrap gap-3">
                  {stores.length > 1 ? (
                    <select
                      aria-label="Store"
                      value={activeStore ?? ""}
                      onChange={(e) => setStore(e.target.value)}
                      className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-3 py-2 text-sm"
                    >
                      {stores.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  ) : null}
                  {active.job_spec ? (
                    <input
                      type="text"
                      value={argValue}
                      onChange={(e) => setArgValue(e.target.value)}
                      placeholder={argPlaceholder}
                      className="flex-1 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm sm:max-w-md"
                    />
                  ) : null}
                </div>
                {active.job_spec ? (
                  <div className="flex flex-wrap items-center gap-3">
                    <JobRunner
                      spec={singleSpec}
                      store={activeStore}
                      args={argValue ? { [argKey]: argValue } : undefined}
                      label={`Start ${active.name}`}
                    />
                    <button
                      type="button"
                      onClick={() => setBulkOpen(true)}
                      className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-4 py-2 text-sm font-semibold text-[var(--text)] hover:border-[var(--accent)]"
                    >
                      Bulk import
                    </button>
                  </div>
                ) : null}
                </>
                )}
              </div>
            </Card>
          ) : (
            <p className="mt-5 text-sm text-[var(--muted)]">
              Select a method above to see its inputs and steps.
            </p>
          )}

          {active ? (
            <BulkImportModal
              open={bulkOpen}
              method={active}
              spec={bulkSpec}
              store={activeStore}
              onClose={() => setBulkOpen(false)}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function StartToggle({
  value,
  onChange,
}: {
  value: StartFrom;
  onChange: (v: StartFrom) => void;
}) {
  const opts: { id: StartFrom; label: string }[] = [
    { id: "slug", label: "Candidate / keyword" },
    { id: "url", label: "Single link" },
  ];
  return (
    <div className="inline-flex rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-0.5">
      {opts.map((o) => (
        <button
          key={o.id}
          type="button"
          onClick={() => onChange(o.id)}
          className={`rounded-md px-3 py-1.5 text-xs font-semibold transition-colors ${
            value === o.id
              ? "bg-[var(--accent)] text-[var(--accent-fg)]"
              : "text-[var(--muted)] hover:text-[var(--text)]"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// The "normal listing" method (General Listing) is handled by the standalone listing app — the
// same project's listing engine where the operator walks every step, including the "fix bad
// images" image-QA step they want to validate. It is EMBEDDED in-app (loaded below, in an
// iframe) rather than opened in a new tab — so it stays inside our operation, not a hop out to
// a separate deployment. The two apps share data later. Branded Listing stays in-app too.
function ExternalAppPanel({ method }: { method: ListingMethod }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold">Runs in the Listing App</div>
          <p className="mt-1 text-sm text-[var(--muted)]">
            {method.external_note ??
              "Loads the listing app below — walk the steps here (incl. \u201cfix bad images\u201d)."}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="shrink-0 rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-semibold text-[var(--accent-fg)] hover:opacity-90"
        >
          {open ? "Hide Listing App" : "Open Listing App"}
        </button>
      </div>
      {open ? (
        <div className="mt-4 overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface)]">
          <iframe
            src={method.external_app}
            title="Listing App"
            className="h-[75vh] w-full border-0"
            sandbox="allow-scripts allow-forms allow-same-origin allow-popups"
          />
        </div>
      ) : null}
    </div>
  );
}

// Bulk import — paste multiple product links (one per line); on submit we kick off one
// import job per link via the same single-import mechanism (POST /api/jobs/bulk).
function BulkImportModal({
  open,
  method,
  spec,
  store,
  onClose,
}: {
  open: boolean;
  method: ListingMethod;
  spec: string;
  store: string | null;
  onClose: () => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<number | null>(null);

  const links = text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

  async function submit() {
    if (!store) {
      setError("No store selected");
      return;
    }
    if (links.length === 0) {
      setError("Paste at least one product link");
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.createBulkJobs(spec, store, links, "url");
      setResult(res.count);
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.message
          : e instanceof Error
            ? e.message
            : "Bulk import failed",
      );
    } finally {
      setBusy(false);
    }
  }

  function close() {
    setText("");
    setError(null);
    setResult(null);
    onClose();
  }

  return (
    <DetailModal
      open={open}
      title={`Bulk import — ${method.name}`}
      subtitle="Paste multiple product links (one per line). Each starts its own import job."
      onClose={close}
      footer={
        <>
          <button
            type="button"
            onClick={close}
            disabled={busy}
            className="rounded-lg border border-[var(--border)] px-3 py-1.5 text-sm font-medium text-[var(--muted)] hover:bg-[var(--surface-2)] disabled:opacity-50"
          >
            {result != null ? "Close" : "Cancel"}
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={busy || links.length === 0}
            className="rounded-lg px-3 py-1.5 text-sm font-semibold disabled:opacity-60"
            style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
          >
            {busy
              ? "Starting…"
              : `Start ${links.length || ""} import${links.length === 1 ? "" : "s"}`.trim()}
          </button>
        </>
      }
    >
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={8}
        placeholder={"https://www.aliexpress.com/item/...\nhttps://www.temu.com/...\nhttps://www.example.com/products/..."}
        className="w-full resize-y rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 font-mono text-xs leading-relaxed"
      />
      <div className="mt-2 flex items-center justify-between text-xs text-[var(--muted)]">
        <span>
          {links.length} link{links.length === 1 ? "" : "s"} · destination{" "}
          <span className="font-medium text-[var(--text)]">{store ?? "—"}</span>
        </span>
      </div>

      {result != null ? (
        <div
          className="mt-3 rounded-lg px-3 py-2 text-xs"
          style={{
            color: "var(--state-winner)",
            background: "color-mix(in srgb, var(--state-winner) 12%, transparent)",
          }}
        >
          Started {result} import job{result === 1 ? "" : "s"}. Track them in Jobs / Activity.
        </div>
      ) : null}

      {error ? (
        <div
          className="mt-3 rounded-lg px-3 py-2 text-xs"
          style={{
            color: "var(--state-killed)",
            background: "color-mix(in srgb, var(--state-killed) 12%, transparent)",
          }}
        >
          {error}
        </div>
      ) : null}
    </DetailModal>
  );
}

function MethodCard({
  method,
  selected,
  onSelect,
}: {
  method: ListingMethod;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button onClick={onSelect} className="text-left">
      <Card
        className={`h-full p-5 transition-colors ${
          selected
            ? "border-[var(--accent)] ring-1 ring-[var(--ring)]"
            : "hover:border-[var(--accent)]"
        }`}
      >
        <div className="flex items-center justify-between gap-2">
          <div className="text-lg font-semibold">{method.name}</div>
          <span
            className={`flex h-5 w-5 items-center justify-center rounded-full border ${
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
        <p className="mt-2 text-sm text-[var(--muted)]">{method.tagline}</p>
        <div className="mt-3">
          <Pill>{method.engine}</Pill>
        </div>
      </Card>
    </button>
  );
}
