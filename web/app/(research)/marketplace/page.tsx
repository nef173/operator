"use client";

import { PageHeader } from "@/components/PageState";
import { ResearchStarter } from "@/components/ResearchStarter";
import { MarketplaceResearch } from "@/components/MarketplaceResearch";

export default function MarketplacePage() {
  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="Marketplace Products"
        subtitle="Top-selling products on AliExpress, Temu and 1688 — proven demand (orders and units sold) and a cost to work from. We use these to find products, not to buy from; your own supplier fills the ones you choose."
      />

      {/* Run panel first — the operator can run without scrolling past the marketplace links. */}
      <ResearchStarter
        surface="marketplace"
        title="Ways to research marketplace products"
      />

      <MarketplaceResearch />
    </div>
  );
}
