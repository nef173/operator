#!/usr/bin/env python3
"""
Enrich a competitors-grid.json sidecar with estimated monthly website traffic
per merchant domain, via DataForSEO's SimilarWeb bulk metrics endpoint.

Adds the following fields to each competitor record:
  - merchant_domain (e.g. "nordstrom.com")
  - monthly_visits (estimated total monthly visits from SimilarWeb)
  - visit_duration_seconds, pages_per_visit, bounce_rate (engagement signals)

Tells the operator "how big a brand is this competitor?" — 50M visits/mo
(Walmart-class) = unbeatable as a brand; 100K-500K visits/mo (Nodpod-class) =
real DTC peer to study; <10K visits/mo = beatable indie.

Cost: one DFS task (~$0.0025 per call) covers up to 1000 domains. For a typical
10-competitor grid, this is ~$0.0025 total — effectively free.

Usage:
  DATAFORSEO_USERNAME=... DATAFORSEO_PASSWORD=... \\
    python enrich_domain_traffic.py \\
      --in dossiers/premium-sleep-mask/competitors-grid.json
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

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


DFS_ENDPOINT = "https://api.dataforseo.com/v3/business_data/similarweb/bulk_metrics/live"


def extract_domain(url: str | None) -> str | None:
    """Get bare host from a URL — strip 'www.' prefix."""
    if not url:
        return None
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def fetch_traffic(domains: list[str], auth: HTTPBasicAuth) -> dict[str, dict]:
    """Call DFS SimilarWeb bulk metrics. Returns {domain: metrics_dict}."""
    if not domains:
        return {}
    payload = [{"targets": domains}]
    headers = {"Content-Type": "application/json"}
    r = requests.post(DFS_ENDPOINT, json=payload, auth=auth, headers=headers, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("status_code") != 20000:
        raise RuntimeError(f"DFS error {data.get('status_code')}: {data.get('status_message')}")
    tasks = data.get("tasks") or []
    if not tasks:
        return {}
    task = tasks[0]
    if task.get("status_code") != 20000:
        raise RuntimeError(f"DFS task error {task.get('status_code')}: {task.get('status_message')}")
    results = task.get("result") or []
    if not results:
        return {}
    items = results[0].get("items") or []
    out: dict[str, dict] = {}
    for item in items:
        target = (item.get("target") or "").lower()
        if not target:
            continue
        out[target] = {
            "monthly_visits": item.get("visits"),
            "visit_duration_seconds": item.get("time_on_site"),
            "pages_per_visit": item.get("pages_per_visit"),
            "bounce_rate": item.get("bounce_rate"),
            "country_alpha2": item.get("country_alpha2_code"),
            "global_rank": item.get("global_rank"),
            "country_rank": item.get("country_rank"),
        }
    return out


def format_visits(n: float | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:.0f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="input", required=True,
                    help="Path to competitors-grid.json")
    args = ap.parse_args()

    username = os.environ.get("DATAFORSEO_USERNAME")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not username or not password:
        print("ERROR: DATAFORSEO_USERNAME and DATAFORSEO_PASSWORD env vars required.",
              file=sys.stderr)
        sys.exit(1)
    auth = HTTPBasicAuth(username, password)

    with open(args.input) as f:
        data = json.load(f)
    if not isinstance(data, list):
        print("ERROR: input must be a JSON list", file=sys.stderr)
        sys.exit(1)

    # Extract unique domains from merchant_url field
    for c in data:
        c["merchant_domain"] = extract_domain(c.get("merchant_url"))
    unique_domains = sorted({c["merchant_domain"] for c in data if c["merchant_domain"]})
    print(f"Looking up traffic for {len(unique_domains)} unique domains...", file=sys.stderr)
    for d in unique_domains:
        print(f"  {d}", file=sys.stderr)

    try:
        metrics = fetch_traffic(unique_domains, auth)
    except Exception as e:
        print(f"ERROR fetching DFS traffic: {e}", file=sys.stderr)
        sys.exit(1)

    # Map back to each competitor
    for c in data:
        d = c.get("merchant_domain")
        if d and d in metrics:
            c.update(metrics[d])
        else:
            c["monthly_visits"] = None

    with open(args.input, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nUpdated {args.input}", file=sys.stderr)

    # Print summary
    print(f"\n{'#':<3} {'Merchant':<28} {'Domain':<28} {'Visits/mo':>10}",
          file=sys.stderr)
    print("-" * 75, file=sys.stderr)
    for i, c in enumerate(data, 1):
        print(f"{i:<3} {(c.get('merchant') or '—')[:27]:<28} "
              f"{(c.get('merchant_domain') or '—')[:27]:<28} "
              f"{format_visits(c.get('monthly_visits')):>10}",
              file=sys.stderr)


if __name__ == "__main__":
    main()
