#!/usr/bin/env python3
"""
discover_keywords.py — efficient AUTO-DISCOVERY of new candidate keywords
=========================================================================
Turns a few SEED category roots into a deduped candidate pool of product
keywords, as cheaply as possible. This is the front-end that feeds the radar
(sv_batch.py → trend_radar.py) so you stop hand-feeding keyword lists.

EFFICIENCY LADDER (cheapest, broadest first — every step is ~$0.01 or free):

  1. EXPAND each seed via TWO complementary DFS sources, 1 call each:
     a. DFS Labs `keyword_ideas`  — breadth: thousands of related keywords WITH
        absolute SV inline (~$0.01/seed). The wide net.
     b. Google Trends rising-queries — freshness: the terms Google flags as
        RISING/Breakout right now (~$0.009/seed). The "what's moving" net.
  2. CLEAN: strip brand/non-product tokens (movie/song/<brand>), drop the seed
     itself, dedupe, drop anything already in the trends cache (already known).
  3. RANK candidates by a cheap pre-score (Labs SV + rising %) so the SV batch
     and the expensive Trends graphs only ever touch the most promising.
  4. (orchestrated downstream) ONE batched SV call gates ALL candidates on
     sellability + sustained-rise; only survivors get a Trends graph.
  5. SELF-EXPAND (snowball): the SUSTAINED_RISE survivors become next-round
     seeds — discovery compounds without new human input.

Net cost for a discovery sweep: ~15 seeds × 2 calls × $0.01 = ~$0.30 to surface
hundreds of candidates, then ONE $0.009 SV batch to gate them. Pennies.

Usage:
  # expand seeds into a candidate pool (cheap):
  python discover_keywords.py --seeds "massager,bidet,knife sharpener" --geo US \
      --out /tmp/candidates.json

  # seeds from a file (one per line or JSON list):
  python discover_keywords.py --seeds seeds.txt --out /tmp/candidates.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

try:
    from dotenv import load_dotenv, find_dotenv
    _p = find_dotenv(usecwd=True)
    if not _p:
        for c in [Path(__file__).resolve().parent, *Path(__file__).resolve().parents]:
            if (c / ".env").exists():
                _p = str(c / ".env"); break
    if _p:
        load_dotenv(_p)
except ImportError:
    pass

LABS_IDEAS = "https://api.dataforseo.com/v3/dataforseo_labs/google/keyword_ideas/live"
TRENDS = "https://api.dataforseo.com/v3/keywords_data/google_trends/explore/live"
GEO_TO_LOCATION = {"US": "United States", "GB": "United Kingdom", "CA": "Canada",
                   "AU": "Australia", "DE": "Germany", "FR": "France"}

# Non-product / media / brand-ish tokens — same spirit as sv_batch sellability.
# A discovered candidate containing these is almost never a dropshippable product.
NON_PRODUCT_TOKENS = {
    "movie", "trailer", "episode", "season", "cast", "actor", "film", "song",
    "lyrics", "album", "concert", "tour", "tickets", "vs", "score", "news",
    "wiki", "near me", "for sale", "used", "rental", "rent", "job", "salary",
    "meaning", "definition", "reddit", "youtube", "amazon", "walmart", "ikea",
    "login", "app", "free", "download", "pdf", "how to", "what is", "why",
}
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "trends"


def slug(kw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", kw.lower()).strip("-")[:60]


def already_known(kw: str, geo: str) -> bool:
    return (CACHE_DIR / f"{slug(kw)}__{geo.upper()}__past_5_years.json").exists()


def is_clean_product(kw: str, seed: str) -> bool:
    k = kw.lower().strip()
    if k == seed.lower().strip():
        return False
    if len(k) < 3 or len(k.split()) > 6:
        return False
    if any(t in k for t in NON_PRODUCT_TOKENS):
        return False
    if any(ch.isdigit() for ch in k) and "20" in k:  # "best X 2025" year-noise
        return False
    return True


def expand_labs(seed: str, geo: str, auth: HTTPBasicAuth, limit: int) -> list[dict]:
    """Wide net: related keywords + SV inline, one call."""
    payload = [{"keywords": [seed], "location_name": GEO_TO_LOCATION.get(geo, "United States"),
                "language_code": "en", "limit": limit,
                "order_by": ["keyword_info.search_volume,desc"]}]
    try:
        r = requests.post(LABS_IDEAS, json=payload, auth=auth, timeout=60)
        r.raise_for_status()
        res = (r.json().get("tasks") or [{}])[0].get("result") or []
        items = res[0].get("items") if res else []
    except Exception as e:
        print(f"    labs expand failed for {seed!r}: {e}", file=sys.stderr)
        return []
    out = []
    for it in items or []:
        ki = it.get("keyword_info") or {}
        out.append({"keyword": it.get("keyword"), "sv": ki.get("search_volume"),
                    "source": "labs", "seed": seed})
    return out


def expand_rising(seed: str, geo: str, auth: HTTPBasicAuth) -> list[dict]:
    """Freshness net: Google's RISING related queries, one call."""
    payload = [{"keywords": [seed], "location_name": GEO_TO_LOCATION.get(geo, "United States"),
                "type": "web", "time_range": "past_12_months",
                "item_types": ["google_trends_queries_list"]}]
    try:
        r = requests.post(TRENDS, json=payload, auth=auth, timeout=60)
        r.raise_for_status()
        res = (r.json().get("tasks") or [{}])[0].get("result") or []
        items = res[0].get("items") if res else []
        ql = next((i for i in (items or []) if i.get("type") == "google_trends_queries_list"), None)
    except Exception as e:
        print(f"    rising expand failed for {seed!r}: {e}", file=sys.stderr)
        return []
    out = []
    for q in ((ql or {}).get("data", {}).get("rising") or []):
        out.append({"keyword": q.get("query"), "rising_value": q.get("value"),
                    "source": "rising", "seed": seed})
    return out


def discover(seeds: list[str], geo: str, auth: HTTPBasicAuth, labs_limit: int,
             skip_known: bool) -> list[dict]:
    pool: dict[str, dict] = {}
    for seed in seeds:
        print(f"  expanding {seed!r} …", file=sys.stderr)
        for cand in expand_labs(seed, geo, auth, labs_limit) + expand_rising(seed, geo, auth):
            kw = (cand.get("keyword") or "").strip().lower()
            if not kw or not is_clean_product(kw, seed):
                continue
            if skip_known and already_known(kw, geo):
                continue
            # merge dup candidates; a keyword found by BOTH labs+rising is stronger
            if kw in pool:
                pool[kw]["sources"] = sorted(set(pool[kw]["sources"]) | {cand["source"]})
                pool[kw]["sv"] = pool[kw].get("sv") or cand.get("sv")
                pool[kw]["rising_value"] = pool[kw].get("rising_value") or cand.get("rising_value")
            else:
                pool[kw] = {"keyword": kw, "sv": cand.get("sv"),
                            "rising_value": cand.get("rising_value"),
                            "sources": [cand["source"]], "seeds": [seed]}
    # pre-score: in BOTH source nets = strongest; else SV-weighted
    cands = list(pool.values())
    for c in cands:
        both = len(c["sources"]) >= 2
        c["corroborated"] = both
        c["prescore"] = (c.get("sv") or 0) * (2 if both else 1) + (
            (c.get("rising_value") or 0) if isinstance(c.get("rising_value"), int) else 0)
    cands.sort(key=lambda c: c["prescore"], reverse=True)
    return cands


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seeds", required=True, help="Comma list, or path to file (lines / JSON list)")
    ap.add_argument("--geo", default="US")
    ap.add_argument("--out", required=True)
    ap.add_argument("--labs-limit", type=int, default=200,
                    help="Max related keywords per seed from Labs (default 200)")
    ap.add_argument("--include-known", action="store_true",
                    help="Don't skip keywords already in the trends cache")
    ap.add_argument("--max-candidates", type=int, default=1000,
                    help="Cap the candidate pool (SV batch takes ≤1000/call)")
    args = ap.parse_args()

    user = os.environ.get("DATAFORSEO_USERNAME"); pw = os.environ.get("DATAFORSEO_PASSWORD")
    if not user or not pw:
        print("ERROR: DATAFORSEO_USERNAME/PASSWORD required", file=sys.stderr); return 1
    auth = HTTPBasicAuth(user, pw)

    if os.path.exists(args.seeds):
        txt = Path(args.seeds).read_text().strip()
        try:
            seeds = json.loads(txt); seeds = seeds if isinstance(seeds, list) else seeds.get("seeds", [])
        except json.JSONDecodeError:
            seeds = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    else:
        seeds = [s.strip() for s in args.seeds.split(",") if s.strip()]

    n_calls = len(seeds) * 2
    print(f"Discovery: {len(seeds)} seeds × 2 sources = {n_calls} calls ~${0.01*n_calls:.2f}",
          file=sys.stderr)
    cands = discover(seeds, args.geo, auth, args.labs_limit, skip_known=not args.include_known)
    cands = cands[:args.max_candidates]

    Path(args.out).write_text(json.dumps(cands, indent=2))
    corro = sum(1 for c in cands if c["corroborated"])
    print(f"  → {len(cands)} candidates ({corro} corroborated by both nets) → {args.out}",
          file=sys.stderr)
    # also emit a bare keyword list for direct piping into sv_batch.py
    kw_only = args.out.replace(".json", "-keywords.json")
    Path(kw_only).write_text(json.dumps([c["keyword"] for c in cands], indent=2))
    print(f"  → keyword list for sv_batch: {kw_only}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
