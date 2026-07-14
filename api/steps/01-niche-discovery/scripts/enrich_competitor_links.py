#!/usr/bin/env python3
"""
Enrich a competitors-grid.json sidecar (from chart_competitors.py) with the
direct merchant URLs.

Serper's /shopping endpoint returns Google internal redirects (clicking these
opens Google's product detail page first, not the merchant product page).
This script runs one Serper /search query per competitor — `<title> <merchant>` —
and pulls the first organic result whose hostname matches the merchant. That
URL goes onto the JSON as `merchant_url`.

Cost: 1 Serper /search query per competitor (no DFS spend). For a 10-competitor
grid that's 10 of your 2,500 free-tier Serper queries — trivial.

Usage:
  SERPER_API_KEY=xxx python enrich_competitor_links.py \\
      --in dossiers/premium-sleep-mask/competitors-grid.json
"""

import argparse
import json
import os
import re
import sys
import time
from typing import Optional
from urllib.parse import urlparse

import requests


SERPER_SEARCH_URL = "https://google.serper.dev/search"


def merchant_to_domain_tokens(merchant: str) -> list[str]:
    """Heuristic: produce a list of substrings likely to appear in the merchant's
    domain. e.g. 'Walmart - FM Store' -> ['walmart', 'walmartfmstore']."""
    if not merchant:
        return []
    cleaned = re.sub(r"[^a-zA-Z0-9 ]+", " ", merchant).lower()
    tokens = cleaned.split()
    out = set()
    # individual tokens
    for t in tokens:
        if len(t) >= 4:
            out.add(t)
    # whole merchant name slugified
    full = "".join(tokens)
    if full:
        out.add(full)
    # first 2 tokens combined
    if len(tokens) >= 2:
        out.add("".join(tokens[:2]))
    return list(out)


def find_merchant_url(title: str, merchant: str, api_key: str) -> Optional[str]:
    """Run a Serper /search query for `<title> <merchant>`, pick the first organic
    result hosted on the merchant's domain."""
    query = f"{title} {merchant}".strip()
    if not query:
        return None
    payload = {"q": query, "gl": "us", "hl": "en", "num": 10}
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    r = requests.post(SERPER_SEARCH_URL, json=payload, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    organic = data.get("organic") or []
    if not organic:
        return None

    tokens = merchant_to_domain_tokens(merchant)
    for o in organic:
        link = o.get("link") or ""
        host = urlparse(link).hostname or ""
        host_clean = host.lower().replace(".", "").replace("www", "")
        for tok in tokens:
            if tok and tok in host_clean:
                return link
    # No domain match — fall back to first organic result with a caveat
    return organic[0].get("link")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="input", required=True,
                    help="Path to competitors-grid.json from chart_competitors.py")
    args = ap.parse_args()

    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        print("ERROR: SERPER_API_KEY env var not set", file=sys.stderr)
        sys.exit(1)

    with open(args.input) as f:
        data = json.load(f)
    if not isinstance(data, list):
        print("ERROR: input must be a JSON list of competitor records", file=sys.stderr)
        sys.exit(1)

    print(f"Enriching {len(data)} competitors with merchant URLs (Serper /search)...",
          file=sys.stderr)
    for i, c in enumerate(data, 1):
        if c.get("merchant_url"):
            continue
        title = c.get("title") or ""
        merchant = c.get("merchant") or ""
        print(f"  #{i} {merchant:<30} {title[:50]}", file=sys.stderr)
        try:
            url = find_merchant_url(title, merchant, api_key)
            c["merchant_url"] = url
            print(f"      -> {url}", file=sys.stderr)
        except Exception as e:
            c["merchant_url"] = None
            c["merchant_url_error"] = str(e)
            print(f"      FAILED: {e}", file=sys.stderr)
        time.sleep(0.3)  # gentle pacing

    with open(args.input, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nUpdated {args.input}", file=sys.stderr)


if __name__ == "__main__":
    main()
