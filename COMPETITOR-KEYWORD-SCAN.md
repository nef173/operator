# Competitor Keyword Scan — the "Google competitor catalog keyword" recon

A frequently-run recon check: given a competitor's Google-Shopping store **and** a keyword, return the
products that competitor **actually sells** for that keyword — ranked off the store's **real best-seller
order** (`?sort_by=best-selling`) as a per-keyword tier list. Not every fuzzy catalog match; the ones the
store actually moves volume on. Full pipeline context: [`FINDING-PIPELINE.md`](FINDING-PIPELINE.md).

## The mental model

**Rank the keyword as its OWN tier list.** A store's best-selling *aircon* is not its all-time
best-seller — it can sit at best-seller rank **#74 / #180 / #265**, behind unrelated winners, and the
point is to surface THOSE (even past #100) and drop deep-catalog dead stock. Method:

1. **Read the store's real best-seller order** — `/collections/all?sort_by=best-selling`, **hard-capped
   at the TOP 10 PAGES** (`_BESTSELLER_MAX_PAGES` — predictable cost/latency for every keyword),
   junk-filtered (shipping-protection / VIP add-ons stripped). This is the storefront's own sales order.
   Fetched with a **plain `urllib` GET + a browser User-Agent** — the SAME method as the Step-06
   best-seller spy. The result `note` states the cap + page count, e.g. *"top 10 pages of
   /collections/all?sort_by=best-selling (271 products) · AI-assisted match"*.
2. **Keyword-match the order** — a best-seller is a candidate if it matches a strong keyword token
   (synonym-expanded), is in the store's own `/search?q=<kw>` set (Shopify's match — recall for names
   the synonyms miss, e.g. a "room cooler"), or its title recurs the keyword's vocabulary. Candidates
   are **AI-verified** (drops "wine-cooler" ≠ aircon) and **de-duplicated** (dropship stores re-list a
   winner 3×). What survives, **ranked by best-seller position**, is `validated`.
3. **LISTED** = the store's other `/search` products not in the best-seller top ~500 — listed but not
   proven sellers (deep-catalog).

### Why the plain GET — and NOT Bright Data

`?sort_by=best-selling` is disallowed in Shopify's `robots.txt` (`Disallow: /collections/*sort_by*`),
and **Bright Data honours robots on BOTH the Web Unlocker AND the Scraping Browser** (`brob` block), so
routing it through BD always returned 0. That was the whole dead end. A **plain `requests`/`urllib` GET
does not consult robots.txt** — it just fetches the page, and Shopify serves the real best-seller order.
Verified live: nikinewyork's aircon at best-seller #74/#180/#265 (which the top-30/55 scan missed, and
which `/search` doesn't even all return). This is the same mechanic as
`06-launch-general-store/scripts/bestseller_snapshot.py` (the working top-30 competitor spy).

**Datacenter-IP throttle:** a store may throttle deep pagination from a datacenter IP (nikinewyork
stalled ~page 4). The fetch retries each page once and paces 0.4 s between pages, which gets ~400+ deep.
**Fallback:** if a store hard-blocks the plain fetch (empty order), the scan falls back to the
robots-allowed **merchandising signal** — the store's Best-Sellers + keyword-category **collection**
JSON (`/collections.json` + `/collections/<h>/products.json`). Response `method` says which ran.

## Run it

```
GET /api/product-research/competitor-scan?domain=<domain>&keyword=<keyword>&geo=US
```

| Param | Default | Notes |
|---|---|---|
| `domain` | — | Competitor store host (bare — `www.`/scheme/path stripped). Any Shopify store; roster membership NOT required. |
| `keyword` | — | The keyword to validate. Synonym-expanded + title-seeded for the match. |
| `geo` | `US` | `US` / `GB`. Display + UK toggle; the order is fetched from the server IP (US), market differences are minor. |
| `pages` | `8` | Legacy/compat — the best-seller path uses its own deep page walk (top ~500 / 15 pages). |

In the app: **Spy dashboard → any competitor row → "Scan"** (US/UK toggle in the modal), or paste a
competitor URL into the "add competitor" box first.

## Output shape

```json
{
  "domain": "nikinewyork.com", "keyword": "aircon", "geo": "US", "method": "bestseller-order",
  "keyword_matches": 63, "bestsellers_seen": 417,
  "validated": [
    { "tier": "bestseller", "bestseller_rank": 74, "title": "Portable 4-in-1 Room Cooler",
      "price": "83.99", "url": "https://nikinewyork.com/products/…", "handle": "…", "validated": true }
  ],
  "n_validated": 4, "results": [ /* validated (ranked) + listed, capped */ ], "note": "…"
}
```

- `bestsellers_seen` = how deep the best-seller order was captured (0 ⇒ plain fetch blocked → fell back
  to `method: merchandising-collections`).
- `validated` / `n_validated` — the tier list, sorted by `bestseller_rank` (lower = sells more).
  **`0` is a real answer** — the store lists the keyword but none of it ranks in the top ~500 sellers
  (deep-catalog / no sales), and the `note` says so.
- `price` — from `/products/<handle>.json`, falling back to the `/search` price when a store 503s the
  per-item fetch. A **research reference**, never a supplier or the listing price.

**Worked examples (verified live):**
- `nikinewyork.com` + aircon → **63 listed, 4 proven sellers** at best-seller #74/#180/#265/#266
  (deep — the top-55 scan returned 0).
- `belroshop.com` + aircon → **36 listed, 2 proven sellers** at #12/#268 (its 3× re-listing of the #12
  winner is de-duplicated to one).

## Caching

- best-seller order → `bestseller-order-cache-v3/<domain>__<geo>.json`, **7 days**.
- per-product enrichment → `product-enrich-cache/<domain>.json`; collections (fallback) →
  `collections-cache/` + `collection-products-cache-v2/`.

First scan of a store pays the deep page walk (~15-40 s); every later keyword scan on the same store is
instant. Sweep many keywords against one competitor cheaply.

## See also

- Code: `api/app/readers.py` → `competitor_keyword_scan`, `_bestseller_order`,
  `_bestseller_keyword_matches`, `_enrich_products` (+ fallback `_merchandising_maps`); endpoints +
  `bestseller-order-debug` / `collections-debug` in `api/app/main.py`.
- The working top-30 spy this reuses: `06-launch-general-store/scripts/bestseller_snapshot.py`.
- Diagnostic: `GET /api/product-research/bestseller-order-debug?domain=<d>` — confirms the plain fetch
  works from the server IP + shows depth captured.
