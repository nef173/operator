#!/usr/bin/env python3
"""
Google Shopping SERP fetcher for /discover-niches.

Three modes:
- dfs: uses DataForSEO Merchant Google Products API (task_post + poll).
       Requires DATAFORSEO_USERNAME + DATAFORSEO_PASSWORD in env / .env.
       Cost: ~$0.002 per keyword × geo (~$0.13 for 33 kw × 2 geos).
       Recommended default — Shopping SERP results returned server-side,
       no JS-render issues, no IP-block issues.
- serper: uses Serper.dev API (requires SERPER_API_KEY env var). ~$30/mo for 2.5K queries.
- urls: prints SERP URLs that Claude (orchestrator) should fetch via WebFetch.
        Last-resort fallback — Google Shopping SERPs are JS-rendered and most
        WebFetch attempts return empty content. Use dfs mode instead.

Usage:
  # Recommended: DataForSEO Merchant Google Products
  DATAFORSEO_USERNAME=... DATAFORSEO_PASSWORD=... \\
    python serp_spy.py --keywords keywords.json \\
                       --geo US,GB \\
                       --mode dfs \\
                       --out serp-data.json

  # Serper.dev:
  SERPER_API_KEY=xxx python serp_spy.py --keywords keywords.json \\
                                         --geo US,GB \\
                                         --mode serper \\
                                         --out serp-data.json

  # WebFetch URL list (fallback only):
  python serp_spy.py --keywords keywords.json \\
                    --geo US,GB \\
                    --mode urls \\
                    --out serp-urls.json

Keyword input: a JSON file with either a flat list of keywords, or an object with
a "keywords" key. Falls back to treating --keywords as a comma-separated string
if the path doesn't exist on disk.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

# Load DATAFORSEO_* env vars from project-root .env. Idempotent + silent if dotenv missing.
try:
    from dotenv import load_dotenv
    _here = Path(__file__).resolve().parent
    for _candidate in [_here, *_here.parents]:
        if (_candidate / ".env").exists():
            load_dotenv(_candidate / ".env")
            break
except ImportError:
    pass


SERPER_URL = "https://google.serper.dev/shopping"

# DataForSEO Merchant Google Products endpoints
DFS_BASE = "https://api.dataforseo.com"
DFS_TASK_POST = f"{DFS_BASE}/v3/merchant/google/products/task_post"
DFS_TASK_GET = f"{DFS_BASE}/v3/merchant/google/products/task_get/advanced"

# DFS location codes (subset — extend as needed)
DFS_LOCATION = {
    "US": 2840,
    "GB": 2826,
    "CA": 2124,
    "AU": 2036,
    "NZ": 2554,
}


def serp_url(keyword: str, geo: str) -> str:
    """Build a Google Shopping SERP URL for the keyword + geo."""
    domain = {"US": "google.com", "GB": "google.co.uk"}.get(geo, "google.com")
    q = urllib.parse.quote_plus(keyword)
    return f"https://www.{domain}/search?tbm=shop&q={q}"


def detect_shopify(url: str | None) -> bool:
    """Cheap heuristic for Shopify-store footprint."""
    if not url:
        return False
    lowered = url.lower()
    return any(s in lowered for s in [".myshopify.com", "/products/", "/collections/", "cdn.shopify.com"])


def fetch_via_dfs(keyword: str, geo: str, auth: HTTPBasicAuth, depth: int = 40,
                  poll_interval: float = 10.0, poll_max: int = 30) -> dict[str, Any]:
    """Fetch a Google Shopping SERP via DataForSEO Merchant Google Products.

    Uses task_post (async submission) + task_get/advanced (poll for result).
    Returns a dict in the same shape as fetch_via_serper.
    """
    location_code = DFS_LOCATION.get(geo.upper())
    if not location_code:
        return {"keyword": keyword, "geo": geo, "source": "dfs",
                "error": f"unsupported geo {geo!r} (supported: {list(DFS_LOCATION)})",
                "listings": [], "listing_count": 0, "shopify_dropshipper_count": 0}

    payload = [{
        "keyword": keyword,
        "location_code": location_code,
        "language_code": "en",
        "depth": depth,
        "priority": 2,  # High priority — task usually completes in 10-30s
    }]
    r = requests.post(DFS_TASK_POST, json=payload, auth=auth, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("status_code") != 20000:
        return {"keyword": keyword, "geo": geo, "source": "dfs",
                "error": f"task_post failed: {d.get('status_message')}",
                "listings": [], "listing_count": 0, "shopify_dropshipper_count": 0}
    task = (d.get("tasks") or [{}])[0]
    task_id = task.get("id")
    if not task_id:
        return {"keyword": keyword, "geo": geo, "source": "dfs",
                "error": f"no task id: {task.get('status_message')}",
                "listings": [], "listing_count": 0, "shopify_dropshipper_count": 0}

    # Poll
    for _ in range(poll_max):
        time.sleep(poll_interval)
        r2 = requests.get(f"{DFS_TASK_GET}/{task_id}", auth=auth, timeout=30)
        if r2.status_code != 200:
            continue
        d2 = r2.json()
        t2 = (d2.get("tasks") or [{}])[0]
        tsc = t2.get("status_code")
        if tsc == 20000:
            res = (t2.get("result") or [{}])[0] if t2.get("result") else {}
            items = res.get("items") or []
            # Filter to actual product listings (drop carousel category groups)
            product_items = [it for it in items if it.get("type") == "google_shopping_serp"]
            parsed = [
                {
                    "title": it.get("title"),
                    "price": it.get("price"),
                    "source": it.get("seller") or it.get("domain"),
                    "link": it.get("url") or it.get("link"),
                    "image_url": it.get("image_url") or it.get("image"),
                    "rating": (it.get("rating") or {}).get("value") if isinstance(it.get("rating"), dict) else it.get("rating"),
                    "reviews": (it.get("rating") or {}).get("votes_count") if isinstance(it.get("rating"), dict) else it.get("reviews"),
                    "rank_absolute": it.get("rank_absolute"),
                    "shopify_footprint": detect_shopify(it.get("url") or it.get("link")),
                }
                for it in product_items
            ]
            # Carousel categories — useful for understanding how Google segments the niche
            carousel_categories = [it.get("title") for it in items
                                   if it.get("type") == "google_shopping_carousel" and it.get("title")]
            shopify_count = sum(1 for l in parsed if l["shopify_footprint"])
            return {
                "keyword": keyword,
                "geo": geo,
                "source": "dfs",
                "listing_count": len(parsed),
                "carousel_categories": carousel_categories,
                "shopify_dropshipper_count": shopify_count,
                "listings": parsed,
                "dfs_check_url": res.get("check_url"),
                "items_count_total": res.get("items_count"),
            }
        if tsc not in (40601, 40602):  # 40601=in queue, 40602=in progress
            return {"keyword": keyword, "geo": geo, "source": "dfs",
                    "error": f"task failed: {tsc} {t2.get('status_message')}",
                    "listings": [], "listing_count": 0, "shopify_dropshipper_count": 0}
    return {"keyword": keyword, "geo": geo, "source": "dfs",
            "error": f"poll timeout after {poll_max*poll_interval:.0f}s",
            "listings": [], "listing_count": 0, "shopify_dropshipper_count": 0}


def fetch_via_serper(keyword: str, geo: str, api_key: str) -> dict[str, Any]:
    """Fetch a Google Shopping SERP via Serper.dev."""
    payload = {"q": keyword, "gl": geo.lower(), "hl": "en"}
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    r = requests.post(SERPER_URL, json=payload, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    listings = data.get("shopping", []) or []
    parsed = [
        {
            "title": l.get("title"),
            "price": l.get("price"),
            "source": l.get("source"),
            "link": l.get("link"),
            "image_url": l.get("imageUrl"),
            "rating": l.get("rating"),
            "reviews": l.get("ratingCount"),
            "shopify_footprint": detect_shopify(l.get("link")),
        }
        for l in listings
    ]
    shopify_count = sum(1 for l in parsed if l["shopify_footprint"])
    return {
        "keyword": keyword,
        "geo": geo,
        "source": "serper",
        "listing_count": len(parsed),
        "shopify_dropshipper_count": shopify_count,
        "listings": parsed,
    }


def load_keywords(arg: str) -> list[str]:
    """Load keywords from a JSON file path, or treat as comma-separated string."""
    if os.path.exists(arg):
        with open(arg) as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(k).strip() for k in data if str(k).strip()]
        if isinstance(data, dict) and "keywords" in data:
            return [str(k).strip() for k in data["keywords"] if str(k).strip()]
        raise ValueError(f"{arg}: JSON must be a list or {{'keywords': [...]}}")
    return [k.strip() for k in arg.split(",") if k.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keywords", required=True,
                    help="Path to JSON file (list or {'keywords':[...]}), OR comma-separated string")
    ap.add_argument("--geo", default="US,GB", help="Comma-separated geos")
    ap.add_argument("--mode", choices=["dfs", "serper", "urls"], default="dfs",
                    help="dfs = DataForSEO Merchant Google Products (recommended); "
                         "serper = Serper.dev API; urls = print URLs for WebFetch")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=50,
                    help="Max keywords to process (cost guard, default 50)")
    ap.add_argument("--workers", type=int, default=8,
                    help="Parallel DFS task workers (default 8; only used in dfs mode)")
    ap.add_argument("--depth", type=int, default=40,
                    help="DFS Shopping depth (default 40)")
    args = ap.parse_args()

    geos = [g.strip().upper() for g in args.geo.split(",") if g.strip()]
    keywords = load_keywords(args.keywords)[:args.limit]

    print(f"Mode: {args.mode}", file=sys.stderr)
    print(f"Processing {len(keywords)} keywords × {len(geos)} geos = "
          f"{len(keywords) * len(geos)} SERPs", file=sys.stderr)

    if args.mode == "urls":
        out = [{"keyword": kw, "geo": geo, "url": serp_url(kw, geo)}
               for kw in keywords for geo in geos]
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\nWrote {len(out)} URLs to {args.out}.", file=sys.stderr)
        print("Next step: have Claude WebFetch each URL in parallel via Agent/Explore.",
              file=sys.stderr)
        return

    if args.mode == "dfs":
        u = os.environ.get("DATAFORSEO_USERNAME", "").strip()
        p = os.environ.get("DATAFORSEO_PASSWORD", "").strip()
        if not u or not p:
            print("ERROR: DATAFORSEO_USERNAME / DATAFORSEO_PASSWORD env vars not set.",
                  file=sys.stderr)
            print("Add them to project-root .env, OR use --mode serper / --mode urls.",
                  file=sys.stderr)
            sys.exit(1)
        auth = HTTPBasicAuth(u, p)
        jobs = [(kw, geo) for kw in keywords for geo in geos]
        est_cost = len(jobs) * 0.002
        print(f"DFS mode: posting {len(jobs)} tasks (~${est_cost:.3f} estimated), "
              f"{args.workers} parallel workers", file=sys.stderr)
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            future_map = {ex.submit(fetch_via_dfs, kw, geo, auth, args.depth): (kw, geo)
                          for kw, geo in jobs}
            for i, fut in enumerate(as_completed(future_map), 1):
                kw, geo = future_map[fut]
                try:
                    res = fut.result()
                    err = res.get("error")
                    if err:
                        print(f"  [{i}/{len(jobs)}] {geo}: {kw!r}  FAIL: {err}",
                              file=sys.stderr)
                    else:
                        print(f"  [{i}/{len(jobs)}] {geo}: {kw!r}  "
                              f"{res.get('listing_count',0)} listings, "
                              f"shopify={res.get('shopify_dropshipper_count',0)}",
                              file=sys.stderr)
                    results.append(res)
                except Exception as e:
                    print(f"  [{i}/{len(jobs)}] {geo}: {kw!r}  EXCEPTION: {e}",
                          file=sys.stderr)
                    results.append({"keyword": kw, "geo": geo, "error": str(e),
                                    "listings": [], "listing_count": 0})
        with open(args.out, "w") as f:
            json.dump(results, f, indent=2)
        ok = sum(1 for r in results if not r.get("error"))
        print(f"\nWrote {len(results)} SERP results to {args.out} ({ok} ok, "
              f"{len(results)-ok} failed)", file=sys.stderr)
        return

    # serper mode
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        print("ERROR: SERPER_API_KEY env var not set.", file=sys.stderr)
        print("Use --mode dfs (recommended) or --mode urls.", file=sys.stderr)
        sys.exit(1)

    results = []
    for kw in keywords:
        for geo in geos:
            print(f"  {geo}: {kw}", file=sys.stderr)
            try:
                results.append(fetch_via_serper(kw, geo, api_key))
            except Exception as e:
                print(f"    failed: {e}", file=sys.stderr)
                results.append({"keyword": kw, "geo": geo, "error": str(e)})
            time.sleep(0.4)

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWrote {len(results)} SERP results to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
