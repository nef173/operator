"use client";

import Link from "next/link";
import { usePath } from "@/components/PathProvider";
import { PathSelector } from "@/components/PathSelector";
import { PATHS, type PipelineStage } from "@/lib/pipeline";
import { Card } from "@/components/ui";
import { PageHeader } from "@/components/PageState";

// The active path's full waterfall, laid out top-to-bottom: each stage produces
// what the next consumes. The path pre-selector sits on top so the operator can
// switch between the General and Niche pipelines. Built stages open; ads/scale
// show as upcoming so the whole shape is visible, not hidden.
export default function PipelinePage() {
  const { path } = usePath();
  const def = PATHS[path];

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader
        title="Pipeline"
        subtitle="Pick a path, then walk the waterfall — each stage hands its output to the next."
      />

      <div className="mb-6">
        <PathSelector variant="rail" />
      </div>

      <div className="mb-4 flex items-baseline justify-between">
        <h2 className="text-lg font-semibold">{def.label} path</h2>
        <span className="text-xs text-[var(--muted)]">{def.tagline}</span>
      </div>

      <ol className="relative">
        {def.stages.map((s, i) => (
          <StageRow key={s.key} stage={s} last={i === def.stages.length - 1} />
        ))}
      </ol>
    </div>
  );
}

function StageRow({ stage, last }: { stage: PipelineStage; last: boolean }) {
  const built = stage.href != null;

  const card = (
    <Card
      className={`flex-1 p-5 transition-colors ${
        built ? "hover:border-[var(--accent)]" : "opacity-70"
      }`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-lg font-semibold">{stage.label}</div>
        {built ? (
          <span className="text-xs font-semibold text-[var(--accent)]">Open →</span>
        ) : (
          <span className="rounded-md bg-[var(--surface-2)] px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide text-[var(--muted)]">
            Upcoming
          </span>
        )}
      </div>
      <p className="mt-1.5 text-sm text-[var(--muted)]">{stage.tagline}</p>
      <div className="mt-3 flex items-center gap-1.5 text-xs">
        <span className="text-[var(--muted)]">Produces</span>
        <span className="font-medium">{stage.produces}</span>
      </div>
    </Card>
  );

  return (
    <li className="flex gap-4">
      {/* number + connecting spine */}
      <div className="flex flex-col items-center">
        <span
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-sm font-bold tabular-nums"
          style={
            built
              ? { background: "var(--accent)", color: "var(--accent-fg)" }
              : { background: "var(--surface-2)", color: "var(--muted)" }
          }
        >
          {stage.n}
        </span>
        {!last ? <span className="my-1 w-px flex-1 bg-[var(--border)]" /> : null}
      </div>

      <div className="flex-1 pb-4">
        {built ? (
          <Link href={stage.href as string} className="flex">
            {card}
          </Link>
        ) : (
          <div className="flex">{card}</div>
        )}
      </div>
    </li>
  );
}
