"use client";

import { Fragment, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useStore } from "@/components/StoreProvider";
import { useApi } from "@/lib/useApi";
import { Card, Pill } from "@/components/ui";
import { PageHeader, Loading, ErrorState, Empty } from "@/components/PageState";
import type { PmUploadResult, PmInvoiceLine, PmBackfillResult } from "@/lib/api";

// Supplier-invoice manager — drag/drop (or browse) one OR MANY supplier .xlsx/.csv files; each file
// auto-routes to its store by filename and its supplier is auto-detected, then the backend parses →
// EUR → per-variant COGS (bumping the store's cogs_version so the P&L + Product Performance reflect
// the new cost). Reused by both Product Management → Invoices and Product Optimization → Invoices.

function InvoiceLines({ invoiceId }: { invoiceId: number }) {
  const det = useApi(() => api.pmInvoiceDetail(invoiceId), [invoiceId]);
  if (det.loading) return <div className="px-4 py-3 text-xs text-[var(--muted)]">Loading lines…</div>;
  if (det.error) return <div className="px-4 py-3 text-xs text-[var(--danger)]">{det.error}</div>;
  const lines = det.data?.lines ?? [];
  const unresolved = lines.filter((l) => l.resolve_status !== "resolved" && l.line_type === "charge");
  return (
    <div className="px-4 py-3">
      {unresolved.length > 0 ? (
        <div className="mb-2 text-xs" style={{ color: "var(--warn,#d97706)" }}>
          {unresolved.length} line{unresolved.length === 1 ? "" : "s"} still unresolved — their COGS
          isn&apos;t attributed to a day until the order date resolves (try &ldquo;Re-resolve dates&rdquo;).
        </div>
      ) : null}
      <table className="w-full text-xs">
        <thead>
          <tr className="text-left uppercase tracking-wide text-[var(--muted)]">
            <th className="py-1 pr-3 font-medium">Order</th>
            <th className="py-1 pr-3 font-medium">Date</th>
            <th className="py-1 pr-3 font-medium">SKU</th>
            <th className="py-1 pr-3 font-medium">Title</th>
            <th className="py-1 pr-3 text-right font-medium">Qty</th>
            <th className="py-1 pr-3 text-right font-medium">Cost €</th>
            <th className="py-1 pr-3 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {lines.slice(0, 300).map((l: PmInvoiceLine) => (
            <tr key={l.id} className="border-t border-[var(--border)]">
              <td className="py-1 pr-3">{l.order_no || "—"}</td>
              <td className="py-1 pr-3 text-[var(--muted)]">{l.order_date || "—"}</td>
              <td className="py-1 pr-3 text-[var(--muted)]">{l.sku || "—"}</td>
              <td className="max-w-[240px] truncate py-1 pr-3">{l.title || "—"}</td>
              <td className="py-1 pr-3 text-right tabular-nums">{l.qty}</td>
              <td className="py-1 pr-3 text-right tabular-nums" style={{ color: l.line_type === "refund" ? "var(--state-killed)" : undefined }}>
                {l.line_type === "refund" ? `-${(l.refund_amount_eur ?? 0).toFixed(2)}` : (l.bill_cost_eur ?? 0).toFixed(2)}
              </td>
              <td className="py-1 pr-3">
                {l.resolve_status === "resolved" ? (
                  <span className="text-[var(--muted)]">resolved</span>
                ) : (
                  <span style={{ color: "var(--warn,#d97706)" }}>{l.resolve_status || "pending"}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {lines.length > 300 ? <div className="mt-1 text-[10px] text-[var(--muted)]">Showing first 300 of {lines.length} lines.</div> : null}
    </div>
  );
}

function eur(v: number | null | undefined) {
  if (v == null) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "EUR" }).format(v);
}

function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const res = reader.result as string;
      resolve(res.slice(res.indexOf(",") + 1)); // strip "data:...;base64," prefix
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

type FileResult = PmUploadResult & { _file: string; _store: string; _autoRouted: boolean; _err?: string };

export function InvoiceManager() {
  const { store, ready } = useStore();
  const list = useApi(() => api.pmInvoices(store), [store]);
  const fileRef = useRef<HTMLInputElement>(null);

  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [results, setResults] = useState<FileResult[]>([]);
  const [backfilling, setBackfilling] = useState(false);
  const [backfill, setBackfill] = useState<PmBackfillResult | null>(null);
  const [openInv, setOpenInv] = useState<number | null>(null);

  async function handleFiles(files: File[]) {
    const list_ = files.filter((f) => /\.(csv|xlsx|xls)$/i.test(f.name));
    if (!list_.length) {
      if (files.length) setErr("Only .csv / .xlsx / .xls invoice files are supported.");
      return;
    }
    setUploading(true);
    setErr(null);
    setResults([]);
    const acc: FileResult[] = [];
    let anyResolving = false;
    for (const file of list_) {
      try {
        const routed = (await api.invoicesResolveStore(file.name).catch(() => null))?.store || store;
        const b64 = await readAsBase64(file);
        const res = await api.pmInvoiceUpload(routed, file.name, b64);
        acc.push({ ...res, _file: file.name, _store: routed, _autoRouted: routed !== store, _err: res.ok ? undefined : (res.error || "failed") });
        if (res.resolving) anyResolving = true;
      } catch (e) {
        acc.push({ ok: false, _file: file.name, _store: store, _autoRouted: false, _err: e instanceof Error ? e.message : String(e) });
      }
      setResults([...acc]);
      list.reload();
    }
    if (anyResolving) {
      for (let i = 0; i < 8; i++) {
        await new Promise((r) => setTimeout(r, 8000));
        list.reload();
      }
    }
    setUploading(false);
    if (fileRef.current) fileRef.current.value = "";
  }

  async function runBackfill() {
    setBackfilling(true);
    setErr(null);
    setBackfill(null);
    try {
      const res = await api.pmBackfill(store);
      if (!res.ok) throw new Error(res.error || "Backfill failed");
      if (res.running) {
        for (let i = 0; i < 8; i++) {
          await new Promise((r) => setTimeout(r, 8000));
          list.reload();
        }
      } else {
        setBackfill(res);
        list.reload();
      }
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : String(e2));
    } finally {
      setBackfilling(false);
    }
  }

  async function remove(id: number) {
    setErr(null);
    try {
      await api.pmInvoiceDelete(id);
      list.reload();
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : String(e2));
    }
  }

  if (!ready) return <Loading />;
  if (!store) return <Empty>No store selected. Pick a store in the sidebar.</Empty>;

  const invoices = list.data?.invoices ?? [];

  return (
    <div>
      <PageHeader
        title="Supplier invoices"
        subtitle={`Cost source for the P&L — default store ${store}`}
        action={
          <button
            onClick={runBackfill}
            disabled={backfilling}
            title="Re-resolve every unresolved invoice line's Shopify order date (after a scope grant / outage / late order sync)"
            className="rounded-lg border border-[var(--border)] px-3 py-2 text-sm font-medium text-[var(--muted)] disabled:opacity-60"
          >
            {backfilling ? "Re-resolving…" : "Re-resolve dates"}
          </button>
        }
      />

      {/* Drop zone — drag many files or click to browse. */}
      <Card className="mb-4 p-2">
        <input ref={fileRef} type="file" accept=".csv,.xlsx,.xls" multiple onChange={(e) => handleFiles(Array.from(e.target.files || []))} className="hidden" />
        <div
          role="button"
          tabIndex={0}
          onClick={() => !uploading && fileRef.current?.click()}
          onKeyDown={(e) => { if ((e.key === "Enter" || e.key === " ") && !uploading) fileRef.current?.click(); }}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); handleFiles(Array.from(e.dataTransfer.files || [])); }}
          className={`flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors ${dragOver ? "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]" : "border-[var(--border)] hover:bg-[var(--surface-2)]"}`}
        >
          <span className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-[var(--surface-2)]">
            {uploading ? (
              <svg className="animate-spin text-[var(--accent)]" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M21 12a9 9 0 1 1-6.2-8.5" /></svg>
            ) : (
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" /></svg>
            )}
          </span>
          <div className="text-base font-semibold text-[var(--text)]">
            {uploading ? "Uploading…" : <>Drop invoices here, or <span className="text-[var(--accent)]">browse</span></>}
          </div>
          <div className="mt-1 max-w-lg text-xs text-[var(--muted)]">
            Supplier .xlsx / .csv — one or many, processed in order. Filenames like{" "}
            <code className="rounded bg-[var(--surface-2)] px-1">CJ-yourshop.com-2026-06-18.xlsx</code>{" "}
            auto-match the store; the supplier is auto-detected.
          </div>
        </div>
      </Card>

      {err ? <div className="mb-4"><ErrorState message={err} /></div> : null}

      {/* Per-file results (multi-file, auto-routed) */}
      {results.length ? (
        <Card className="mb-4 p-4">
          <div className="mb-2 text-sm font-semibold">Uploaded {results.length} file{results.length === 1 ? "" : "s"}</div>
          <div className="divide-y divide-[var(--border)]">
            {results.map((r, i) => (
              <div key={i} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                <span className="flex min-w-0 items-center gap-2">
                  <Pill tone={r._err ? "warn" : r.duplicate ? "warn" : r.resolving ? "info" : "ok"}>
                    {r._err ? "Failed" : r.duplicate ? "Duplicate" : r.resolving ? "Resolving" : "Done"}
                  </Pill>
                  <span className="truncate font-medium">{r._file}</span>
                </span>
                <span className="flex flex-wrap items-center gap-x-3 text-xs text-[var(--muted)]">
                  <span>→ <span className="font-semibold text-[var(--text)]">{r._store}</span>{r._autoRouted ? <span className="ml-1 rounded bg-[color-mix(in_srgb,var(--accent)_14%,transparent)] px-1 text-[10px] font-semibold text-[var(--accent)]">auto-routed</span> : null}</span>
                  {r.supplier ? <span>supplier <span className="font-semibold text-[var(--text)]">{r.supplier}</span></span> : null}
                  {r._err ? <span className="text-[var(--state-killed)]">{r._err}</span> : <span>{r.line_count ?? 0} lines · {eur(r.total_eur)}</span>}
                  {r.cross_store_hints && Object.keys(r.cross_store_hints).length > 0 ? (
                    <span style={{ color: "var(--warn,#d97706)" }}>⚠ orders live on {Object.entries(r.cross_store_hints).sort((a, b) => b[1] - a[1]).map(([s, n]) => `${s} (${n})`).join(", ")}</span>
                  ) : null}
                </span>
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      {backfill ? (
        <Card className="mb-4 p-4">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Pill tone={backfill.unresolved ? "warn" : "ok"}>Re-resolved</Pill>
            <span className="text-[var(--muted)]">{backfill.scanned} scanned · {backfill.resolved} resolved · {backfill.unresolved} still unresolved</span>
          </div>
        </Card>
      ) : null}

      <Card className="p-5">
        <div className="mb-3 text-sm font-semibold">Uploaded invoices · {store}</div>
        {list.loading ? (
          <div className="text-sm text-[var(--muted)]">Loading…</div>
        ) : list.error ? (
          <ErrorState message={list.error} />
        ) : invoices.length === 0 ? (
          <div className="text-sm text-[var(--muted)]">No invoices yet for this store. Drop a supplier CSV or XLSX above to establish real COGS.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--border)] text-left text-xs uppercase tracking-wide text-[var(--muted)]">
                  <th className="py-2 pr-4 font-medium">File</th>
                  <th className="py-2 pr-4 font-medium">Supplier</th>
                  <th className="py-2 pr-4 text-right font-medium">Lines</th>
                  <th className="py-2 pr-4 text-right font-medium">Total</th>
                  <th className="py-2 pr-4 text-right font-medium">Refunds</th>
                  <th className="py-2 pr-4 font-medium">Uploaded</th>
                  <th className="py-2 pr-4"></th>
                </tr>
              </thead>
              <tbody>
                {invoices.map((inv) => (
                  <Fragment key={inv.id}>
                    <tr className="border-b border-[var(--border)] last:border-0">
                      <td className="py-2 pr-4 font-medium">
                        <button onClick={() => setOpenInv(openInv === inv.id ? null : inv.id)} className="hover:underline" title="Show invoice lines">
                          {openInv === inv.id ? "▾ " : "▸ "}{inv.filename ?? `#${inv.id}`}
                        </button>
                      </td>
                      <td className="py-2 pr-4 text-[var(--muted)]">{inv.supplier ?? "—"}</td>
                      <td className="py-2 pr-4 text-right text-[var(--muted)]">{inv.line_count}</td>
                      <td className="py-2 pr-4 text-right">{eur(inv.total_eur)}</td>
                      <td className="py-2 pr-4 text-right text-[var(--muted)]">{inv.refund_eur ? eur(inv.refund_eur) : "—"}</td>
                      <td className="py-2 pr-4 text-[var(--muted)]">{inv.uploaded_at?.slice(0, 10)}</td>
                      <td className="py-2 pr-4 text-right">
                        <button onClick={() => remove(inv.id)} className="rounded-md border border-[var(--border)] px-2 py-1 text-xs text-[var(--muted)] hover:text-[var(--danger)]">Delete</button>
                      </td>
                    </tr>
                    {openInv === inv.id ? (
                      <tr><td colSpan={7} className="bg-[var(--surface)] p-0"><InvoiceLines invoiceId={inv.id} /></td></tr>
                    ) : null}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
