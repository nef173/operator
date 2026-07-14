"use client";

import { useState } from "react";
import { PageHeader } from "@/components/PageState";
import { ResearchStarter } from "@/components/ResearchStarter";
import { TrendResearch, NewsRadarCard, EventsCalendarCard } from "@/components/TrendResearch";

// One accordion across all three research panels: `active` holds either a research-method
// id (Trend Radar detail), "news", or "events" — so opening any panel closes the others.
const NEWS = "__news__";
const EVENTS = "__events__";

export default function TrendsPage() {
  const [active, setActive] = useState<string | null>(null);
  const methodSelected = active === NEWS || active === EVENTS ? null : active;

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="Trend Research"
        subtitle="Keyword momentum across every dossier — surface what's rising right now, this month, or over the last 1–3 months, and time the listing batch to the signal."
      />

      <ResearchStarter
        surface="trend"
        title="Ways to research trends"
        gridCols={3}
        selected={methodSelected}
        onSelectedChange={setActive}
        extra={
          <>
            <NewsRadarCard
              open={active === NEWS}
              onOpenChange={(v) => setActive(v ? NEWS : null)}
            />
            <EventsCalendarCard
              open={active === EVENTS}
              onOpenChange={(v) => setActive(v ? EVENTS : null)}
            />
          </>
        }
      />

      <TrendResearch />
    </div>
  );
}
