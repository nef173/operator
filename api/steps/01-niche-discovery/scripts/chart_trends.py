#!/usr/bin/env python3
"""
Generate a Google-Trends-style PNG chart from a trends.json file produced
by trends_dfs.py (which now preserves raw_series + raw_dates).

For each {keyword, geo} entry in the input JSON, produces one PNG.
PNG file is named: <out-dir>/trends-<slug>.png

Usage:
  python chart_trends.py --in trends.json --out-dir dossiers/premium-sleep-mask/
"""

import argparse
import json
import os
import sys
from datetime import datetime

import matplotlib

matplotlib.use("Agg")  # non-interactive backend, no display needed
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter, YearLocator
import numpy as np


# Map ISO-2 geo codes to display names (matches the DFS Trends API location names)
GEO_DISPLAY = {
    "US": "United States",
    "GB": "United Kingdom",
    "CA": "Canada",
    "AU": "Australia",
    "NZ": "New Zealand",
    "DE": "Germany",
    "FR": "France",
}


def slugify(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in s).strip("-")[:60]


def smooth_series(values: list[float], window: int = 3) -> list[float]:
    """Apply a small moving-average to smooth the line — matches Google Trends rendering."""
    if len(values) < window or window <= 1:
        return list(values)
    arr = np.array(values, dtype=float)
    pad = window // 2
    padded = np.pad(arr, (pad, pad), mode="edge")
    kernel = np.ones(window) / window
    smoothed = np.convolve(padded, kernel, mode="valid")
    return smoothed.tolist()


def render_chart(record: dict, out_path: str) -> None:
    """Render a Google-Trends-style line chart and save as PNG.

    Overlays Web search trend (Google blue #1A73E8) AND Google Shopping trend
    (orange #F9AB00) when both series are present. Web is the dominant interest
    signal; Shopping is the commercial-intent overlay.
    """
    series = record.get("raw_series") or []
    dates = record.get("raw_dates") or []
    shopping_series = record.get("shopping_series") or []
    shopping_dates = record.get("shopping_dates") or []
    keyword = record.get("keyword", "unknown")
    geo = record.get("geo", "")

    if not series or not dates or len(series) != len(dates):
        raise ValueError(f"Bad data for {keyword}: series={len(series)} dates={len(dates)}")

    xs = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
    smoothed_web = smooth_series(series, window=3)
    has_shopping = (
        bool(shopping_series)
        and bool(shopping_dates)
        and len(shopping_series) == len(shopping_dates)
    )
    if has_shopping:
        shopping_xs = [datetime.strptime(d, "%Y-%m-%d") for d in shopping_dates]
        smoothed_shopping = smooth_series(shopping_series, window=3)
    location_label = GEO_DISPLAY.get(geo, geo or "—")

    # Wide aspect ratio matching Google Trends
    fig, ax = plt.subplots(figsize=(16, 4.8), dpi=110)
    fig.patch.set_facecolor("white")

    # Header — placed above the plot area (figure-level text, not axes)
    fig.text(0.045, 0.96, "Interest over time", fontsize=14,
             color="#202124", fontweight="500")
    fig.text(0.045, 0.915, f"{location_label} · Past 5 years",
             fontsize=10, color="#5F6368")
    fig.text(0.045, 0.875, f'Search term: "{keyword}"',
             fontsize=9, color="#9AA0A6", style="italic")
    fig.text(0.92, 0.96, "Google Trends", fontsize=9,
             color="#9AA0A6", ha="right", style="italic")

    # Legend chips top-right (only when Shopping series is present)
    if has_shopping:
        # Web Search chip
        fig.text(0.62, 0.92, "● ", fontsize=12, color="#1A73E8", ha="right")
        fig.text(0.625, 0.92, "Web Search", fontsize=10, color="#3C4043", ha="left")
        # Google Shopping chip
        fig.text(0.78, 0.92, "● ", fontsize=12, color="#F9AB00", ha="right")
        fig.text(0.785, 0.92, "Google Shopping", fontsize=10, color="#3C4043", ha="left")

    # Plot — Web Search blue line with subtle area fill
    ax.plot(xs, smoothed_web, color="#1A73E8", linewidth=2.0,
            solid_capstyle="round", label="Web Search")
    ax.fill_between(xs, smoothed_web, color="#1A73E8", alpha=0.08)

    # Shopping overlay (Google yellow/orange)
    if has_shopping:
        ax.plot(shopping_xs, smoothed_shopping, color="#F9AB00",
                linewidth=2.0, solid_capstyle="round", label="Google Shopping",
                alpha=0.9)
        ax.fill_between(shopping_xs, smoothed_shopping, color="#F9AB00", alpha=0.06)

    # Y-axis: 0–100 scale, ticks at every 20
    ax.set_ylim(0, 105)
    ax.set_yticks([0, 20, 40, 60, 80, 100])
    ax.set_xlim(xs[0], xs[-1])

    # X-axis: year-only labels
    ax.xaxis.set_major_locator(YearLocator())
    ax.xaxis.set_major_formatter(DateFormatter("%Y"))

    # No axis labels, no chart title — keep it clean
    ax.set_xlabel("")
    ax.set_ylabel("")

    # Grid: horizontal only, very light gray
    ax.grid(True, axis="y", linestyle="-", color="#E8EAED", linewidth=0.7)
    ax.set_axisbelow(True)

    # Spines: only bottom visible, in light gray
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#DADCE0")
    ax.spines["bottom"].set_linewidth(0.7)

    # Ticks: gray text, no tick marks on Y
    ax.tick_params(axis="x", colors="#5F6368", labelsize=10,
                   length=4, color="#DADCE0")
    ax.tick_params(axis="y", colors="#5F6368", labelsize=10,
                   left=False, pad=8)

    # Layout — leave headroom for header
    plt.subplots_adjust(top=0.80, bottom=0.12, left=0.045, right=0.98)
    plt.savefig(out_path, dpi=110, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="input", required=True,
                    help="Path to trends.json (from trends_dfs.py)")
    ap.add_argument("--out-dir", required=True,
                    help="Directory to write PNG file(s) into")
    args = ap.parse_args()

    with open(args.input) as f:
        data = json.load(f)

    if not isinstance(data, list):
        print("ERROR: trends.json must be a list of records", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)
    n_written = 0
    for rec in data:
        if "error" in rec:
            print(f"Skipping {rec.get('keyword')} ({rec.get('geo')}): "
                  f"error={rec['error']}", file=sys.stderr)
            continue
        if not rec.get("raw_series"):
            print(f"Skipping {rec.get('keyword')} ({rec.get('geo')}): "
                  f"no raw_series in record (re-run trends_dfs.py after updating).",
                  file=sys.stderr)
            continue
        slug = slugify(f"{rec['keyword']}-{rec.get('geo','')}")
        out_path = os.path.join(args.out_dir, f"trends-{slug}.png")
        try:
            render_chart(rec, out_path)
            print(f"Wrote {out_path}", file=sys.stderr)
            n_written += 1
        except Exception as e:
            print(f"Failed for {rec.get('keyword')}: {e}", file=sys.stderr)

    print(f"\nWrote {n_written} chart(s).", file=sys.stderr)


if __name__ == "__main__":
    main()
