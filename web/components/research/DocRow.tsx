"use client";

import { useState } from "react";
import { api, type DossierDoc } from "@/lib/api";
import { Pill } from "@/components/ui";

// One markdown doc, lazy-fetched as plain text on first <details> open.
// No markdown library — rendered as readable preformatted text.
export function DocRow({ slug, doc }: { slug: string; doc: DossierDoc }) {
  const [text, setText] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    if (text != null || loading) return;
    setLoading(true);
    setErr(null);
    try {
      const res = await fetch(api.dossierFileUrl(slug, doc.name), {
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
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-5 py-3.5 hover:bg-[var(--surface-2)]">
        <div className="flex items-center gap-2.5">
          <span className="text-[var(--muted)] transition-transform group-open:rotate-90">
            ›
          </span>
          <span className="font-mono text-sm">{doc.name}</span>
        </div>
        <Pill>{doc.kind}</Pill>
      </summary>
      <div className="px-5 pb-4">
        {loading ? (
          <div className="text-sm text-[var(--muted)]">Loading…</div>
        ) : err ? (
          <div className="text-sm text-[var(--state-killed)]">
            Couldn’t load doc ({err})
          </div>
        ) : text != null ? (
          <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--surface-2)] p-4 font-mono text-xs leading-relaxed text-[var(--text)]">
            {text}
          </pre>
        ) : null}
      </div>
    </details>
  );
}
