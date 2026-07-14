#!/usr/bin/env python3
"""
trend_radar.py — the orchestrator (cost-optimized, cache-first)
================================================================
Classifies a keyword set into the 4 demand modes and emits ranked launch lists.

THE COST-OPTIMIZED PIPELINE (why each step is in this order):

  Stage 1  SV BATCH (1 call, ~$0.009 for up to 1000 kw)   [sv_batch.py]
           → absolute volume + CPC + 12-mo history for EVERY keyword.
           → triage: which clear the volume floor (earn a Trends graph),
             which look like recent movers (earn a fresh breakout pull).
           This is the gate that stops us paying for graphs on dead keywords.

  Stage 2  5y WEEKLY GRAPH — only for volume-floor survivors    [cache-first]
           → cache hit  = $0   (deep history is frozen; 30-day TTL)
           → cache miss = ~$0.009 lean web-graph-only pull, then cached forever
           Powers SEASONAL + EVERGREEN + RECENT_SURGE classification.

  Stage 3  FRESH DAILY PULL (past_90_days) — only for breakout candidates
           → the ONLY thing that can see a 1-7 day REALTIME_BREAKOUT.
           Event-driven (SV flagged it as moving), not polling → cheap.

  Stage 4  CLASSIFY via trends_lib + rank by absolute SV (never the 0-100 scale).

Net @ 500 kw/mo with cache+triage: ~$1-4/mo (vs ~$37 naive). See WORKFLOW.md.

This trial/v1 build wires Stages 1, 2, 4 against the CACHE (no live fetch yet —
fetch hooks are marked TODO so a dry-run classifies everything already cached
for $0). Stage 3 realtime is wired in trends_lib; the daily fetch hook is TODO.

Usage:
  # classify everything currently in cache (free):
  python trend_radar.py --from-cache --geo US

  # classify a specific keyword list from cache + report what's missing:
  python trend_radar.py --keywords "christmas lights,yoga mat" --geo US

  # ingest a raw trends.json (e.g. the trial pull or a free-UI download):
  python trend_radar.py --trends-json /tmp/trial_trends.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import trends_cache
import trends_lib

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR.parent / "cache" / "trends"


def load_sv(sv_json: str | None) -> dict[str, dict]:
    if not sv_json or not Path(sv_json).exists():
        return {}
    data = json.loads(Path(sv_json).read_text())
    return {m["keyword"]: m for m in data if m.get("keyword")}


def records_from_cache(geo: str) -> list[dict]:
    out = []
    for p in sorted(CACHE_DIR.glob(f"*__{geo.upper()}__past_5_years.json")):
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        if r.get("raw_series"):
            out.append(r)
    return out


def records_for_keywords(keywords: list[str], geo: str) -> tuple[list[dict], list[str]]:
    found, missing = [], []
    for kw in keywords:
        rec = trends_cache.get_or_none(kw, geo, "past_5_years") or trends_cache.load(kw, geo, "past_5_years")
        if rec and rec.get("raw_series"):
            found.append(rec)
        else:
            missing.append(kw)
    return found, missing


# mode → which ranked bucket it belongs in
def bucket_of(primary: str) -> str:
    if primary == "NOT_SELLABLE":
        return "not_sellable"
    if primary.startswith("REALTIME_BREAKOUT"):
        return "realtime_breakout"
    if primary.startswith("SUSTAINED_RISE"):
        return "sustained_rise"
    if primary.startswith("PEAKED"):
        return "peaked"
    if primary == "RECENT_SURGE":
        return "recent_surge"
    if primary.startswith("SEASONAL"):
        if any(x in primary for x in ("OPTIMAL", "GOOD", "OK_IN_TREND", "RAMPING", "AT_PEAK_SOON")):
            return "seasonal_approaching"
        return "seasonal_offseason"
    if primary == "EVERGREEN":
        return "evergreen"
    return "flat"


BUCKET_ORDER = [
    ("realtime_breakout", "🔴 REALTIME BREAKOUT (1-7 day spike — race it now)"),
    ("sustained_rise", "🚀 SUSTAINED RISE (multi-year compounding growth — ET-style, the durable winners)"),
    ("recent_surge", "🟠 RECENT SURGE (multi-week ramp — move fast, verify not a fad)"),
    ("seasonal_approaching", "🟡 SEASONAL — APPROACHING (pre-position before the ramp)"),
    ("evergreen", "🟢 EVERGREEN (flat + high SV — safe store backbone)"),
    ("seasonal_offseason", "⚪ SEASONAL — off-season (calendar it for next cycle)"),
    ("peaked", "📉 PEAKED (rose then cooling — too late, skip)"),
    ("flat", "·  FLAT / unclassified"),
    ("not_sellable", "🚫 NOT SELLABLE (rising person/event/concept — no product market)"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-cache", action="store_true", help="Classify everything in cache")
    src.add_argument("--keywords", help="Comma list (cache-first; reports misses)")
    src.add_argument("--trends-json", help="Ingest a raw DFS trends.json directly")
    ap.add_argument("--geo", default="US")
    ap.add_argument("--sv-json", default=None, help="sv_batch.py output for SV ranking + triage")
    ap.add_argument("--today", default=date.today().isoformat())
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    today = date.fromisoformat(args.today)
    sv = load_sv(args.sv_json)

    missing: list[str] = []
    if args.from_cache:
        recs = records_from_cache(args.geo)
    elif args.keywords:
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
        recs, missing = records_for_keywords(kws, args.geo)
    else:
        recs = json.loads(Path(args.trends_json).read_text())

    # attach SV monthly + competition_index onto each record BEFORE classifying,
    # so SUSTAINED_RISE + sellability gate have their data.
    for r in recs:
        m = sv.get(r.get("keyword"), {})
        if m:
            r.setdefault("monthly_searches", m.get("monthly_searches"))
            r.setdefault("competition_index", m.get("competition_index"))
    results = [trends_lib.classify(r, today) for r in recs]
    # attach SV for ranking
    for c in results:
        m = sv.get(c["keyword"], {})
        c["search_volume"] = m.get("search_volume")
        c["cpc"] = m.get("cpc")

    buckets: dict[str, list] = {k: [] for k, _ in BUCKET_ORDER}
    for c in results:
        if c["primary"] == "INSUFFICIENT_DATA":
            continue
        buckets[bucket_of(c["primary"])].append(c)
    # rank each bucket by absolute SV desc (None last)
    for k in buckets:
        buckets[k].sort(key=lambda c: (c.get("search_volume") or -1), reverse=True)

    print(f"\nTREND RADAR — {args.geo} — today {today} (ISO wk {trends_lib._iso_week(today.isoformat())})")
    print(f"classified {len(results)} keywords from "
          f"{'cache' if args.from_cache or args.keywords else 'trends-json'}\n")
    for key, title in BUCKET_ORDER:
        b = buckets[key]
        if not b:
            continue
        print(f"{title}  ({len(b)})")
        for c in b:
            svtxt = f"SV {c['search_volume']:>7,}" if c.get("search_volume") else "SV    ?  "
            extra = ""
            if c["primary"].startswith("SEASONAL"):
                extra = f"  peak {c['peak_month']} / ramp {c['ramp_month']}"
                if c.get("lead_reason"):
                    extra += f"  — {c['lead_reason']}"
            elif c["primary"].startswith("SUSTAINED_RISE"):
                extra = (f"  R²={c.get('sustained_r2')} {c.get('sustained_ann_growth')}%/yr "
                         f"({c.get('sustained_source')})")
            elif c["primary"].startswith("PEAKED"):
                extra = f"  was rising, now cooling (R²={c.get('sustained_r2')})"
            elif c["primary"] == "RECENT_SURGE":
                extra = f"  surge {c['surge_x']}× (peak {c.get('surge_peak_date')})"
            elif c["primary"].startswith("REALTIME"):
                extra = f"  spike {c.get('realtime_spike_x')}×"
            elif c["primary"] == "EVERGREEN":
                extra = f"  cov {c['detrended_cov']}"
            print(f"    {c['keyword']:30} {svtxt}{extra}")
        print()

    if missing:
        print(f"⚠ NOT in cache ({len(missing)}) — need a 5y pull "
              f"(~${0.009*len(missing):.3f}): {', '.join(missing)}\n")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2, default=str))
        print(f"wrote {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
