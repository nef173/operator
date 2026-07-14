"""News Radar snapshot — the EARLIEST leading signal in the trend-replication chain.

  real-world event -> NEWS coverage spike -> public Google searches -> Shopping demand -> sales
        (heatwave)        (THIS LAYER)          (trends.json)            (08 ads)        (winner)

News leads the Google-search breakout by hours-to-days on acute events (the Decors Deluxe
air-conditioner play — one layer earlier than the "portable surged before air conditioner"
rising-sub-query lesson). This module is the app's thin WRAPPER around the canonical sensor
script `01-niche-discovery/scripts/news_radar.py` — it does NOT reimplement the GDELT
velocity scoring; it runs the script over a theme watchlist and persists the result as a
snapshot, exactly like optimization.json / trends.json.

Design (mirrors the rest of the app):
  * SNAPSHOT model, not live-on-pageload. `sync()` runs news_radar.py and writes
    news-radar/news.json: {synced_at, geo, timespan, params, signals[]}. The page reads the
    snapshot fast (readers.news_signals) and shows "last synced N ago".
  * WRAP, don't reimplement — news_radar.py is the single source of truth for the signal,
    run with the api's own venv Python (it is stdlib-only; GDELT needs no credentials).
  * A theme WATCHLIST (news-radar/themes.json) maps a news theme -> the product keywords it
    drives (heatwave -> portable air conditioner / air cooler / cooling mat). Seeded with the
    AC case + a handful of genuine event->demand maps the operator can grow.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from . import config

_SCRIPT = config.repo_root() / "01-niche-discovery" / "scripts" / "news_radar.py"
_SYNC_TIMEOUT = 240  # GDELT can be slow across many themes; generous ceiling

# Seed watchlist: news theme -> the product keywords it drives. Each is a genuine
# real-world-event -> existing-demand map (the news-velocity sensor watches the theme;
# a BREAKOUT seeds product research on these keywords). The operator grows this over time.
DEFAULT_THEMES: dict[str, list[str]] = {
    "air conditioning": ["portable air conditioner", "air cooler", "evaporative cooler"],
    "heatwave": ["portable fan", "neck fan", "cooling mat"],
    "paddling pool": ["paddling pool", "inflatable pool", "kids pool"],
    "hosepipe ban": ["water butt", "watering can", "drip irrigation kit"],
    "storm": ["portable generator", "power bank", "rechargeable torch"],
    "flooding": ["sandbags", "wet dry vacuum", "dehumidifier"],
    "cold snap": ["electric heater", "heated blanket", "draught excluder"],
}


def snapshot_path() -> "os.PathLike[str]":
    return config.news_dir() / "news.json"


def themes_path() -> "os.PathLike[str]":
    return config.news_dir() / "themes.json"


def ensure_themes_file() -> "os.PathLike[str]":
    """Return the watchlist path, seeding it with DEFAULT_THEMES on first use."""
    path = themes_path()
    if not os.path.exists(path):
        os.makedirs(config.news_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_THEMES, fh, indent=2)
    return path


# The radar scans every market every sync (no per-run country picker) — each signal
# is tagged with its own geo, so one card can be GB and the next US. GDELT's
# sourcecountry filter is what makes a UK-only story (e.g. the air-con council ban)
# surface under GB without diluting it across the world.
MARKETS = ["US", "GB", "AU", "CA"]
_STATE_ORDER = {"BREAKOUT": 0, "RISING": 1, "FLAT": 2, "NO_DATA": 3}


def _run_radar(cmd: list[str], geo: str) -> tuple[list | None, str | None]:
    """Run news_radar.py and parse its JSON list. Returns (signals, None) or (None, error)."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(config.repo_root()), capture_output=True, text=True, timeout=_SYNC_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, f"{geo}: timed out after {_SYNC_TIMEOUT}s"
    if proc.returncode != 0:
        return None, f"{geo}: " + ((proc.stderr or proc.stdout or "").strip() or "news_radar.py failed")[:400]
    try:
        signals = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        return None, f"{geo}: could not parse output: {e}"
    if not isinstance(signals, list):
        return None, f"{geo}: did not return a signal list"
    return signals, None


def _sync_one(geo: str, timespan: str, themes_file: "os.PathLike[str]") -> tuple[list | None, str | None]:
    """Run the SEEDED watchlist for ONE market. Returns (signals, None) or (None, error)."""
    return _run_radar([
        sys.executable, str(_SCRIPT),
        "--themes-file", str(themes_file),
        "--geo", geo,
        "--timespan", timespan,
        "--artlist",  # distinct-outlet corroboration (also drives the coverage-breakout path)
    ], geo)


def _discover_one(geo: str, timespan: str) -> tuple[list | None, str | None]:
    """Open-ended DISCOVERY for ONE market — scans the generic demand triggers and pulls the
    concrete story out of the headlines, so a NEW event (a wildfire, a recall, a shortage)
    surfaces without anyone pre-listing its theme. Low workers: GDELT 429-rate-limits shared
    IPs and discovery fires ~2x the calls of the seeded pass."""
    return _run_radar([
        sys.executable, str(_SCRIPT),
        "--discover",
        "--geo", geo,
        "--timespan", timespan,
        "--discover-min-state", "RISING",
        "--workers", "2",
    ], geo)


def _enrich_discovered_keywords(discovered: list) -> None:
    """Map each discovered story's headlines → the consumer products it drives, IN PLACE.

    Discovery surfaces the STORY (a wildfire, an egg shortage); turning that into buyable
    product keywords needs judgement. When the Assistant LLM gateway is configured we ask it
    to do that mapping and to DROP non-commercial stories (politics, crime, sport). When it
    is NOT configured this is a no-op — the card still shows the story + candidate-topic chips
    and the operator one-clicks to research. Best-effort: any failure leaves the list as-is."""
    try:
        from . import assistant
    except Exception:
        return
    if not getattr(assistant, "_llm_configured", lambda: False)():
        return
    # Build a compact prompt: theme + a few headlines per discovered story.
    items = []
    for i, s in enumerate(discovered):
        heads = [h.get("title", "") for h in (s.get("top_headlines") or [])[:3] if h.get("title")]
        if heads:
            items.append({"i": i, "trigger": s.get("theme", ""), "headlines": heads})
    if not items:
        return
    system = (
        "You map breaking-news stories to the consumer products their demand drives, for a "
        "Google-Shopping dropshipping operator. For each item, return up to 3 specific, "
        "buyable product search keywords a shopper would type (e.g. wildfire smoke -> "
        "'air purifier','n95 mask','hepa filter'). If the story is NOT commercial (politics, "
        "crime, sport, general news), return an EMPTY list for it. Reply ONLY with JSON: "
        '{"map":[{"i":<index>,"products":["kw",...]}]}'
    )
    msg = [{"role": "user", "content": json.dumps({"items": items})}]
    try:
        reply = assistant._call_llm(system, msg)
        start, end = reply.find("{"), reply.rfind("}")
        data = json.loads(reply[start:end + 1]) if start >= 0 and end > start else {}
    except Exception:
        return
    for row in (data.get("map") or []):
        try:
            idx = int(row.get("i"))
            prods = [str(p).strip() for p in (row.get("products") or []) if str(p).strip()][:3]
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(discovered) and prods:
            discovered[idx]["product_keywords"] = prods


def sync(geo: str = "ALL", timespan: str = "28d", discover: bool = True) -> dict:
    """Scan EVERY market (concurrently) over the theme watchlist and persist one merged snapshot.

    `geo` defaults to ALL (scan every market); pass a single ISO-2 code to scan just one.
    `discover` (default True) ALSO runs the open-ended demand-trigger discovery so NEW stories
    surface, not just the seeded watchlist.
    Best-effort & partial: a per-market GDELT failure is collected as a warning but the
    other markets still land, so one rate-limited country never blanks the whole panel."""
    from concurrent.futures import ThreadPoolExecutor

    themes_file = ensure_themes_file()
    geos = MARKETS if geo.upper() in ("ALL", "WORLD", "") else [geo.upper()]

    all_signals: list = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=len(geos)) as ex:
        results = list(ex.map(lambda g: (g, _sync_one(g, timespan, themes_file)), geos))
    for _g, (sigs, err) in results:
        if err:
            errors.append(err)
        elif sigs:
            all_signals.extend(sigs)

    # Open-ended discovery pass — catches stories no one pre-listed. Run sequentially per
    # market (GDELT 429s shared IPs under load) and de-dupe against the seeded themes so a
    # discovered "heatwave" doesn't double the seeded one. Best-effort: discovery failure
    # never blanks the seeded snapshot.
    if discover:
        seeded_themes = {str(s.get("theme", "")).lower() for s in all_signals}
        discovered: list = []
        # Concurrent across markets (one process per market, each capped at 2 GDELT workers)
        # — bounds wall-time to ~one market's run instead of summing all four, while keeping
        # total concurrency low enough to survive GDELT's shared-IP throttle.
        with ThreadPoolExecutor(max_workers=len(geos)) as ex:
            dres = list(ex.map(lambda g: (g, _discover_one(g, timespan)), geos))
        for _g, (sigs, err) in dres:
            if err:
                errors.append(f"discover {err}")
            elif sigs:
                discovered.extend(sigs)
        # Drop discovered candidates whose trigger word collides with a seeded theme.
        discovered = [s for s in discovered
                      if str(s.get("theme", "")).lower() not in seeded_themes]
        if discovered:
            _enrich_discovered_keywords(discovered)
        all_signals.extend(discovered)

    if not all_signals and errors:
        return {"ok": False, "error": "; ".join(errors)}

    # Merge: leading states first, then by surge magnitude — so the loudest story across
    # ALL countries sits at the top regardless of which market it came from.
    all_signals.sort(
        key=lambda s: (_STATE_ORDER.get(s.get("state"), 9), -float(s.get("surge_ratio", 0) or 0))
    )

    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap = {
        "synced_at": now,
        "geo": "ALL" if len(geos) > 1 else geos[0],
        "markets": geos,
        "timespan": timespan,
        "params": {"source": "gdelt", "themes": len(all_signals), "markets": len(geos)},
        "signals": all_signals,
    }
    if errors:
        snap["warnings"] = errors
    path = snapshot_path()
    try:
        os.makedirs(config.news_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=2)
    except OSError as e:
        return {"ok": False, "error": f"built snapshot but could not write {path}: {e}"}
    breakout = sum(1 for s in all_signals if isinstance(s, dict) and s.get("state") == "BREAKOUT")
    return {"ok": True, "synced_at": now, "geo": snap["geo"], "signals": len(all_signals),
            "breakout": breakout, "warnings": errors or None}
