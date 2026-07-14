# Operator App — Lean (Research · Listing · Optimization)

A **lean fork** of the Google Stores Operator App containing ONLY the
**research → find products → list → optimize feed** pipeline and its control plane.
Everything PrimeCore-operational (P&L / finance, product management, issue management,
multi-market, tasks, store management) has been removed, and it starts with **no stores,
no API keys, and no results** — a fresh operator plugs in their own.

> Forked from the full operator app on 2026-07-11. If you need the P&L / product-management /
> issues / multi-market halves, use the full app instead — this copy deliberately drops them.

## What's included

| Sub-app | Routes | What it does |
|---|---|---|
| **Research & Listing** | `/home` `/research` `/sku-plan` `/product-research` `/marketplace` `/keyword-discovery` `/trends` `/sourcing-match` `/listing-plan` `/pipeline` `/dossiers` `/stores` `/new-listing` | Keyword/trend discovery → SKU plan → **find real products** across lanes (AliExpress · 1688 · Temu · Amazon · Google Competitor Catalog · GoogleShopping Search) → photo-dedup → list to Shopify (draft-first). |
| **Product Feed & Optimization** | `/feed` `/feed/optimize` `/feed/alerts` `/feed/automation` | GMC feed-readiness checks + the product-grid optimizer (hide / tag / note / status per product, automation rules, perf alerts). |
| **Control plane** | `/settings` `/activity` `/costs` `/assistant` `/guide` | Settings → Connections (all API/AI keys + per-store Shopify creds), the run log, pipeline cost accounting, the read-only AI copilot. |

**Removed vs the full app:** `finance` (P&L), `company_pl`, `product_mgmt`, `issues`,
`multimarket` (+ GMC accounts / Trustpilot / markets / localization), `tasks`, `store_mgmt`,
`markifact` (ads write) — 14 backend modules, ~135 API routes, and 7 web route groups.
The optimize sub-app keeps working without the finance modules — its COGS / ad-spend / FX
enrichment simply degrades to empty (revenue + merchandising still work).

## Fresh setup

**1. Backend** (FastAPI, port 8077):
```bash
cd api
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8077
```
With no `DATABASE_URL` it uses a local SQLite (`data/app.db`, auto-created empty). Set
`DATABASE_URL=postgresql://…` (Neon) for a real deployment. Optional: `GOOGLE_STORES_DATA`
points the data root at a persistent volume.

**2. Frontend** (Next.js 16, port 3000):
```bash
cd web
npm install
NEXT_PUBLIC_API_BASE=http://localhost:8077 npm run dev
```

**3. Add your credentials.** Open http://localhost:3000 → **Settings → Connections**. Nothing
is a hard-coded key — every credential is entered here and stored in the DB. The must-haves to do
anything research→listing: **DataForSEO** · **Bright Data token** · **TMAPI** · an **LLM gateway**
(OpenRouter or Gemini) · a per-store **Shopify** triple (`shop_domain` + `client_id` + `client_secret`).

> **Read [`ONBOARDING.md`](ONBOARDING.md) §0.1 first** — the capability → connector map: every
> feature and exactly what to connect for it. [`FINDING-PIPELINE.md`](FINDING-PIPELINE.md) covers the
> product-finding lanes in depth; [`COMPETITOR-KEYWORD-SCAN.md`](COMPETITOR-KEYWORD-SCAN.md) the
> best-seller-validated competitor scan; [`SETUP-OWN-DEPLOYMENT.md`](SETUP-OWN-DEPLOYMENT.md) your own
> Railway deployment. (ONBOARDING §3a describes the reference deployment — stand up your own instead.)

## Layout

```
operator-app-clean/
├── api/    FastAPI backend — research/finding/listing/optimize + the vendored pipeline steps
│   ├── app/       45 modules (the 14 ops modules removed)
│   └── steps/     the research/listing pipeline scripts the API shells out to
└── web/    Next.js 16 frontend — 3 sub-apps + control plane (7 ops route groups removed)
```

Not a git repo yet — `git init` when you're ready to deploy (Railway deploys via `git push`).
API keys live in the DB, never in git; `.env` files are git-ignored (none are shipped here).

## Verified

Both halves were checked after the strip: the API imports + boots on an empty SQLite DB
(`/api/health` 200, `/api/overview` shows 0 stores / 0 SKUs — a true empty start,
`/api/connections` lists the connector rows to fill), and the web passes `tsc` + `next build`
(31 routes, zero ops routes).
