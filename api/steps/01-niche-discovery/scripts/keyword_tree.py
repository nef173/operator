#!/usr/bin/env python3
"""
Keyword tree expansion engine for /discover-niches.

Takes seed head terms for a niche and expands them via:
- Google autocomplete (free, no auth) — bare seed + buyer-intent prefixes + audience suffixes
- pytrends related_queries (top + rising) per geo

Outputs raw expansion data as JSON. Semantic clustering into the 4-branch tree
(head / modifiers / buyer-intent / adjacent-products) is done by Claude in the
orchestrator skill — this script just gathers the raw signal.

Usage:
  python keyword_tree.py --seeds "posture corrector,back stretcher" \\
                         --geo US,GB \\
                         --out keywords-raw.json
"""

import argparse
import json
import sys
import time
from typing import Any

import requests

try:
    from pytrends.request import TrendReq
except ImportError:
    print("ERROR: pytrends not installed. Run: pip install -r requirements-niche-discovery.txt",
          file=sys.stderr)
    sys.exit(1)


AUTOCOMPLETE_URL = "https://suggestqueries.google.com/complete/search"
PREFIX_VARIANTS = ["best", "review"]
SUFFIX_VARIANTS = ["for women", "for men"]


def google_autocomplete(query: str, gl: str = "us") -> list[str]:
    """Hit Google's free autocomplete endpoint. Returns up to 10 suggestions."""
    params = {"client": "firefox", "q": query, "hl": "en", "gl": gl}
    try:
        r = requests.get(AUTOCOMPLETE_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data[1] if len(data) > 1 and isinstance(data[1], list) else []
    except Exception as e:
        print(f"  autocomplete failed for '{query}' (gl={gl}): {e}", file=sys.stderr)
        return []


def expand_seed_via_autocomplete(seed: str, geos: list[str]) -> dict[str, Any]:
    """Bare + prefix + suffix variants per geo."""
    out = {}
    for geo in geos:
        gl = geo.lower()
        bare = google_autocomplete(seed, gl=gl)
        prefix_variants = {p: google_autocomplete(f"{p} {seed}", gl=gl) for p in PREFIX_VARIANTS}
        suffix_variants = {s: google_autocomplete(f"{seed} {s}", gl=gl) for s in SUFFIX_VARIANTS}
        out[geo] = {
            "bare": bare,
            "prefix_variants": prefix_variants,
            "suffix_variants": suffix_variants,
        }
        time.sleep(0.3)
    return out


def pytrends_related(seeds: list[str], geo: str) -> dict[str, dict[str, list[str]]]:
    """Pull related_queries (top + rising) from Google Trends per seed."""
    pt = TrendReq(hl="en-US", tz=0, retries=2, backoff_factor=0.5)
    out: dict[str, dict[str, list[str]]] = {}
    for seed in seeds:
        try:
            pt.build_payload([seed], cat=0, timeframe="today 12-m", geo=geo)
            related = pt.related_queries() or {}
            entry = related.get(seed, {}) or {}
            top_df = entry.get("top")
            rising_df = entry.get("rising")
            out[seed] = {
                "top": top_df["query"].tolist() if top_df is not None and not top_df.empty else [],
                "rising": rising_df["query"].tolist() if rising_df is not None and not rising_df.empty else [],
            }
        except Exception as e:
            print(f"  pytrends related failed for '{seed}' (geo={geo}): {e}", file=sys.stderr)
            out[seed] = {"top": [], "rising": []}
        time.sleep(1.0)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", required=True, help="Comma-separated seed head terms")
    ap.add_argument("--geo", default="US,GB", help="Comma-separated geos (default US,GB)")
    ap.add_argument("--out", required=True, help="Output JSON path")
    args = ap.parse_args()

    seeds = [s.strip() for s in args.seeds.split(",") if s.strip()]
    geos = [g.strip().upper() for g in args.geo.split(",") if g.strip()]

    print(f"Expanding {len(seeds)} seeds across geos: {geos}", file=sys.stderr)

    result: dict[str, Any] = {
        "seeds": seeds,
        "geos": geos,
        "autocomplete": {},
        "trends_related": {},
    }

    print("Stage 1: Google autocomplete expansion...", file=sys.stderr)
    for seed in seeds:
        print(f"  {seed}", file=sys.stderr)
        result["autocomplete"][seed] = expand_seed_via_autocomplete(seed, geos)

    print("Stage 2: pytrends related queries (per geo)...", file=sys.stderr)
    for geo in geos:
        print(f"  geo={geo}", file=sys.stderr)
        result["trends_related"][geo] = pytrends_related(seeds, geo=geo)

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nWrote {args.out}", file=sys.stderr)

    # Summary counts
    autocomplete_count = 0
    for seed_data in result["autocomplete"].values():
        for geo_data in seed_data.values():
            autocomplete_count += len(geo_data.get("bare", []))
            for variants in geo_data.get("prefix_variants", {}).values():
                autocomplete_count += len(variants)
            for variants in geo_data.get("suffix_variants", {}).values():
                autocomplete_count += len(variants)
    trends_count = sum(
        len(seed_data["top"]) + len(seed_data["rising"])
        for geo_data in result["trends_related"].values()
        for seed_data in geo_data.values()
    )
    print(f"Autocomplete suggestions collected: {autocomplete_count}", file=sys.stderr)
    print(f"Trends-related queries collected:   {trends_count}", file=sys.stderr)


if __name__ == "__main__":
    main()
