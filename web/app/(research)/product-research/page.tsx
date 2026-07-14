"use client";

import { PageHeader } from "@/components/PageState";
import { WinningProducts } from "@/components/WinningProducts";

export default function ProductResearchPage() {
  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="Winning Products"
        subtitle="See what's already selling — competitors' best sellers, products climbing the ranks, new arrivals, Amazon Best Sellers & New Releases, and ads that have been running a long time on Meta. (Marketplace movers have their own tab.)"
      />
      <WinningProducts />
    </div>
  );
}
