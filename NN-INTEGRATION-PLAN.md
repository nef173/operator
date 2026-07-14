# Operator App → NN Operations — Integration Plan & Runbook

Goal (from owner): bring this app **into the NN Operations dashboard as a standalone
sub-app** (like Finance / Listing), on **NN's login (full pass-through — one sign-in)**,
with **API keys sourced from NN's Master Settings** (adding any missing keys there), and
**reskinned to the NN look**.

## The hard constraint
This app is **Next.js 16 (web) + Python FastAPI (api)** — ~70k LOC, incl. a Playwright /
Bright Data scraping pipeline. It **cannot be rewritten into NN's Node/Express + esbuild
monolith**; that would destroy the 40k-line Python product. So "native sub-app" is achieved
by running its two services and wiring them so it *behaves and looks* native:

```
NN dashboard (Node/Express monolith, existing)
   └─ /operator/*  ──reverse-proxy──►  operator-web (Next.js)  ──►  operator-api (FastAPI) ──► Postgres
        ▲ gated by NN's existing login             ▲ keys injected as ENV from NN Master Settings
```

## Why this is tractable (two seams already exist in the app)
1. **Keys via ENV (no Python change needed).** `api/app/connections.py` resolves every
   credential as `{**as_env(), **os.environ}` — **process env wins over its own DB**. So NN
   just injects the keys as **environment variables** on the operator-api service and they're
   used automatically. Env names are the `API_FIELDS`/`STORE_FIELDS` registry in connections.py.
2. **Auth is a bearer token** (`api/app/auth.py`), sessions = SHA-256 in DB, seeded `admin/admin`.
   Pass-through = add a "trusted proxy header" auth path (NN signs a header when proxying;
   api trusts it), so no second login.

---

## STATUS — done tonight (safe, no NN prod touched)
- ✅ **Reskin to NN palette** — `web/app/globals.css` design tokens remapped to NN's cool-gray
  canvas + neutral near-black accent (flips light/dark exactly like NN's `--primary`). The whole
  UI is CSS-var driven, so this recolors every page. Added the few tokens the app referenced but
  never defined (`--card`, `--fg`, `--danger`, `--warning`, `--info`, `--state-warn`, `--state-winner-bg`).
- ✅ **Font → Inter** (`web/app/layout.tsx`) to match NN (kept the `--font-geist-sans` var name).
- ✅ Verified the web app builds (see build check).

## TODO — needs owner + infra (do together; requires Railway/secrets)
1. **Repo**: move `operator-app/` into the NN repo (`nn-operations/operator-app/{api,web}`).
   Keep it OUT of NN's Node build (Railway root dir for NN stays `/`; add two NEW services with
   root dirs `operator-app/api` and `operator-app/web` + watch paths so NN's own deploy is untouched).
2. **Database**: create a **Neon Postgres** (free tier), set `DATABASE_URL` (pooled) on operator-api.
3. **Deploy** operator-api (Dockerfile present) → grab URL; set `CORS_ORIGINS` = web URL.
   Deploy operator-web with `NEXT_PUBLIC_API_BASE` = api URL (baked at build → redeploy on change).
4. **NN reverse-proxy**: mount `/operator/*` in NN's Express → operator-web, INSIDE NN's login
   gate (same pattern as the `/listing` mount). Add sidebar + app-switcher entry "Operator"
   (icon `research`).
5. **Auth pass-through**: NN proxy injects a shared-secret-signed header (e.g. `X-NN-User`,
   `X-NN-Sig`); add a dependency in `api/app/auth.py` that accepts it and resolves/creates the
   matching operator user. Set the shared secret as env on both sides.
6. **Keys from Master Settings (env injection)** — map + inject on the operator-api service:

   | Operator env var | NN Master Settings source | Action |
   |---|---|---|
   | `DATAFORSEO_USERNAME` / `DATAFORSEO_PASSWORD` | DataForSEO login/password | reuse (exists) |
   | `OPENROUTER_API_KEY` | OpenRouter key | reuse (exists) |
   | `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` (+ per-store) | store client id/secret | reuse (exists) |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | Merchant Center service-account JSON | reuse (just added to NN) |
   | `SLACK_BOT_TOKEN` / `SLACK_DEFAULT_CHANNEL` | Slack | reuse (exists) |
   | `GEMINI_API_KEY` | — | optional (OpenRouter covers it) |
   | `BRIGHTDATA_API_TOKEN` (+ `BRIGHTDATA_TEMU_DATASET`, `BRIGHTDATA_DATASET_TOKEN`, SERP zone, `BRIGHTDATA_BROWSER_CDP`) | **MISSING** | **add to Master Settings** |
   | `TMAPI_TOKEN` (1688) | **MISSING** | **add to Master Settings** |
   | `APIFY_TOKEN` (Temu) | **MISSING** | **add to Master Settings** |
   | `TRENDTRACK_API_TOKEN` | **MISSING** | **add to Master Settings** |
   | `CJ_EMAIL` / `CJ_API_KEY` (optional) | **MISSING** | add if used |
   | Google Ads tokens (optional) | **MISSING** | add if Ads used |

   NN then passes these to the operator-api service as env vars (Railway service variables, or a
   small NN endpoint the operator-api reads at boot). Because process env wins, they take effect
   with zero Python changes; the operator's own Settings → Connections becomes a fallback.
7. **Stores from NN**: operator per-store creds (`shop_domain`, `client_id`, `client_secret`) come
   from NN's store config — feed them the same way (env or a seed call), mirroring how `/listing` does it.
8. **Reskin pass 2** (optional polish): align the operator Sidebar chrome to NN's sidebar; hide its
   own login screen once pass-through is live.

## Open decisions for the owner
- Confirm the 2-new-Railway-services approach (vs. bundling Python into one container — not recommended).
- Provide the missing keys (Bright Data, TMAPI, Apify, TrendTrack) — or confirm which lanes to skip.
- Neon account for the Postgres DB (free tier is fine).
