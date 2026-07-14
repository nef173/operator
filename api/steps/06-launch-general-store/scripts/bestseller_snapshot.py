#!/usr/bin/env python3
"""
bestseller_snapshot.py — Competitor Best-Seller Spy, snapshot stage (Step 06).

Captures a competitor Shopify store's BEST-SELLING product order for a given day.

Mechanic (validated 2026-06-16 on belroshop.com / creative.lighting / cozeymaison.com):
  - The .json endpoints (/products.json, /collections/x/products.json) IGNORE sort_by.
  - TRUE best-seller ORDER comes from the RENDERED collection page
    /collections/<handle>?sort_by=best-selling — read product handles in DOM order.
  - /products/<handle>.json (or bulk /products.json) then ENRICHES each handle
    (title, price, created_at, image, vendor, type).

Writes one snapshot per store per day: <out>/<store>/<YYYY-MM-DD>.json
Pair with bestseller_diff.py to compute movers.

Usage:
  bestseller_snapshot.py --stores belroshop.com,creative.lighting --depth 30 --out snapshots
  bestseller_snapshot.py --stores stores.txt --depth 80
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Add-on / non-catalog products that ALWAYS top best-selling order (every order
# includes them) and must be stripped — they are noise, not real movers.
JUNK_SUBSTRINGS = (
    "shipping-protection", "package-protection", "route-protection",
    "order-protection", "delivery-protection", "protection-plan",
    "insurance", "warranty", "-vip", "vip-", "tip", "gift-card",
    "giftcard", "donation", "deposit", "sample-", "-sample",
    "subscription", "membership", "extended-warranty", "onward",
    "seel", "corso", "worry-free",
)
JUNK_TYPES = ("shipping", "protection", "insurance", "warranty", "service",
              "gift card", "tip", "donation")

HANDLE_RE = re.compile(r"/products/([a-z0-9][a-z0-9\-]*)")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def is_junk(handle, ptype=""):
    h = handle.lower()
    if any(s in h for s in JUNK_SUBSTRINGS):
        return True
    if ptype and any(t in ptype.lower() for t in JUNK_TYPES):
        return True
    return False


def fetch(session, url, timeout=20):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (404, 403):
                return r
            log(f"    [{r.status_code}] {url} (retry {attempt+1})")
        except requests.RequestException as e:
            log(f"    [err] {url}: {e} (retry {attempt+1})")
        time.sleep(1.0 + attempt)
    return None


def store_currency(session, store):
    """Best-effort base currency from /meta.json. Note: products.json prices are
    PRESENTMENT-currency (geo-IP dependent) on Shopify Markets stores, so price is
    enrichment only — the movers signal is RANK + created_at (currency-independent)."""
    r = fetch(session, f"https://{store}/meta.json")
    if r and r.status_code == 200:
        try:
            return r.json().get("currency")
        except ValueError:
            pass
    return None


def extract_order(html):
    """Return product handles in DOM order, deduped (first occurrence wins)."""
    seen, ordered = set(), []
    for h in HANDLE_RE.findall(html):
        if h not in seen:
            seen.add(h)
            ordered.append(h)
    return ordered


def collection_order(session, store, collection, depth, max_pages=6, pause=0.5):
    """Walk /collections/<c>?sort_by=best-selling pages until depth handles."""
    handles = []
    seen = set()
    for page in range(1, max_pages + 1):
        url = f"https://{store}/collections/{collection}?sort_by=best-selling&page={page}"
        r = fetch(session, url)
        if not r or r.status_code != 200:
            break
        page_handles = [h for h in extract_order(r.text) if h not in seen]
        if not page_handles:
            break
        for h in page_handles:
            seen.add(h)
            handles.append(h)
        if len(handles) >= depth * 2:  # over-fetch; junk gets filtered later
            break
        time.sleep(pause)
    return handles


def enrich(session, store, handle, currency, pause=0.25):
    """Per-product enrichment via /products/<handle>.json.

    Prices are in the store's BASE currency (`currency`, from /meta.json). They are
    correct USD only when the store's base currency is USD, or when the session
    egresses through a US IP so a Markets store resolves to its US/USD market."""
    url = f"https://{store}/products/{handle}.json"
    r = fetch(session, url)
    time.sleep(pause)
    if not r or r.status_code != 200:
        return None
    try:
        p = r.json().get("product", {})
    except ValueError:
        return None
    variants = p.get("variants", []) or []
    prices = [float(v["price"]) for v in variants if v.get("price")]
    compares = [float(v["compare_at_price"]) for v in variants
                if v.get("compare_at_price")]
    images = p.get("images", []) or []
    return {
        "handle": handle,
        "title": p.get("title", ""),
        "product_type": p.get("product_type", ""),
        "vendor": p.get("vendor", ""),
        "created_at": p.get("created_at", ""),
        "updated_at": p.get("updated_at", ""),
        "price": min(prices) if prices else None,
        "compare_at": max(compares) if compares else None,
        "currency": currency,
        "price_is_usd": currency == "USD",
        "image": images[0]["src"] if images else None,
        "url": f"https://{store}/products/{handle}",
        "variant_count": len(variants),
    }


def snapshot_store(session, store, depth, collection):
    log(f"  → {store}: fetching best-selling order…")
    raw = collection_order(session, store, collection, depth)
    if not raw:
        log(f"  ✗ {store}: no products found (collection /{collection} blocked or empty)")
        return None
    currency = store_currency(session, store)
    if currency and currency != "USD":
        log(f"  ⚠ {store}: base currency is {currency}, not USD — prices are NOT US "
            f"pricing. Re-run with --proxy <US-proxy> to resolve the US/USD market.")
    log(f"  {store}: {len(raw)} raw handles, enriching + filtering…")
    products, rank = [], 0
    for handle in raw:
        if len(products) >= depth:
            break
        if is_junk(handle):
            continue
        meta = enrich(session, store, handle, currency)
        if not meta:
            continue
        if is_junk(handle, meta.get("product_type", "")):
            continue
        rank += 1
        meta["rank"] = rank
        products.append(meta)
    log(f"  ✓ {store}: {len(products)} catalog products captured ({currency or '?'})")
    return products, currency


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stores", required=True,
                    help="comma list of domains OR path to a file (one domain per line)")
    ap.add_argument("--depth", type=int, default=30, help="top-N to capture (default 30)")
    ap.add_argument("--collection", default="all", help="collection handle (default 'all')")
    ap.add_argument("--out", default="snapshots", help="output dir (default ./snapshots)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default today)")
    ap.add_argument("--proxy", default=None,
                    help="US proxy URL (http://user:pass@host:port) so Shopify Markets "
                         "stores resolve to the US/USD market — fixes non-USD pricing")
    args = ap.parse_args()

    # resolve store list
    if os.path.isfile(args.stores):
        with open(args.stores) as f:
            stores = [ln.strip() for ln in f
                      if ln.strip() and not ln.strip().startswith("#")]
    else:
        stores = [s.strip() for s in args.stores.split(",") if s.strip()]
    stores = [s.replace("https://", "").replace("http://", "").rstrip("/")
              for s in stores]

    date = args.date or dt.date.today().isoformat()
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    if args.proxy:
        session.proxies.update({"http": args.proxy, "https": args.proxy})
        log(f"egress via proxy {args.proxy.split('@')[-1]} (US/USD market)")

    ok, non_usd = 0, []
    for store in stores:
        result = snapshot_store(session, store, args.depth, args.collection)
        if not result:
            continue
        products, currency = result
        if currency and currency != "USD":
            non_usd.append(f"{store} ({currency})")
        store_dir = os.path.join(args.out, store)
        os.makedirs(store_dir, exist_ok=True)
        path = os.path.join(store_dir, f"{date}.json")
        with open(path, "w") as f:
            json.dump({
                "store": store,
                "date": date,
                "collection": args.collection,
                "depth": args.depth,
                "base_currency": currency,
                "prices_usd": currency == "USD" or bool(args.proxy),
                "price_note": ("prices in store base currency "
                               f"({currency or '?'}); "
                               + ("US/USD via proxy egress" if args.proxy
                                  else "USD only when base_currency==USD — re-run "
                                       "with --proxy <US-proxy> for non-USD stores")),
                "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
                "count": len(products),
                "products": products,
            }, f, indent=2, ensure_ascii=False)
        log(f"  saved {path}")
        ok += 1

    log(f"\nDone. {ok}/{len(stores)} stores snapshotted for {date}.")
    if non_usd and not args.proxy:
        log(f"⚠ {len(non_usd)} store(s) NOT USD pricing: {', '.join(non_usd)}")
        log("  → re-run with --proxy <US-proxy> to capture US pricing for these.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
