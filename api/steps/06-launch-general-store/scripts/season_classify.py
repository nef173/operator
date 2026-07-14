#!/usr/bin/env python3
"""
season_classify.py — codifies the Lane-1 SEASON / CAPTURE-BUCKET classifier (WORKFLOW D4e + D4f).

The general-store funnel's entry gate (Lane 1) currently runs ad-hoc: keyword_data.py gives SV +
a 12-month trend, trends_dfs.py gives 5y slope + weekly momentum, and a human eyeballs whether a
keyword is LIST-NOW / BUILD-AHEAD / EVERGREEN / SKIP. This script makes that bucketing a real,
reproducible artifact so the orchestrator (/general-store-research) and candidate_queue.py can
consume it.

INPUTS
------
  --keywords  keyword-data.json   (REQUIRED) output of 01-niche-discovery/scripts/keyword_data.py
                                   shape: {"results":[{keyword, monthly_searches, competition_index,
                                           monthly_searches_trend:[{year,month,search_volume}, ...12]}]}
  --trends    trends-dfs.json      (OPTIONAL) output of 01-niche-discovery/scripts/trends_dfs.py
                                   a LIST of {keyword, geo, raw_series, slope_per_period,
                                   growth_ratio_recent_vs_early, seasonality_variance, peak_month,
                                   related_queries:{rising:[{query,change}]}}
                                   When present, current-week MOMENTUM (last-8 vs prior-8 of the weekly
                                   raw_series) LEADS the list-now read (D4f). When absent, we fall back
                                   to the monthly trend's recent trajectory (weaker — flagged in output).

GATE (D4e/Lane-1, operator 2026-06-16)
--------------------------------------
  SV >= 10,000  -> PASS   (PREFER >= 20,000 -> PASS-STRONG)
  SV <  10,000  -> FAIL   (not a candidate, full stop; capture_bucket = SKIP, reason = below-floor)

CAPTURE BUCKETS (D4e) for gate-PASS keywords, from the 12-month seasonal shape + months-to-peak:
  ⚡ BREAKOUT     never auto-set from MONTHLY data (D4d: monthly can't see a 1-7d spike). We only EMIT a
                 "verify-breakout" FLAG when trends rising-queries carry a BREAKOUT change or weekly
                 momentum spikes hard — the operator confirms with a daily-Trends pass.
  🟢 LIST-NOW     peak <= ~1 month out AND ascending into peak NOW.
  🟡 BUILD-AHEAD  peak >= 2 months out -> PARK; scheduled list-date = peak - ~5 weeks (4-6wk + index runway).
  ⚪ EVERGREEN    low seasonal amplitude (flat) -> backfill only.
  ❌ SKIP         peaked already AND descending now (never list into a falling curve).

POSTURE (--posture, default trend-ride)
---------------------------------------
  trend-ride  (general store)  the buckets above as-is: catch the wave, PARK build-ahead until ~5wk
                               pre-peak, skip the falling curve.
  niche       (niche store)    BUILD A CATALOG NOW. The seasonal read is kept (field `seasonal_read`)
                               but it no longer GATES the listing — for a niche store an evergreen or
                               even seasonal-ish staple is worth listing now + monitoring, not parking
                               a year. So gate-cleared keywords resolve to LIST-NOW; BUILD-AHEAD keeps
                               its peak as `ramp_date` + `monitor:true` (list now, scale ads at the ramp),
                               and a cooling (SKIP) staple becomes LIST-NOW + monitor rather than dropped.

Reporting standard (D4f): SV is ALWAYS printed next to the keyword; the list-now decision LEADS with
current-week momentum; the 5y growth_ratio is historical CONTEXT only, never "rising now."

USAGE
-----
  PY=06-launch-general-store/scripts/season_classify.py
  python $PY --keywords keyword-data.json --out season.json
  python $PY --keywords keyword-data.json --trends trends-dfs.json --out season.json --min-sv 10000
  python $PY --keywords keyword-data.json --trends trends-dfs.json --geo US     # pick the geo from trends

Pure stdlib — matches the repo-native backbone (WORKFLOW D3).
"""
import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

# ----- tunables (mirror the locked decisions; override via flags where it matters) -----
DEFAULT_MIN_SV = 10_000          # D4e hard floor
PREFER_SV = 20_000               # D4e preferred floor

# VOLUME TIERS (project-wide, operator 2026-07-04). The 10k floor is the GATE, not the focus —
# search volume is the winner-probability lever, so we bias the whole funnel toward the biggest
# keywords. Each gate-cleared keyword gets a volume_tier; downstream (sku_plan.products_by_tier)
# lists MORE products for a prime keyword than a floor-band one. Thresholds = lower bound / tier.
VOLUME_TIERS = {"prime": 200_000, "strong": 100_000, "solid": 30_000, "entry": 10_000}
VOLUME_TIER_LABELS = {
    "prime": "Prime (≥200k)", "strong": "Strong (100–200k)", "solid": "Solid (30–100k)",
    "entry": "Entry (10–30k)", "below": "Below floor (<10k)",
}


def volume_tier(sv, tiers: dict = VOLUME_TIERS) -> str:
    """Map a monthly SV to its winner-probability tier (prime/strong/solid/entry/below)."""
    if sv is None:
        return "below"
    v = float(sv)
    if v >= tiers.get("prime", 200_000):
        return "prime"
    if v >= tiers.get("strong", 100_000):
        return "strong"
    if v >= tiers.get("solid", 30_000):
        return "solid"
    if v >= tiers.get("entry", 10_000):
        return "entry"
    return "below"
EVERGREEN_AMPLITUDE = 0.15       # (max-min)/mean below this = flat -> EVERGREEN
ASCEND_RATIO = 1.05              # momentum/recent ratio above this = ascending
DESCEND_RATIO = 0.95             # below this = descending
LISTNOW_MAX_MONTHS = 1           # peak within this many months out = list-now window
BUILDAHEAD_MAX_MONTHS = 5        # peak 2..this months out = build-ahead; beyond = park (still build-ahead)
LIST_LEAD_WEEKS = 5              # list this many weeks before the peak (4-6wk band, D4e)


def _slug(kw: str) -> str:
    return "-".join("".join(c if c.isalnum() or c == " " else " " for c in kw.lower()).split())[:40]


def _amplitude(svs: list[float]) -> float:
    svs = [v for v in svs if v is not None]
    if not svs:
        return 0.0
    mean = sum(svs) / len(svs)
    if mean == 0:
        return 0.0
    return (max(svs) - min(svs)) / mean


def _monthly_recent_ratio(trend: list[dict]) -> float | None:
    """Fallback momentum from the 12-mo monthly trend: mean(last 3) / mean(prior 3)."""
    svs = [t.get("search_volume") for t in trend if t.get("search_volume") is not None]
    if len(svs) < 6:
        return None
    last3 = svs[-3:]
    prior3 = svs[-6:-3]
    p = sum(prior3) / len(prior3)
    if p == 0:
        return None
    return (sum(last3) / len(last3)) / p


def _weekly_momentum(raw_series: list) -> float | None:
    """D4f primary momentum: mean(last 8 weeks) / mean(prior 8 weeks) of the weekly trends series."""
    s = [v for v in (raw_series or []) if v is not None]
    if len(s) < 16:
        return None
    last8 = s[-8:]
    prior8 = s[-16:-8]
    p = sum(prior8) / len(prior8)
    if p == 0:
        return None
    return (sum(last8) / len(last8)) / p


def _peak_month_from_trend(trend: list[dict]) -> tuple[int | None, int | None]:
    """Return (peak_calendar_month 1-12, current/most-recent calendar month) from the monthly trend."""
    rows = [t for t in trend if t.get("search_volume") is not None and t.get("month")]
    if not rows:
        return None, None
    peak = max(rows, key=lambda t: t.get("search_volume") or 0)
    current = rows[-1]  # most recent reported month
    return int(peak["month"]), int(current["month"])


def _months_to_peak(peak_month: int, current_month: int) -> int:
    return (peak_month - current_month) % 12


def _has_breakout_signal(tr: dict | None) -> bool:
    if not tr:
        return False
    rising = ((tr.get("related_queries") or {}).get("rising")) or []
    for q in rising:
        ch = str(q.get("change", "")).upper()
        if "BREAKOUT" in ch:
            return True
    return False


def _list_date(peak_month: int, ref: _dt.date, lead_weeks: int = LIST_LEAD_WEEKS) -> str:
    """Next occurrence of peak_month from ref, minus lead_weeks. Returns ISO date."""
    year = ref.year if peak_month >= ref.month else ref.year + 1
    peak = _dt.date(year, peak_month, 15)  # mid-month as the peak anchor
    return (peak - _dt.timedelta(weeks=lead_weeks)).isoformat()


def _apply_niche_posture(out: dict) -> dict:
    """Niche store: build the core catalog NOW. The seasonal read is preserved in `seasonal_read`
    but stops GATING the listing — a niche store lists its evergreen/seasonal-ish staples now and
    MONITORS the seasonal ones to time ad-scaling, rather than parking them for months."""
    sr = out["capture_bucket"]
    out["seasonal_read"] = sr
    if sr == "BUILD-AHEAD":
        out["ramp_date"] = out.get("list_date")   # repurpose: when the seasonal ramp hits -> scale ads
        out["list_date"] = None
        out["capture_bucket"] = "LIST-NOW"
        out["monitor"] = True
        out["reason"] = (f"niche: list now (catalog); seasonal ramp ~{out['ramp_date']} "
                         f"— monitor to scale ads")
    elif sr == "EVERGREEN":
        out["capture_bucket"] = "LIST-NOW"
        out["reason"] = "niche: evergreen staple — list now (core catalog)"
    elif sr == "SKIP":
        out["capture_bucket"] = "LIST-NOW"
        out["monitor"] = True
        out["reason"] = "niche: cooling now but listable catalog staple — list + monitor"
    # LIST-NOW / BREAKOUT stay as-is
    return out


def classify(rec: dict, tr: dict | None, min_sv: int, ref: _dt.date, posture: str = "trend-ride") -> dict:
    kw = rec.get("keyword")
    sv = rec.get("monthly_searches")
    trend = rec.get("monthly_searches_trend") or []
    svs = [t.get("search_volume") for t in trend]

    out = {
        "keyword": kw,
        "slug": _slug(kw or ""),
        "sv": sv,
        "volume_tier": volume_tier(sv),          # winner-probability tier (drives product count downstream)
        "volume_tier_label": VOLUME_TIER_LABELS.get(volume_tier(sv), volume_tier(sv)),
        "competition_index": rec.get("competition_index"),
        "cpc": rec.get("cpc"),
        "gate": None,
        "capture_bucket": None,
        "seasonal_read": None,    # niche posture: the trend-ride bucket before the list-now remap
        "monitor": False,         # niche posture: watch the seasonal ramp to time ad-scaling
        "ramp_date": None,        # niche posture: repurposed build-ahead peak (scale ads here, not list)
        "amplitude": round(_amplitude(svs), 3),
        "peak_month": None,
        "current_month": None,
        "months_to_peak": None,
        "momentum_weekly": None,      # D4f primary (from trends_dfs raw_series)
        "momentum_monthly": None,     # fallback (from the 12-mo monthly trend)
        "momentum_source": None,
        "ascending_now": None,
        "growth_5y_context": (tr or {}).get("growth_ratio_recent_vs_early"),  # CONTEXT ONLY (D4f)
        "verify_breakout": False,
        "list_date": None,
        "reason": "",
    }

    # ---- GATE ----
    if sv is None:
        out["gate"] = "FAIL"
        out["capture_bucket"] = "SKIP"
        out["reason"] = "no SV data"
        return out
    if sv < min_sv:
        out["gate"] = "FAIL"
        out["capture_bucket"] = "SKIP"
        out["reason"] = f"SV {sv:,} below {min_sv:,} floor"
        return out
    out["gate"] = "PASS-STRONG" if sv >= PREFER_SV else "PASS"

    # ---- momentum (D4f: weekly leads; monthly is fallback) ----
    mw = _weekly_momentum((tr or {}).get("raw_series"))
    mm = _monthly_recent_ratio(trend)
    out["momentum_weekly"] = round(mw, 3) if mw is not None else None
    out["momentum_monthly"] = round(mm, 3) if mm is not None else None
    ratio = mw if mw is not None else mm
    out["momentum_source"] = "weekly" if mw is not None else ("monthly" if mm is not None else None)
    if ratio is not None:
        out["ascending_now"] = ratio >= ASCEND_RATIO
    ascending = bool(out["ascending_now"])
    descending = ratio is not None and ratio <= DESCEND_RATIO

    # ---- seasonal shape ----
    peak_m, cur_m = _peak_month_from_trend(trend)
    out["peak_month"], out["current_month"] = peak_m, cur_m
    amp = out["amplitude"]

    # breakout: never auto-set from monthly; flag for daily-Trends verification only
    if _has_breakout_signal(tr) or (mw is not None and mw >= 1.6):
        out["verify_breakout"] = True

    # ---- bucket ----
    if peak_m is None:
        # no usable seasonal trend (amplitude/peak unknowable) -> evergreen unless momentum is rising
        out["capture_bucket"] = "LIST-NOW" if ascending else "EVERGREEN"
        out["reason"] = "no monthly trend; momentum-only read"
        return _apply_niche_posture(out) if posture == "niche" else out

    mtp = _months_to_peak(peak_m, cur_m)
    out["months_to_peak"] = mtp

    if amp < EVERGREEN_AMPLITUDE:
        out["capture_bucket"] = "EVERGREEN"
        out["reason"] = f"flat (amplitude {amp:.2f} < {EVERGREEN_AMPLITUDE}); backfill only"
    elif mtp <= LISTNOW_MAX_MONTHS:
        if descending:
            # at/just-past peak and already falling -> don't list into a falling curve
            out["capture_bucket"] = "SKIP"
            out["reason"] = f"peak ~now (mtp {mtp}) but momentum falling ({ratio:.2f}) — past the wave"
        else:
            out["capture_bucket"] = "LIST-NOW"
            out["reason"] = (f"peak {mtp}mo out + ascending"
                             if ascending else
                             f"peak {mtp}mo out (momentum {ratio if ratio else 'n/a'}) — list into ramp")
    elif mtp <= BUILDAHEAD_MAX_MONTHS:
        out["capture_bucket"] = "BUILD-AHEAD"
        out["list_date"] = _list_date(peak_m, ref)
        out["reason"] = f"peak {mtp}mo out -> PARK; list ~{out['list_date']} ({LIST_LEAD_WEEKS}wk lead)"
    else:
        # peak >6mo out (e.g. Nov/Dec gifting seen from June) -> park, too early to list now
        out["capture_bucket"] = "BUILD-AHEAD"
        out["list_date"] = _list_date(peak_m, ref)
        out["reason"] = f"peak {mtp}mo out (far) -> PARK; list ~{out['list_date']}"

    return _apply_niche_posture(out) if posture == "niche" else out


def _index_trends(trends: list, geo: str | None) -> dict:
    """keyword -> trends record. If geo given, prefer that geo; else first non-error per keyword."""
    idx: dict[str, dict] = {}
    for t in trends or []:
        if t.get("error"):
            continue
        kw = t.get("keyword")
        if not kw:
            continue
        if geo and (t.get("geo") or "").upper() != geo.upper():
            # keep as a fallback only if nothing better is stored yet
            idx.setdefault("__fallback__:" + kw, t)
            continue
        idx[kw] = t
    # fold in fallbacks for keywords with no geo-matched record
    for k, v in list(idx.items()):
        if k.startswith("__fallback__:"):
            real = k.split(":", 1)[1]
            idx.setdefault(real, v)
            del idx[k]
    return idx


def main():
    ap = argparse.ArgumentParser(description="Lane-1 season/capture-bucket classifier (D4e+D4f)")
    ap.add_argument("--keywords", required=True, help="keyword_data.py output JSON")
    ap.add_argument("--trends", help="trends_dfs.py output JSON (optional momentum source)")
    ap.add_argument("--out", help="write classified JSON here (else stdout)")
    ap.add_argument("--min-sv", type=int, default=DEFAULT_MIN_SV, help=f"SV gate floor (default {DEFAULT_MIN_SV})")
    ap.add_argument("--geo", help="prefer this geo from the trends file (e.g. US)")
    ap.add_argument("--posture", choices=["trend-ride", "niche"], default="trend-ride",
                    help="trend-ride (general store, park build-ahead) | niche (list catalog now + "
                         "monitor seasonal ramp). Default trend-ride.")
    ap.add_argument("--today", help="override 'today' as YYYY-MM-DD (for build-ahead list-date math)")
    args = ap.parse_args()

    def _read_json(path: str):
        try:
            return json.loads(Path(path).read_text())
        except FileNotFoundError:
            sys.exit(f"❌ file not found: {path}")
        except json.JSONDecodeError as e:
            sys.exit(f"❌ invalid JSON in {path}: {e}")

    kw_doc = _read_json(args.keywords)
    results = kw_doc.get("results", kw_doc) if isinstance(kw_doc, dict) else kw_doc
    if not isinstance(results, list):
        sys.exit(f"❌ {args.keywords}: expected a list of results or a dict with a 'results' key")
    trends = _read_json(args.trends) if args.trends else []
    tr_idx = _index_trends(trends, args.geo)
    ref = _dt.date.fromisoformat(args.today) if args.today else _dt.date.today()

    classified = []
    for rec in results:
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        classified.append(classify(rec, tr_idx.get(rec.get("keyword")), args.min_sv, ref, args.posture))

    # sort: gate-pass first, then by bucket priority, then SV desc
    bucket_rank = {"BREAKOUT": 0, "LIST-NOW": 1, "BUILD-AHEAD": 2, "EVERGREEN": 3, "SKIP": 4}
    classified.sort(key=lambda c: (
        0 if str(c["gate"]).startswith("PASS") else 1,
        bucket_rank.get(c["capture_bucket"], 9),
        -(c["sv"] or 0),
    ))

    passed = [c for c in classified if str(c["gate"]).startswith("PASS")]
    tier_counts = {t: sum(1 for c in passed if c.get("volume_tier") == t)
                   for t in ("prime", "strong", "solid", "entry", "below")}
    doc = {
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "today": ref.isoformat(),
        "posture": args.posture,
        "min_sv": args.min_sv,
        "n": len(classified),
        "n_gate_pass": len(passed),
        "by_volume_tier": tier_counts,        # focus = the biggest tiers (prime/strong)
        "results": classified,
    }

    if args.out:
        Path(args.out).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        print(f"✓ wrote {args.out}", file=sys.stderr)
    else:
        print(json.dumps(doc, indent=2, ensure_ascii=False))

    # human summary to stderr (D4f: SV always shown, momentum-led)
    icons = {"BREAKOUT": "⚡", "LIST-NOW": "🟢", "BUILD-AHEAD": "🟡", "EVERGREEN": "⚪", "SKIP": "❌"}
    tier_icon = {"prime": "🔥", "strong": "⬆", "solid": "•", "entry": "·", "below": " "}
    tsum = "  ".join(f"{tier_icon[t]}{t} {tier_counts[t]}" for t in ("prime", "strong", "solid", "entry"))
    print(f"\n{doc['n_gate_pass']}/{doc['n']} cleared the {args.min_sv:,} SV gate "
          f"(posture: {args.posture}) — focus the biggest: {tsum}", file=sys.stderr)
    for c in classified:
        if not str(c["gate"]).startswith("PASS"):
            continue
        mom = (f"mom {c['momentum_source'][0]}={c['momentum_weekly'] or c['momentum_monthly']}"
               if c["momentum_source"] else "mom n/a")
        bk = c["capture_bucket"]
        vt = c.get("volume_tier", "")
        flag = "  ⚡verify-breakout" if c["verify_breakout"] else ""
        flag += "  👁monitor" if c.get("monitor") else ""
        print(f"  {icons.get(bk,' ')} {bk:<12} {tier_icon.get(vt,' ')}{vt:<6} {c['keyword']:<28} "
              f"SV={c['sv']:>7,}  {mom}  — {c['reason']}{flag}", file=sys.stderr)


if __name__ == "__main__":
    main()
