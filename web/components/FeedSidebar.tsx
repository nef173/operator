"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";
import { AppSwitcher } from "@/components/AppSwitcher";
import { useStore } from "@/components/StoreProvider";

// Standalone chrome for the "Product Feed & Optimization" app. Deliberately NOT the
// Research & Listing sidebar — this app has its own identity and nav. Items that
// aren't built yet are shown as "Soon" so the shape is legible without faking depth.

type Item = { href: string; label: string; icon: ReactNode; soon?: boolean };

const I = (p: ReactNode) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    {p}
  </svg>
);

const ICONS = {
  feed: I(<><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18M9 21V9" /></>),
  optimize: I(<><path d="M3 17l6-6 4 4 8-8" /><path d="M21 7v6M21 7h-6" /></>),
  automation: I(<path d="M13 2 3 14h7l-1 8 10-12h-7z" />),
  alerts: I(<><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><path d="M12 9v4M12 17h.01" /></>),
};

// GMC/feed connections are set up in the main Settings → Connections surface, not here.
// Once a connection is live it can surface its own view; until then this nav stays lean.
const NAV: { section: string; items: Item[] }[] = [
  {
    section: "Feed",
    items: [
      { href: "/feed/optimize", label: "Product Performance", icon: ICONS.optimize },
      { href: "/feed/alerts", label: "Alerts", icon: ICONS.alerts },
      { href: "/feed/automation", label: "Automation", icon: ICONS.automation },
    ],
  },
];

// Sidebar store dropdown (mirrors Pythago's store switcher). Reads/writes the shared
// active store; every surface in the app re-reads it. Closed by default; simple native
// toggle so it works without a popover lib.
function StoreSelect() {
  const { store, setStore, stores } = useStore();
  const [open, setOpen] = useState(false);
  const shopIcon = (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 9l1-5h16l1 5M4 9h16v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM9 13h6" />
    </svg>
  );
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-3 py-2 text-sm font-semibold capitalize hover:bg-[var(--surface-2)]"
      >
        <span className="text-[var(--accent)]">{shopIcon}</span>
        <span className="flex-1 truncate text-left">{store || "Select store"}</span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-[var(--muted)]">
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
      {open ? (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface)] shadow-lg">
          {stores.map((s) => (
            <button
              key={s}
              onClick={() => { setStore(s); setOpen(false); }}
              className={`flex w-full items-center gap-2 px-3 py-2 text-sm font-medium capitalize hover:bg-[var(--surface-2)] ${
                store === s ? "text-[var(--accent)]" : "text-[var(--text)]"
              }`}
            >
              <span className="opacity-70">{shopIcon}</span>
              <span className="flex-1 truncate text-left">{s}</span>
              {store === s ? <span className="text-[var(--accent)]">✓</span> : null}
            </button>
          ))}
          {stores.length === 0 ? (
            <div className="px-3 py-2 text-xs text-[var(--muted)]">No stores registered</div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function FeedSidebar() {
  const pathname = usePathname();
  const isActive = (href: string) => (href === "/feed" ? pathname === href : pathname.startsWith(href));

  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-[var(--border)] bg-[var(--surface)]">
      {/* Logo opens the fast app navigator (Frontend/Backend + each app). */}
      <AppSwitcher
        logo={ICONS.feed}
        title="Product Feed Management"
        subtitle=""
        activeAppId="product-feed"
      />

      {/* Store selector — Pythago-style, in the sidebar. Spans the whole operation; the
          active store is shared across every app via StoreProvider. */}
      <div className="px-3 pt-3">
        <StoreSelect />
      </div>

      <nav className="flex-1 overflow-y-auto px-3 py-2">
        {NAV.map((group) => (
          <div key={group.section} className="mb-5">
            <div className="px-3 pb-1.5 text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
              {group.section}
            </div>
            {group.items.map((item) => {
              const active = !item.soon && isActive(item.href);
              const inner = (
                <>
                  <span style={{ color: active ? "var(--accent)" : "inherit" }}>{item.icon}</span>
                  <span className="flex-1">{item.label}</span>
                  {item.soon ? (
                    <span className="rounded bg-[var(--surface-2)] px-1.5 py-0.5 text-[10px] font-semibold uppercase text-[var(--muted)]">
                      Soon
                    </span>
                  ) : null}
                </>
              );
              const cls = `mb-0.5 flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                active
                  ? "bg-[var(--surface-2)] text-[var(--text)]"
                  : item.soon
                    ? "cursor-not-allowed text-[var(--muted)] opacity-60"
                    : "text-[var(--muted)] hover:bg-[var(--surface-2)] hover:text-[var(--text)]"
              }`;
              return item.soon ? (
                <div key={item.href} className={cls} aria-disabled>
                  {inner}
                </div>
              ) : (
                <Link key={item.href} href={item.href} className={cls}>
                  {inner}
                </Link>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="border-t border-[var(--border)] px-5 py-3 text-xs text-[var(--muted)]">
        App · Feed v0.1
      </div>
    </aside>
  );
}
