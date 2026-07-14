"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { AppSwitcher } from "@/components/AppSwitcher";
import { usePath } from "@/components/PathProvider";
import { PathSelector } from "@/components/PathSelector";
import { StoreSelect } from "@/components/StoreSelect";
import type { PipelinePath } from "@/lib/pipeline";

type Item = { href: string; label: string; icon: ReactNode };

const I = (p: ReactNode) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    {p}
  </svg>
);

const ICONS = {
  dashboard: I(<><rect x="3" y="3" width="7" height="9" /><rect x="14" y="3" width="7" height="5" /><rect x="14" y="12" width="7" height="9" /><rect x="3" y="16" width="7" height="5" /></>),
  pipeline: I(<><circle cx="5" cy="6" r="2" /><circle cx="5" cy="18" r="2" /><circle cx="19" cy="12" r="2" /><path d="M7 6h6a4 4 0 0 1 4 4v0M7 18h6a4 4 0 0 0 4-4v0" /></>),
  analyse: I(<><path d="M3 3v18h18" /><path d="M7 14l4-4 3 3 5-6" /></>),
  niche: I(<><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></>),
  keyword: I(<><circle cx="11" cy="11" r="8" /><path d="M11 7v8M7 11h8" /></>),
  product: I(<><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></>),
  marketplace: I(<><path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z" /><path d="M3 6h18" /><path d="M16 10a4 4 0 0 1-8 0" /></>),
  trend: I(<><path d="M3 17l6-6 4 4 8-8" /><path d="M21 7v6M21 7h-6" /></>),
  skuPlan: I(<><path d="M3 6h18M3 12h18M3 18h12" /><circle cx="19" cy="18" r="2" /></>),
  stores: I(<><path d="M3 9l1-5h16l1 5" /><path d="M4 9v11h16V9" /><path d="M9 22V12h6v10" /></>),
  newListing: I(<><circle cx="12" cy="12" r="9" /><path d="M12 8v8M8 12h8" /></>),
  sourcing: I(<><path d="M20 7L12 3 4 7l8 4 8-4z" /><path d="M4 7v6l8 4 8-4V7" /><path d="M12 11v10" /></>),
  activity: I(<><path d="M22 12h-4l-3 9L9 3l-3 9H2" /></>),
  settings: I(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></>),
  costs: I(<><line x1="12" y1="1" x2="12" y2="23" /><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></>),
  decisions: I(<><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></>),
  autonomy: I(<><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3" /><path d="M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2" /></>),
  assistant: I(<><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" /></>),
  guide: I(<><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /><path d="M9 7h6M9 11h4" /></>),
};

const RESEARCH_BY_PATH: Record<PipelinePath, Item[]> = {
  // Research is a hub: an overview entry + one entry per discovery surface. Each
  // surface (Trend / Keyword / Winning Products / Marketplace) feeds the shared
  // candidate queue — the products we look for across every source. Trend sits
  // above Keyword because keywords stream out of trends (trend → keyword → list).
  general: [
    { href: "/trends", label: "Trend Research", icon: ICONS.trend },
    { href: "/keyword-discovery", label: "Keyword Research", icon: ICONS.keyword },
    { href: "/sku-plan", label: "SKU Plan", icon: ICONS.skuPlan },
    { href: "/product-research", label: "Winning Products", icon: ICONS.product },
    { href: "/marketplace", label: "Marketplace Products", icon: ICONS.marketplace },
  ],
  niche: [
    { href: "/dossiers", label: "Niche Research", icon: ICONS.niche },
  ],
};

// Overview/hub routes that must match exactly (so a child route doesn't keep the
// parent highlighted). Everything else uses prefix matching.
const EXACT_MATCH = new Set(["/home"]);

export function Sidebar() {
  const pathname = usePathname();
  const { path } = usePath();
  const isActive = (href: string) =>
    EXACT_MATCH.has(href) ? pathname === href : pathname.startsWith(href);

  const nav: { section: string; items: Item[] }[] = [
    {
      section: "Overview",
      items: [
        { href: "/home", label: "Dashboard", icon: ICONS.dashboard },
        { href: "/decisions", label: "Decisions & Assistant", icon: ICONS.decisions },
      ],
    },
    { section: "Research", items: RESEARCH_BY_PATH[path] },
    {
      section: "Listings",
      items: [
        { href: "/sourcing-match", label: "Sourcing Match", icon: ICONS.sourcing },
        { href: "/listing-plan", label: "Daily Listings", icon: ICONS.pipeline },
        { href: "/stores", label: "Stores & Queue", icon: ICONS.stores },
        { href: "/new-listing", label: "New Listing", icon: ICONS.newListing },
      ],
    },
    {
      section: "System",
      items: [
        { href: "/autonomy", label: "Autonomy", icon: ICONS.autonomy },
        { href: "/guide", label: "Guide", icon: ICONS.guide },
        { href: "/costs", label: "Costs", icon: ICONS.costs },
      ],
    },
  ];

  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-[var(--border)] bg-[var(--surface)]">
      {/* Logo opens the fast app navigator (Frontend/Backend + each app). */}
      <AppSwitcher
        logo="GS"
        title="Google Stores"
        subtitle="Operator"
        activeAppId="research-listing"
      />

      {/* Path pre-selector — General vs Niche */}
      <div className="px-3 pb-3">
        <div className="px-2 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
          Path
        </div>
        <PathSelector />
      </div>

      {/* Global active-store — one choice, read by every page */}
      <div className="px-3 pb-3">
        <div className="px-2 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
          Store
        </div>
        <StoreSelect />
      </div>

      <nav className="flex-1 overflow-y-auto px-3 py-2">
        {nav.map((group) => (
          <div key={group.section} className="mb-5">
            <div className="px-3 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
              {group.section}
            </div>
            {group.items.map((item) => {
              const active = isActive(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                    active
                      ? "bg-[var(--surface-2)] text-[var(--text)]"
                      : "text-[var(--muted)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
                  }`}
                >
                  <span style={{ color: active ? "var(--accent)" : "inherit" }}>
                    {item.icon}
                  </span>
                  {item.label}
                </Link>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="border-t border-[var(--border)] px-5 py-3 text-xs text-[var(--muted)]">
        v0.1 · Phase 3
      </div>
    </aside>
  );
}
