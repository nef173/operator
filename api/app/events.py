"""World-events calendar — the PREDICTABLE, dated demand signal in the trend chain.

  real-world event -> NEWS coverage spike -> public Google searches -> Shopping demand -> sales
  (heatwave = news.py, UNPREDICTABLE)                 |
  (Easter / July 4th / World Cup = THIS MODULE, PREDICTABLE + DATED)

News Radar (news.py / GDELT) catches UNPREDICTABLE breaking events — you react to them. This
module is the opposite class: a curated, per-country calendar of PREDICTABLE, recurring or
scheduled events (holidays, seasonal moments, world events). It is the ONLY trend signal you
can plan a build-ahead batch around with certainty, so it is the purest feed for the listing
plan's "build ahead (1–3 mo)" bucket: an event + a lead-time is exactly a build-ahead keyword.

Design (mirrors news.py's watchlist pattern, but COMPUTED — no external fetch):
  * A curated seed calendar (DEFAULT_EVENTS) maps each event -> the product keywords it drives
    (Ostern -> osterdeko / easter basket; July 4th -> american flag / bbq). Seeded to
    events-calendar/events.json on first use; the operator grows/edits it, exactly like the
    news themes.json watchlist.
  * NO snapshot / NO sync — the "signal" is pure date math over the static list, so every read
    computes the next occurrence + days-until + horizon live (cheap, credential-free, stdlib).
  * Each event resolves its NEXT occurrence (>= today): fixed month/day, Western Easter via
    computus (+ offset for Good Friday / Easter Monday / Carnival), or an explicit one-off date
    list (World Cup, Olympics). Then a horizon is derived from the lead-time so it slots into
    the same now / build-ahead / later language the listing plan already speaks.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from datetime import date

from . import config

# Horizon thresholds (relative to lead time). An event you should SELL for right now is one
# whose run-up (lead_weeks before the date) has already started; you BUILD AHEAD for events
# still 1–3 months beyond that run-up; everything further out is on the radar but not yet.
_BUILD_AHEAD_EXTRA_DAYS = 75  # how far before the run-up starts we still call it "build ahead"


# Curated seed calendar. Each event maps a real, dated moment -> the buyable product keywords
# its demand drives, tagged with the country whose shoppers search it and a lead time (weeks
# before the date you should already be listed). The operator grows this over time.
#   type "fixed"  -> annual, same month/day every year (compute next occurrence)
#   type "easter" -> Western Easter (computus) + `offset` days (0 = Sunday, -2 = Good Friday…)
#   type "dates"  -> explicit one-off / multi-year dates ["YYYY-MM-DD", …] (World Cup, Olympics)
#
# KEYWORDS = real, high-search-volume PRODUCT-CATEGORY head terms shoppers actually type (the
# kind you build a listing title around), NOT soft descriptive phrases. e.g. Oktoberfest is
# "dirndl / lederhosen / trachten", not "beer party supplies". These seed the Find-Products
# hand-off; the live search volume + ranking still comes from the keyword pipeline (DataForSEO).
DEFAULT_EVENTS: list[dict] = [
    # ── Global / cross-market commerce tentpoles ─────────────────────────────
    {"name": "Christmas", "country": "GLOBAL", "type": "fixed", "month": 12, "day": 25,
     "lead_weeks": 8, "category": "Holiday / Gifting",
     "keywords": ["christmas tree", "christmas lights", "artificial christmas tree", "advent calendar", "christmas ornaments", "christmas stockings"]},
    {"name": "Valentine's Day", "country": "GLOBAL", "type": "fixed", "month": 2, "day": 14,
     "lead_weeks": 5, "category": "Romance / Gifting",
     "keywords": ["rose bear", "valentines gifts for her", "couples gifts", "heart necklace", "personalized gifts"]},
    {"name": "Halloween", "country": "GLOBAL", "type": "fixed", "month": 10, "day": 31,
     "lead_weeks": 6, "category": "Halloween / Costume",
     "keywords": ["halloween costume", "halloween decorations", "inflatable halloween decorations", "animatronic halloween", "pumpkin decorations"]},
    {"name": "New Year's Eve", "country": "GLOBAL", "type": "fixed", "month": 12, "day": 31,
     "lead_weeks": 4, "category": "Party",
     "keywords": ["party decorations", "led party lights", "disco ball", "champagne glasses", "photo booth props"]},
    {"name": "Black Friday", "country": "GLOBAL", "type": "dates",
     "dates": ["2025-11-28", "2026-11-27", "2027-11-26"],
     "lead_weeks": 6, "category": "Sale event",
     "keywords": ["smart home devices", "kitchen gadgets", "wireless earbuds", "massage gun", "home gadgets"]},
    {"name": "Cyber Monday", "country": "GLOBAL", "type": "dates",
     "dates": ["2025-12-01", "2026-11-30", "2027-11-29"],
     "lead_weeks": 6, "category": "Sale event",
     "keywords": ["wireless earbuds", "smart watch", "phone accessories", "smart home devices", "bluetooth speaker"]},
    {"name": "Singles' Day (11.11)", "country": "GLOBAL", "type": "fixed", "month": 11, "day": 11,
     "lead_weeks": 5, "category": "Sale event",
     "keywords": ["wireless earbuds", "smart watch", "home gadgets", "phone accessories", "beauty tools"]},
    {"name": "Diwali", "country": "GLOBAL", "type": "dates",
     "dates": ["2026-11-08", "2027-10-29"],
     "lead_weeks": 5, "category": "Festival / Lights",
     "keywords": ["diwali decorations", "fairy lights", "diya candles", "led string lights", "rangoli"]},
    {"name": "Hanukkah", "country": "GLOBAL", "type": "dates",
     "dates": ["2026-12-05", "2027-12-25"],
     "lead_weeks": 4, "category": "Holiday / Gifting",
     "keywords": ["menorah", "hanukkah decorations", "dreidel", "hanukkah gifts", "led menorah"]},

    # ── United States ────────────────────────────────────────────────────────
    {"name": "Independence Day (July 4th)", "country": "US", "type": "fixed", "month": 7, "day": 4,
     "lead_weeks": 4, "category": "Patriotic / BBQ",
     "keywords": ["american flag", "patriotic decorations", "4th of july outfit", "bbq grill accessories", "outdoor string lights"]},
    {"name": "Super Bowl", "country": "US", "type": "dates",
     "dates": ["2026-02-08", "2027-02-14"],
     "lead_weeks": 4, "category": "Sports / Party",
     "keywords": ["football party supplies", "beer dispenser", "tv wall mount", "team fan gear", "snack serving tray"]},
    {"name": "Thanksgiving", "country": "US", "type": "dates",
     "dates": ["2025-11-27", "2026-11-26", "2027-11-25"],
     "lead_weeks": 5, "category": "Holiday / Kitchen",
     "keywords": ["thanksgiving decorations", "turkey carving set", "fall table decor", "tablecloth", "dinnerware set"]},
    {"name": "Back to School (US)", "country": "US", "type": "fixed", "month": 8, "day": 15,
     "lead_weeks": 6, "category": "Back to school",
     "keywords": ["backpack", "school supplies", "lunch box", "water bottle", "pencil case", "desk organizer"]},
    {"name": "Labor Day (US)", "country": "US", "type": "dates",
     "dates": ["2026-09-07", "2027-09-06"],
     "lead_weeks": 4, "category": "Sale / Outdoor",
     "keywords": ["patio furniture", "grill", "cooler", "outdoor rug", "air conditioner", "mattress topper"]},
    {"name": "Veterans Day (US)", "country": "US", "type": "fixed", "month": 11, "day": 11,
     "lead_weeks": 3, "category": "Patriotic / Sale",
     "keywords": ["american flag", "patriotic decorations", "flag pole", "outdoor flag", "memorial gifts"]},

    # ── United Kingdom ─────────────────────────────────────────────────────────
    {"name": "Bonfire Night (Guy Fawkes)", "country": "GB", "type": "fixed", "month": 11, "day": 5,
     "lead_weeks": 4, "category": "Seasonal / Outdoor",
     "keywords": ["sparklers", "fire pit", "outdoor heater", "led gloves", "glow sticks"]},
    {"name": "Mother's Day (UK)", "country": "GB", "type": "dates",
     "dates": ["2026-03-15", "2027-03-14"],
     "lead_weeks": 5, "category": "Gifting",
     "keywords": ["mothers day gifts", "personalised necklace", "spa gift set", "jewellery gifts", "flower bouquet"]},
    {"name": "Boxing Day", "country": "GB", "type": "fixed", "month": 12, "day": 26,
     "lead_weeks": 5, "category": "Sale event",
     "keywords": ["fitness equipment", "kitchen gadgets", "smart home devices", "home gadgets"]},

    # ── Germany ────────────────────────────────────────────────────────────────
    {"name": "Ostern (Easter)", "country": "DE", "type": "easter", "offset": 0,
     "lead_weeks": 5, "category": "Spring / Easter",
     "keywords": ["osterdeko", "osterhase", "osternest", "ostergeschenke", "osterdekoration fenster"]},
    {"name": "Karfreitag (Good Friday)", "country": "DE", "type": "easter", "offset": -2,
     "lead_weeks": 5, "category": "Spring / Easter",
     "keywords": ["osterdeko", "frühlingsdeko", "ostergeschenke", "tischdeko ostern"]},
    {"name": "Oktoberfest", "country": "DE", "type": "dates",
     "dates": ["2026-09-19", "2027-09-18"],
     "lead_weeks": 6, "category": "Festival / Costume",
     "keywords": ["dirndl", "lederhosen", "trachten", "dirndl dress", "oktoberfest costume", "bierkrug maßkrug"]},
    {"name": "1. Advent", "country": "DE", "type": "dates",
     "dates": ["2026-11-29", "2027-11-28"],
     "lead_weeks": 5, "category": "Advent / Deko",
     "keywords": ["adventskranz", "adventskalender", "weihnachtsdeko", "led lichterkette", "weihnachtskerzen"]},

    # ── France ─────────────────────────────────────────────────────────────────
    {"name": "Bastille Day", "country": "FR", "type": "fixed", "month": 7, "day": 14,
     "lead_weeks": 4, "category": "Patriotic / Party",
     "keywords": ["drapeau français", "guirlande lumineuse", "décoration de fête", "accessoires barbecue"]},
    {"name": "La Rentrée (Back to School)", "country": "FR", "type": "fixed", "month": 9, "day": 1,
     "lead_weeks": 6, "category": "Back to school",
     "keywords": ["cartable", "fournitures scolaires", "trousse", "sac à dos", "agenda scolaire"]},

    # ── Australia (Southern Hemisphere — the four GLOBAL seasons are INVERTED here, so AU demand
    #    is carried by explicit dated events; Dec = summer/outdoor Christmas, not cold-weather) ──
    {"name": "Australia Day", "country": "AU", "type": "fixed", "month": 1, "day": 26,
     "lead_weeks": 4, "category": "Patriotic / BBQ",
     "keywords": ["australian flag", "bbq accessories", "esky cooler", "beach gear", "outdoor thongs"]},
    {"name": "Father's Day (AU/NZ)", "country": "AU", "type": "dates",
     "dates": ["2026-09-06", "2027-09-05"],
     "lead_weeks": 4, "category": "Gifting",
     "keywords": ["fathers day gifts", "bbq tools", "beer gifts", "tool kit", "grooming kit"]},
    {"name": "AFL Grand Final", "country": "AU", "type": "dates",
     "dates": ["2026-09-26", "2027-09-25"],
     "lead_weeks": 4, "category": "Sports / Fan gear",
     "keywords": ["afl scarf", "team merchandise", "party decorations", "footy socks", "beanie"]},
    {"name": "Melbourne Cup", "country": "AU", "type": "dates",
     "dates": ["2026-11-03", "2027-11-02"],
     "lead_weeks": 4, "category": "Race day / Party",
     "keywords": ["fascinator", "party decorations", "sweepstake kit", "champagne glasses", "picnic set"]},
    {"name": "Aussie Summer Christmas", "country": "AU", "type": "dates",
     "dates": ["2026-12-25", "2027-12-25"],
     "lead_weeks": 7, "category": "Summer / Outdoor Christmas",
     "keywords": ["outdoor christmas lights", "inflatable pool", "beach christmas decor", "esky cooler", "bbq"]},

    # ── Switzerland ────────────────────────────────────────────────────────────
    {"name": "Swiss National Day (Bundesfeier)", "country": "CH", "type": "fixed", "month": 8, "day": 1,
     "lead_weeks": 4, "category": "Patriotic / Party",
     "keywords": ["schweizer fahne", "lampions", "feuerwerk", "grill zubehör", "girlande"]},
    {"name": "Samichlaus (St. Nicholas)", "country": "CH", "type": "fixed", "month": 12, "day": 6,
     "lead_weeks": 4, "category": "Advent / Gifting",
     "keywords": ["adventskalender", "nikolaus geschenke", "weihnachtsdeko", "lichterkette", "samichlaus sack"]},

    # ── Italy ──────────────────────────────────────────────────────────────────
    {"name": "Ferragosto", "country": "IT", "type": "fixed", "month": 8, "day": 15,
     "lead_weeks": 5, "category": "Summer / Beach",
     "keywords": ["ombrellone mare", "materassino gonfiabile", "borsa frigo", "ventilatore portatile", "gonfiabili piscina"]},
    {"name": "Immacolata (tree-up day)", "country": "IT", "type": "fixed", "month": 12, "day": 8,
     "lead_weeks": 5, "category": "Christmas / Decor",
     "keywords": ["albero di natale", "luci natalizie", "decorazioni natalizie", "presepe", "ghirlanda natalizia"]},
    {"name": "Befana (Epiphany)", "country": "IT", "type": "fixed", "month": 1, "day": 6,
     "lead_weeks": 3, "category": "Gifting / Stockings",
     "keywords": ["calza befana", "caramelle", "giocattoli bambini", "calze natalizie", "dolci befana"]},

    # ── Denmark ────────────────────────────────────────────────────────────────
    {"name": "Mortensaften (St. Martin's Eve)", "country": "DK", "type": "fixed", "month": 11, "day": 10,
     "lead_weeks": 3, "category": "Feast / Kitchen",
     "keywords": ["stegeso", "termometer stegt", "andefedt", "bradepande", "gaffel og kniv"]},
    {"name": "Jul (Danish Christmas)", "country": "DK", "type": "dates",
     "dates": ["2026-12-24", "2027-12-24"],
     "lead_weeks": 6, "category": "Christmas / Decor",
     "keywords": ["julepynt", "julekalender", "adventskrans", "julelys", "julehjerter"]},

    # ── The four seasons (cross-market, Northern-Hemisphere astronomical dates) ──
    # type "season" spans a START..END window: while today is inside it you're "in season"
    # (list now); before it, days-until-start drives the build-ahead read. Products key off the
    # season START (that's when you want to already be listed), so lead_weeks is generous.
    {"name": "Spring", "country": "GLOBAL", "type": "season",
     "month": 3, "day": 20, "end_month": 6, "end_day": 20,
     "lead_weeks": 8, "category": "Season / Garden & Outdoor",
     "keywords": ["garden tools", "raised garden bed", "planter box", "outdoor furniture", "solar garden lights"]},
    {"name": "Summer", "country": "GLOBAL", "type": "season",
     "month": 6, "day": 21, "end_month": 9, "end_day": 22,
     "lead_weeks": 10, "category": "Season / Pool & Beach",
     "keywords": ["inflatable pool", "pool float", "beach umbrella", "portable fan", "cooling towel"]},
    {"name": "Autumn / Fall", "country": "GLOBAL", "type": "season",
     "month": 9, "day": 23, "end_month": 12, "end_day": 20,
     "lead_weeks": 8, "category": "Season / Cozy & Decor",
     "keywords": ["fall decorations", "autumn wreath", "cozy throw blanket", "pumpkin decor", "leaf rake"]},
    {"name": "Winter", "country": "GLOBAL", "type": "season",
     "month": 12, "day": 21, "end_month": 3, "end_day": 19,
     "lead_weeks": 8, "category": "Season / Cold-weather",
     "keywords": ["heated blanket", "space heater", "snow shovel", "thermal gloves", "humidifier"]},

    # ── Scheduled world events ─────────────────────────────────────────────────
    {"name": "FIFA World Cup 2026", "country": "GLOBAL", "type": "dates", "dates": ["2026-06-11"],
     "lead_weeks": 10, "category": "Sports / Fan gear",
     "keywords": ["soccer jersey", "national flag", "soccer fan gear", "football scarf", "soccer ball"]},
    {"name": "Winter Olympics 2026 (Milan-Cortina)", "country": "GLOBAL", "type": "dates",
     "dates": ["2026-02-06"],
     "lead_weeks": 8, "category": "Sports / Fan gear",
     "keywords": ["national flag", "winter sports gear", "fan merchandise", "watch party supplies", "ski accessories"]},
]


def events_path() -> "os.PathLike[str]":
    return config.events_dir() / "events.json"


def ensure_events_file() -> "os.PathLike[str]":
    """Return the calendar path, seeding it with DEFAULT_EVENTS on first use (operator-growable)."""
    path = events_path()
    if not os.path.exists(path):
        os.makedirs(config.events_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_EVENTS, fh, indent=2, ensure_ascii=False)
    return path


def _load_events() -> list[dict]:
    """Read the calendar file (seeded on first use); fall back to the in-code seed on any error."""
    try:
        with open(ensure_events_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else DEFAULT_EVENTS
    except (OSError, json.JSONDecodeError):
        return DEFAULT_EVENTS


def _easter(year: int) -> date:
    """Western (Gregorian) Easter Sunday — Anonymous Gregorian computus. Pure integer math."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _season_dates(ev: dict, year: int) -> tuple[date, date]:
    """Resolve a season's [start, end] for a given start-year. Winter wraps the year boundary
    (Dec 21 → Mar 19), so an end month/day that falls before the start rolls into the next year."""
    sm, sd = int(ev.get("month", 1)), int(ev.get("day", 1))
    em, ed = int(ev.get("end_month", sm)), int(ev.get("end_day", sd))
    start = date(year, sm, sd)
    end = date(year, em, ed) if (em, ed) >= (sm, sd) else date(year + 1, em, ed)
    return start, end


def _season_occurrence(ev: dict, today: date) -> tuple[date, date, bool] | None:
    """For a season, return (start, end, in_season). If today is inside a window, that window
    (in_season=True); otherwise the soonest upcoming window (in_season=False)."""
    best: tuple[date, date, bool] | None = None
    for yr in (today.year - 1, today.year, today.year + 1):
        start, end = _season_dates(ev, yr)
        if start <= today <= end:
            return start, end, True
        if start >= today and (best is None or start < best[0]):
            best = (start, end, False)
    return best


def _next_occurrence(ev: dict, today: date) -> date | None:
    """Resolve the next date >= today for an event, by its type. None if it has no future date."""
    kind = str(ev.get("type") or "fixed").lower()
    if kind == "fixed":
        month, day = int(ev.get("month", 1)), int(ev.get("day", 1))
        for yr in (today.year, today.year + 1):
            try:
                d = date(yr, month, day)
            except ValueError:
                return None
            if d >= today:
                return d
        return None
    if kind == "easter":
        offset = int(ev.get("offset", 0))
        for yr in (today.year, today.year + 1):
            d = _easter(yr) + _dt.timedelta(days=offset)
            if d >= today:
                return d
        return None
    if kind == "dates":
        future = []
        for s in ev.get("dates", []):
            try:
                d = _dt.date.fromisoformat(str(s))
            except ValueError:
                continue
            if d >= today:
                future.append(d)
        return min(future) if future else None
    return None


def _horizon(days_until: int, lead_weeks: int) -> str:
    """Map days-until + lead-time to the listing-plan's timing language.

      now         — inside the run-up (list immediately): 0 <= days_until <= lead_weeks*7
      build_ahead — 1–3 months before the run-up starts (prep the batch now)
      later       — still on the radar, beyond the build window
    """
    run_up = max(0, int(lead_weeks)) * 7
    if days_until <= run_up:
        return "now"
    if days_until <= run_up + _BUILD_AHEAD_EXTRA_DAYS:
        return "build_ahead"
    return "later"


_HORIZON_LABEL = {"now": "List now", "build_ahead": "Build ahead", "later": "Upcoming"}


def upcoming(country: str = "ALL", within_days: int = 150) -> dict:
    """Compute the upcoming events within a horizon, sorted by soonest first.

    `country` = "ALL" (every market, GLOBAL always included) or an ISO-2 code (that market +
    GLOBAL, since global tentpoles apply everywhere). `within_days` caps how far out to look.
    """
    today = date.today()
    within_days = max(7, min(400, int(within_days)))
    want = (country or "ALL").upper()

    rows: list[dict] = []
    countries_present: set[str] = set()
    for ev in _load_events():
        c = str(ev.get("country") or "GLOBAL").upper()
        countries_present.add(c)
        if want not in ("ALL", "") and c not in (want, "GLOBAL"):
            continue
        kind = str(ev.get("type") or "fixed").lower()

        # Seasons span a window: while we're inside it, it's "in season" (list now, days_until 0);
        # otherwise it behaves like any dated event counting down to the season start.
        in_season = False
        season_start = season_end = None
        if kind == "season":
            occ = _season_occurrence(ev, today)
            if occ is None:
                continue
            season_start, season_end, in_season = occ
            nxt = today if in_season else season_start
        else:
            nxt = _next_occurrence(ev, today)
        if nxt is None:
            continue
        days_until = (nxt - today).days
        if days_until > within_days:
            continue
        lead_weeks = int(ev.get("lead_weeks", 4))
        horizon = "now" if in_season else _horizon(days_until, lead_weeks)
        rows.append({
            "name": ev.get("name"),
            "country": c,
            "category": ev.get("category"),
            "next_date": nxt.isoformat(),
            "days_until": days_until,
            "weeks_until": round(days_until / 7, 1),
            "lead_weeks": lead_weeks,
            "horizon": horizon,
            "horizon_label": _HORIZON_LABEL[horizon],
            "recurring": kind in ("fixed", "easter", "season"),
            "is_season": kind == "season",
            "in_season": in_season,
            "season_start": season_start.isoformat() if season_start else None,
            "season_end": season_end.isoformat() if season_end else None,
            "keywords": [str(k) for k in (ev.get("keywords") or []) if str(k).strip()],
        })

    rows.sort(key=lambda r: r["days_until"])
    totals = {
        "events": len(rows),
        "now": sum(1 for r in rows if r["horizon"] == "now"),
        "build_ahead": sum(1 for r in rows if r["horizon"] == "build_ahead"),
        "later": sum(1 for r in rows if r["horizon"] == "later"),
    }
    return {
        "as_of": today.isoformat(),
        "country": want,
        "within_days": within_days,
        "countries": sorted(countries_present),
        "events": rows,
        "totals": totals,
    }
