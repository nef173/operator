#!/usr/bin/env python3
"""
News Radar — the EARLIEST leading indicator in the trend-replication chain.

  Real-world event -> NEWS coverage spike -> public Google searches -> Shopping demand -> sales
        (heatwave)      (THIS SCRIPT)          (trends_dfs.py)          (08 ads)        (winner)

News leads the Google search breakout by hours-to-days on acute events. By the time a
story saturates the Shopping SERP, the pre-list window is closing. This script watches
news *volume velocity* per theme and fires a leading-signal flag while search may still
be flat — that gap is the A-grade pre-list lead window (the Decors Deluxe air-conditioner
play, one layer earlier than the "portable surged before air conditioner" sub-query lesson).

WHY GDELT (not DataForSEO here): DFS Google News gives a *snapshot* of what ranks now;
GDELT's DOC 2.0 API gives a native *article-volume time series* (every 15 min, global,
100+ languages) — exactly the acceleration curve we need, and it's FREE (fits the
project's free-first / $50-mo rule). DFS Google News stays as the optional CONFIRM layer
(is the story ranking on the Google surface our buyers see) via --dfs-confirm.

SIGNAL: for each theme x geo we pull raw articles/day, compute a trailing baseline
(median of the older window), the recent peak (best COMPLETE recent day — today is
partial and excluded from the peak), and the surge ratio. State:
  BREAKOUT  surge >= --breakout-x  AND recent >= --min-abs   (the alert)
  RISING    surge >= --rising-x
  FLAT      otherwise
We also find alert_date = the first day the curve crossed baseline x breakout-x (when an
operator watching this would have been pinged) and, with --artlist, the distinct-outlet
count on the peak day (corroboration: a 6-outlet 24h surge != a 1-outlet mention).

USAGE:
  python news_radar.py --themes "air conditioning,paddling pool" --geo GB --out news.json
  python news_radar.py --themes-file themes.json --geo GB --artlist --dfs-confirm
  # themes.json: ["air conditioning", "paddling pool"]  OR
  #   {"air conditioning": ["portable air conditioner","air cooler","evaporative cooler"],
  #    "paddling pool": ["paddling pool","kids pool","inflatable pool"]}

OUTPUT: JSON list of per-theme signal dicts (see build_signal). Sorted breakout-first,
then by surge ratio. Designed to feed the app's Trend Research surface as a third signal
source (alongside breakout flag + rising-sub-query) and the 06 candidate queue.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from statistics import median
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

# Optional .env load (only needed for --dfs-confirm). Silent if python-dotenv missing.
try:
    from dotenv import load_dotenv
    _here = Path(__file__).resolve().parent
    for _c in [_here, *_here.parents]:
        if (_c / ".env").exists():
            load_dotenv(_c / ".env")
            break
except ImportError:
    pass

GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
DFS_NEWS = "https://api.dataforseo.com/v3/serp/google/news/live/advanced"

# GDELT sourcecountry codes (FIPS-like 2-letter); maps our ISO-2 geos.
GEO_TO_GDELT = {"US": "US", "GB": "UK", "CA": "CA", "AU": "AS", "NZ": "NZ", "DE": "GM", "FR": "FR"}
# DFS news location names.
GEO_TO_LOCATION = {"US": "United States", "GB": "United Kingdom", "CA": "Canada",
                   "AU": "Australia", "NZ": "New Zealand", "DE": "Germany", "FR": "France"}


# ── Open-ended DISCOVERY ──────────────────────────────────────────────────────
# The seeded watchlist only catches themes someone wrote down. DISCOVERY catches
# ANY new story by watching the generic DEMAND TRIGGERS that precede a consumer
# buying spike — a real-world event almost always reaches the public through one of
# these lenses (a shortage, a recall, a heatwave, a power cut, a viral moment). We
# query the trigger, then pull the concrete story out of the actual headlines
# (document-frequency entity extraction), so "shortage" surfaces as "egg shortage",
# "recall" as "stroller recall", etc. — without anyone pre-listing it.
DEMAND_EVENT_PATTERNS: list[str] = [
    # scarcity / supply shock — the classic "everyone suddenly needs X"
    "shortage", "recall", "panic buying", "stockpiling",
    # weather / seasonal — acute, geo-bound demand
    "heatwave", "cold snap", "storm warning", "flooding", "wildfire", "snow storm", "drought",
    # infrastructure failure — emergency-purchase demand
    "power outage", "blackout", "water restriction", "boil water notice",
    # health / safety — protective-purchase demand
    "outbreak", "air quality warning",
]

# Common English + news-wire filler dropped from headline entity extraction so the
# concrete story word survives ("egg", "stroller, "lithium") and the noise doesn't.
_TOPIC_STOPWORDS: set[str] = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "had", "are",
    "was", "were", "will", "would", "could", "should", "can", "may", "might", "into",
    "over", "after", "amid", "says", "said", "say", "new", "now", "out", "off", "but",
    "not", "you", "your", "his", "her", "its", "their", "they", "them", "what", "when",
    "where", "why", "how", "who", "all", "more", "most", "some", "than", "then", "here",
    "there", "been", "being", "about", "against", "before", "during", "while", "amid",
    "warning", "warns", "alert", "news", "report", "reports", "update", "live", "watch",
    "video", "photos", "uk", "us", "england", "britain", "american", "british", "year",
    "years", "day", "days", "week", "weeks", "people", "home", "homes", "first", "last",
    "best", "top", "get", "got", "set", "see", "make", "made", "back", "down", "still",
    "amid", "across", "due", "thousands", "hundreds", "million", "millions", "many",
}


def extract_topics(headlines: list[dict], pattern: str, top: int = 6) -> list[str]:
    """Pull the concrete recurring story words out of a trigger's headlines.

    Document-frequency (count each word once per headline) so a topic covered by
    MANY separate articles wins over one long headline. Drops stopwords and the
    trigger's own words. The result turns a generic trigger ("shortage") into the
    real story chips ("egg", "butter", "saline")."""
    import re as _re
    from collections import Counter

    pat_tokens = {w for w in _re.findall(r"[a-z][a-z'-]+", pattern.lower())}
    df: "Counter[str]" = Counter()
    for h in headlines:
        title = (h.get("title") or "").lower()
        words = _re.findall(r"[a-z][a-z'-]{2,}", title)
        for w in {w for w in words if w not in _TOPIC_STOPWORDS and w not in pat_tokens and len(w) > 2}:
            df[w] += 1
    # A real story is one several outlets independently mention — require >=2 headlines.
    return [w for w, n in df.most_common(top * 3) if n >= 2][:top]


def load_themes(themes_arg: str | None, themes_file: str | None) -> dict[str, list[str]]:
    """Return {theme: [product_keywords]}. A bare list maps each theme to [itself]."""
    if themes_file:
        with open(themes_file) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k).strip(): [str(x).strip() for x in (v or [str(k)])]
                    for k, v in data.items() if str(k).strip()}
        if isinstance(data, list):
            return {str(t).strip(): [str(t).strip()] for t in data if str(t).strip()}
        raise ValueError(f"{themes_file}: JSON must be a list or {{theme: [keywords]}}")
    if themes_arg:
        return {t.strip(): [t.strip()] for t in themes_arg.split(",") if t.strip()}
    raise ValueError("provide --themes or --themes-file")


def _http_get(url: str, timeout: int = 40, retries: int = 4) -> str:
    """GET with backoff. GDELT's public endpoint 429s shared IPs; on persistent 429 the
    caller is told to use a residential proxy / Bright Data SERP zone."""
    last_code = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (news-radar)"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last_code = e.code
            if e.code in (429, 502, 503):
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(
        f"GDELT GET failed after {retries} tries (last HTTP {last_code}). "
        f"The public endpoint rate-limits shared IPs — run from a residential IP or route "
        f"through a proxy / Bright Data SERP zone.")


def gdelt_timeline(theme: str, gdelt_country: str, timespan: str) -> list[dict]:
    """Raw articles/day series for an EXACT-PHRASE theme in one source country."""
    query = f'"{theme}" sourcecountry:{gdelt_country}'
    url = f"{GDELT_DOC}?query={urllib.parse.quote(query)}&mode=timelinevolraw&timespan={timespan}&format=json"
    raw = _http_get(url)
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        return []  # GDELT returns HTML on a malformed query; treat as no data
    tl = j.get("timeline") or []
    if not tl:
        return []
    return [{"date": p.get("date", "")[:8], "value": int(p.get("value", 0))}
            for p in tl[0].get("data", [])]


def gdelt_outlets(theme: str, gdelt_country: str, timespan: str = "2d") -> tuple[int, list[dict]]:
    """Distinct source domains covering the theme in the recent window (corroboration),
    plus a few representative recent HEADLINES so the operator sees the ACTUAL story
    (e.g. "UK orders air-con removal") instead of just the abstract theme label."""
    query = f'"{theme}" sourcecountry:{gdelt_country}'
    url = (f"{GDELT_DOC}?query={urllib.parse.quote(query)}&mode=artlist&maxrecords=250"
           f"&timespan={timespan}&format=json&sort=datedesc")
    try:
        j = json.loads(_http_get(url))
    except (json.JSONDecodeError, RuntimeError):
        return 0, []
    articles = j.get("articles") or []
    count = len({a.get("domain", "") for a in articles if a.get("domain")})
    # Top headlines: newest first (sort=datedesc), one per outlet so we don't show the
    # same story five times, title + outlet + url so the card is clickable proof.
    headlines: list[dict] = []
    seen: set[str] = set()
    for a in articles:
        title = (a.get("title") or "").strip()
        domain = (a.get("domain") or "").strip()
        if not title or domain in seen:
            continue
        seen.add(domain)
        headlines.append({"title": title, "outlet": domain,
                          "url": a.get("url", ""), "seendate": a.get("seendate", "")})
        if len(headlines) >= 6:
            break
    return count, headlines


def build_signal(theme: str, product_keywords: list[str], geo: str, series: list[dict],
                 breakout_x: float, rising_x: float, min_abs: int,
                 recent_days: int, outlets: int | None,
                 outlets_breakout: int, outlets_rising: int,
                 headlines: list[dict] | None = None) -> dict[str, Any]:
    """Turn a raw volume series into a leading-signal dict."""
    if not series:
        return {"theme": theme, "geo": geo, "state": "NO_DATA",
                "product_keywords": product_keywords, "top_headlines": headlines or []}
    # The last point is "today" — partial, so it's excluded from the BASELINE (a partial
    # day would understate it). But breaking news (<24h old) lives entirely in this
    # bucket, so we still let today's partial COUNT toward the recent peak below —
    # otherwise a same-day breakout reads FLAT (the air-conditioning miss).
    complete = series[:-1] if len(series) > 1 else series
    today = series[-1] if len(series) > 1 else None
    values = [p["value"] for p in complete]
    # Recent peak = best of the last `recent_days` complete days, INCLUDING today's partial.
    recent_slice = complete[-recent_days:] if len(complete) >= recent_days else complete
    recent_peak = max((p["value"] for p in recent_slice), default=0)
    recent_peak_date = max(recent_slice, key=lambda p: p["value"])["date"] if recent_slice else ""
    if today is not None and today.get("value", 0) > recent_peak:
        recent_peak = today["value"]
        recent_peak_date = today["date"]
    # Baseline = median of the OLDER days (everything before the recent window). Median
    # resists the spike itself inflating the baseline.
    older = values[:-recent_days] if len(values) > recent_days else values
    baseline = max(median(older) if older else 0.0, 1.0)
    surge = round(recent_peak / baseline, 1)
    # Overall peak (any complete day) + the alert date = first day crossing baseline*breakout_x.
    peak = max(complete, key=lambda p: p["value"]) if complete else {"date": "", "value": 0}
    thresh = baseline * breakout_x
    alert_date = next((p["date"] for p in complete if p["value"] >= thresh and p["value"] >= min_abs), None)

    # Primary read = daily-volume surge.
    basis = "volume"
    if surge >= breakout_x and recent_peak >= min_abs:
        state = "BREAKOUT"
    elif surge >= rising_x:
        state = "RISING"
    else:
        state = "FLAT"
    # Corroboration read = how MANY distinct outlets are covering it right now. Broad
    # multi-outlet coverage is the loudest "real story today" signal and catches
    # same-day breakouts whose spike hasn't yet landed in the daily volume timeline.
    # (e.g. "air conditioning" with 84 outlets but a lagged volume series.)
    if outlets is not None:
        if outlets >= outlets_breakout and state != "BREAKOUT":
            state, basis = "BREAKOUT", "coverage"
        elif outlets >= outlets_rising and state == "FLAT":
            state, basis = "RISING", "coverage"

    return {
        "theme": theme,
        "geo": geo,
        "product_keywords": product_keywords,
        "state": state,
        "basis": basis,                      # "volume" (daily surge) or "coverage" (outlet breadth)
        "baseline_per_day": round(baseline, 1),
        "recent_peak": recent_peak,
        "recent_peak_date": recent_peak_date,
        "surge_ratio": surge,
        "peak_value": peak["value"],
        "peak_date": peak["date"],
        "alert_date": alert_date,            # when an operator watching this would've been pinged
        "distinct_outlets": outlets,         # None unless --artlist
        "top_headlines": headlines or [],    # the ACTUAL recent stories (one per outlet)
        "today_partial": series[-1] if len(series) > 1 else None,
        "timeline": series,
    }


def dfs_news_confirm(keyword: str, geo: str) -> dict[str, Any]:
    """Optional CONFIRM layer: does Google News surface this story now? (DFS, canonical)."""
    user = os.environ.get("DATAFORSEO_USERNAME")
    pw = os.environ.get("DATAFORSEO_PASSWORD")
    if not (user and pw):
        return {"error": "DATAFORSEO_USERNAME/PASSWORD not set"}
    import base64
    payload = [{"keyword": keyword, "location_name": GEO_TO_LOCATION.get(geo, "United States"),
                "language_code": "en", "depth": 20}]
    body = json.dumps(payload).encode()
    req = urllib.request.Request(DFS_NEWS, data=body, method="POST", headers={
        "Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(),
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            j = json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}
    items = (((j.get("tasks") or [{}])[0].get("result") or [{}])[0].get("items") or [])
    news = [it for it in items if it.get("type") == "news_search"]
    return {"google_news_items": len(news),
            "top_titles": [it.get("title") for it in news[:5] if it.get("title")]}


def main() -> None:
    ap = argparse.ArgumentParser(description="News Radar — GDELT news-velocity leading-signal sensor")
    ap.add_argument("--themes", help="comma-separated themes (exact news phrases)")
    ap.add_argument("--themes-file", help="JSON list of themes OR {theme: [product_keywords]}")
    ap.add_argument("--geo", default="GB", help="ISO-2 geo (US, GB, ...). Default GB")
    ap.add_argument("--timespan", default="28d", help="GDELT lookback window (default 28d)")
    ap.add_argument("--recent-days", type=int, default=3, help="recent window for peak/surge (default 3)")
    ap.add_argument("--breakout-x", type=float, default=5.0, help="surge multiple for BREAKOUT (default 5x)")
    ap.add_argument("--rising-x", type=float, default=2.0, help="surge multiple for RISING (default 2x)")
    ap.add_argument("--min-abs", type=int, default=25, help="min recent articles/day to avoid tiny-number noise (default 25)")
    ap.add_argument("--outlets-breakout", type=int, default=40, help="distinct outlets in the recent window for a coverage-driven BREAKOUT (default 40)")
    ap.add_argument("--outlets-rising", type=int, default=18, help="distinct outlets for a coverage-driven RISING (default 18)")
    ap.add_argument("--discover", action="store_true",
                    help="open-ended mode: scan generic demand triggers (shortage, recall, "
                         "heatwave, power outage, viral…) and pull the concrete story out of the "
                         "headlines — catches NEW stories not on any watchlist. Forces --artlist.")
    ap.add_argument("--discover-min-state", default="RISING", choices=["BREAKOUT", "RISING", "FLAT"],
                    help="in --discover mode, only emit candidates at/above this state (default RISING) "
                         "so always-on trigger words don't spam the panel")
    ap.add_argument("--artlist", action="store_true", help="also fetch distinct-outlet count (extra GDELT call/theme)")
    ap.add_argument("--dfs-confirm", action="store_true", help="add DFS Google-News confirm for breakout themes")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", help="write JSON here (default: stdout)")
    args = ap.parse_args()

    # Discovery mode replaces the watchlist with the generic demand triggers and forces
    # artlist (we need the headlines to extract the concrete story).
    discover = bool(args.discover)
    if discover:
        themes = {p: [] for p in DEMAND_EVENT_PATTERNS}
        args.artlist = True
    else:
        themes = load_themes(args.themes, args.themes_file)
    gcountry = GEO_TO_GDELT.get(args.geo.upper(), "US")
    _DISCOVER_RANK = {"BREAKOUT": 0, "RISING": 1, "FLAT": 2, "NO_DATA": 3}
    _min_rank = _DISCOVER_RANK.get(args.discover_min_state, 1)

    def work(theme: str, kws: list[str]) -> dict | None:
        series = gdelt_timeline(theme, gcountry, args.timespan)
        outlets, headlines = gdelt_outlets(theme, gcountry) if args.artlist else (None, [])
        sig = build_signal(theme, kws, args.geo.upper(), series, args.breakout_x,
                           args.rising_x, args.min_abs, args.recent_days, outlets,
                           args.outlets_breakout, args.outlets_rising, headlines)
        if discover:
            # In discovery, the trigger word is just a lens — the real value is the
            # concrete story pulled from the headlines. Drop quiet triggers so the panel
            # only shows triggers that are actually active right now.
            if _DISCOVER_RANK.get(sig.get("state"), 9) > _min_rank:
                return None
            sig["discovered"] = True
            sig["candidate_topics"] = extract_topics(headlines or [], theme)
        if args.dfs_confirm and kws and sig.get("state") == "BREAKOUT":
            sig["dfs_news_confirm"] = dfs_news_confirm(kws[0], args.geo.upper())
        return sig

    signals: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(work, t, kws): t for t, kws in themes.items()}
        for fut in as_completed(futs):
            try:
                sig = fut.result()
                if sig is not None:
                    signals.append(sig)
            except Exception as e:
                print(f"  ! {futs[fut]}: {e}", file=sys.stderr)

    # Rank within a state by the stronger of the two signals: daily-volume surge, or
    # outlet breadth normalised to the rising threshold (so a coverage-driven breakout
    # with low surge but 80+ outlets still sorts above a thin one).
    order = {"BREAKOUT": 0, "RISING": 1, "FLAT": 2, "NO_DATA": 3}

    def magnitude(s: dict) -> float:
        surge = float(s.get("surge_ratio", 0) or 0)
        outlets = float(s.get("distinct_outlets", 0) or 0)
        return max(surge, outlets / max(args.outlets_rising, 1))

    signals.sort(key=lambda s: (order.get(s.get("state"), 9), -magnitude(s)))

    out = json.dumps(signals, indent=2)
    if args.out:
        Path(args.out).write_text(out)
        breakouts = sum(1 for s in signals if s.get("state") == "BREAKOUT")
        rising = sum(1 for s in signals if s.get("state") == "RISING")
        print(f"Wrote {len(signals)} signals -> {args.out}  ({breakouts} BREAKOUT, {rising} RISING)")
    else:
        print(out)


if __name__ == "__main__":
    main()
