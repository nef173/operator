#!/usr/bin/env python3
"""
Google Trends via DataForSEO — replaces trends_check.py (pytrends) since pytrends
is reliably 429'd in 2025-26.

For each keyword × geo, returns:
- 5y interest_over_time slope (rising / flat / declining)
- Seasonality variance (low = evergreen)
- Peak / trough months
- Recent vs early growth ratio
- Mean + stdev of relative interest
- **Top + Rising related queries** (past 12 months) — matches new Google Trends UI panels (2026-05-13)

Uses DataForSEO Trends Live endpoint:
  POST /v3/keywords_data/google_trends/explore/live

Each call sends TWO tasks in one batched request:
- 5y interest_over_time graph
- Past-12-month Top + Rising related queries list

Cost: ~$0.05 per keyword × geo (covers both sub-tasks in one HTTP call).
Auth: HTTP Basic with DATAFORSEO_USERNAME + DATAFORSEO_PASSWORD env vars.

Usage:
  DATAFORSEO_USERNAME=... DATAFORSEO_PASSWORD=... \\
    python trends_dfs.py --keywords "weighted sleep mask,tongue scraper" \\
                         --geo US \\
                         --out trends.json
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import mean, stdev
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

# Load DATAFORSEO_* env vars from project-root .env (walks up from script location).
# Idempotent — safe to call repeatedly. Silent if python-dotenv is missing.
try:
    from dotenv import load_dotenv, find_dotenv
    _env_path = find_dotenv(usecwd=True) or find_dotenv(filename=".env", raise_error_if_not_found=False)
    if not _env_path:
        # Walk up from this script's directory to find a .env (project root)
        _here = Path(__file__).resolve().parent
        for _candidate in [_here, *_here.parents]:
            if (_candidate / ".env").exists():
                _env_path = str(_candidate / ".env")
                break
    if _env_path:
        load_dotenv(_env_path)
except ImportError:
    pass  # python-dotenv not installed — fall back to live env vars only


DFS_ENDPOINT = "https://api.dataforseo.com/v3/keywords_data/google_trends/explore/live"

# Map ISO-2 country codes to DataForSEO location names
GEO_TO_LOCATION = {
    "US": "United States",
    "GB": "United Kingdom",
    "CA": "Canada",
    "AU": "Australia",
    "NZ": "New Zealand",
    "DE": "Germany",
    "FR": "France",
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


def linear_slope(series: list[float]) -> float:
    n = len(series)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(series) / n
    num = sum((xs[i] - mean_x) * (series[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def _dfs_call(payload: list[dict], auth: HTTPBasicAuth) -> list:
    """One DFS Live POST. Returns combined results across all tasks in the response."""
    r = requests.post(DFS_ENDPOINT, json=payload, auth=auth, timeout=60)
    r.raise_for_status()
    data = r.json()
    if data.get("status_code") != 20000:
        raise RuntimeError(f"DFS error {data.get('status_code')}: {data.get('status_message')}")
    tasks = data.get("tasks") or []
    combined: list = []
    for task in tasks:
        if task.get("status_code") != 20000:
            print(f"    DFS sub-task warn {task.get('status_code')}: {task.get('status_message')}",
                  file=sys.stderr)
            continue
        combined.extend(task.get("result") or [])
    return combined


def fetch_trends(keyword: str, geo: str, auth: HTTPBasicAuth) -> dict[str, Any]:
    """Three DFS calls per keyword × geo: web graph + shopping graph + past-12mo queries.

    Returns dict with keys 'web_results', 'shopping_results', 'queries_results' so
    downstream parsing can disambiguate the two graph series.
    Cost: 3 × $0.025 = ~$0.075 per keyword × geo.
    """
    location_name = GEO_TO_LOCATION.get(geo, "United States")
    # Call 1: 5y web search graph
    web_graph_payload = [{
        "keywords": [keyword],
        "location_name": location_name,
        "type": "web",
        "date_from": None,
        "time_range": "past_5_years",
        "item_types": ["google_trends_graph"],
    }]
    # Call 2: 5y Google Shopping ("froogle") graph — added 2026-05-13
    # Same 0-100 normalized scale as web, but only counts Shopping-tab searches.
    # Overlay on the chart for commercial-intent vs general-interest comparison.
    shopping_graph_payload = [{
        "keywords": [keyword],
        "location_name": location_name,
        "type": "froogle",
        "date_from": None,
        "time_range": "past_5_years",
        "item_types": ["google_trends_graph"],
    }]
    # Call 3: past-12-month top + rising related queries (web search context)
    queries_payload = [{
        "keywords": [keyword],
        "location_name": location_name,
        "type": "web",
        "date_from": None,
        "time_range": "past_12_months",
        "item_types": ["google_trends_queries_list"],
    }]

    out = {"web_results": [], "shopping_results": [], "queries_results": []}
    try:
        out["web_results"] = _dfs_call(web_graph_payload, auth)
    except Exception as e:
        print(f"    web graph call failed: {e}", file=sys.stderr)
    try:
        out["shopping_results"] = _dfs_call(shopping_graph_payload, auth)
    except Exception as e:
        print(f"    shopping graph call failed: {e}", file=sys.stderr)
    try:
        out["queries_results"] = _dfs_call(queries_payload, auth)
    except Exception as e:
        print(f"    queries call failed: {e}", file=sys.stderr)
    return out


def parse_trends_response(result: list[dict]) -> tuple[list[float], list[str]]:
    """Extract weekly interest values + their ISO date strings from DFS response.

    Scans ALL result rows for the graph item (combined-results may have multiple).
    """
    if not result:
        return [], []
    graph = None
    for r_row in result:
        items = (r_row or {}).get("items") or []
        graph = next((i for i in items if i.get("type") == "google_trends_graph"), None)
        if graph:
            break
    if not graph:
        return [], []
    series = []
    dates = []
    for row in graph.get("data") or []:
        # DFS shape: {"date_from": "...", "date_to": "...", "values": [int_or_None, ...]}
        vals = row.get("values") or []
        if not vals:
            continue
        v = vals[0]
        if v is None:
            continue
        series.append(float(v))
        dates.append(row.get("date_from") or "")
    return series, dates


def parse_queries_response(result: list[dict]) -> dict[str, list[dict]]:
    """Extract Top and Rising related queries from DFS response.

    DFS shape: items[].type = "google_trends_queries_list", with .data:
      {"top": [{"query": "...", "value": <interest 0-100>}], "rising": [{"query": "...", "value": <% change or 'BREAKOUT'>}]}
    Returns {"top": [...], "rising": [...]} normalized.
    """
    if not result:
        return {"top": [], "rising": []}
    queries_item = None
    for r_row in result:
        items = (r_row or {}).get("items") or []
        queries_item = next(
            (i for i in items if i.get("type") == "google_trends_queries_list"),
            None,
        )
        if queries_item:
            break
    if not queries_item:
        return {"top": [], "rising": []}
    data = queries_item.get("data") or {}
    top = []
    rising = []
    for q in (data.get("top") or []):
        top.append({
            "query": q.get("query"),
            "search_interest": q.get("value"),
        })
    for q in (data.get("rising") or []):
        # Value is either an integer % change OR the string "Breakout" (>5000%)
        rising.append({
            "query": q.get("query"),
            "change": q.get("value"),
        })
    return {"top": top, "rising": rising}


def analyze(keyword: str, geo: str, auth: HTTPBasicAuth) -> dict[str, Any]:
    fetched = fetch_trends(keyword, geo, auth)
    # Web search series (primary analysis source)
    series, dates = parse_trends_response(fetched["web_results"])
    # Shopping series (for overlay on chart, no separate stats computed)
    shopping_series, shopping_dates = parse_trends_response(fetched["shopping_results"])
    # Related queries from the queries call
    queries = parse_queries_response(fetched["queries_results"])
    if not series:
        return {
            "keyword": keyword,
            "geo": geo,
            "error": "empty web series from DFS",
            "related_queries": queries,
            "shopping_series": shopping_series,
            "shopping_dates": shopping_dates,
        }

    n = len(series)
    overall_mean = mean(series)
    series_stdev = stdev(series) if n > 1 else 0.0
    slope = linear_slope(series)

    # Seasonality: month-of-year aggregation from date_from strings (YYYY-MM-DD)
    by_month: dict[int, list[float]] = {}
    for d, v in zip(dates, series):
        try:
            m = int(d.split("-")[1])
        except Exception:
            continue
        by_month.setdefault(m, []).append(v)
    monthly_avg = {m: mean(vs) for m, vs in by_month.items() if vs}
    peak = max(monthly_avg, key=monthly_avg.get) if monthly_avg else 0
    trough = min(monthly_avg, key=monthly_avg.get) if monthly_avg else 0
    seasonal_variance = (
        (monthly_avg[peak] - monthly_avg[trough]) / overall_mean if overall_mean else 0
    )

    recent_window = min(52, n // 2)
    recent = mean(series[-recent_window:]) if recent_window else overall_mean
    early = mean(series[:recent_window]) if recent_window else overall_mean
    growth_ratio = (recent / early) if early else 0

    return {
        "keyword": keyword,
        "geo": geo,
        "data_points": n,
        "mean_interest": round(overall_mean, 2),
        "stdev_interest": round(series_stdev, 2),
        "slope_per_period": round(slope, 4),
        "trend_verdict": (
            "rising" if slope > 0.05
            else "declining" if slope < -0.05
            else "flat"
        ),
        "growth_ratio_recent_vs_early": round(growth_ratio, 2),
        "seasonality_variance": round(seasonal_variance, 2),
        "evergreen_verdict": (
            "evergreen" if seasonal_variance < 0.4
            else "seasonal" if seasonal_variance < 0.8
            else "highly_seasonal"
        ),
        "peak_month": int(peak),
        "trough_month": int(trough),
        "monthly_avg": {str(k): round(v, 2) for k, v in monthly_avg.items()},
        "raw_series": series,             # Web search trend (primary)
        "raw_dates": dates,
        "shopping_series": shopping_series,  # Google Shopping trend (overlay)
        "shopping_dates": shopping_dates,
        "related_queries": queries,          # {"top": [...], "rising": [...]}
        "source": "dataforseo",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keywords", required=True,
                    help="Comma-separated, OR path to JSON file with keyword list")
    ap.add_argument("--geo", default="US,GB")
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=6,
                    help="Parallel API workers (default 6). DFS Trends takes ~15-20s "
                         "per kw×geo serial — parallelism brings 16 calls from ~5min to ~1min.")
    args = ap.parse_args()

    username = os.environ.get("DATAFORSEO_USERNAME")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not username or not password:
        print("ERROR: DATAFORSEO_USERNAME and DATAFORSEO_PASSWORD env vars required.",
              file=sys.stderr)
        sys.exit(1)
    auth = HTTPBasicAuth(username, password)

    geos = [g.strip().upper() for g in args.geo.split(",") if g.strip()]
    keywords = load_keywords(args.keywords)

    print(f"Analyzing {len(keywords)} keywords × {len(geos)} geos via DataForSEO Trends",
          file=sys.stderr)
    n_calls = len(keywords) * len(geos) * 3  # web graph + shopping graph + queries
    print(f"  Estimated cost: ~${0.025 * n_calls:.2f} "
          f"({n_calls} API calls @ ~$0.025 — 3 per kw×geo: web graph + shopping graph + queries)",
          file=sys.stderr)

    jobs = [(kw, geo) for kw in keywords for geo in geos]
    print(f"  Running {len(jobs)} jobs with {args.workers} parallel workers",
          file=sys.stderr)
    results: list[dict[str, Any]] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        future_map = {ex.submit(analyze, kw, geo, auth): (kw, geo) for kw, geo in jobs}
        for i, fut in enumerate(as_completed(future_map), 1):
            kw, geo = future_map[fut]
            try:
                res = fut.result()
                print(f"  [{i}/{len(jobs)}] {geo}: {kw!r}  ok", file=sys.stderr)
                results.append(res)
            except Exception as e:
                print(f"  [{i}/{len(jobs)}] {geo}: {kw!r}  FAIL: {e}", file=sys.stderr)
                results.append({"keyword": kw, "geo": geo, "error": str(e)})

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    elapsed = time.time() - t0
    ok = sum(1 for r in results if not r.get("error"))
    print(f"\nWrote {args.out}  ({ok} ok, {len(results)-ok} failed, {elapsed:.0f}s)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
