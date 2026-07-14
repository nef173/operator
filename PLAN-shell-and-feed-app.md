# Plan — Operation Shell + Product Feed & Optimization app

Status: implemented 2026-06-21. Surgical/additive build on the existing operator app.

## 1. The vision (operator's mental model)

An OS-like hierarchy ABOVE the current single app:

```
Google Stores Operation System          ← NEW top-level shell / launcher ("the view before")
  └─ Store (Nosura, …)                   ← each store exposes a BACKEND path + a FRONTEND path
       └─ Frontend                       ← opens an app selector
            ├─ Research & Listing                  ← the WHOLE current app (everything today)
            └─ Product Feed & Optimization         ← NEW v1 app (this build)
```

The current app (Dashboard / Research / Listings / System) is now framed as ONE app —
**"Research & Listing"** — launched from the shell. A second app, **"Product Feed &
Optimization"**, is added. Adding a 3rd later must be trivial (a registry entry).

## 2. Information architecture

### 2a. The shell route `/shell`
A standalone launcher page (renders WITHOUT the normal sidebar chrome via a route-group
layout). Shows:
- A header: "Google Stores · Operation System".
- One **Store card** per registered store (from `GET /api/shell` → real stores, with live
  SKU/category counts so the card is honest, never a stub).
- Each store card exposes two paths:
  - **Backend** — the pipeline/control plane (links to `/settings`, the system/control surface
    that already exists). Honestly labelled as the data/control layer.
  - **Frontend** — opens the **app selector** for that store.
- The **app selector** lists the registered apps for the store:
  - **Research & Listing** → `/` (the current app; the store is pre-selected where relevant).
  - **Product Feed & Optimization** → `/feed`.

### 2b. The clickable logo navigator
The top-left "Google Stores / Operator" logo in `Sidebar.tsx` becomes a `Link` to `/shell`
("the view before"). Minimal, additive edit — wrap the existing markup in a `next/link`
`<Link href="/shell">` and add a subtle hover affordance + an "up" chevron. No other sidebar
behavior changes.

### 2c. App registry (extensibility)
A single source of truth `web/lib/apps.ts` exporting `OPERATOR_APPS: OperatorApp[]`
(`{id, name, tagline, href, icon, status}`). The shell renders from this list, so a 3rd app
is one array entry + its route. `status: "live" | "soon"` lets future apps appear as
disabled/coming-soon without a route.

## 3. Product Feed & Optimization app (v1 scope)

This is the Shopify → Google Merchant (GMC) product-feed surface. In this project "feed
optimization" means (per CLAUDE.md / 08-ads-pmax / MEMORY): GMC-ready **titles** (head
keyword in the first words, no brand in indexed text), **google_product_category** set from
the Standard Product Taxonomy, valid **images** (no text/price/logo overlay on the main
image), **price/compare-at**, **SEO title cap** (Shopify silently caps ~95 chars), and
description presence.

### What's REAL now (computed from on-disk pipeline data)
The backend `GET /api/feed/{store}` derives a per-SKU **feed-readiness** report from the SAME
real artifacts the listing pipeline writes (`listing-queue.json` + each category's
`_expansion-spec.json`). For every SKU it runs deterministic, honest checks:

| Check | Rule | Source of truth |
|---|---|---|
| Title present | non-empty | spec/queue title |
| Title length | ≤ 150 chars (GMC max) | title |
| Keyword-first | category head keyword appears in first 70 chars of title | category keyword |
| Brand-in-title | flags if a known store/brand token is in the indexed title (V2/V10 rule) | title |
| google_product_category | category has a `category_gid` (taxonomy node) set | spec `category_gid` |
| Price set | price > 0 | queue price |
| SEO title cap | seo_title ≤ 95 chars (silent-cap trap) | spec seo_title |
| Description | body_html present, ≥ ~120 chars | spec body_html |
| Image present | ≥ 1 generated gallery image on disk | sku images dir |

Each SKU rolls up to PASS / NEEDS-WORK with a list of concrete issues. Store-level rollup:
counts by status, top recurring issues, % feed-ready. This is all real — no fabricated GMC
account numbers, no invented impressions.

### What's an HONEST placeholder (clearly marked "not connected")
- **Live GMC account status / disapprovals / impressions** — requires the Google Merchant
  Content API, which is NOT wired (per MEMORY: ads/GMC API is the one hard external dep,
  deferred). Surfaced as an explicit "Not connected" panel naming the exact next step
  (connect GMC Content API) — NOT faked numbers.
- **Title rewrite execution** — the app PROPOSES a GMC-optimized title (keyword-first,
  brand-stripped, ≤150) for SKUs that fail the keyword-first/brand checks, shown as a
  suggestion the operator can copy. It does NOT write back to Shopify (draft-first, no
  auto-publish). The supplemental-feed write path is a labelled follow-up.

### Routes
- `/feed` — store picker + store-level feed-readiness rollup + per-SKU issue table with
  proposed title rewrites. Honest "GMC not connected" panel for the live-account half.

### Backend
- `app/feed.py` — pure functions computing the readiness report from readers' existing
  data (no new disk format). `GET /api/feed/{store}` in `main.py`. Read-only, additive.

## 4. Honesty posture
- Store cards show real counts (or "—" when absent), never invented.
- Feed checks are deterministic and explained; every issue cites its rule.
- The GMC live half is explicitly "Not connected" with the next step, never stubbed numbers.
- No write-back / no auto-publish anywhere in this build.

## 5. Files
New (frontend): `web/app/(shell)/shell/page.tsx`, `web/app/(shell)/layout.tsx`,
`web/app/feed/page.tsx`, `web/lib/apps.ts`. New (backend): `api/app/feed.py`.
Edited (minimal/additive): `web/components/Sidebar.tsx` (logo → Link), `web/lib/api.ts`
(+`feedReport` client + types), `api/app/main.py` (+`/api/shell`, +`/api/feed/{store}`).

## 6. Follow-ups
- Wire GMC Content API → replace the "Not connected" panel with live disapprovals.
- Add a "build supplemental feed" job spec that applies accepted title rewrites (draft-first).
- Per-store app enable/disable in the registry once more apps exist.
