"""Server-side Google-Shopping recon — the ALWAYS-ON scan for background automation.

Why this exists
---------------
The premium scan (01c/paid_shopping_scan.py) drives a real fingerprinted Chrome through
AdsPower to SCREENSHOT the live page. That can't run during 24/7 background automation —
nobody is there to open the AdsPower 'warmup us' profile. So this is the production scan
path: it reads the SAME Google Shopping landscape directly from DataForSEO's structured
API, with no browser and no screenshots at all.

What it captures (the obvious "is this a *real* shopping scan?" questions, answered):
  * IP / COUNTRY — DataForSEO runs the query from inside the target country (location_name),
    so the prices/sellers/grid are the real geolocated US (or UK) Shopping results, not
    whatever country our server sits in.
  * The full Shopping TAB grid — via the Merchant API (`merchant/google/products`), the
    actual Shopping tab, not just the few "popular products" on the web page. ~40 products
    with price + compare-at (old_price) + seller + domain + ratings + review counts + the
    **`shop_ad_aclk` Sponsored flag** — which lets us see, server-side, WHICH competitors are
    running Shopping ADS (a real paid signal, no browser needed).
  * Google's category / intent ROWS — the Shopping carousels ("Gel-Based Cooling Mats", …)
    are exactly the sub-segments the AdsPower scan reads off the page; one SKU maps to each.
  * Searched sub-keywords — related searches + "people also ask" from the organic SERP.
  * Structured DATA, not vision-on-a-screenshot — titles/prices are exact, never an OCR guess.

The one thing only AdsPower adds is the literal Sponsored-PLA *screenshots*; the ad DATA
itself (who is advertising) we already get from `shop_ad_aclk`.

It writes the SAME two files as the AdsPower scan (`paid-scan.json` + `paid-landscape.md`)
so everything downstream consumes either tier identically.

Stdlib only. Creds come from the process env (Connections -> jobs._base_env), with a final
fallback to the project-root .env so it authenticates even outside the app.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_DFS = "https://api.dataforseo.com/v3"
_DFS_ORGANIC = f"{_DFS}/serp/google/organic/live/advanced"
_DFS_MERCHANT_POST = f"{_DFS}/merchant/google/products/task_post"
_DFS_MERCHANT_GET = f"{_DFS}/merchant/google/products/task_get/advanced"
# Amazon merchant is a SEPARATE endpoint with a LIVE/advanced variant (synchronous — no polling,
# unlike Google merchant which is task+poll only). NOTE: Amazon REJECTS `language_name` with
# "40501 Invalid Field" (Google requires it) — Amazon takes `language_code` instead.
_DFS_AMAZON_LIVE = f"{_DFS}/merchant/amazon/products/live/advanced"
_DFS_SV = f"{_DFS}/keywords_data/google_ads/search_volume/live"

# Minimal geo -> DFS location_name. US + UK is the project's locked geo scope.
_GEO_LOCATION = {
    "US": "United States", "USA": "United States", "UNITED STATES": "United States",
    "UK": "United Kingdom", "GB": "United Kingdom", "UNITED KINGDOM": "United Kingdom",
}


def _load_creds() -> tuple[str, str]:
    """DFS user/pass from env, falling back to the project-root .env (key=value lines)."""
    u = (os.environ.get("DATAFORSEO_USERNAME") or "").strip()
    p = (os.environ.get("DATAFORSEO_PASSWORD") or "").strip()
    if u and p:
        return u, p
    here = Path(__file__).resolve()
    for parent in here.parents:
        env = parent / ".env"
        if env.is_file():
            for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "DATAFORSEO_USERNAME" and not u:
                    u = v
                elif k == "DATAFORSEO_PASSWORD" and not p:
                    p = v
            if u and p:
                return u, p
    return u, p


def _post(url: str, payload, auth_header: str, method: str = "POST") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Basic {auth_header}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 (trusted DFS endpoint)
        return json.loads(r.read().decode())


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Merchant (the real grid)
def fetch_merchant(keyword: str, location_name: str, auth: str, poll_secs: int = 180,
                   depth: int = 100) -> dict:
    """Post a Merchant Google-Products task and poll task_get until it's ready (or times out).
    Returns {"items": [...], "cost": float} — the real geolocated Shopping-tab grid + carousels.
    `depth` = how many grid results to pull. Dropshippers appear THROUGHOUT the grid — on page 1
    too (lunirel.com is position 2 on UK 'air conditioner'), so we scan the FULL grid, not page 1.
    100 is the Merchant endpoint's HARD MAX (depth>100 → 40501 'Invalid Field') ≈ pages 1-2.5 /
    ~100 products; deeper than that needs the AdsPower browser scan, not this API."""
    post = _post(_DFS_MERCHANT_POST, [{
        "keyword": keyword, "location_name": location_name, "language_name": "English",
        "depth": max(40, int(depth)),
    }], auth)
    task = (post.get("tasks") or [{}])[0]
    tid = task.get("id")
    cost = post.get("cost") or 0.0
    if not tid:
        raise RuntimeError(task.get("status_message") or "merchant task_post returned no id")
    deadline = time.time() + poll_secs
    while time.time() < deadline:
        time.sleep(6)
        got = _post(f"{_DFS_MERCHANT_GET}/{tid}", None, auth, method="GET")
        t = (got.get("tasks") or [{}])[0]
        sc = t.get("status_code")
        if sc == 20000:
            cost += got.get("cost") or 0.0
            result = (t.get("result") or [{}])[0] or {}
            return {"items": result.get("items") or [], "cost": cost}
        # 40601/40602 = in-queue/in-progress; anything else is terminal.
        if sc not in (40601, 40602, 40100):
            raise RuntimeError(t.get("status_message") or f"merchant task error {sc}")
    raise TimeoutError("merchant task did not finish in time")


def extract_merchant(items: list[dict]) -> dict:
    products, carousels = [], []
    for it in items:
        t = it.get("type")
        if t == "google_shopping_serp":
            cur = _num(it.get("price"))
            old = _num(it.get("old_price"))
            disc = None
            if cur is not None and old and old > 0 and cur <= old:
                disc = round((old - cur) / old * 100, 1)
            products.append({
                "title": it.get("title"),
                "seller": it.get("seller"),
                "domain": it.get("domain"),
                # DFS merchant items expose the photo as `product_images` (a list of gstatic
                # URLs), NOT `image_url` (that's the serp/shopping endpoint's field). Take the
                # first so 'Find products' cards render the real image, not a blank square.
                "image_url": ((it.get("product_images") or [None])[0]
                              or it.get("image_url") or it.get("image")),
                "price_current": cur,
                "price_regular": old,
                "discount_pct": disc,
                "currency": it.get("currency"),
                "rating": _num((it.get("product_rating") or {}).get("value")
                               if isinstance(it.get("product_rating"), dict) else it.get("product_rating")),
                "reviews": it.get("reviews_count"),
                "sponsored": bool(it.get("shop_ad_aclk")),
                "url": it.get("url") or it.get("shopping_url"),
            })
        elif t == "google_shopping_carousel":
            carousels.append(it.get("title"))
    return {"products": products, "carousels": [c for c in carousels if c]}


# ---------------------------------------------------------------- Organic (sub-keyword intent)
# ---------------------------------------------------------------- Amazon (finding lane 4)
def fetch_amazon(keyword: str, location_name: str, auth: str, depth: int = 30) -> dict:
    """Amazon keyword product search via merchant/amazon/products LIVE/advanced (synchronous — the
    Amazon endpoint HAS a live variant, so no task+poll like Google merchant). Returns
    {"items": [...], "cost": float}. `amazon_serp` items are the organic ranked products for the
    keyword; `amazon_paid` are sponsored. Amazon takes `language_code` (NOT `language_name`)."""
    resp = _post(_DFS_AMAZON_LIVE, [{
        "keyword": keyword, "location_name": location_name,
        "language_code": "en_US", "depth": max(10, int(depth)),
    }], auth)
    task = (resp.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        raise RuntimeError(task.get("status_message") or f"amazon task error {task.get('status_code')}")
    result = (task.get("result") or [{}])[0] or {}
    return {"items": result.get("items") or [], "cost": resp.get("cost") or 0.0}


def extract_amazon(items: list[dict], include_sponsored: bool = True) -> list[dict]:
    """Parse amazon_serp / amazon_paid items → product rows. Canonicalises the URL to
    amazon.com/dp/<ASIN> (drops the noisy `ref=` tracking tail), reads the nested rating block,
    and keeps the `bought_past_month` demand signal + Amazon's-Choice / Best-Seller badges."""
    keep = {"amazon_serp"} | ({"amazon_paid"} if include_sponsored else set())
    out: list[dict] = []
    for it in items:
        if it.get("type") not in keep:
            continue
        asin = str(it.get("data_asin") or it.get("asin") or "").strip()
        url = f"https://www.amazon.com/dp/{asin}" if asin else (it.get("url") or None)
        rat = it.get("rating") if isinstance(it.get("rating"), dict) else {}
        out.append({
            "title": it.get("title"),
            "price": _num(it.get("price_from")) or _num(it.get("price")),
            "price_regular": _num(it.get("price_to")),
            "currency": it.get("currency") or "USD",
            "image": it.get("image_url"),
            "url": url,
            "asin": asin or None,
            "rating": _num(rat.get("value")),
            "reviews": rat.get("votes_count"),
            "bought_past_month": it.get("bought_past_month"),
            "is_amazon_choice": bool(it.get("is_amazon_choice")),
            "is_best_seller": bool(it.get("is_best_seller")),
            "sponsored": it.get("type") == "amazon_paid",
        })
    return out


def amazon_search(keyword: str, geo: str = "US", depth: int = 30, auth: str | None = None) -> list[dict]:
    """Convenience wrapper: (auth or load creds) → fetch → extract. Returns [] on ANY failure
    (missing creds / network / DFS error) so a finding lane never breaks the whole 'Find products'
    aggregate — Amazon is one supplementary lane among five.

    `auth` = a pre-built base64 'user:pass' header. An IN-PROCESS caller (e.g. the FastAPI request
    that runs the finding lane) MUST pass this, sourced from connections.runtime_get(), because the
    operator app keeps DataForSEO creds in Connections (DB), not os.environ — _load_creds() only sees
    the process env / a .env file, so it returns nothing on the live deploy. Subprocess CLI callers
    (jobs) get creds injected into os.environ via jobs._base_env(), so they can omit `auth`."""
    try:
        if not auth:
            u, p = _load_creds()
            if not u or not p:
                return []
            auth = base64.b64encode(f"{u}:{p}".encode()).decode()
        loc = _GEO_LOCATION.get(str(geo or "US").upper(), "United States")
        return extract_amazon(fetch_amazon(keyword, loc, auth, depth=depth).get("items") or [])
    except Exception:  # noqa: BLE001 — advisory lane, degrade to empty
        return []


def fetch_sub_keywords(keyword: str, location_name: str, auth: str) -> list[str]:
    try:
        resp = _post(_DFS_ORGANIC, [{
            "keyword": keyword, "location_name": location_name,
            # depth 10 (was 20): sub-keywords come from the SERP's related-searches / refinement
            # elements which sit in the first page, so a shallower (cheaper) scan captures them.
            "language_name": "English", "device": "desktop", "depth": 10,
        }], auth)
    except Exception:  # noqa: BLE001 — sub-keywords are a bonus; never fail the scan over them
        return []
    tasks = resp.get("tasks") or []
    items = (((tasks[0].get("result") or [{}])[0] if tasks else {}) or {}).get("items") or []
    out: list[str] = []
    for it in items:
        t = it.get("type")
        if t == "related_searches":
            out += [s for s in (it.get("items") or []) if isinstance(s, str)]
        elif t == "people_also_ask":
            out += [el.get("title") for el in (it.get("items") or []) if el.get("title")]
    seen, subs = set(), []
    for s in out:
        k = (s or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            subs.append(s.strip())
    return subs


# ---------------------------------------------------------------- sub-keyword search volume
LAST_SV_ERROR: str | None = None  # last fetch_keyword_sv failure reason (surfaced in the scan output)


def fetch_keyword_sv(terms: list[str], location_name: str, auth: str) -> dict:
    """Batch Google-Ads search volume (DFS) for the scan's sub-keywords + category rows, so the SKU
    plan shows real SEARCHES/MO instead of '—'. Best-effort — an empty map on any failure never
    fails the scan. Returns {lowercased-term -> monthly search volume}."""
    global LAST_SV_ERROR
    LAST_SV_ERROR = None
    # DFS Google-Ads search_volume REJECTS the whole batch (error 40501) if ANY keyword has an
    # invalid character — and the sub-keywords include people-also-ask questions ("Do portable air
    # conditioners really work well?"). Drop questions / invalid-symbol / over-long terms first (they
    # aren't product keywords anyway) so the valid sub-keywords still get their volume.
    def _clean(x: object) -> str | None:
        t = (x or "").strip()
        return t if (t and len(t) <= 80 and not any(c in t for c in "?!*<>[]{}|\\\"")) else None
    uniq = [t for t in dict.fromkeys(_clean(x) for x in terms) if t][:300]
    if not uniq:
        return {}
    try:
        resp = _post(_DFS_SV, [{
            "keywords": uniq, "location_name": location_name, "language_name": "English",
            "search_partners": False,
        }], auth)
    except urllib.error.HTTPError as e:  # capture the DFS body so we can SEE why (403/402/…)
        try:
            LAST_SV_ERROR = f"HTTP {e.code}: {e.read().decode()[:300]}"
        except Exception:  # noqa: BLE001
            LAST_SV_ERROR = f"HTTP {e.code}"
        return {}
    except Exception as e:  # noqa: BLE001 — SV is a bonus column, never fail the scan over it
        LAST_SV_ERROR = f"{type(e).__name__}: {e}"
        return {}
    sc = resp.get("status_code")
    if sc != 20000:
        LAST_SV_ERROR = f"DFS status {sc}: {resp.get('status_message')}"
    out: dict = {}
    tasks = resp.get("tasks") or []
    results = ((tasks[0].get("result") or []) if tasks else []) or []
    for r in results:
        kw = (r.get("keyword") or "").strip().lower()
        if kw:
            out[kw] = r.get("search_volume")
    if not out and not LAST_SV_ERROR:
        t0 = tasks[0] if tasks else {}
        LAST_SV_ERROR = (f"empty: loc={location_name!r} task_status={t0.get('status_code')}/"
                         f"{t0.get('status_message')} result_count={t0.get('result_count')} "
                         f"result_type={type(t0.get('result')).__name__}")
    return out


# ---------------------------------------------------------------- recon assembly
def summarize(merch: dict, sub_keywords: list[str]) -> dict:
    products = merch["products"]
    prices = [p["price_current"] for p in products if p["price_current"] is not None]
    discounts = [p["discount_pct"] for p in products if p["discount_pct"] is not None]
    sellers: dict[str, int] = {}
    for p in products:
        key = p["seller"] or p["domain"]
        if key:
            sellers[key] = sellers.get(key, 0) + 1
    sponsored = [p for p in products if p["sponsored"]]
    with_reviews = [p for p in products if p.get("reviews")]
    price_ladder = None
    if prices:
        price_ladder = {
            "min": round(min(prices), 2), "max": round(max(prices), 2),
            "median": round(statistics.median(prices), 2), "count": len(prices),
        }
    return {
        "products": products,
        "category_rows": merch["carousels"],
        "sub_keywords": sub_keywords,
        "price_ladder": price_ladder,
        "discount_norm_pct": round(statistics.median(discounts), 1) if discounts else None,
        "seller_concentration": dict(sorted(sellers.items(), key=lambda kv: -kv[1])),
        "sponsored_count": len(sponsored),
        "sponsored_sellers": sorted({p["seller"] or p["domain"] for p in sponsored if (p["seller"] or p["domain"])}),
        "reviewed_share": round(len(with_reviews) / len(products), 2) if products else None,
    }


def to_markdown(keyword: str, geo: str, rec: dict) -> str:
    md = [f"# Google Shopping recon — {keyword} ({geo})", "",
          "> Server-side via DataForSEO (geolocated, no browser). Real Shopping-tab grid: "
          "products, price ladder, discount norms, who's advertising, category rows + searched "
          "sub-keywords. The only thing AdsPower adds is the literal Sponsored-ad screenshots.", ""]
    pl = rec.get("price_ladder")
    if pl:
        md += ["## Price ladder",
               f"- **{pl['count']} products**: ${pl['min']} -> ${pl['median']} (median) -> ${pl['max']}"]
        if rec.get("discount_norm_pct") is not None:
            md.append(f"- **Typical discount**: ~{rec['discount_norm_pct']}% off compare-at")
        md.append("")
    if rec.get("category_rows"):
        md += ["## Google's category rows (one SKU per segment)",
               *[f"- {c}" for c in rec["category_rows"]], ""]
    if rec.get("sponsored_count") is not None:
        md += ["## Who's running Shopping ads (server-side `shop_ad_aclk`)",
               f"- **{rec['sponsored_count']} of {len(rec['products'])}** products are Sponsored"]
        if rec.get("sponsored_sellers"):
            md.append("- Advertisers: " + ", ".join(rec["sponsored_sellers"]))
        md.append("")
    if rec.get("seller_concentration"):
        md += ["## Seller concentration",
               *[f"- {s} x{n}" for s, n in list(rec["seller_concentration"].items())[:15]], ""]
    if rec.get("products"):
        md += ["## Competing products"]
        for p in rec["products"][:40]:
            price = f"${p['price_current']}" if p["price_current"] is not None else "—"
            if p["discount_pct"]:
                price += f" (-{p['discount_pct']}%)"
            flags = " · 🅰 ad" if p["sponsored"] else ""
            rat = ""
            if p.get("rating"):
                rat = f" · {p['rating']}★" + (f"({p['reviews']})" if p.get("reviews") else "")
            md.append(f"- **{p['title']}** — {price}{rat} · {p['seller'] or p['domain'] or '—'}{flags}")
        md.append("")
    if rec.get("sub_keywords"):
        md += ["## Searched sub-keywords (intent map)", *[f"- {s}" for s in rec["sub_keywords"]], ""]
    return "\n".join(md)


def main() -> int:
    ap = argparse.ArgumentParser(description="Server-side Google Shopping recon (DFS Merchant).")
    ap.add_argument("--keyword", "--keywords", dest="keyword", required=True)
    ap.add_argument("--geo", default="US")
    ap.add_argument("--store", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--depth", type=int, default=40,
                    help="Shopping-grid results to scan. Default 40 (≈ page 1) — the Merchant task cost "
                         "scales with depth, and 40 products is plenty for finding + the price ladder / "
                         "category rows. Raise toward the HARD MAX 100 only for deep competitor recon.")
    args = ap.parse_args()

    keyword = (args.keyword or "").split(",")[0].strip()
    if not keyword:
        # Fail fast + explicit: an empty keyword can only produce a nonsense empty Shopping grid,
        # which used to record as a mysterious "failed" scan (and, from the sub-keyword warmer, a
        # wasted DataForSEO call). A missing keyword is a caller bug — say so and stop.
        print(json.dumps({"ok": False, "error": "no keyword — nothing to scan"}))
        return 1
    geo = (args.geo or "US").upper()
    location = _GEO_LOCATION.get(geo, "United States")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    u, p = _load_creds()
    if not (u and p):
        print(json.dumps({"ok": False, "error": "no DataForSEO credentials (set them in "
                          "Connections -> Data, or in the project-root .env)"}))
        return 1
    auth = base64.b64encode(f"{u}:{p}".encode()).decode()

    tier, engine, merch_cost, engine_note = "server-side-dfs", "dataforseo/merchant/google/products", 0.0, ""
    try:
        merch = fetch_merchant(keyword, location, auth, depth=args.depth)
        extracted = extract_merchant(merch["items"])
        merch_cost = merch.get("cost") or 0.0
        # Real SV for the sub-keywords + category rows (DFS) — only on the healthy primary path.
        sub_keywords = fetch_sub_keywords(keyword, location, auth)
    except Exception as e:  # noqa: BLE001 — DFS Merchant failed (HTTP 402 out-of-credit / transport)
        # FALLBACK: scrape the Google Shopping tab via the BrightData Web Unlocker so Google-Shopping
        # finding survives a DataForSEO outage. The DFS sub-keyword/SV calls are skipped (they 402 too);
        # BD's own category rows carry the sub-keyword intent.
        bd_tok = (os.environ.get("BRIGHTDATA_API_TOKEN")
                  or os.environ.get("BRIGHTDATA_DATASET_TOKEN") or "").strip()
        bd_zone = (os.environ.get("BRIGHTDATA_SERP_ZONE") or "web_unlocker1").strip()
        extracted = {"products": [], "carousels": []}
        if bd_tok:
            import shopping_scan_bd  # noqa: PLC0415 — lazy; a DFS-only run never imports it
            extracted = shopping_scan_bd.scan(keyword, bd_tok, bd_zone, geo=geo)
        if not extracted.get("products"):
            why = "no BrightData token for fallback" if not bd_tok else "BrightData fallback returned no products"
            print(json.dumps({"ok": False, "error": f"Merchant scan failed: {e}; {why}"}))
            return 1
        tier, engine = "brightdata-fallback", "brightdata/web-unlocker/google-shopping"
        engine_note = f"DataForSEO Merchant unavailable ({type(e).__name__}); scraped via BrightData Web Unlocker."
        sub_keywords = list(extracted.get("carousels") or [])

    rec = summarize({"products": extracted["products"], "carousels": extracted["carousels"]}, sub_keywords)
    sub_keyword_sv = ({} if tier != "server-side-dfs"
                      else fetch_keyword_sv(list(rec["sub_keywords"]) + list(rec["category_rows"]), location, auth))

    manifest = {
        "keyword": keyword, "geo": geo, "location": location, "store": args.store,
        "tier": tier, "engine": engine, "engine_note": engine_note,
        "cost": round(merch_cost, 4),
        "products": rec["products"],
        "category_rows": rec["category_rows"],
        "price_ladder": rec["price_ladder"],
        "discount_norm_pct": rec["discount_norm_pct"],
        "seller_concentration": rec["seller_concentration"],
        "sponsored_count": rec["sponsored_count"],
        "sponsored_sellers": rec["sponsored_sellers"],
        "reviewed_share": rec["reviewed_share"],
        "sub_keywords": rec["sub_keywords"],
        "sub_keyword_sv": sub_keyword_sv,
        "paid_ads_note": ("Sponsored advertisers identified server-side via shop_ad_aclk; the "
                          "literal Sponsored-PLA screenshots need the AdsPower browser."),
    }
    (out_dir / "paid-scan.json").write_text(json.dumps(manifest, indent=2))
    (out_dir / "paid-landscape.md").write_text(to_markdown(keyword, geo, rec))

    pl = rec["price_ladder"] or {}
    print(json.dumps({
        "ok": True, "tier": tier, "geo": geo, "location": location,
        "products": len(rec["products"]), "category_rows": len(rec["category_rows"]),
        "sponsored": rec["sponsored_count"], "price_ladder": pl,
        "sub_keywords": len(rec["sub_keywords"]),
        "sub_keyword_sv": len(sub_keyword_sv),
        "sub_keyword_sv_error": LAST_SV_ERROR,
        "out": str(out_dir / "paid-landscape.md"),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
