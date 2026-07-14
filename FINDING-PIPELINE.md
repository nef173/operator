# Operator App — Product-Finding Pipeline

The reference for how the app turns a **trend/keyword** into **real products to list**. This is
the in-app, server-side finder that runs inside the FastAPI request (not the agent-MCP discovery
funnel in `06-launch-general-store/`). Code lives in `api/app/readers.py` + a handful of helper
modules; nothing here reimplements pipeline intelligence — it aggregates live scrapes + the
tracked-store catalogs behind one call.

**Entry points (one shared engine, two callers):**

| Function (`api/app/readers.py`) | Used by | Lanes it runs |
|---|---|---|
| `found_products_for_head(store, keyword, terms)` | SKU-plan "Find products" drill-in (`GET /api/sku-plan/found`) | ALL 5 lanes + weight allocation |
| `find_keyword_products(keyword, terms, fetch)` | Trend/keyword `FindProductsModal`, movers, plan pools | Marketplace + Amazon (store-independent) |

Both are fronted by a **cache-first** read: the instant path (`find_keyword_products_instant` /
`find_keyword_products_instant`) serves the 7-day finding cache synchronously and, on a cold
keyword, fires **one** deduped background warm thread (`_fire_finding_warm`) that does the live
fetch → cache. The client re-polls while `warming`. Live fetches are ~30–45 s cold, ~0 warm.

Everything found is a **RESEARCH/DISCOVERY reference**, never a supplier and never the listing
price. The listing price comes from the research source per
`feedback_price_basis_from_research_source_not_1688` / `feedback_cogs_basis_use_compare_at_not_flash_sale`;
the actual supplier is the operator's private sourcing agent (AliExpress / Temu / 1688 / Amazon /
competitor stores are RESEARCH-ONLY).

---

## The AI-assist layer (STANDARD on every finding surface)

Before the lanes fan out, the keyword is expanded by a **cheap LLM** into related buyer phrasings
so the parallel lanes catch products a single literal query misses.

- **Module:** `api/app/ai_find_assist.py` — `expand_terms(keyword)` returns `[keyword]` + up to 5
  AI-proposed related search terms (synonyms + form/spec variants of the SAME product), deduped,
  head-first.
- **Wired into BOTH** `found_products_for_head` (always) and `find_keyword_products` (on a live
  `fetch=True` warm — cache-only overviews reuse the already-warmed `ai-terms` cache for free).
- **Model:** the same locked cheap tier as the AI product gate (`anthropic/claude-haiku-4.5` on
  OpenRouter; `gemini-2.5-flash` on a Gemini-direct gateway). Ignores the operator's chat-model
  selector so it never gets expensive.
- **Cached + fail-open:** each unique keyword is expanded once and cached at
  `finding-cache/ai-terms/<slug>.json`; steady-state cost is ~0. With **no LLM gateway configured**
  (`OPENROUTER_API_KEY` / `GEMINI_API_KEY`), or on any error, it degrades to just `[keyword]` — the
  finder runs exactly as before, never blocked.
- **Response carries `ai_terms`** so the UI can show the phrasings the lanes fanned out across.
- **Impact (measured):** "bamboo sheets" went from 10 products (all AliExpress) → 50 (a balanced
  25 AliExpress + 25 1688) once the assist + the marketplace fixes landed.

---

## The 5 finding lanes

### 1. Marketplace lane — `_marketplace_lane_products`
The operator's dropship-SOURCING marketplaces. Three sub-sources fetched **concurrently** per
build term, deduped + relevance-filtered into per-source buckets, then **round-robined** (one row
per source per cycle) up to an aggregate cap of ~50 — so every source that returned is represented,
but one hot source can still fill the cap when the others are dry.

| Sub-source | Status | Method / module | Creds |
|---|---|---|---|
| **AliExpress** | **WORKS (the reliable workhorse)** | BD **Web Unlocker** on the listing page `/w/wholesale-<kw>.html?SortType=total_tranpro_desc` (sorted by orders) → per-product JSON island parsed by regex → `{item_id, title, price, compare_at, sold, image, url}`, sorted by **sold count** (proven demand). `api/app/aliexpress_search.py` (reuses the canonical parser `06-launch-general-store/scripts/parse_ae_listing.py`). Item pages are blocked; **listing pages render**. English titles. | `BRIGHTDATA_API_TOKEN` + a Web Unlocker zone (`BRIGHTDATA_SERP_ZONE`, live default `web_unlocker1`) |
| **1688 / Alibaba** | **WORKS** | **TMAPI** `/1688/search/items` — a managed API that runs 1688's own keyword search behind one `apiToken` and returns native 1688 offers (title / price / MOQ / supplier / image / url) in a **single synchronous call**. `api/app/alibaba_search.py` → vendored canonical client `tmapi_1688.py`. Prices are CNY; titles are **translated** (Chinese / mixed). No sold-count (B2B). | `TMAPI_TOKEN` |
| **Temu** | **WORKS (via Apify)** | Apify actor `crw/temu-products-scraper` runs Temu's keyword search → real products (title / price / compare-at / `sales_num` sold / rating / image / product url / mall seller), demand-ranked. Cents→$, "4.9K+"→4900. Slow actor run → background warm (`_fire_temu_warm`), 7d cache. `api/app/apify_temu.py`. **BD can't scrape Temu** (see "Temu status" below). | `APIFY_TOKEN` (works on Apify FREE) |

**Relevance filter applies to AliExpress ONLY.** AliExpress serves English titles, so an
English keyword filter (`_keyword_relevant`) drops the occasional off-topic AliExpress result.
1688 (TMAPI) and Temu return **translated** titles (e.g. Spanish "Sábanas de Bambú", Chinese)
whose latin tokens never equal the English keyword — so the English filter would false-drop them.
Those sources already keyword-searched server-side, so their rows are trusted as-is. (This is
exactly why 1688 previously read 0 for "bamboo sheets" despite TMAPI returning matches.)

> **All three marketplace sub-sources are live:** `aliexpress` (BD Web Unlocker) + `temu` (Apify) +
> `alibaba`/1688 (TMAPI). If the `_marketplace_lane_products` docstring still reads as 1688/Temu being
> "kept out of the live lane", that's a stale comment — trust the code + this doc.

**Why TMAPI for 1688 (switched 2026-07):** the old BD **Scraper-Studio collector**
(`c_mqj72pgy2qnjxxluwm`, DCA REST `/dca/trigger` → poll `/dca/dataset?id=`) was async, slow
(30–100 s), and flaky — it kept **timing out and returning 0 products**. TMAPI replaces it with one
synchronous call. The collector is retired.

### 2. Google Competitor Catalog (`competitor_catalog`) — best-seller-LED, then `spy_catalog_products_for_keyword`
The operator's PRIMARY model (scan known Google dropship-competitors), now **best-seller-validated
first**. Two stages inside the one lane:

1. **Proven sellers LEAD** (`_competitor_bestseller_products`) — a fast catalog pass finds which roster
   stores CARRY the keyword, then `competitor_keyword_scan` best-seller-validates those stores (off the
   real `/collections/all?sort_by=best-selling` order) → their actual best-selling SKUs for the keyword,
   ranked by best-seller position, tagged `source: "competitor_bestseller"`, each with the **full
   product URL** for the listing handoff. Bounded 25 s + per-store 7d order cache + 2-day result cache;
   uncached stores warm in the background. Verified: `aircon` → zerobreeze Mark 3/Mark 2 (BS#1/#2),
   belroshop portable AC (BS#12), … 8 proven sellers across the roster.
2. **Broader catalog matches** (`spy_catalog_products_for_keyword`) — the rest of the roster's
   `/products.json` keyword matches, appended after the proven sellers (deduped).

- **ROSTER-DEPENDENT** — coverage is only as broad as the tracked stores; **grows over time** as the
  Google Shopping lane records newly-discovered dropshippers into the roster (`spy_roster_persist.py`).
- The single-store version is the **Spy dashboard → Scan** modal (`competitor_keyword_scan`,
  `COMPETITOR-KEYWORD-SCAN.md`) — same engine, one store. The best-seller order is **market-neutral**
  (one store = one best-seller order; NOT geo-routed US/UK — US/UK targeting lives in the discovery
  lanes, not a single store's order). Best-seller fetch = **plain GET** (top 10 pages), NOT Bright
  Data (BD honors Shopify's robots `Disallow: /collections/*sort_by*`); rotating US residential proxy
  fallback for rate-limited stores; outcomes shown in the Connections-health "best-seller fetch" entry.

### Best-seller-validated competitor keyword scan — `competitor_keyword_scan` (frequently run)

The **sales-validated variant** of the competitor-catalog lane, and the one the operator runs most
often when researching a specific competitor's Google Shopping store. Problem it solves:
keyword-matching a competitor's whole Shopify `/products.json` surfaces **DEAD STOCK** — products
that match the keyword but rank #2000 in the store and **never sold**. `/products.json` carries **no
sales signal**, so a plain catalog match can't tell a proven seller from deep-catalog filler. This
scan matches ONLY the competitor's proven best-sellers.

**How it works** (rank off the store's REAL best-seller order — the working spy method):

1. **Best-seller order** — `_bestseller_order` walks `/collections/all?sort_by=best-selling` deep
   (top ~500), junk-filtered, via a **plain `urllib` GET + browser UA** (NOT Bright Data). Cached per
   (domain, geo). Retries + paces pages so a datacenter IP isn't throttled off deep pages.
2. **Keyword-match the order** — `_bestseller_keyword_matches` keeps the best-sellers matching the
   keyword: strong synonym token, OR membership in the store's `/search` set (recall for names the
   synonyms miss), then **AI-verified** (drops off-target) and **de-duplicated** (dropship re-listings).
   Enriched title/price via `/products/<h>.json` (→ `/search` price fallback). RANKED by best-seller
   position → the per-keyword tier list = `validated`.
3. **LISTED** = the store's other `/search?q=<kw>` products not in the best-seller top ~500.
4. **Fallback** — if the store hard-blocks the plain fetch (empty order), `_merchandising_maps` uses the
   robots-allowed Best-Sellers + keyword-category **collection** JSON instead. `method` says which ran.

**Why the plain GET, not BD:** `?sort_by=best-selling` is `Disallow`ed in Shopify robots.txt, and BD
honours robots on BOTH the Web Unlocker AND the Scraping Browser (`brob`) — so BD always returned 0. A
plain request doesn't consult robots.txt and fetches the real order. Net effect: nikinewyork's aircon
surfaces at best-seller #74/#180/#265 (deep — the old top-55 scan returned 0); belroshop's at #12/#268.

**Endpoint:** `GET /api/product-research/competitor-scan?domain=<>&keyword=<>&geo=US` →

```json
{ "domain": "<>", "keyword": "<>", "geo": "US", "method": "bestseller-order",
  "keyword_matches": 63, "bestsellers_seen": 417,
  "validated": [ { "tier": "bestseller", "bestseller_rank": 74, "title": "…", "price": "83.99",
                   "url": "…", "handle": "…" } ],
  "n_validated": 4, "results": [ /* validated + listed, capped */ ], "note": "…" }
```

`geo` is `US`/`GB` (display + UK toggle in the modal). Works on **any** Shopify store, roster or not.
See **`COMPETITOR-KEYWORD-SCAN.md`** for the run-it workflow + the `bestseller-order-debug` diagnostic.

**Use this vs. the plain competitor-catalog lane (§2):**

| You want… | Use |
|---|---|
| A competitor's **PROVEN sellers** for a keyword (research one store deeply, no dead stock) | **Best-seller-validated scan** (`competitor_keyword_scan`) — this one |
| **Any** roster catalog match across **all** tracked stores (broad supply into a Find) | Competitor-catalog lane (`spy_catalog_products_for_keyword`, §2) |

### 3. GoogleShopping Search (`google_shopping`) — `_google_shopping_catalog_products`
Two-stage: **(A)** DFS Merchant Google-Shopping paid-scan (`shopping_scan_dfs.py`) surfaces the
PLA listings for the keyword → identify the dropshipper **STORES** behind them; **(B)** scan each
store's catalog for the keyword. When a store has no catalog scan yet, a **background
shopping-scan** is fired (`_fire_shopping_scan`).

- **The seller→domain fix (`api/app/seller_domain.py`, NEW):** DFS Merchant returns seller
  **display NAMES** ("Cozy Earth"), never the store's **domain** (verified: `domain=null` for 80/80
  listings), so stage B starved to 0 for ~every keyword. `seller_domain.resolve(name)` recovers the
  domain via a DFS **organic** SERP (`"<seller> official site"` → first result host that isn't a
  marketplace / retail giant / social / review site). **Cached** per seller (a brand's domain is
  stable → steady-state ~0), **bounded**, and resolved in **parallel**.
- **Stage B gate (`_is_dropshipper_store`, AI classifier):** decides brand-vs-dropshipper on the
  resolved store's catalog. Brand-dominated niches stay low (correctly — we don't scrape a real
  brand's own store); generic / gadget niches flow.

### 4. Amazon lane — `_amazon_lane_products`
DFS Merchant **Amazon** keyword search (`shopping_scan_dfs.fetch_amazon` — Amazon has a
SYNCHRONOUS live variant, unlike Google merchant). Parses title / price / canonical
`amazon.com/dp/<ASIN>` url / image / rating / votes / **bought_past_month** (demand) + Choice /
Best-Seller badges. Per build-term, parallel, 7-day cache, demand-sorted. **Weight-gated** (only
runs when its source weight > 0).

> DFS Amazon quirk (for maintainers): the Amazon endpoint **rejects** `language_name` (which Google
> requires) — it needs `language_code` (e.g. `en_US`).

### 5. Meta lane — `_trendtrack_competitors`
Dropship **advertisers** running Meta ads for the keyword, via **TrendTrack** — supplementary
corroboration, not a leading indicator. **Weight-gated.**

---

## Allocation — `_allocate_lanes` (two-pass, dry-lane redistribution)

`found_products_for_head` targets the plan's `products_per_build`. Lane weights come from
Settings → **"Where products come from"** (`sku_plan` `source_weights`; a weight of **0 turns a
lane OFF** — it isn't fetched or shown).

1. **Pass 1** — every lane gets `min(available, its weighted quota)`.
2. **Pass 2** — the budget freed by lanes that came back **thin** (a dry Google lane, tracked
   stores that don't carry the term — common for many keywords) is handed to lanes that still have
   supply, in operator-priority order (`competitor_catalog → google_shopping → amazon →
   marketplace → meta`).

So a Google-led 40/40/20 split whose two Google lanes are dry fills toward the target **from
marketplace** instead of collapsing to ~10; when every lane supplies its quota there's no surplus,
the weighted split holds exactly, and no lane floods past its share. The total never exceeds the
target. With **no weights persisted**, every lane is on and uncapped (show everything).

---

## Temu status — WORKS via Apify (BrightData can't scrape Temu)

Temu keyword-finding runs on the server via the **Apify actor `crw/temu-products-scraper`**
(`api/app/apify_temu.py`), gated on `APIFY_TOKEN`. It returns real Temu products (title, price +
market/compare-at price, `sales_num` sold, rating, reviews, image, product URL, mall/seller),
demand-ranked. Verified live: a find for "neck massager" produced `AliExpress 8 + Temu 8 + 1688 8`.

**Why not BrightData** (every path tested, all failed — do NOT re-attempt a BD Temu scraper):
- **BD Temu dataset** — the account's only Temu dataset is **"Temu.com"**, **collect-by-URL only**:
  can't discover-by-keyword → empty snapshots (that still burn BD credits).
- **BD Web Unlocker** (raw `/request`), **MCP `scrape_as_markdown`**, **MCP scraping-browser** all
  return **empty** for Temu's search page — PDD's anti-bot wall.

**Actor choice matters:** `gio21/temu-scraper` returns paid-gated **mock** data on the FREE plan
(`_mock:true`); `amit123/temu-products-scraper` returned empty. **`crw`** is the one that returns
real data on FREE. The sync run endpoint (`run-sync-get-dataset-items`) is slow (~1–3 min server-side
render), so `_temu_cached` → `_fire_temu_warm` → `apify_temu.search` runs it in a **background warm**
(7d cache) — Temu never blocks a Find and fills in on the next one. Each run spends Apify platform
credits (FREE ≈ $5/mo), so the lane is keyword-cached. The connector-health panel (Settings →
Connections) shows the Apify/Temu status.

---

## Diagnostics

All under `/api/product-research/*` (thin wrappers over `readers.py`):

| Endpoint | Reader | What it tells you |
|---|---|---|
| `GET /api/product-research/lane-health?keyword=<kw>` | `finding_lane_health` | Runs each marketplace sub-source (AliExpress, 1688/TMAPI, Temu) + Amazon **in isolation** with error capture → per-source live / dry / erroring **and why**. Includes the **TMAPI client-load + token state** and the **Temu dataset resolution** so a 0 is explainable (import failed / no token / no dataset / genuinely empty) instead of a silent blank. LIVE fetch — call sparingly. |
| `GET /api/product-research/temu-datasets` | `temu_datasets` | The account's BD Temu-matching datasets (name + id) via `GET /datasets/list` — one fast call, no snapshot trigger. Confirms whether a **keyword-discover** dataset is subscribed (auto-resolve picks the first name-match, which may be a URL-collect type that can't discover-by-keyword). |
| `GET /api/product-research/temu-probe?keyword=<kw>` | `temu_web_probe` | Evidence probe for the Temu Web-Unlocker path — does BD return extractable Temu data for a keyword? (Confirms the wall before any parser work.) |
| `GET /api/product-research/sponsored-plas?keyword=<kw>&geo=<cc>` | `paid_shopping_scan_bd` | Google **Sponsored-products** carousel (the paying dropship competitors + advertiser domains) captured **server-side via the BD Scraping Browser** (a real per-country residential Chrome driven over CDP with `playwright` — NO AdsPower). Routes through a `-country-<cc>` residential IP (the SERP's actual IP decides ad-serving), handles Google's consent wall, parses the "Sponsored products" section → `{sponsored_products, sponsored_results, advertisers}`. DFS and the BD Web Unlocker CANNOT get these ads (no ads block / labels stripped); only the Scraping Browser can. Needs `BRIGHTDATA_BROWSER_CDP` (auto-provisioned; needs `BRIGHTDATA_CUSTOMER_ID`). Verified 2026-07 (air cooler GB → 29 sponsored products). |
| `GET /api/product-research/competitor-scan?domain=<d>&keyword=<kw>` | `competitor_keyword_scan` | Single-store best-seller-validated scan — a competitor's **proven sellers** for a keyword ranked off its real `/collections/all?sort_by=best-selling` order (plain GET, top 10 pages), NOT dead stock. The lane-level version (`competitor_bestseller`) runs this across the roster. See `COMPETITOR-KEYWORD-SCAN.md`. |
| `POST /api/sku-plan/photo-dedup` `{store,keyword}` · `GET /api/sku-plan/found` | `photo_dedup.run` / `maybe_fire_on_find` | **Gemini-vision photo-duplicate check** — groups the SAME physical product across lanes by **image** (title only bridges imageless products), flags members past the `dedup_cap` setting so the listing handoff never lists a duplicate more than N times. Auto-runs on `/found`; POST forces a re-run. Full spec: `ONBOARDING.md` §7.1. Needs the LLM/vision gateway. |

---

## Known limits + operator actions to improve supply

| Limit | Why | What the operator can do |
|---|---|---|
| **Temu absent** from every Find | The subscribed Temu dataset is URL-collect; Temu's search page is anti-bot-walled; Apify dropped. | **Subscribe a keyword-DISCOVER Temu dataset** in the BD Web-Scraper library and set its `gd_…` id in `BRIGHTDATA_TEMU_DATASET` (+ optionally `BRIGHTDATA_DATASET_TOKEN`). The lane then re-enables automatically. Verify with `temu-datasets` / `lane-health`. |
| **Competitor-catalog lane finds nothing** for a niche | Roster-dependent — the tracked spy stores don't carry that category. | **Add niche-relevant dropshippers to the spy roster.** The Google Shopping lane also records discovered dropshippers over time, so coverage compounds as more keywords are researched. |
| **Google Shopping lane low** on a niche | Correct behaviour — the niche is brand-dominated, so `_is_dropshipper_store` gates the brands out. | Nothing to fix; generic / gadget niches flow. If a store is wrongly classified, that's a `_is_dropshipper_store` tuning question (code). |
| **1688 titles/prices non-English / CNY** | 1688 is a CN domestic B2B platform (TMAPI returns its native offers). | Expected — 1688 is a research/COGS-basis reference, not a listing source. The marketplace relevance filter is already Temu/1688-exempt so these aren't false-dropped. |
| **Find is slow (~30–45 s) on a cold keyword** | Live BD Web Unlocker (AliExpress) + DFS (Amazon) fetches. | Nothing — the cache-first path returns instantly and warms in the background; the second Find on that keyword is fast (7-day cache). |
| **AI-assist not widening the search** | No LLM gateway configured. | Set `OPENROUTER_API_KEY` (or `GEMINI_API_KEY`) in Connections. Without it the finder still runs on the bare keyword (fail-open). |

---

## Where each fact lives in code

| Topic | File |
|---|---|
| Lane aggregation + weight allocation | `api/app/readers.py` — `found_products_for_head`, `find_keyword_products`, `_allocate_lanes` |
| Marketplace lane (concurrency + round-robin + AliExpress-only relevance filter) | `api/app/readers.py` — `_marketplace_lane_products`, `_aliexpress_cached`, `_alibaba_cached`, `_temu_cached` |
| AliExpress fetch | `api/app/aliexpress_search.py` (+ canonical parser `06-launch-general-store/scripts/parse_ae_listing.py`) |
| 1688 / Alibaba fetch (TMAPI) | `api/app/alibaba_search.py` → `tmapi_1688.py` (vendored under `05-launch-niche-store/china-source-match/scripts`) |
| Temu (BD dataset; gated off) | `api/app/temu_search.py` |
| AI finding-assist | `api/app/ai_find_assist.py` |
| Seller name → domain resolver | `api/app/seller_domain.py` |
| Competitor keyword scan (best-seller-order) | `api/app/readers.py` — `competitor_keyword_scan`, `_bestseller_order`, `_bestseller_keyword_matches`, `_enrich_products` (+ fallback `_merchandising_maps`); `GET /api/product-research/competitor-scan` (+ `bestseller-order-debug`, `collections-debug`) in `api/app/main.py`; workflow doc `COMPETITOR-KEYWORD-SCAN.md` |
| Google Shopping 2-stage + dropshipper gate | `api/app/readers.py` — `_google_shopping_catalog_products`, `_is_dropshipper_store`, `_fire_shopping_scan`; `api/app/shopping_scan_dfs.py`; `api/app/spy_roster_persist.py` |
| Google **Sponsored-PLA** capture (server-side, BD Scraping Browser over CDP) | `api/app/paid_shopping_scan_bd.py` → `GET /api/product-research/sponsored-plas` in `api/app/main.py` |
| Amazon lane | `api/app/readers.py` — `_amazon_lane_products`; `api/app/shopping_scan_dfs.py` |
| Meta lane | `api/app/readers.py` — `_trendtrack_competitors` |
| Diagnostics endpoints | `api/app/main.py` (`/api/product-research/*`) → `readers.finding_lane_health` / `temu_datasets` / `temu_web_probe` |

See also `ONBOARDING.md` §7 (setup-oriented summary of the same lanes) and
`memory/project_amazon_marketplace_finding_lanes.md` (the build history).
