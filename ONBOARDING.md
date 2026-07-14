# Operator App — Fresh Setup / Onboarding

The complete "I have nothing set up yet — get me running" guide for the Google Stores
Operator App. Follow it top to bottom.

The app is a **FastAPI backend** (`api/app/`) + **Next.js frontend** (`web/`) that runs the
Google-Shopping dropshipping pipeline: keyword/trend research → find products (AliExpress + 1688 +
Temu + Amazon + competitor/Google-Shopping catalogs — see
[§7](#7-product-finding) + [`FINDING-PIPELINE.md`](FINDING-PIPELINE.md)) → SKU plan → listing →
(optionally) ads/scale.

> **The one thing to internalise first.** Almost NO credential is a plain environment variable.
> API keys and Shopify tokens are entered **in the running app** under **Settings → Connections**
> and stored in the database. `git push` ships code and **never touches a single key**. The only
> env vars you set on the host are the plumbing ones in [§4](#4-host-env-vars-plumbing-only): the
> database URL, data volume, tenant label, CORS, and (optionally) an owner login. Everything else
> is a Connections row. This is by design — see `api/app/connections.py` (the module docstring is
> the authoritative spec).

Related docs:
- `README.md` — what the app is + the two-service layout + local run.
- `FINDING-PIPELINE.md` — the product-finding lanes in depth (sources, AI-assist, allocation, diagnostics).
- `COMPETITOR-KEYWORD-SCAN.md` — how to run the best-seller-validated competitor keyword scan (a competitor's proven sellers for a keyword, not dead stock).
- `DEPLOY.md` — Railway deploy mechanics (services, watch-paths, Neon, pricing) in depth.
- `SETUP-OWN-DEPLOYMENT.md` — running your **own** deployment next to PrimeCore's + multi-business updates.

---

## 0. TL;DR checklist

1. **Run it** — local ([§1](#1-run-it-locally)) or Railway ([§3](#3-deploy-to-railway)).
2. **Sign in** — default `admin` / `admin` (or set `OWNER_NAME` + `OWNER_PASSWORD`, [§4](#4-host-env-vars-plumbing-only)).
3. **Open Settings → Connections** and fill in credentials ([§5](#5-connections--every-credential)). The must-haves to do *anything*:
   - `DATAFORSEO_USERNAME` + `DATAFORSEO_PASSWORD` — all research data (Amazon + Google-Shopping + competitor lanes).
   - `BRIGHTDATA_API_TOKEN` — AliExpress product-finding (+ the SERP zone).
   - `TMAPI_TOKEN` — 1688 product-finding.
   - `OPENROUTER_API_KEY` — the AI assistant + the AI product gate + the AI finding-assist + the dropshipper classifier.
   - Per store: `shop_domain` + `client_id` + `client_secret` — Shopify listing.
4. **Provision Bright Data zones** — click the setup button on the Connections page ([§6](#6-bright-data-mcp--zone-auto-provision)).
5. **(Optional) Install the Bright Data MCP** for agent-side research — same BD account token ([§6](#6-bright-data-mcp--zone-auto-provision)).
6. **Verify** — `GET /api/health` returns the deployed `commit`; a "Find products" run returns real rows.

---

## 0.1 What's possible — capability → connector map

Every research + listing capability and exactly what you must connect for it. **Nothing here is a
plain env var** — each "Needs" item is a Settings → Connections row (or a per-store field). A
capability with no connector still works; one with a missing connector **fails soft** (returns empty
/ skips), never crashes. This is the map a fresh session should read first.

| Capability | What it does | Needs (Connections) |
|---|---|---|
| **Keyword / trend research** | Head-keyword + sub-keyword discovery, SV / CPC / SERP density / Google Trends, the ≥10k-SV gate + volume tiers, the world-events calendar. | **DataForSEO** (`DATAFORSEO_USERNAME`+`_PASSWORD`) — canonical for ALL data. Optionally **TrendTrack** (trend feed). |
| **Find products — AliExpress** (workhorse lane) | Real supplier products, order-sorted, English titles → research ref (never the sell price). | **Bright Data** `BRIGHTDATA_API_TOKEN` + a Web Unlocker zone (auto-provisioned). |
| **Find products — 1688 / Alibaba** | Native 1688 offers (translated titles). | **TMAPI** `TMAPI_TOKEN`. |
| **Find products — Temu** | Temu keyword products, demand-ranked (BD can't scrape Temu). | **Apify** `APIFY_TOKEN` (works on the free plan). |
| **Find products — Google Competitor Catalog** | A keyword's **best-seller-validated proven sellers** across the tracked competitor roster, with full product URLs → listing handoff. | Spy roster (grows itself) + an **LLM gateway** (AI-verify). Best-seller fetch itself needs **no key** (plain GET); optional BD residential proxy for rate-limited stores. |
| **Find products — GoogleShopping Search** | Google Shopping dropshippers for the keyword → scan their catalogs. | **DataForSEO** + an **LLM gateway** (dropshipper classifier). |
| **Find products — Amazon** | Amazon keyword products + `bought_past_month` demand. | **DataForSEO**. |
| **Find products — Meta** | Dropship advertisers running Meta ads for the keyword. | **TrendTrack**. |
| **Sponsored-PLA competitor capture** | The Google "Sponsored products" carousel (paying competitors + advertiser domains), per country. | **Bright Data Scraping Browser** (`BRIGHTDATA_BROWSER_CDP` + `BRIGHTDATA_CUSTOMER_ID`) + `playwright`. |
| **AI finding-assist** | Expands each keyword into ~5 buyer phrasings so lanes fan out (standard on every find). | **LLM gateway** (`OPENROUTER_API_KEY` or `GEMINI_API_KEY`) — fail-open. |
| **AI product gate / bad-keyword exclusion** | Keeps brands / places / non-products / oversized items out of the SKU plan. | **LLM gateway** — fail-open (deterministic filters still run). |
| **Photo-duplicate check (Gemini vision)** | Groups the SAME physical product across lanes by **image**; enforces the `dedup_cap` so a product is never listed more than N times ([§7.1](#71-photo-duplicate-check-gemini-vision--never-list-the-same-product-twice)). | **LLM/vision gateway** (`OPENROUTER_API_KEY` or `GEMINI_API_KEY`). |
| **List to Shopify** | Create draft-first products (title / description / price / category / images), publish channels, set Google feed fields. | Per store: `shop_domain` + `client_id` + `client_secret` (client-credentials grant). |
| **Product images (hero + gallery)** | Generate the Google-Shopping hero + Amazon-style gallery from the locked supplier photo. | Image model (`nano_banana_pro` via Higgsfield MCP, or OpenRouter Gemini image) + **Gemini** vision-QA gate. |
| **GMC trust / feed** | Merchant Center standing, feed diagnostics, trust-signal fixes. | **Google Merchant Center** connectors ([§5.3](#53-group-google-merchant-center)). ⚠️ Only Ovayla is GMC-suspended — never add/delete on it. |
| **Optimization (ads / P&L)** | Negatives, custom-label bidding tiers, margin/P&L. Plan/analysis-only until a Google Ads API is wired. | **Google Ads** ([§5.4](#54-group-google-ads)); P&L reads the app's own order/cost data. |

**The 5 must-haves to do *anything* research→listing:** DataForSEO · Bright Data token · TMAPI ·
an LLM gateway (OpenRouter or Gemini) · a per-store Shopify triple. Everything else widens coverage.

---

## 1. Run it locally

**Backend** (port 8077):

```bash
cd operator-app/api
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8077
```

With no `DATABASE_URL`, the backend uses a local SQLite file at `api/data/app.db` (fine for
dev; the Postgres adapter in `app/db.py` switches on automatically the moment `DATABASE_URL`
is set). It also loads `api/.env` into the environment at import time (see `app/config.py`),
so plumbing vars can live there locally.

**Frontend** (port 3000):

```bash
cd operator-app/web
npm install
npm run dev
```

Open http://localhost:3000. The frontend reads `NEXT_PUBLIC_API_BASE` (defaults to
`http://localhost:8077`; set it in `web/.env.local`).

You still fill in credentials via **Settings → Connections** in the running app — locally they
persist to the SQLite DB. Nothing else is needed to boot; research/listing views just read
empty until Connections are filled.

---

## 2. Prerequisites (accounts you'll need)

You do not need all of these on day one — the app degrades gracefully and each Connections
field is independent. Get them in priority order:

| Priority | Account | Powers | Where |
|---|---|---|---|
| **Required** | **DataForSEO** | ALL research data (keyword SV, SERP, Amazon merchant, Google-Shopping grid) | dataforseo.com (pay-as-you-go, ~$0.001–0.002/task) |
| **Required** | **Bright Data** | AliExpress product-finding (Web Unlocker); Google-Lens reverse-image | brightdata.com (one account token drives everything) |
| **Required** | **TMAPI** | 1688 product-finding (`/1688/search/items`) — the marketplace lane's 1688 source | tmapi provider |
| **Required** | **OpenRouter** | AI assistant + AI product-gate ("is this a dropship product?") + AI finding-assist + dropshipper classifier | openrouter.ai |
| **Required (to list)** | **Shopify** custom app per store | Listing / catalog / publish / P&L | each store's Shopify admin → Settings → Apps → Develop apps |
| Recommended | **Neon Postgres** | Durable state on Railway (Railway FS is ephemeral) | neon.tech (free tier) |
| Optional | **Gemini** (Google AI Studio) | Direct vision/image-gen transport; else OpenRouter serves Gemini | aistudio.google.com |
| Optional | **TrendTrack** | Premium spy/discovery (scaling US Shopify stores, Meta ads, winners) | trendtrack.io |
| Optional | **Google Ads / Merchant Center** | Ads reads + GMC diagnostics | Google Cloud + Google Ads |
| Optional | **CJ Dropshipping** | Fallback supplier | cjdropshipping.com |
| Optional | **OpenWeb Ninja** | Live Trustpilot scores | openwebninja.com |
| Optional | **Slack / Pumble** | Dispute-escalation ping | slack.com / pumble.com |

---

## 3. Deploy to Railway

The production model is **`git push` → Railway auto-builds**. One GitHub repo, two Railway
services (api + web), one Neon Postgres.

### 3a. The live PrimeCore deployment (how this repo ships today)

```bash
cd operator-app
git push primecore main
```

- Remote `primecore` → `github.com/PrimeCore24/operator-app`. Railway is wired to it with
  auto-deploy, so the push triggers a build. (There is **no** Railway-CLI deploy path for
  PrimeCore — it is git-push only.)
- Service names: the **api** service is `operator-app-production`; the **web** service is
  `operation-primecore`.
- The api Dockerfile (`api/Dockerfile`) + `api/railway.json` define the build; health check is
  `/api/health`, single replica (the always-on background worker lives inside the one uvicorn
  process — do not scale to >1 replica or you get duplicate schedulers).
- **Verify a deploy landed:** `GET https://<api-url>/api/health` returns
  `{"commit": "<7-char SHA>", ...}` — Railway injects `RAILWAY_GIT_COMMIT_SHA`; match it to
  your pushed commit.

> `origin` in this checkout points at `github.com/mediantssolutions/operator-app` (a separate
> mirror). Push to whichever remote is wired to the Railway project you want to deploy.

### 3b. Standing up a brand-new Railway project from scratch

See `DEPLOY.md` for the full walkthrough. In short:

1. **Neon** — create a project, copy the **pooled** connection string (host contains `-pooler`).
2. **GitHub** — push `operator-app/` to a repo.
3. **Railway** — New → GitHub Repo **twice** (same repo):
   - **api** service: Root Directory `api`, Watch Paths `/api/**` (builds `api/Dockerfile`).
   - **web** service: Root Directory `web`, Watch Paths `/web/**` (builds `web/Dockerfile`).
4. Set the host env vars ([§4](#4-host-env-vars-plumbing-only)) on each service.
5. Deploy **api** first, grab its public URL, set `NEXT_PUBLIC_API_BASE` (web) + `CORS_ORIGINS`
   (api), then deploy **web**.

---

## 4. Host env vars (plumbing only)

These are the **only** things you set on the host (Railway service variables, or `api/.env`
locally). They are infrastructure wiring — **not** API keys. Everything credential-shaped goes
in Connections ([§5](#5-connections--every-credential)).

### api service

| Var | Required? | What it does |
|---|---|---|
| `DATABASE_URL` | **Yes on Railway** | Neon **pooled** Postgres URL. Unset → local SQLite at `api/data/app.db` (dev only; Railway's FS is ephemeral so a deploy wipes SQLite). `app/db.py` auto-switches to Postgres when set. |
| `CORS_ORIGINS` | Yes (prod) | Comma-separated origins allowed to call the api = the web service's public URL. Defaults to `http://localhost:3000`. |
| `GOOGLE_STORES_DATA` | Recommended (prod) | Per-deployment **live data** root (queues, dossiers, finding-cache, AI hide-set). Point at a mounted **Railway volume** so it survives redeploys. Unset → falls back to the code root (OK on a laptop, lossy on Railway). |
| `GOOGLE_STORES_ROOT` | No | Where the pipeline/step scripts live. Defaults to the image (`/app` on Railway). Leave unset. |
| `TENANT` | Optional | Human label for which business this deployment serves (shown in Settings → System). Real isolation comes from `DATABASE_URL` + `GOOGLE_STORES_DATA`, not this. Can also be set live in Settings. |
| `OWNER_NAME` + `OWNER_PASSWORD` | Optional | Provision the real owner login from env, so no password lives in the repo. Set both → that name becomes a full-access owner and its password is (re)set on every boot (idempotent). If unset, the app still ships a working `admin`/`admin` login (`app/users.py`). |
| `USERS_SECRET_KEY` | Optional | Key for the at-rest password encryption in `users.py`. Unset → a per-install random key is generated and persisted once. |
| `DB_POOL_MAX` | Optional | Max DB connections (keep small on Neon free). Default 5. |
| `OPERATOR_JOB_WORKERS` | Optional | Background job-thread concurrency. Default 3. |

### web service

| Var | Required? | What it does |
|---|---|---|
| `NEXT_PUBLIC_API_BASE` | **Yes** | The api service's public URL. ⚠️ Inlined into the browser bundle at **build time** — change it → **redeploy** web (not just restart). |

---

## 5. Connections — every credential

Open the running app → **Settings → Connections**. Every field below is entered there. It is
persisted in the database (`app_settings` key `connections`) so it survives a redeploy once
`DATABASE_URL` points at Neon. Secrets are masked in the UI (`••••last4`) and are never
returned raw over the API.

**Programmatic alternative:** `PUT /api/connections`. The body is grouped —
`{"api": {ENV_NAME: value, ...}, "shopify": {"<store>": {key: value, ...}}}` — matching
`connections.update()` (`api` = the account-wide `API_FIELDS`, `shopify` = per-store
`STORE_FIELDS`). Only the keys you supply are written; an empty string clears that field;
updates never wipe the rest.

**The in-process rule (do not break it):** any reader that runs inside FastAPI resolves a key
via `connections.runtime_get(env)`, **never** `os.environ[...]`. Subprocess pipeline jobs get
the same keys injected via `connections.as_env()`. Precedence is always *process env wins, then
the DB-stored value*, so a real shell env var can override a Connections value but the Connections
value is the normal source of truth. **Adding a new credential** = append one row to `API_FIELDS`
(account-wide) or `STORE_FIELDS` (per store) in `api/app/connections.py`; the UI renders it
automatically and `public_view`/`update` pick it up.

The exact field registry is `API_FIELDS` (account-wide) and `STORE_FIELDS` (per store) in
`api/app/connections.py`. Documented here group by group.

### 5.1 Group "Data"

| Env name | Secret | What it powers | Where to get it / notes |
|---|:---:|---|---|
| `DATAFORSEO_USERNAME` | no | **The canonical data provider.** Keyword search-volume, SERP, Amazon merchant search, Google-Shopping merchant grid, store discovery. | dataforseo.com login email. |
| `DATAFORSEO_PASSWORD` | yes | (same) | dataforseo.com API password. Pay-as-you-go, ~$0.001–0.002/task. |
| `BRIGHTDATA_API_TOKEN` | yes | **The one Bright Data key.** Powers server-side **AliExpress** product-finding (Web Unlocker), the Google-Lens SERP zone, and (same token) the agent-side Bright Data MCP. (1688 finding uses `TMAPI_TOKEN`, not this.) | brightdata.com → Account settings → API token. Also auto-satisfies the `BRIGHT_DATA_API_TOKEN` and `BRIGHT_DATA_DATASET_TOKEN` spellings via aliasing — enter it **once**. |
| `APIFY_TOKEN` | yes | **The Temu product-finding source.** Bright Data can't scrape Temu (PDD anti-bot), so the Temu lane runs the Apify actor `crw/temu-products-scraper` (keyword search → real products; works on the Apify FREE plan). Blank = Temu skipped, lane runs on AliExpress + 1688. | apify.com → Settings → Integrations → API token (`apify_api_…`). |
| `BRIGHTDATA_TEMU_DATASET` | no | **Deprecated for Temu** — Temu now uses `APIFY_TOKEN` (above). This BD dataset path never worked (the account's only Temu dataset is URL-collect). Leave blank. | — |
| `BRIGHTDATA_DATASET_TOKEN` | no | Dataset-scoped BD token (legacy Temu-dataset path, now unused). | Falls back to `BRIGHTDATA_API_TOKEN`. |
| `TMAPI_TOKEN` | yes | **The 1688 product-finding source** — the marketplace lane's `/1688/search/items` call. | TMAPI provider. |
| `TRENDTRACK_API_TOKEN` | yes | Premium spy/discovery (scaling US Shopify stores, Meta ads, winning products). | TrendTrack → workspace settings → enable Public API access → "Get API key" (a `tt_live_…` token). |
| `BRIGHTDATA_CUSTOMER_ID` | no | The `brd-customer-XXXX` id. Needed to assemble the CN residential proxy URL **and** the Scraping-Browser CDP endpoint (`BRIGHTDATA_BROWSER_CDP`, below) — the one value the BD API can't return. | Bright Data dashboard → Account settings. |
| `BRIGHTDATA_SERP_ZONE` | no | SERP zone name (Google-Lens reverse-image search in china-source-match). **Auto-filled by the BD provision button** ([§6](#6-bright-data-mcp--zone-auto-provision)). | Also serves as the AliExpress Web-Unlocker zone; the live default when blank is `web_unlocker1` (see [§7](#7-product-finding)). |
| `BRIGHTDATA_SERP_ZONE_PASSWORD` | yes | Password for the SERP zone. | Auto-filled by the BD provision button. |
| `BD_CN_PROXY` | yes | CN residential proxy URL for 1688 mtop scraping (embeds its own creds). | Auto-assembled by the BD provision button **once `BRIGHTDATA_CUSTOMER_ID` is set**. |
| `BRIGHTDATA_BROWSER_CDP` | no | CDP `wss://` endpoint for the **Bright Data Scraping Browser** — the server-side Google **Sponsored-PLA** capture (`paid_shopping_scan_bd.py`, `GET /api/product-research/sponsored-plas`). A real per-country residential Chrome driven over CDP with `playwright` (no local AdsPower). | **Auto-provisioned** by the BD setup button (assembles it from the `browser_api` zone) **once `BRIGHTDATA_CUSTOMER_ID` is set**. |

### 5.2 Group "AI"

| Env name | Secret | What it powers | Where to get it / notes |
|---|:---:|---|---|
| `OPENROUTER_API_KEY` | yes | The in-app **AI assistant** (chat copilot) **and** the **AI product gate** (see [§8](#8-research-bad-keyword-exclusion--ai)). Universal OpenAI-compatible hub — serves Claude/GPT/Gemini. | openrouter.ai → Keys. The product gate is pinned to `anthropic/claude-haiku-4.5` on OpenRouter. |
| `GEMINI_API_KEY` | yes | **Optional.** Direct Gemini transport for vision (image QA), image gen (`nano_banana` = Gemini 2.5 Flash Image), and the assistant. If blank, OpenRouter serves Gemini (`google/gemini-2.5-flash`). Fill only to hit Gemini directly (slightly cheaper; the direct path for image gen). | aistudio.google.com → Get API key. |

> **Assistant/Vision gateway is derived, not a separate field.** The in-app assistant + VisionScan
> + translate resolve their LLM gateway from a key you already entered — **Gemini first** (also the
> locked vision model), else **OpenRouter**. There is no "Assistant LLM API Key" field. An explicit
> shell `ASSISTANT_LLM_API_KEY` / `ASSISTANT_LLM_BASE_URL` / `ASSISTANT_LLM_MODEL` overrides the
> derivation. So set **at least one** of `OPENROUTER_API_KEY` or `GEMINI_API_KEY` or the assistant
> and the AI product gate are disabled (the gate fails **open** — it never blocks the pipeline).

### 5.3 Group "Google Merchant Center"

| Env name | Secret | What it powers | Where to get it |
|---|:---:|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes (multiline) | The **easy** Merchant Center connection for stores you own: GMC account-issue / product-disapproval / report reader jobs authenticate server-to-server. | Google Cloud → create a service account → enable the Merchant API → add its email as a user in Merchant Center → Account access → paste the full downloaded JSON key here. No consent popup. |

### 5.4 Group "Google Ads"

Account-wide OAuth **app** credential + the manager (MCC) login id. *Which* account to act on is
per store ([§5.9](#59-per-store-fields-group-google)).

| Env name | Secret | Notes |
|---|:---:|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | yes | Google Ads API developer token. |
| `GOOGLE_ADS_CLIENT_ID` | no | OAuth client id. |
| `GOOGLE_ADS_CLIENT_SECRET` | yes | OAuth client secret. |
| `GOOGLE_ADS_REFRESH_TOKEN` | yes | Authorizes every Ads/GMC job. |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | no | Manager (MCC) id, e.g. `123-456-7890`. |

> Google Ads native API is **read-only** (GAQL/reporting) — good for the 09 Scale reads.
> The **write** layer (create campaigns/budgets/negatives) is Markifact (OAuth-MCP, no key),
> surfaced as an integration row and currently manual/deferred. Ads execution is deferred.

### 5.5 Group "Sourcing"

| Env name | Secret | Notes |
|---|:---:|---|
| `CJ_EMAIL` | no | CJ Dropshipping — the **fallback** supplier path (only for SKUs the private agent / 1688 can't carry). |
| `CJ_API_KEY` | yes | CJ Dropshipping API key. |

### 5.6 Group "Chat"

Dispute-escalation ping (Orders & Issues). One adapter (`chat_adapter.py`), `CHAT_PROVIDER`
selects Slack vs Pumble. One-way "ping a human" only.

| Env name | Secret | Notes |
|---|:---:|---|
| `CHAT_PROVIDER` | no | `slack` or `pumble`. |
| `SLACK_BOT_TOKEN` | yes | `xoxb-…` (→ `chat.postMessage`). |
| `SLACK_DEFAULT_CHANNEL` | no | id or `#name`, e.g. `#disputes`. |
| `PUMBLE_API_KEY` | yes | Pumble addon API key. |
| `PUMBLE_DEFAULT_CHANNEL` | no | e.g. `disputes`. |

### 5.7 Group "Trustpilot"

| Env name | Secret | Notes |
|---|:---:|---|
| `OPENWEB_NINJA_API_KEY` | yes | Trustpilot blocks direct access, so store scores are read via the OpenWeb Ninja Trustpilot API (free tier ~100 req/month, scores cached 24h). Read by `trustpilot.py` via `runtime_get`. |

### 5.8 Shopify (per store) — group "Store"

There is **no** app-wide Shopify Client ID/Secret. Each store is its own Shopify organization,
so **every store carries its OWN custom-app credentials**. The 24h Admin API token is **minted
on demand** from the Client ID + Secret via Shopify's client-credentials grant (cached ~23h) —
you do **not** paste a `shpat_` token.

**Per store, create a custom app first:** in the store's Shopify admin → **Settings → Apps →
Develop apps → Create an app** → configure Admin API scopes → paste the **required scopes**
(the Connections UI shows the exact copy-box; the source of truth is `REQUIRED_SHOPIFY_SCOPES`
in `connections.py`, fetched via `GET /api/shopify/scopes`) → Install → copy the Client ID +
Secret.

Then in Settings → Connections, on the store row:

| Key | Secret | Notes |
|---|:---:|---|
| `display_name` | no | Store name shown in the UI. |
| `shop_domain` | no | `your-store.myshopify.com`. |
| `client_id` | no | This store's custom-app Client ID. |
| `client_secret` | yes | This store's custom-app secret (`shpss_…`). |
| `admin_token` | yes | **Optional** legacy override — a `shpat_` token that skips minting. Leave blank to auto-mint from Client ID/Secret. |
| `data_start_date` | no | Blank = all history. ISO date to read from a start date (e.g. a rebrand). |

A store is "connected" when it can authenticate: either a manual `admin_token`, **or**
`shop_domain` + `client_id` + `client_secret` present (no network call is made just to show
this in Settings).

### 5.9 Per-store fields — group "Google"

Not how you *connect* Google — they only pick **which** account to act on after the Google
OAuth flow is authorized (surfaced as a picker from `google_oauth.list_accounts()`):

| Key | Notes |
|---|---|
| `google_ads_customer_id` | e.g. `123-456-7890`. |
| `merchant_center_id` | This store's GMC id. |

### 5.10 Per-store fields — groups "Finance" and "Orders & Issues"

Not credentials — per-store profit-model knobs + ParcelPanel key. Read by `finance.py` /
issues sync with sensible defaults when blank:

| Key | Group | Notes |
|---|---|---|
| `cog_rate` | Finance | COGS formula fallback rate, e.g. `0.30`. |
| `cog_floor_pct` | Finance | Invoice-coverage floor, e.g. `0.25`. |
| `tx_rate` | Finance | Transaction fee rate, e.g. `0.056`. |
| `tx_fix_fee` | Finance | Fixed fee per order, e.g. `0.25`. |
| `report_currency` | Finance | Reporting currency, e.g. `EUR`. |
| `parcelpanel_api_key` | Orders & Issues | ParcelPanel API key (per store); blank = that store's parcel sync is skipped. |

### 5.11 OAuth integrations (not paste-a-key)

Rendered as info/connect rows, not editable secret fields:

- **Google (Ads + Merchant Center)** — OAuth 2.0 + PKCE using the `GOOGLE_ADS_*` client above.
  Authorize once; the refresh token authorizes every Ads/GMC job.
- **Markifact (Google Ads write)** — OAuth 2.1 + PKCE, no key. The campaign-write path.
  Client flow not wired yet → ads execution is manual/deferred.

---

## 6. Bright Data MCP + zone auto-provision

**One Bright Data account token** does two jobs:

1. **Server-side finding** inside the app — the **AliExpress** marketplace lane (Web Unlocker) +
   the SERP zone — via `BRIGHTDATA_API_TOKEN` in Connections ([§7](#7-product-finding)). (The
   **1688** lane uses `TMAPI_TOKEN`, not Bright Data.)
2. **Agent-side research MCP** (the `mcp__brightdata__*` tools) when you drive research from an
   agent session — same account token.

### 6a. Zone auto-provision (the click, no hand-created zones)

The old flow asked for a pile of Bright Data fields (SERP customer id + zone + zone password +
CN proxy URL). Those are **not extra keys** — they are *resources that must exist inside your BD
account*. `api/app/brightdata.py` `provision()` creates them from the single token:

- `POST /zone` to ensure the zones exist (idempotent, adopts existing by name):
  - **`gstores_serp`** (type `serp`, country `us`, Google on) — Google-Lens / SERP for
    china-source-match.
  - **`gstores_cn`** (type `resi`, country `cn`) — CN residential proxy for 1688 mtop.
  - a **`browser_api`** (Scraping Browser) zone — the server-side Google **Sponsored-PLA** capture.
- reads each zone's password (`GET /zone/passwords`) and writes `BRIGHTDATA_SERP_ZONE`,
  `BRIGHTDATA_SERP_ZONE_PASSWORD` back into Connections automatically.
- once you've pasted `BRIGHTDATA_CUSTOMER_ID` (the one value the BD API can't return — from your
  BD dashboard → Account settings), it assembles the full `BD_CN_PROXY` URL **and** the Scraping-Browser
  CDP endpoint (`BRIGHTDATA_BROWSER_CDP`) for you (re-run the setup after pasting the customer id and
  they fill themselves).

Creating a zone is billable, so this runs **only** when you click the provision button on the
Connections page — never silently on token paste. It is idempotent; re-run any time.

> **Note on the AliExpress Web-Unlocker zone.** `provision()` sets up the SERP + CN zones. The
> AliExpress lane instead uses a **Web Unlocker** zone whose live default name is `web_unlocker1`
> (see `readers.py` — `BRIGHTDATA_SERP_ZONE` else `web_unlocker1`). If your BD account's Web
> Unlocker zone has a different name, set `BRIGHTDATA_SERP_ZONE` in Connections to that name.

### 6b. Installing the agent-side Bright Data MCP

For agent-driven research (running the pipeline skills from a Claude/agent session), install the
Bright Data MCP with the **same account API token**. The Bright Data plugin's
`agent-onboarding` skill installs the CLI + agent skills + auth in one command; or add the MCP
server pointed at your BD token directly. Because it's the same account, the token you already
put in Connections is the token the MCP uses — no second credential.

---

## 7. Product-finding

The finder turns a trend/keyword into **real products to list**. Everything it returns is a
**research/discovery** reference — the **listing price comes from the research source, never the
marketplace number**, and the actual supplier is the operator's private sourcing agent.

> **Full reference: [`FINDING-PIPELINE.md`](FINDING-PIPELINE.md)** — every lane, the AI-assist
> layer, the weight allocation, diagnostics, and the operator actions to widen supply. This section
> is the setup-oriented summary.

**5 lanes**, aggregated by `readers.found_products_for_head` (SKU-plan "Find products") and
`readers.find_keyword_products` (trend/keyword modal). Results cache on the data volume for 7 days;
the read path is cache-first (returns instantly, warms a cold keyword in a background thread). Any
lane that fails returns `[]` (advisory) — it never breaks the aggregate view.

**AI-assist is STANDARD on every find** (`api/app/ai_find_assist.py`): a cheap LLM (same locked
`claude-haiku-4.5` tier as the AI product gate) expands the keyword into ~5 related buyer phrasings
so the parallel lanes fan out across them and catch products a single literal query misses (cached
per keyword → ~0 steady-state cost; **fail-open** — no LLM gateway → just the bare keyword). The
response carries `ai_terms`. Impact: "bamboo sheets" went 10 → 50 products.

| Lane | Status | Method | Needs |
|---|---|---|---|
| **Marketplace → AliExpress** | **WORKS (workhorse)** | BD **Web Unlocker** on the listing page `/w/wholesale-<kw>.html?SortType=total_tranpro_desc` (sorted by orders) → per-product JSON island → `{item_id, title, price, compare_at, sold, image, url}`, sorted by **sold count**. English titles. `api/app/aliexpress_search.py`. | `BRIGHTDATA_API_TOKEN` + a Web Unlocker zone (`BRIGHTDATA_SERP_ZONE`, live default `web_unlocker1`). |
| **Marketplace → 1688 / Alibaba** | **WORKS** | **TMAPI** `/1688/search/items` — one **synchronous** call returns native 1688 offers (title / price / MOQ / supplier / image / url). `api/app/alibaba_search.py` → vendored `tmapi_1688.py`. **Titles are translated** (Chinese/mixed), prices CNY, no sold-count. | `TMAPI_TOKEN`. |
| **Marketplace → Temu** | **WORKS (via Apify)** | Apify actor `crw/temu-products-scraper` — keyword search → real Temu products (title / price / compare-at / sold / rating / image / url / seller), demand-ranked. Slow actor run → background warm, 7d cache. `api/app/apify_temu.py`. (BD can't scrape Temu.) | `APIFY_TOKEN` (works on Apify FREE). |
| **Google Competitor Catalog** (`competitor_catalog`) | **WORKS (roster-dependent)** | **LEADS with best-seller-validated proven sellers.** A fast catalog pass finds which roster stores CARRY the keyword, then `competitor_keyword_scan` ranks each carrying store's products off its REAL best-seller order (`/collections/all?sort_by=best-selling`, **top 10 pages**, plain-GET — see the best-seller note below) → proven sellers (a #12 best-seller beats deep-catalog dead stock), tagged `source=competitor_bestseller`, **each with the full product URL** so they flow straight into the listing handoff. Plain `/products.json` catalog matches fill the tail. `_competitor_bestseller_products` + `spy_catalog_products_for_keyword`. The single-store version is the Spy-dashboard scan modal ([`COMPETITOR-KEYWORD-SCAN.md`](COMPETITOR-KEYWORD-SCAN.md), `GET /api/product-research/competitor-scan`). | Tracked stores in the spy roster + an LLM gateway (AI-verify the matches). |
| **GoogleShopping Search** (`google_shopping`) | **WORKS (generic niches)** | 2-stage: DFS Merchant paid-scan → dropshipper **stores** → scan their catalogs. `api/app/seller_domain.py` resolves seller NAME→domain via a DFS organic SERP (DFS Merchant returns names, not domains — the lane previously starved to 0). `_is_dropshipper_store` (AI) gates brand-vs-dropshipper, so brand-dominated niches stay low. | DataForSEO + an LLM gateway (the dropshipper classifier). |
| **Amazon** | **WORKS (weight-gated)** | DFS Merchant Amazon keyword search → title / price / ASIN url / rating / **bought_past_month** + badges. `readers._amazon_lane_products`. | DataForSEO. |
| **Meta** | **WORKS (weight-gated, supplementary)** | Dropship advertisers running Meta ads for the keyword, via TrendTrack. `readers._trendtrack_competitors`. | TrendTrack. |

The three marketplace sub-sources run **concurrently** per build term and are round-robined into
the aggregate; the five lanes are then **weight-allocated** (`_allocate_lanes`) per Settings →
"Where products come from", with **dry-lane redistribution** (a dry Google lane's budget flows to
lanes that can supply, so a Google-led split doesn't collapse the find). A weight of **0 turns a
lane OFF**.

> **1688 switched to TMAPI (2026-07).** The old BD **Scraper-Studio collector**
> (`c_mqj72pgy2qnjxxluwm`, DCA REST) was async, slow, and kept **timing out to 0 products** — TMAPI
> replaced it with one synchronous call. The relevance filter now applies to **AliExpress only**,
> because 1688 (and Temu) return translated titles the English filter would false-drop.

> **Temu runs via Apify (2026-07).** BrightData can't scrape Temu — every BD method (Web Unlocker raw
> `/request`, MCP `scrape_as_markdown`, MCP scraping-browser) returns empty (PDD anti-bot wall), and
> the account's only Temu BD dataset is URL-collect (can't discover-by-keyword). The Apify actor
> **`crw/temu-products-scraper`** renders Temu's keyword search and returns real products — verified
> on the Apify **FREE** plan (`gio21` returns paid-gated mock data; `amit123` returns empty; `crw` is
> the one that works free). `_temu_cached` → `_fire_temu_warm` → `apify_temu.search`, gated on
> `APIFY_TOKEN`; the slow actor run stays in the background warm (7d cache) so it never blocks a Find.
> Each run spends Apify platform credits (FREE ≈ $5/mo), so the lane is keyword-cached.

> **Google Sponsored-PLA capture is server-side now (2026-07).** The Google "Sponsored products"
> carousel — the paying dropship competitors + advertiser domains (lunirel.com, Picadex, Zenido
> UK…) — is captured **via the Bright Data Scraping Browser** (`api/app/paid_shopping_scan_bd.py`,
> `GET /api/product-research/sponsored-plas?keyword=&geo=`), **NOT AdsPower** (which has been removed;
> the old local AdsPower profile is only a deprecated fallback). It connects over CDP to a real
> per-country residential Chrome (`playwright` `connect_over_cdp` — no local Chromium), routes through
> a `-country-<cc>` residential IP (the SERP's actual IP is what makes Google serve that country's
> ads), clicks through the consent wall, and parses the "Sponsored products" section. **DFS and the
> BD Web Unlocker cannot get these ads** (no ads block / labels stripped) — only the Scraping Browser
> can. Needs `BRIGHTDATA_BROWSER_CDP` (auto-provisioned; requires `BRIGHTDATA_CUSTOMER_ID`) + the
> `playwright` dependency in the API venv. Verified 2026-07 (air cooler GB → 29 sponsored products).

> **Best-seller order is fetched by a PLAIN GET, not Bright Data (2026-07).** The Google Competitor
> Catalog lane + the Spy scan modal both rank a store's products off its REAL best-seller order at
> `/collections/all?sort_by=best-selling`. Shopify robots.txt has `Disallow: /collections/*sort_by*`,
> and **BD honors robots on BOTH the Web Unlocker and the Scraping Browser** (`brob` block) → routing
> it through BD always returns 0. A plain `urllib`/`requests` GET + a browser UA does NOT consult
> robots.txt, so it fetches the real order (same method as the Step-06 `bestseller_snapshot.py` spy).
> `_bestseller_order` is **hard-capped at the top 10 pages** (`_BESTSELLER_MAX_PAGES=10`) + a 40s
> wall-clock budget (a slow store returns a partial top-N, never hangs). Chain: **plain datacenter IP
> → rotating US residential proxy** (`_bd_residential_proxy()`, derived off `BD_CN_PROXY`, re-routed
> `-country-cn`→`-country-us` — the resi zone; NEVER CN geo for US stores) when a store rate-limits /
> bot-blocks the datacenter IP; some stores hard-block even from residential → graceful **AI-verified
> merchandising-collection** fallback. It is **market-neutral** — a store's best-seller order is ONE
> order, so it's NOT geo-routed US/UK (US/UK targeting lives in the discovery lanes, not a single
> store's order). Fetch outcomes are logged (`_record_bs_fetch` → `bestseller_fetch_health()`) and
> shown in the **Connections-health "Competitor best-seller fetch"** entry so operators see when/why a
> fetch fell back. Caches: `bestseller-order-cache-v5`, `competitor-scan-cache-v5`.

**Diagnostics** (`/api/product-research/*`): `lane-health?keyword=` (per-source live/dry/error +
TMAPI client-load + Temu dataset state), `temu-datasets` (is a keyword-discover Temu dataset
subscribed?), `temu-probe?keyword=` (Web-Unlocker viability probe), `sponsored-plas?keyword=&geo=`
(server-side Sponsored-PLA competitor capture via the BD Scraping Browser), `competitor-scan?domain=
&keyword=` (single-store best-seller-validated scan), `bestseller-order-debug` / `collections-debug`.

### 7.1 Photo-duplicate check (Gemini vision) — never list the same product twice

The finder aggregates across lanes/competitors, so the SAME physical product can surface multiple
times (a belroshop AC == a marketplace AC == a Google-Shopping AC). `api/app/photo_dedup.py` groups
them so the operator never lists a duplicate more than the settings cap allows.

- **IMAGE-primary** (operator rule: *dedup by the photo, not title/description*). Gemini vision
  (locked cheap `google/gemini-2.5-flash` via the LLM gateway) groups products that show the **same
  physical product** across different listing photos. A title match only bridges products the vision
  could **not** see (missing image / fetch failed) — it never overrides the photo grouping (that
  title-override is what once merged a Frigidaire with two LGs).
- **Auto-runs ON FIND** — `maybe_fire_on_find` fires a background run the first time a keyword's found
  products are viewed (`GET /api/sku-plan/found`), when the count grows, or when the last run was
  title-only (self-heal). Cost-bounded: ≤60 images/run, incremental batches of 10, deduped per
  (store, keyword), **fail-open**. `POST /api/sku-plan/photo-dedup` forces a synchronous re-run.
- **Cap enforcement** — each photo group's members past the **`dedup_cap`** setting (Settings, default
  **3**) are flagged `photo_dup_drop`. The listing handoff (`actions.add_found_products`) reads the
  persisted drop set (`photo_dedup.dropped_keys`) and **skips** those products, so the same physical
  product is never listed more than `dedup_cap` times (returns `dup_skipped`).
- **Needs:** the LLM/vision gateway (`OPENROUTER_API_KEY` or `GEMINI_API_KEY`). With no key it still
  runs the title-identity + store-catalog layers (no photo grouping). **Background-thread gotcha:** the
  gateway creds are DB-derived, and a daemon thread has no request-scoped DB session — capture them in
  the request thread and `os.environ.setdefault` them inside the worker (see the code comment).

---

## 8. Research bad-keyword exclusion + AI

Junk keywords (brands, places, non-products, retailers like Lowe's/Walmart, SERP modifiers like
"deals on"/"nearby") are kept out of the SKU plan by three stacked layers. The single gate every
surface uses is `readers.is_listable_keyword(kw)` (`api/app/readers.py`), which combines:

1. **Deterministic product-domain filter** — `_is_generic_product_trend` rejects brands / events
   / travel / services / auto / airline / dates.
2. **The AI non-product hide-set** — `_is_nonproduct_kw` checks the persisted set of terms the AI
   gate flagged as not-a-dropship-product (banks, motels, perishables, oversized items, …).
3. **Title-quality filter** — `_segment_is_buildable` drops SERP-modifier / retailer / local-intent
   noise. The static lists (`api/app/readers.py`):
   - `_NON_SKU_PHRASES` — e.g. `how to`, `near me`, `for sale`, `deals on`, `pick up today`,
     `in stock`, `home depot`, `best buy`, …
   - `_NON_SKU_WORDS` — e.g. `review`, `vs`, `coupon`, `cheap`, `nearby`, `today`, `best`, plus
     distinctive retailer names `lowes`/`costco`/`wayfair`/`homedepot`/`bestbuy`/`ikea`/… (kept
     deliberately conservative so real products like "shop vac"/"price gun" survive).

**The AI product gate** (`api/app/ai_product_gate.py`) is the second-opinion classifier that
catches novel junk the static lists can't know — it asks the LLM *"is this a generic physical
product a dropshipper could source and resell?"* Design contract:

- **Model:** pinned to the cheap, reliable **`anthropic/claude-haiku-4.5`** on OpenRouter (on a
  Gemini-direct gateway it uses `gemini-2.5-flash`). It ignores the operator's chat model
  selector so the classifier never gets expensive.
- **Inclusive + fail-open:** rejects only when *confident* it's not a product; anything unsure is
  kept, and any gateway/API/parse error → included. It can only add safety, never block the
  pipeline or drop real products.
- **Cached + persisted:** verdicts are cached (`ai-keyword-verdicts.json`, seller verdicts in
  `ai-seller-verdicts.json`) so steady-state cost is ~0. The confident non-products are swept
  into the persisted hide-set at **`data/ai-nonproduct-keywords.json`** (via
  `sweep_nonproduct_keywords`), which the read filter then uses for a fast in-memory drop.

**Requirement:** the same LLM gateway (`OPENROUTER_API_KEY` or `GEMINI_API_KEY` in Connections)
powers **four** cheap-tier consumers — the AI assistant, the AI product gate, the **AI
finding-assist** (`ai_find_assist.expand_terms`, which widens every "Find products" run — see
[§7](#7-product-finding)), and the Google-Shopping lane's **dropshipper classifier**
(`_is_dropshipper_store`). With none set, all four fail open (the gate/finder run on the bare
inputs) and the assistant is disabled — the deterministic filters still run.

---

## 9. Deploy + vendored steps

The api service runs some of the research/listing pipeline as **subprocesses**. Those step
scripts are **vendored into the image** — they are copied into `api/steps/` and shipped in git,
because a GitHub-push deploy only ships **git-tracked** files.

- **Sync command:** `operator-app/scripts/sync-steps.sh` copies the step scripts (`.py` + a few
  extras) from the Google Stores tree into `api/steps/`. **Workflow: edit a step script → re-run
  `sync-steps.sh` → commit → push.** If you forget to re-sync + commit, the deploy ships the old
  script (or none), and the job degrades to a needs-operator handoff.
- **What's vendored** (see `sync-steps.sh` + `api/Dockerfile`):
  - `01-niche-discovery/scripts` (incl. `temu_dataset.py`)
  - `06-launch-general-store/scripts` (incl. `parse_ae_listing.py` — the canonical AliExpress
    parser)
  - `05-launch-niche-store/china-source-match/scripts` (incl. `alibaba_bulk.py`)
- **Path in the image:** the Dockerfile **strips the `steps/` prefix** —
  `COPY steps/06-…/scripts ./06-…/scripts` — so vendored scripts live at
  `/app/06-…/scripts` in the image (and `jobs.py` resolves them at
  `repo_root()/<step>/scripts` with `GOOGLE_STORES_ROOT=/app`). The Dockerfile also builds each
  step's `.venv` at exactly those paths. This is why the marketplace modules probe **both**
  layouts (`steps/06-…` locally, `06-…` in the image).

---

## 10. First-run verification

1. **API up:** `GET /api/health` → `{"ok": true, "commit": "<sha>", ...}`. The `commit` matches
   your last pushed SHA (confirms the deploy landed).
2. **Sign in:** `admin` / `admin`, or your `OWNER_NAME` / `OWNER_PASSWORD`. Owners create/rename
   everyone else inside the app.
3. **Connections green:** Settings → Connections shows DataForSEO + Bright Data + OpenRouter
   `configured`, and each store `connected`.
4. **Bright Data provisioned:** click the BD setup button → `gstores_serp` + `gstores_cn` created,
   SERP zone/password filled. Paste `BRIGHTDATA_CUSTOMER_ID` and re-run to fill `BD_CN_PROXY`.
5. **Research works:** run a keyword through "Find products" → real AliExpress + 1688 rows come
   back (Temu will be absent — expected, see [§7](#7-product-finding)). If a lane reads 0, hit
   `GET /api/product-research/lane-health?keyword=<kw>` to see which source is dry/erroring and why.
6. **Data persists:** on Railway, confirm `DATABASE_URL` (Neon) + a mounted `GOOGLE_STORES_DATA`
   volume so nothing is lost on the next deploy.

---

## Appendix — where each fact lives in code

| Topic | File |
|---|---|
| Credential registry (`API_FIELDS`, `STORE_FIELDS`), `runtime_get`, `as_env`, Shopify token minting, required scopes | `api/app/connections.py` |
| Bright Data zone auto-provision (`provision()`) | `api/app/brightdata.py` |
| AliExpress lane (Web Unlocker) | `api/app/aliexpress_search.py` |
| 1688/Alibaba lane (TMAPI `/1688/search/items`) | `api/app/alibaba_search.py` → vendored `tmapi_1688.py` |
| Temu lane (BD dataset; gated off — see [§7](#7-product-finding)) | `api/app/temu_search.py` |
| AI finding-assist (keyword → related buyer phrasings) | `api/app/ai_find_assist.py` |
| Seller name → domain resolver (Google Shopping lane) | `api/app/seller_domain.py` |
| Finding-lane wiring + allocation (`found_products_for_head`, `find_keyword_products`, `_marketplace_lane_products`, `_allocate_lanes`) | `api/app/readers.py` |
| Finding diagnostics (`finding_lane_health`, `temu_datasets`, `temu_web_probe`) | `api/app/readers.py` → `/api/product-research/*` in `api/app/main.py` |
| Full finding-pipeline reference | [`FINDING-PIPELINE.md`](FINDING-PIPELINE.md) |
| Marketplace lane wiring + keyword-exclusion (`is_listable_keyword`, `_NON_SKU_*`, hide-set) | `api/app/readers.py` |
| AI product gate (`classify`, model pin, cache) | `api/app/ai_product_gate.py` |
| Host env vars (`data_root`, `GOOGLE_STORES_DATA`, `TENANT`, CORS) | `api/app/config.py` |
| DB adapter (SQLite ↔ Postgres switch on `DATABASE_URL`) | `api/app/db.py` |
| Owner login bootstrap (`OWNER_NAME`/`OWNER_PASSWORD`, default `admin`) | `api/app/users.py`, `api/app/auth.py` |
| Vendored-steps sync | `operator-app/scripts/sync-steps.sh`, `api/Dockerfile` |
| Deploy mechanics (services, watch-paths, Neon, pricing) | `DEPLOY.md` |
| Own deployment + multi-business | `SETUP-OWN-DEPLOYMENT.md` |
