#!/usr/bin/env python3
"""
trends_cache.py — two-zone local cache for trend series (the cost killer)
=========================================================================
The 5-year weekly series has two zones with opposite cache rules:

  • DEEP HISTORY (everything older than ~8 weeks) — frozen forever. Cache it
    permanently; never re-pay.
  • RECENT EDGE (last ~8 weeks) — Google revises it + breakouts live here.
    A 5y pull's edge is allowed to go stale (seasonal/evergreen read the deep
    history, so a 30-day TTL is fine). The BREAKOUT detector instead reads a
    separate fresh past_90_days slice with a short TTL.

So "check a keyword we checked before" = $0 for the seasonal/evergreen path,
and only the cheap recent slice is ever re-paid for breakout.

Cache layout (flat, greppable, git-diffable — NOT one blob):
  01-niche-discovery/cache/trends/<slug>__<geo>__<range>.json

Each file = a single DFS-schema record ({keyword, geo, raw_series, raw_dates,
fetched_at, range, source}). Detectors read these unchanged whether they came
from the API or a manual free-UI download.

Commands:
  # seed the cache from all existing dossier trends.json files (free — already paid):
  python trends_cache.py import-existing

  # list what's cached:
  python trends_cache.py list

  # check freshness of one keyword:
  python trends_cache.py status --keyword "hiking backpack" --geo US
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
CACHE_DIR = SCRIPT_DIR.parent / "cache" / "trends"

# staleness windows (days) per range type
TTL_DAYS = {
    "past_5_years": 30,     # deep history barely moves; seasonal/evergreen tolerant
    "past_90_days": 1,      # breakout edge — refresh ~daily
    "past_7_days": 0,       # always fresh
}


def slugify(kw: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", kw.lower()).strip("-")
    return s[:60] or "kw"


def cache_path(keyword: str, geo: str, range_type: str = "past_5_years") -> Path:
    return CACHE_DIR / f"{slugify(keyword)}__{geo.upper()}__{range_type}.json"


def load(keyword: str, geo: str, range_type: str = "past_5_years") -> dict | None:
    p = cache_path(keyword, geo, range_type)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def is_fresh(rec: dict, range_type: str = "past_5_years") -> bool:
    ttl = TTL_DAYS.get(range_type, 30)
    fetched = rec.get("fetched_at")
    if not fetched:
        return False
    try:
        age = (date.today() - date.fromisoformat(fetched[:10])).days
    except Exception:
        return False
    return age <= ttl


def save(keyword: str, geo: str, raw_series: list, raw_dates: list,
         range_type: str = "past_5_years", source: str = "api") -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = cache_path(keyword, geo, range_type)
    rec = {
        "keyword": keyword, "geo": geo.upper(), "range": range_type,
        "raw_series": raw_series, "raw_dates": raw_dates,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "data_points": len(raw_series),
    }
    p.write_text(json.dumps(rec, indent=2))
    return p


def get_or_none(keyword: str, geo: str, range_type: str = "past_5_years"):
    """Return cached record if present AND fresh; else None (caller fetches)."""
    rec = load(keyword, geo, range_type)
    if rec and is_fresh(rec, range_type) and (rec.get("raw_series")):
        return rec
    return None


# ── seeding from existing dossiers (free — already paid for) ───────────────────
def import_existing() -> None:
    patterns = [
        str(PROJECT_ROOT / "dossiers" / "**" / "trends*.json"),
        str(PROJECT_ROOT / "dossiers-pain-first" / "**" / "trends*.json"),
    ]
    files = sorted({f for pat in patterns for f in glob.glob(pat, recursive=True)})
    imported = skipped = 0
    for f in files:
        try:
            recs = json.loads(Path(f).read_text())
        except Exception:
            continue
        if not isinstance(recs, list):
            continue
        for r in recs:
            kw = r.get("keyword"); geo = r.get("geo") or "US"
            series = r.get("raw_series") or []
            dates = r.get("raw_dates") or []
            if not kw or len(series) < 100:
                skipped += 1
                continue
            # don't clobber a fresher existing cache entry
            existing = load(kw, geo, "past_5_years")
            if existing and len(existing.get("raw_series") or []) >= len(series):
                skipped += 1
                continue
            save(kw, geo, series, dates, "past_5_years", source=f"dossier:{Path(f).parent.name}")
            imported += 1
    print(f"imported {imported} keyword records into {CACHE_DIR}", file=sys.stderr)
    print(f"skipped {skipped} (empty/duplicate/already-fresher)", file=sys.stderr)


def cmd_list() -> None:
    if not CACHE_DIR.exists():
        print("cache empty (run: import-existing)"); return
    files = sorted(CACHE_DIR.glob("*.json"))
    print(f"{len(files)} cached records in {CACHE_DIR}\n")
    for p in files:
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        fresh = "fresh" if is_fresh(r, r.get("range", "past_5_years")) else "STALE"
        print(f"  {r.get('keyword'):28} {r.get('geo'):3} {r.get('range'):14} "
              f"pts={r.get('data_points'):>3} {r.get('fetched_at','?')[:10]} [{fresh}]")


def cmd_status(kw: str, geo: str) -> None:
    for rt in ("past_5_years", "past_90_days", "past_7_days"):
        r = load(kw, geo, rt)
        if not r:
            print(f"  {rt:14} MISS"); continue
        fresh = "FRESH" if is_fresh(r, rt) else "stale"
        print(f"  {rt:14} {fresh}  pts={r.get('data_points')} fetched={r.get('fetched_at','?')[:10]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import-existing")
    sub.add_parser("list")
    st = sub.add_parser("status")
    st.add_argument("--keyword", required=True)
    st.add_argument("--geo", default="US")
    args = ap.parse_args()

    if args.cmd == "import-existing":
        import_existing()
    elif args.cmd == "list":
        cmd_list()
    elif args.cmd == "status":
        cmd_status(args.keyword, args.geo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
