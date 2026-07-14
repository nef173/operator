#!/usr/bin/env python3
"""
trend_report.py — Exploding-Topics-style trend report from OUR data
====================================================================
Produces a ranked markdown report like explodingtopics.com/blog/trending-topics,
generated from our own DFS pipeline. Matches ET's documented format (verified via
Semrush KB 2026-06-15):
  • ranked table: # / keyword / growth% / status
  • ET's 3 statuses = our modes: EXPLODING / STEADY(=regular) / PEAKED
  • PLUS our extras ET lacks: absolute SV, sellability gate (no movies/events),
    CPC (commercial value), and the data source per row.

Input = the JSON from sv_batch.py (must include 4yr monthly_searches +
competition_index). Computes sustained_rise per keyword via trends_lib, then
renders the report. No extra API calls — pure rendering over already-pulled data.

Usage:
  # from an sv_batch.py output (4yr monthly):
  python trend_report.py --sv-json /tmp/sv.json --geo US --out report.md

  # title + min growth filter:
  python trend_report.py --sv-json /tmp/sv.json --title "Trending Products — US" \
      --min-growth 30 --out report.md
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import trends_lib


def classify_row(m: dict) -> dict | None:
    """Run sustained_rise + sellability on one SV record. Returns a report row or
    None if not sellable / no usable data."""
    ci = m.get("competition_index")
    ms = m.get("monthly_searches")
    kw = m.get("keyword")
    # sellability gate
    is_sellable = True if ci is None else ci >= 25
    if not is_sellable:
        return {"keyword": kw, "status": "NOT_SELLABLE", "sv": m.get("search_volume"),
                "ci": ci, "growth": None, "r2": None, "tier": None}
    sr = trends_lib.sustained_rise_adaptive(ms, None, None)
    if not sr.get("source"):
        return None
    # map to ET-style status
    if sr.get("is_sustained_rise"):
        status = "EXPLODING" if sr["tier"] == "exploding" else "STEADY"
    elif sr.get("is_peaked"):
        status = "PEAKED"
    elif sr.get("is_choppy_rise"):
        status = "RISING (choppy)"
    else:
        status = "FLAT"
    return {"keyword": kw, "status": status, "sv": m.get("search_volume"),
            "ci": ci, "cpc": m.get("cpc"),
            "growth": sr.get("annualized_growth_pct"), "r2": sr.get("r2"),
            "tier": sr.get("tier"), "stage": sr.get("stage"),
            "source": sr.get("source")}


STATUS_EMOJI = {"EXPLODING": "🚀", "STEADY": "📈", "RISING (choppy)": "🟠",
                "PEAKED": "📉", "FLAT": "·", "NOT_SELLABLE": "🚫"}
STATUS_ORDER = ["EXPLODING", "STEADY", "RISING (choppy)", "PEAKED", "FLAT", "NOT_SELLABLE"]


def render(rows: list[dict], title: str, geo: str, min_growth: int) -> str:
    out = [f"# {title}", "",
           f"_Generated {date.today().isoformat()} · geo {geo} · method: Google search-volume "
           f"growth (log-linear slope + R²), ecommerce-sellability gated_", ""]
    # group by status
    by_status: dict[str, list] = {s: [] for s in STATUS_ORDER}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)
    # rank the actionable buckets by growth, others by SV
    for s in STATUS_ORDER:
        b = by_status.get(s) or []
        if s in ("EXPLODING", "STEADY", "RISING (choppy)"):
            b = [r for r in b if (r.get("growth") or 0) >= min_growth]
            b.sort(key=lambda r: (r.get("growth") or 0), reverse=True)
        else:
            b.sort(key=lambda r: (r.get("sv") or 0), reverse=True)
        by_status[s] = b

    # headline: the EXPLODING + STEADY = the "trending products" list (ET's core)
    actionable = by_status["EXPLODING"] + by_status["STEADY"] + by_status["RISING (choppy)"]
    out.append(f"## 🚀 Trending Products — {len(actionable)} rising & sellable")
    out.append("")
    out.append("| # | Product | Growth/yr | Status | Search Vol | CPC | Steadiness (R²) |")
    out.append("|---|---|---|---|---|---|---|")
    for i, r in enumerate(actionable, 1):
        sv = f"{r['sv']:,}" if r.get("sv") else "—"
        cpc = f"${r['cpc']:.2f}" if r.get("cpc") else "—"
        g = f"{r['growth']:,}%" if r.get("growth") is not None else "—"
        out.append(f"| {i} | {r['keyword']} | {g} | {STATUS_EMOJI.get(r['status'])} {r['status']} "
                   f"| {sv} | {cpc} | {r.get('r2')} |")
    out.append("")

    # peaked (too late) + not-sellable (filtered out) — shown for transparency
    for s, label in [("PEAKED", "📉 Peaked (rose then cooling — skip)"),
                     ("NOT_SELLABLE", "🚫 Filtered out — not a sellable product (person/event/concept)")]:
        b = by_status.get(s) or []
        if not b:
            continue
        out.append(f"## {label}  ({len(b)})")
        out.append(", ".join(r["keyword"] for r in b[:40]))
        out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sv-json", required=True, help="sv_batch.py output (must have 4yr monthly_searches)")
    ap.add_argument("--geo", default="US")
    ap.add_argument("--title", default="Trending Products Report")
    ap.add_argument("--min-growth", type=int, default=20, help="Min annualized growth%% to list (default 20)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    data = json.loads(Path(args.sv_json).read_text())
    rows = [r for r in (classify_row(m) for m in data) if r]
    report = render(rows, args.title, args.geo, args.min_growth)
    if args.out:
        Path(args.out).write_text(report)
        print(f"wrote {args.out}", file=__import__("sys").stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
