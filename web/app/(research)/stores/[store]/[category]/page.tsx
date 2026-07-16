"use client";

import { use, useState } from "react";
import Link from "next/link";
import {
  api,
  type CategorySku,
  type DedupReport,
  type SkuState,
  type ImageQaReport,
  type ImageQaRow,
  type ImageQaVerdict,
  type ImageQaFixKind,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { Card, Stat, StateBadge, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { sanitizeHtml } from "@/lib/sanitize";

function money(n: number | null) {
  return n == null ? "—" : `$${n.toFixed(2)}`;
}

// The listing state machine's forward path + the two terminal outcomes. From any
// active state the operator can also kill; winner/live are reachable as the SKU graduates.
const NEXT_STATES: Record<string, SkuState[]> = {
  candidate: ["keyword-clustered", "killed"],
  "keyword-clustered": ["drafted", "candidate", "killed"],
  drafted: ["live", "keyword-clustered", "killed"],
  live: ["testing", "killed"],
  testing: ["winner", "killed"],
  winner: ["live", "killed"],
  killed: ["candidate"],
};

function nextStatesFor(state: SkuState | null): SkuState[] {
  if (!state) return ["candidate"];
  return NEXT_STATES[state] ?? [];
}

export default function CategoryDetailPage({
  params,
}: {
  params: Promise<{ store: string; category: string }>;
}) {
  const { store, category } = use(params);
  const { data, error, loading, reload } = useApi(
    () => api.storeCategory(store, category),
    [store, category],
  );

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title={data?.keyword ?? category.replace(/-/g, " ")}
        subtitle={
          data
            ? [data.product_type, data.vendor, data.category_fullname]
                .filter(Boolean)
                .join(" · ")
            : "Category"
        }
        action={
          <Link
            href={`/stores/${store}`}
            className="rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-medium text-[var(--muted)] hover:text-[var(--text)]"
          >
            ← {store}
          </Link>
        }
      />

      {loading ? <Loading /> : null}
      {error ? <ErrorState message={error} /> : null}

      {data ? (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <Stat label="Search vol" value={data.sv != null ? data.sv.toLocaleString() : "—"} />
            <Stat label="Bucket" value={data.capture_bucket ?? "—"} />
            <Stat label="Built SKUs" value={data.skus.length} hint={`${data.n_spec_skus} in spec`} />
            <Stat
              label="Subject"
              value={<span className="capitalize">{data.subject ?? "—"}</span>}
            />
            <Stat
              label="Dedup flags"
              value={data.dedup?.n_violations ?? 0}
              hint={data.dedup ? `${data.dedup.n_clusters} near-dup clusters` : "no report"}
            />
          </div>

          {data.docs.length > 0 ? (
            <Card className="mt-6 overflow-hidden">
              <div className="border-b border-[var(--border)] px-5 py-3 text-sm font-semibold">
                Competitive recon
              </div>
              {data.docs.map((doc) => (
                <ReconDoc key={doc} store={store} slug={data.slug} name={doc} />
              ))}
            </Card>
          ) : null}

          {data.dedup ? <DedupCard dedup={data.dedup} /> : null}

          <ImageQaPanel store={store} slug={data.slug} />

          <h2 className="mb-3 mt-8">SKUs ({data.skus.length})</h2>
          <div className="space-y-4">
            {data.skus.map((s) => (
              <SkuCard key={s.id} store={store} slug={data.slug} s={s} onChange={reload} />
            ))}
          </div>
          {data.skus.length === 0 ? (
            <Empty>No SKU folders built for this category yet.</Empty>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

// Lazy markdown doc (plain-text preview, no markdown lib) from a category folder.
function ReconDoc({ store, slug, name }: { store: string; slug: string; name: string }) {
  const [text, setText] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    if (text != null || loading) return;
    setLoading(true);
    setErr(null);
    try {
      const res = await fetch(api.storeFileUrl(store, `${slug}/${name}`), {
        cache: "no-store",
      });
      if (!res.ok) throw new Error(`${res.status}`);
      setText(await res.text());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <details
      className="group border-t border-[var(--border)] first:border-t-0"
      onToggle={(e) => {
        if ((e.target as HTMLDetailsElement).open) load();
      }}
    >
      <summary className="flex cursor-pointer list-none items-center gap-2.5 px-5 py-3.5 hover:bg-[var(--surface-2)]">
        <span className="text-[var(--muted)] transition-transform group-open:rotate-90">›</span>
        <span className="font-mono text-sm">{name}</span>
      </summary>
      <div className="px-5 pb-4">
        {loading ? (
          <div className="text-sm text-[var(--muted)]">Loading…</div>
        ) : err ? (
          <div className="text-sm text-[var(--state-killed)]">Couldn’t load ({err})</div>
        ) : text != null ? (
          <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--surface-2)] p-4 font-mono text-xs leading-relaxed text-[var(--text)]">
            {text}
          </pre>
        ) : null}
      </div>
    </details>
  );
}

function DedupCard({ dedup }: { dedup: DedupReport }) {
  return (
    <Card className="mt-4 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div className="text-sm font-semibold">Vision dedup</div>
        <div className="text-xs text-[var(--muted)]">
          {dedup.skus_scanned ?? "—"} SKUs · {dedup.images_hashed ?? "—"} images hashed · max{" "}
          {dedup.max_per_product ?? "—"}/product
        </div>
      </div>
      {dedup.clusters.length === 0 ? (
        <div className="mt-3 text-sm text-[var(--muted)]">No near-duplicate clusters.</div>
      ) : (
        <div className="mt-3 flex flex-wrap gap-2">
          {dedup.clusters.map((c, i) => (
            <span
              key={i}
              className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs"
              style={{
                color: c.violation ? "var(--state-killed)" : "var(--muted)",
                background: c.violation
                  ? "color-mix(in srgb, var(--state-killed) 14%, transparent)"
                  : "var(--surface-2)",
              }}
              title={c.violation ? "Exceeds max-per-product — review" : "Within allowed reuse"}
            >
              {c.violation ? "⚠ " : ""}
              {c.skus.join(" · ")} ({c.size})
            </span>
          ))}
        </div>
      )}
    </Card>
  );
}

function SkuCard({
  store,
  slug,
  s,
  onChange,
}: {
  store: string;
  slug: string;
  s: CategorySku;
  onChange: () => void;
}) {
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--border)] px-5 py-4">
        <div className="min-w-0">
          <div className="font-medium leading-snug">{s.title ?? s.id}</div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs text-[var(--muted)]">{s.id}</span>
            {s.state ? <StateBadge state={s.state} /> : null}
            {!s.has_spec ? (
              <span className="text-[11px] uppercase text-[var(--state-killed)]">no spec</span>
            ) : null}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-4 text-sm tabular-nums">
          <div className="text-right">
            <div className="text-[11px] uppercase text-[var(--muted)]">Cost</div>
            <div>{money(s.cogs)}</div>
          </div>
          <div className="text-right">
            <div className="text-[11px] uppercase text-[var(--muted)]">Price</div>
            <div>{money(s.price)}</div>
          </div>
          <StateControl store={store} slug={slug} s={s} onChange={onChange} />
        </div>
      </div>

      {/* Generated gallery */}
      {s.images.length > 0 ? (
        <div className="flex gap-3 overflow-x-auto px-5 py-4">
          {s.images.map((img) => (
            <figure key={img.path} className="shrink-0">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={api.storeImageUrl(store, img.path)}
                alt={img.role ?? img.file}
                className="h-28 w-28 rounded-lg border border-[var(--border)] bg-[var(--surface-2)] object-cover"
                loading="lazy"
              />
              {img.role ? (
                <figcaption className="mt-1 text-center text-[11px] capitalize text-[var(--muted)]">
                  {img.role}
                </figcaption>
              ) : null}
            </figure>
          ))}
        </div>
      ) : (
        <div className="px-5 py-4 text-sm text-[var(--muted)]">No generated images yet.</div>
      )}

      {/* Variants + tags + supplier refs */}
      <div className="grid gap-4 px-5 pb-4 sm:grid-cols-2">
        <div>
          <div className="mb-1.5 text-[11px] uppercase tracking-wide text-[var(--muted)]">
            Variants ({s.variants.length})
          </div>
          {s.variants.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {s.variants.map((v, i) => (
                <span
                  key={i}
                  className="inline-flex items-center gap-1.5 rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-2 py-0.5 text-xs"
                >
                  <span>{v.size ?? "—"}</span>
                  {v.price ? (
                    <span className="font-semibold tabular-nums text-[var(--accent)]">
                      ${v.price}
                    </span>
                  ) : null}
                </span>
              ))}
            </div>
          ) : (
            <div className="text-xs text-[var(--muted)]">—</div>
          )}
        </div>
        <div>
          <div className="mb-1.5 text-[11px] uppercase tracking-wide text-[var(--muted)]">
            Tags · supplier refs
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {s.tags.map((t) => (
              <Pill key={t}>{t}</Pill>
            ))}
            <span
              className="inline-flex items-center gap-1 text-xs text-[var(--muted)]"
              title={s.supplier_refs.manifest ? "Has _manifest.txt" : "No manifest"}
            >
              {s.supplier_refs.count} supplier ref{s.supplier_refs.count === 1 ? "" : "s"}
              {s.supplier_refs.manifest ? " ✓" : ""}
            </span>
          </div>
        </div>
      </div>

      {/* SEO + body copy preview */}
      {s.seo_title || s.body_html ? (
        <details className="group border-t border-[var(--border)]">
          <summary className="flex cursor-pointer list-none items-center gap-2.5 px-5 py-3 text-sm hover:bg-[var(--surface-2)]">
            <span className="text-[var(--muted)] transition-transform group-open:rotate-90">›</span>
            <span className="font-medium">PDP copy & SEO</span>
          </summary>
          <div className="space-y-3 px-5 pb-4">
            {s.seo_title ? (
              <div>
                <div className="text-[11px] uppercase text-[var(--muted)]">SEO title</div>
                <div className="text-sm">{s.seo_title}</div>
              </div>
            ) : null}
            {s.seo_description ? (
              <div>
                <div className="text-[11px] uppercase text-[var(--muted)]">SEO description</div>
                <div className="text-sm text-[var(--muted)]">{s.seo_description}</div>
              </div>
            ) : null}
            {s.body_html ? (
              <div>
                <div className="mb-1 text-[11px] uppercase text-[var(--muted)]">Body HTML</div>
                <div
                  className="prose-sm max-w-none rounded-lg bg-[var(--surface-2)] p-4 text-sm leading-relaxed [&_strong]:font-semibold [&_p]:mb-2"
                  dangerouslySetInnerHTML={{ __html: sanitizeHtml(s.body_html) }}
                />
              </div>
            ) : null}
          </div>
        </details>
      ) : null}
    </Card>
  );
}

// Advance a SKU through the listing state machine. Picks a valid next state, confirms
// (the locked "confirm each write" rule), POSTs, then reloads the category truth.
function StateControl({
  store,
  slug,
  s,
  onChange,
}: {
  store: string;
  slug: string;
  s: CategorySku;
  onChange: () => void;
}) {
  const [target, setTarget] = useState<SkuState | null>(null);
  const options = nextStatesFor(s.state);

  if (options.length === 0) {
    return null;
  }

  return (
    <>
      <select
        aria-label={`Advance ${s.id}`}
        value=""
        onChange={(e) => {
          const v = e.target.value as SkuState;
          if (v) setTarget(v);
        }}
        className="rounded-lg border border-[var(--border)] bg-[var(--surface-2)] px-2 py-1.5 text-xs font-medium hover:border-[var(--accent)]"
      >
        <option value="" disabled>
          Set state…
        </option>
        {options.map((st) => (
          <option key={st} value={st}>
            → {st}
          </option>
        ))}
      </select>

      <ConfirmDialog
        open={target != null}
        title="Change SKU state"
        tone={target === "killed" ? "danger" : "accent"}
        confirmLabel={target ? `Set ${target}` : "Set"}
        body={
          <span>
            Move <span className="font-mono">{s.id}</span> from{" "}
            <span className="font-semibold">{s.state ?? "—"}</span> to{" "}
            <span className="font-semibold">{target}</span>?
            <br />
            This writes to the live listing queue.
          </span>
        }
        onConfirm={async () => {
          if (target) await api.setSkuState(store, slug, s.id, target);
        }}
        onDone={onChange}
        onClose={() => setTarget(null)}
      />
    </>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// VisionScan image-QA gate — operator-review-FIRST. Run the scan → the AI proposes
// a per-image verdict (PASS / FIX / REJECT) over the actual thumbnails; the operator
// changes any verdict, then Applies. Nothing commits until Apply. Go-live is
// AUTO-CLEAN-OVER-BLOCK: only a REJECT blocks; removable overlays are queued for a
// free local clean (no AI regen).
const VERDICT_TONE: Record<ImageQaVerdict, string> = {
  PASS: "var(--state-winner, #2a9d63)",
  FIX: "var(--state-testing, #b7791f)",
  REJECT: "var(--state-killed)",
};
const FIX_KINDS: ImageQaFixKind[] = ["auto-clean", "upscale", "language-rewrite", "regen", "resource"];
const FIX_LABEL: Record<ImageQaFixKind, string> = {
  "auto-clean": "auto-clean (free local strip)",
  upscale: "upscale (free local 1024²)",
  "language-rewrite": "language rewrite",
  regen: "regen (supplier-ref)",
  resource: "re-source product",
};

function rowKey(r: ImageQaRow) {
  return `${r.sku ?? ""}::${r.file ?? r.path ?? ""}`;
}

function flattenRows(report: ImageQaReport): ImageQaRow[] {
  if (report.skus?.length) return report.skus.flatMap((s) => s.images);
  return report.images ?? [];
}

function ImageQaPanel({ store, slug }: { store: string; slug: string }) {
  const [report, setReport] = useState<ImageQaReport | null>(null);
  const [overrides, setOverrides] = useState<
    Record<string, { verdict: ImageQaVerdict; fix_kind: ImageQaFixKind | null }>
  >({});
  const [scanning, setScanning] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState<ImageQaReport | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function runScan() {
    setScanning(true);
    setErr(null);
    setApplied(null);
    setOverrides({});
    try {
      setReport(await api.imageQaScan(store, slug));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setScanning(false);
    }
  }

  function effective(r: ImageQaRow): { verdict: ImageQaVerdict; fix_kind: ImageQaFixKind | null; overridden: boolean } {
    const o = overrides[rowKey(r)];
    if (o) return { verdict: o.verdict, fix_kind: o.fix_kind, overridden: true };
    return { verdict: r.verdict, fix_kind: r.fix_kind, overridden: false };
  }

  function setVerdict(r: ImageQaRow, verdict: ImageQaVerdict) {
    const fk =
      verdict === "FIX"
        ? (r.fix_kind && r.fix_kind !== "resource" ? r.fix_kind : "auto-clean")
        : verdict === "REJECT"
        ? "resource"
        : null;
    setOverrides((m) => ({ ...m, [rowKey(r)]: { verdict, fix_kind: fk } }));
  }

  function setFixKind(r: ImageQaRow, fix_kind: ImageQaFixKind) {
    setOverrides((m) => ({
      ...m,
      [rowKey(r)]: { verdict: m[rowKey(r)]?.verdict ?? r.verdict, fix_kind },
    }));
  }

  async function apply() {
    if (!report) return;
    setApplying(true);
    setErr(null);
    try {
      const verdicts: ImageQaRow[] = flattenRows(report).map((r) => {
        const o = overrides[rowKey(r)];
        return o ? { ...r, operator_override: { verdict: o.verdict, fix_kind: o.fix_kind } } : r;
      });
      const res = await api.imageQaApply(store, slug, verdicts, report.decision_id ?? null);
      setApplied(res);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setApplying(false);
    }
  }

  const rows = report ? flattenRows(report) : [];
  // Recompute live counts from the operator's current (overridden) verdicts.
  const live = rows.reduce(
    (acc, r) => {
      const v = effective(r).verdict;
      acc[v] += 1;
      if (v === "REJECT") acc.blocks = true;
      return acc;
    },
    { PASS: 0, FIX: 0, REJECT: 0, blocks: false } as {
      PASS: number;
      FIX: number;
      REJECT: number;
      blocks: boolean;
    },
  );

  return (
    <Card className="mt-4 overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-3.5">
        <div>
          <div className="text-sm font-semibold">VisionScan image-QA</div>
          <div className="text-xs text-[var(--muted)]">
            Operator-review-first · AI proposes, you approve/change, then apply
          </div>
        </div>
        <button
          onClick={runScan}
          disabled={scanning}
          className="rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
        >
          {scanning ? "Scanning…" : report ? "Re-scan" : "Run image-QA scan"}
        </button>
      </div>

      {err ? (
        <div className="px-5 py-3 text-sm text-[var(--state-killed)]">{err}</div>
      ) : null}

      {report ? (
        <div className="px-5 py-4">
          {/* gate + model status */}
          <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
            <Pill>{report.summary.total} images</Pill>
            <span style={{ color: VERDICT_TONE.PASS }}>{live.PASS} pass</span>
            <span style={{ color: VERDICT_TONE.FIX }}>{live.FIX} fix</span>
            <span style={{ color: VERDICT_TONE.REJECT }}>{live.REJECT} reject</span>
            <span
              className="ml-auto rounded-md px-2 py-0.5 font-semibold"
              style={{
                color: live.blocks ? "var(--state-killed)" : VERDICT_TONE.PASS,
                background: live.blocks
                  ? "color-mix(in srgb, var(--state-killed) 14%, transparent)"
                  : "color-mix(in srgb, var(--state-winner, #2a9d63) 14%, transparent)",
              }}
            >
              go-live {live.blocks ? "BLOCKED" : "CLEAR"}
            </span>
          </div>

          {!report.model_available ? (
            <div className="mb-3 rounded-lg bg-[var(--surface-2)] p-3 text-xs text-[var(--muted)]">
              The app can&apos;t check the photos itself yet — every image is marked{" "}
              <span className="font-semibold">looks OK, take a look</span>. Copy and run the command
              below yourself to have the AI check the photos, mark each one, then apply. (Add a vision
              key in Settings to have the app do this for you.)
              {report.command ? (
                <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-md bg-[var(--surface)] p-2 font-mono text-[11px] text-[var(--text)]">
                  {report.command}
                </pre>
              ) : null}
            </div>
          ) : null}

          {/* per-image verdict rows */}
          <div className="space-y-2">
            {rows.map((r) => {
              const eff = effective(r);
              return (
                <div
                  key={rowKey(r)}
                  className="flex flex-wrap items-center gap-3 rounded-lg border border-[var(--border)] p-2"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={r.path ? api.storeImageUrl(store, r.path) : ""}
                    alt={r.file ?? ""}
                    className="h-16 w-16 shrink-0 rounded-md border border-[var(--border)] bg-[var(--surface-2)] object-cover"
                    loading="lazy"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-xs text-[var(--muted)]">{r.sku}</span>
                      <span
                        className="rounded px-1.5 py-0.5 text-[10px] uppercase"
                        style={{ background: "var(--surface-2)", color: "var(--muted)" }}
                        title={r.tier === "hero" ? "Hero / Google feed image — strictest" : "Gallery image"}
                      >
                        {r.tier}
                      </span>
                      <span className="font-mono text-[11px] text-[var(--muted)]">{r.file}</span>
                      {eff.overridden ? (
                        <span className="text-[10px] uppercase text-[var(--accent)]">changed</span>
                      ) : null}
                      {r.needs_vision ? (
                        <span className="text-[10px] uppercase text-[var(--state-testing,#b7791f)]">
                          verify
                        </span>
                      ) : null}
                    </div>
                    {r.reasons?.length ? (
                      <div className="mt-0.5 text-xs text-[var(--muted)]">{r.reasons[0]}</div>
                    ) : null}
                    {r.notes ? (
                      <div className="mt-0.5 text-[11px] italic text-[var(--muted)]">“{r.notes}”</div>
                    ) : null}
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    {(["PASS", "FIX", "REJECT"] as ImageQaVerdict[]).map((v) => (
                      <button
                        key={v}
                        onClick={() => setVerdict(r, v)}
                        className="rounded-md px-2 py-1 text-xs font-semibold"
                        style={
                          eff.verdict === v
                            ? { color: "white", background: VERDICT_TONE[v] }
                            : { color: VERDICT_TONE[v], background: "var(--surface-2)" }
                        }
                      >
                        {v}
                      </button>
                    ))}
                    {eff.verdict === "FIX" ? (
                      <select
                        aria-label="Fix kind"
                        value={eff.fix_kind ?? "auto-clean"}
                        onChange={(e) => setFixKind(r, e.target.value as ImageQaFixKind)}
                        className="rounded-md border border-[var(--border)] bg-[var(--surface-2)] px-1.5 py-1 text-[11px]"
                      >
                        {FIX_KINDS.filter((k) => k !== "resource").map((k) => (
                          <option key={k} value={k}>
                            {FIX_LABEL[k]}
                          </option>
                        ))}
                      </select>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              onClick={apply}
              disabled={applying}
              className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            >
              {applying ? "Applying…" : "Apply changes"}
            </button>
            <span className="text-xs text-[var(--muted)]">
              Fix / Remove start the matching repair; auto-clean runs free and never blocks go-live.
            </span>
          </div>

          {applied ? (
            <div className="mt-3 rounded-lg bg-[var(--surface-2)] p-3 text-xs">
              <span className="font-semibold">Applied.</span> go-live{" "}
              <span
                style={{
                  color: applied.go_live.blocks ? "var(--state-killed)" : VERDICT_TONE.PASS,
                }}
              >
                {applied.go_live.verdict}
              </span>{" "}
              · {applied.summary.PASS} pass · {applied.summary.FIX} fix · {applied.summary.REJECT} reject
              {applied.persisted === false ? (
                <span className="text-[var(--state-killed)]"> · report not persisted</span>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : (
        <div className="px-5 py-4 text-sm text-[var(--muted)]">
          Run the scan to get per-image PASS / FIX / REJECT proposals you can review and change
          before the listing goes live.
        </div>
      )}
    </Card>
  );
}
