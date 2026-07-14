#!/usr/bin/env python3
"""
Evergreen + seasonality + slope check via Google Trends.

For each keyword × geo, computes:
- 5y interest_over_time slope (rising / flat / declining)
- Seasonality variance (low = evergreen, high = seasonal spike)
- Peak / trough months
- Recent vs early growth ratio
- Mean + stdev of relative interest

Outputs a flat list of analysis records as JSON.

Usage:
  python trends_check.py --keywords "posture corrector,back stretcher" \\
                         --geo US,GB \\
                         --out trends.json
"""

import argparse
import json
import os
import sys
import time
from statistics import mean, stdev
from typing import Any

try:
    from pytrends.request import TrendReq
except ImportError:
    print("ERROR: pytrends not installed. Run: pip install -r requirements-niche-discovery.txt",
          file=sys.stderr)
    sys.exit(1)


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
    """Least-squares slope of y vs x where x = 0..n-1."""
    n = len(series)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(series) / n
    num = sum((xs[i] - mean_x) * (series[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def analyze(pt: TrendReq, keyword: str, geo: str) -> dict[str, Any]:
    """Pull 5y interest_over_time and compute summary stats."""
    pt.build_payload([keyword], cat=0, timeframe="today 5-y", geo=geo)
    df = pt.interest_over_time()
    if df is None or df.empty or keyword not in df.columns:
        return {"keyword": keyword, "geo": geo, "error": "no data"}

    series = [float(v) for v in df[keyword].tolist()]
    if not series:
        return {"keyword": keyword, "geo": geo, "error": "empty series"}

    n = len(series)
    overall_mean = mean(series) if series else 0
    series_stdev = stdev(series) if n > 1 else 0
    slope = linear_slope(series)

    # Seasonality: month-of-year aggregation
    months = df.index.month.tolist()
    by_month: dict[int, list[float]] = {}
    for m, v in zip(months, series):
        by_month.setdefault(int(m), []).append(float(v))
    monthly_avg = {m: mean(vs) for m, vs in by_month.items()}
    peak = max(monthly_avg, key=monthly_avg.get) if monthly_avg else 0
    trough = min(monthly_avg, key=monthly_avg.get) if monthly_avg else 0
    seasonal_variance = (
        (monthly_avg[peak] - monthly_avg[trough]) / overall_mean if overall_mean else 0
    )

    # Recent vs early — pytrends 5y returns weekly data, ~52 points/year
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
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keywords", required=True,
                    help="Comma-separated, OR path to JSON file with keyword list")
    ap.add_argument("--geo", default="US,GB")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    geos = [g.strip().upper() for g in args.geo.split(",") if g.strip()]
    keywords = load_keywords(args.keywords)

    print(f"Analyzing {len(keywords)} keywords × {len(geos)} geos via Google Trends",
          file=sys.stderr)

    pt = TrendReq(hl="en-US", tz=0, retries=2, backoff_factor=0.5)
    results = []
    for kw in keywords:
        for geo in geos:
            print(f"  {geo}: {kw}", file=sys.stderr)
            try:
                results.append(analyze(pt, kw, geo))
            except Exception as e:
                print(f"    failed: {e}", file=sys.stderr)
                results.append({"keyword": kw, "geo": geo, "error": str(e)})
            time.sleep(1.0)  # pytrends rate-limit politeness

    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
