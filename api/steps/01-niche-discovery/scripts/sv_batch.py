#!/usr/bin/env python3
"""
sv_batch.py — one-call absolute Search Volume + the cheap triage gate
=====================================================================
DFS google_ads/search_volume: up to 1000 keywords per request, SAME price for
1 or 1000 (~$0.009/call per the docs). So the ENTIRE keyword set's absolute
volume + CPC + 12-month history comes back in ONE call.

This is the cost-optimization linchpin of the radar:
  • Trends 0-100 can't tell flat-and-huge from flat-and-dead. SV can.
  • Run SV FIRST → drop low-volume keywords before paying for any Trends graph.
  • The 12-month monthly_searches array gives a free recent-movement signal:
    a keyword whose last 1-3 months jumped is a breakout CANDIDATE → only THEN
    spend on a daily 90-day Trends pull. Breakout becomes event-driven, not
    polling — which is what takes the 500-kw bill from ~$4 to ~$1.

Outputs per keyword:
  search_volume (avg monthly), cpc, competition, competition_index,
  monthly_searches[12], recent_mom_ratio (last month vs trailing-3 median),
  and triage flags: passes_volume_floor, breakout_candidate.

Usage:
  python sv_batch.py --keywords "hiking backpack,christmas lights,yoga mat" \
      --geo US --out /tmp/sv.json --volume-floor 5000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from statistics import median

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

ENDPOINT = "https://api.dataforseo.com/v3/keywords_data/google_ads/search_volume/live"
GEO_TO_LOCATION = {"US": "United States", "GB": "United Kingdom", "CA": "Canada",
                   "AU": "Australia", "DE": "Germany", "FR": "France"}


def recent_mom_ratio(monthly: list[dict]) -> float | None:
    """Last month's volume vs median of the trailing 3 months before it.
    >1.5 ≈ recent surge → breakout candidate worth a daily Trends pull.
    monthly_searches is newest-first or oldest-first depending on DFS; we sort
    by (year, month) to be safe.
    """
    if not monthly or len(monthly) < 4:
        return None
    pts = sorted(monthly, key=lambda m: (m.get("year", 0), m.get("month", 0)))
    vols = [m.get("search_volume") or 0 for m in pts]
    last = vols[-1]
    base = median(vols[-4:-1]) if len(vols) >= 4 else median(vols[:-1])
    return round(last / base, 2) if base > 0 else None


def fetch_sv(keywords: list[str], geo: str, auth: HTTPBasicAuth,
             date_from: str | None = "2022-06-01") -> dict[str, dict]:
    """One batched call (chunks of 1000). Returns {keyword: metrics}.
    date_from pulls up to 4 years of monthly_searches (uniform monthly grid) for
    the SUSTAINED_RISE detector. Default ~4y back; None = 12-month default.
    """
    location = GEO_TO_LOCATION.get(geo, "United States")
    out: dict[str, dict] = {}
    for i in range(0, len(keywords), 1000):
        chunk = keywords[i:i + 1000]
        item = {"keywords": chunk, "location_name": location, "language_code": "en"}
        if date_from:
            item["date_from"] = date_from
        payload = [item]
        r = requests.post(ENDPOINT, json=payload, auth=auth, timeout=120)
        r.raise_for_status()
        data = r.json()
        if data.get("status_code") != 20000:
            raise RuntimeError(f"DFS {data.get('status_code')}: {data.get('status_message')}")
        for task in data.get("tasks") or []:
            for res in task.get("result") or []:
                kw = res.get("keyword")
                if not kw:
                    continue
                monthly = res.get("monthly_searches") or []
                out[kw] = {
                    "keyword": kw,
                    "search_volume": res.get("search_volume"),
                    "cpc": res.get("cpc"),
                    "competition": res.get("competition"),
                    "competition_index": res.get("competition_index"),
                    "monthly_searches": monthly,
                    "recent_mom_ratio": recent_mom_ratio(monthly),
                }
    return out


# Commercial-intent vs media/IP/event tokens. A rising MOVIE/ARTIST/EVENT has search
# volume but no product market — these tokens in the keyword or its related queries
# flag "not dropship-sellable" even if SV is high.
NON_PRODUCT_TOKENS = {
    "movie", "trailer", "episode", "season", "cast", "actor", "actress", "film",
    "song", "lyrics", "album", "concert", "tour", "tickets", "artist", "rapper",
    "vs", "score", "game", "match", "election", "stock", "price prediction",
    "net worth", "wiki", "death", "obituary", "weather", "news", "live stream",
}


def sellability(m: dict, comp_index_floor: int) -> dict:
    """Is this a dropshippable PRODUCT keyword, or a rising person/event/concept?

    Primary signal = competition_index (% of ad slots advertisers compete for).
    A real product market has many sellers bidding → high index. A movie/eclipse/
    artist has searches but ~0 advertiser competition. Validated 2026-06-15:
    pickleball paddle=100, walking pad=100 (sellable) vs solar eclipse=1,
    world cup=10, leap year=0 (not). CPC alone is NOT enough — solar eclipse had
    CPC $4.05 but index 1 (a few glasses bidders, no real market).
    Secondary guard: non-product tokens in the keyword string.
    """
    ci = m.get("competition_index") or 0
    kw = (m.get("keyword") or "").lower()
    has_non_product = any(t in kw for t in NON_PRODUCT_TOKENS)
    is_sellable = ci >= comp_index_floor and not has_non_product
    return {
        "is_sellable": is_sellable,
        "competition_index": ci,
        "sellability_reason": (
            f"non-product token in keyword" if has_non_product
            else f"competition_index {ci} < {comp_index_floor} (no product market)"
            if ci < comp_index_floor else f"product market (index {ci})"
        ),
    }


def triage(metrics: dict[str, dict], volume_floor: int, mom_threshold: float,
           comp_index_floor: int = 25) -> dict[str, dict]:
    for kw, m in metrics.items():
        sv = m.get("search_volume") or 0
        mom = m.get("recent_mom_ratio")
        sell = sellability(m, comp_index_floor)
        m.update(sell)
        m["passes_volume_floor"] = sv >= volume_floor
        m["breakout_candidate"] = bool(mom and mom >= mom_threshold)
        # SELLABILITY IS THE FIRST GATE — don't spend Trends pulls on non-products
        m["needs_trends_graph"] = m["is_sellable"] and m["passes_volume_floor"]
        m["needs_breakout_pull"] = m["needs_trends_graph"] and m["breakout_candidate"]
        # flag: SV missing but keyword may still be a real product (regulated/new)
        # → caller should fall back to Trends-weekly for SUSTAINED_RISE detection
        m["sv_missing"] = not m.get("monthly_searches")
    return metrics


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keywords", required=True, help="Comma list or path to JSON list")
    ap.add_argument("--geo", default="US")
    ap.add_argument("--out", required=True)
    ap.add_argument("--volume-floor", type=int, default=5000,
                    help="Min avg monthly SV to earn a Trends graph (default 5000)")
    ap.add_argument("--mom-threshold", type=float, default=1.5,
                    help="recent month/trailing-3 ratio to flag a breakout candidate")
    ap.add_argument("--comp-index-floor", type=int, default=25,
                    help="Min competition_index to count as a sellable product market (default 25)")
    ap.add_argument("--date-from", default="2022-06-01",
                    help="Pull monthly_searches back to this date (~4y) for SUSTAINED_RISE")
    args = ap.parse_args()

    user = os.environ.get("DATAFORSEO_USERNAME")
    pw = os.environ.get("DATAFORSEO_PASSWORD")
    if not user or not pw:
        print("ERROR: DATAFORSEO_USERNAME/PASSWORD required", file=sys.stderr)
        return 1
    auth = HTTPBasicAuth(user, pw)

    if os.path.exists(args.keywords):
        kws = json.loads(Path(args.keywords).read_text())
        kws = kws if isinstance(kws, list) else kws.get("keywords", [])
    else:
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]

    print(f"SV batch: {len(kws)} keywords / {args.geo} in "
          f"{(len(kws)+999)//1000} call(s) ~${0.009*((len(kws)+999)//1000):.3f}", file=sys.stderr)
    metrics = fetch_sv(kws, args.geo, auth, date_from=args.date_from)
    metrics = triage(metrics, args.volume_floor, args.mom_threshold, args.comp_index_floor)

    Path(args.out).write_text(json.dumps(list(metrics.values()), indent=2))
    n_graph = sum(1 for m in metrics.values() if m["needs_trends_graph"])
    n_brk = sum(1 for m in metrics.values() if m["needs_breakout_pull"])
    print(f"  → {len(metrics)} returned | {n_graph} earn a Trends graph | "
          f"{n_brk} breakout candidates → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
