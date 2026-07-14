#!/usr/bin/env python3
"""
Google Ads keyword volume + CPC via DataForSEO — fills the gap left by not having
a Google Ads API developer token.

For a list of keywords, returns per keyword:
- monthly_searches (exact monthly volume, last reported month)
- search_volume_12mo (12-month sum)
- cpc (avg CPC in USD, from Google Ads)
- competition (LOW / MEDIUM / HIGH from Google Ads)
- competition_index (0-100 from Google Ads)
- monthly_searches_trend (list of last 12 monthly SV values for seasonality)

Uses DataForSEO Google Ads Search Volume Live endpoint:
  POST /v3/keywords_data/google_ads/search_volume/live

Cost: ~$0.05 per task. Each task can include up to 1,000 keywords.
Auth: HTTP Basic with DATAFORSEO_USERNAME + DATAFORSEO_PASSWORD env vars.

CACHE (added 2026-05-13): Results are cached at `01-niche-discovery/cache/
keyword-data-cache.json`. On every run, the input keyword list is split into
(cached & still-fresh) vs (need-to-fetch). Only the latter triggers an API call.
TTL default 90 days; override with --cache-ttl-days or bypass with --no-cache.

Usage:
  DATAFORSEO_USERNAME=... DATAFORSEO_PASSWORD=... \\
    python keyword_data.py --keywords keywords.json \\
                           --location "United States" \\
                           --out keyword-data.json
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
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


DFS_ENDPOINT = "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live"
CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "cache",
    "keyword-data-cache.json",
)

# Convenience map for the common geos we run
GEO_TO_LOCATION = {
    "US": "United States",
    "GB": "United Kingdom",
    "CA": "Canada",
    "AU": "Australia",
    "NZ": "New Zealand",
}


def load_keywords(arg: str) -> list[str]:
    if os.path.exists(arg):
        with open(arg) as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(k).strip() for k in data if str(k).strip()]
        if isinstance(data, dict) and "keywords" in data:
            return [str(k).strip() for k in data["keywords"] if str(k).strip()]
        raise ValueError(f"{arg}: JSON must be a list or {{'keywords': [...]}}")
    return [k.strip() for k in arg.split(",") if k.strip()]


def fetch_keyword_data(keywords: list[str], location_name: str,
                       auth: HTTPBasicAuth) -> list[dict]:
    """One DFS call returns up to 1000 keywords worth of SV+CPC data."""
    payload = [{
        "keywords": keywords,
        "location_name": location_name,
        "language_name": "English",
        "search_partners": False,
    }]
    r = requests.post(DFS_ENDPOINT, json=payload, auth=auth, timeout=120)
    r.raise_for_status()
    data = r.json()
    if data.get("status_code") != 20000:
        raise RuntimeError(f"DFS error {data.get('status_code')}: {data.get('status_message')}")
    tasks = data.get("tasks") or []
    if not tasks:
        return []
    task = tasks[0]
    if task.get("status_code") != 20000:
        raise RuntimeError(f"DFS task error {task.get('status_code')}: {task.get('status_message')}")
    return task.get("result") or []


def parse_record(r: dict) -> dict[str, Any]:
    """Pull the fields we care about out of a DFS keyword-result row."""
    monthly_trend = r.get("monthly_searches") or []
    return {
        "keyword": r.get("keyword"),
        "location_code": r.get("location_code"),
        "monthly_searches": r.get("search_volume"),
        "cpc": r.get("cpc"),
        "competition": r.get("competition"),
        "competition_index": r.get("competition_index"),
        "low_top_of_page_bid": r.get("low_top_of_page_bid"),
        "high_top_of_page_bid": r.get("high_top_of_page_bid"),
        "monthly_searches_trend": [
            {"year": m.get("year"), "month": m.get("month"), "search_volume": m.get("search_volume")}
            for m in monthly_trend
        ],
    }


# --------------------- Cache layer ---------------------

def _cache_key(keyword: str, location_name: str) -> str:
    """Compose a cache key from keyword + location. Lowercase keyword for consistency."""
    return f"{keyword.strip().lower()}|{location_name.strip()}"


def load_cache() -> dict:
    """Load the keyword-data cache from disk. Returns {} if missing/unreadable."""
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"  warning: cache load failed ({e}); starting empty", file=sys.stderr)
        return {}


def save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def cache_is_fresh(entry: dict, ttl_days: int) -> bool:
    """Check if a cached entry is within TTL."""
    fetched_at = entry.get("fetched_at")
    if not fetched_at:
        return False
    try:
        ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except Exception:
        return False
    now = datetime.now(timezone.utc)
    return (now - ts) < timedelta(days=ttl_days)


def partition_keywords(keywords: list[str], location_name: str, cache: dict,
                       ttl_days: int) -> tuple[list[dict], list[str]]:
    """Split keywords into (cached-fresh records) and (need-to-fetch keywords)."""
    fresh: list[dict] = []
    to_fetch: list[str] = []
    for kw in keywords:
        key = _cache_key(kw, location_name)
        entry = cache.get(key)
        if entry and cache_is_fresh(entry, ttl_days):
            r = entry.get("record") or {}
            r = dict(r)  # copy
            r["_source"] = "cache"
            r["_fetched_at"] = entry.get("fetched_at")
            fresh.append(r)
        else:
            to_fetch.append(kw)
    return fresh, to_fetch


def update_cache(cache: dict, parsed_records: list[dict], location_name: str) -> None:
    """Write newly-fetched records into the cache with current timestamp."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in parsed_records:
        kw = r.get("keyword")
        if not kw:
            continue
        cache[_cache_key(kw, location_name)] = {
            "fetched_at": now_iso,
            "record": {k: v for k, v in r.items() if not k.startswith("_")},
        }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keywords", required=True,
                    help="Comma-separated, OR path to JSON file (list or {'keywords':[...]})")
    ap.add_argument("--location", default="United States",
                    help="DataForSEO location name (default 'United States'). "
                         "Also accepts ISO-2 (US/GB/CA/AU/NZ) which is mapped.")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache-ttl-days", type=int, default=90,
                    help="How long cached entries stay fresh (default 90 days)")
    ap.add_argument("--no-cache", action="store_true",
                    help="Bypass cache entirely; force a fresh DFS fetch for all keywords")
    args = ap.parse_args()

    keywords = load_keywords(args.keywords)
    if not keywords:
        print("ERROR: no keywords provided", file=sys.stderr)
        sys.exit(1)

    # De-dupe input while preserving order
    seen = set()
    keywords = [k for k in keywords if not (k.lower() in seen or seen.add(k.lower()))]

    location_name = GEO_TO_LOCATION.get(args.location.upper(), args.location)

    # ---- Cache partition (skip the API for keywords we already have fresh) ----
    cache: dict = {} if args.no_cache else load_cache()
    if args.no_cache:
        print("Cache: bypassed (--no-cache)", file=sys.stderr)
        cached_records: list[dict] = []
        keywords_to_fetch = keywords
    else:
        cached_records, keywords_to_fetch = partition_keywords(
            keywords, location_name, cache, args.cache_ttl_days
        )
        print(f"Cache: {len(cached_records)} hits, {len(keywords_to_fetch)} to fetch "
              f"(TTL {args.cache_ttl_days} days, cache file: {CACHE_PATH})",
              file=sys.stderr)

    # ---- Fetch the misses (single batched DFS call) ----
    fresh_records: list[dict] = []
    if keywords_to_fetch:
        username = os.environ.get("DATAFORSEO_USERNAME")
        password = os.environ.get("DATAFORSEO_PASSWORD")
        if not username or not password:
            print("ERROR: DATAFORSEO_USERNAME and DATAFORSEO_PASSWORD env vars required "
                  "(needed for cache misses).", file=sys.stderr)
            sys.exit(1)
        auth = HTTPBasicAuth(username, password)

        BATCH = 1000
        n_calls = (len(keywords_to_fetch) + BATCH - 1) // BATCH
        print(f"Fetching {len(keywords_to_fetch)} keywords @ '{location_name}' via DataForSEO",
              file=sys.stderr)
        print(f"  Cost: ~${0.05 * n_calls:.2f} ({n_calls} API call(s) @ ~$0.05 each)",
              file=sys.stderr)

        batches = [keywords_to_fetch[i:i + BATCH] for i in range(0, len(keywords_to_fetch), BATCH)]

        def _fetch_batch(idx_batch):
            idx, batch = idx_batch
            try:
                results = fetch_keyword_data(batch, location_name, auth)
                parsed = [parse_record(r) for r in results]
                for p in parsed:
                    p["_source"] = "fresh"
                return parsed
            except Exception as e:
                print(f"    batch {idx + 1} failed: {e}", file=sys.stderr)
                return [{"keyword": kw, "error": str(e), "_source": "error"} for kw in batch]

        # Run the DFS batches concurrently — they're independent HTTP round-trips, so a
        # multi-batch fetch no longer pays N × latency. Capped at 6 (DFS-safe; matches
        # trends_dfs.py). Single batch falls through with no thread overhead.
        if len(batches) == 1:
            print(f"  batch 1/{n_calls} ({len(batches[0])} keywords)", file=sys.stderr)
            fresh_records.extend(_fetch_batch((0, batches[0])))
        else:
            with ThreadPoolExecutor(max_workers=min(6, len(batches))) as ex:
                futures = {ex.submit(_fetch_batch, (i, b)): i for i, b in enumerate(batches)}
                for fut in as_completed(futures):
                    print(f"  batch {futures[fut] + 1}/{n_calls} done", file=sys.stderr)
                    fresh_records.extend(fut.result())

        # ---- Update cache with fresh results ----
        if not args.no_cache:
            update_cache(cache, [r for r in fresh_records if "error" not in r], location_name)
            save_cache(cache)
            print(f"  Updated cache with {len([r for r in fresh_records if 'error' not in r])} "
                  f"new entries.", file=sys.stderr)
    else:
        print("All keywords served from cache — $0 spent on DFS this run.",
              file=sys.stderr)

    # ---- Merge results in original input order ----
    by_key = {}
    for r in cached_records + fresh_records:
        if r.get("keyword"):
            by_key[r["keyword"].lower()] = r
    all_parsed = []
    for kw in keywords:
        rec = by_key.get(kw.lower())
        if rec is None:
            rec = {"keyword": kw, "error": "no result returned", "_source": "missing"}
        all_parsed.append(rec)

    with open(args.out, "w") as f:
        json.dump({
            "location": location_name,
            "n_keywords": len(keywords),
            "n_cache_hits": len(cached_records),
            "n_fetched": len([r for r in fresh_records if r.get("_source") == "fresh"]),
            "results": all_parsed,
        }, f, indent=2)
    print(f"\nWrote {args.out}", file=sys.stderr)

    # Brief summary
    with_data = [r for r in all_parsed if r.get("monthly_searches") is not None]
    print(f"\nGot data for {len(with_data)}/{len(keywords)} keywords", file=sys.stderr)
    if with_data:
        top = sorted(with_data, key=lambda r: -(r.get("monthly_searches") or 0))[:10]
        print("\nTop 10 by monthly_searches:", file=sys.stderr)
        print(f"  {'SV':>10}  {'CPC':>6}  {'comp':>6}  keyword", file=sys.stderr)
        for r in top:
            sv = r.get("monthly_searches") or 0
            cpc = r.get("cpc")
            comp = r.get("competition") or "-"
            print(f"  {sv:>10,}  ${cpc or 0:>5.2f}  {comp:>6}  {r['keyword']}",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
