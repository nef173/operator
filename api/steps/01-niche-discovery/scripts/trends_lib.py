#!/usr/bin/env python3
"""
trends_lib.py — shared trend-analysis core for the Trend Radar
===============================================================
Pure-stdlib math on a 5-year WEEKLY interest series (DFS google_trends_graph
shape: parallel raw_series[] + raw_dates[]). No API calls live here — fetching
+ caching are separate layers. Every detector imports from here so the
climatology is defined ONCE.

Corrects the bugs the 2026-06-15 trial exposed:
  1. Averaged climatology laundered a one-off 2026 spike into a fake "season"
     (hiking backpack: flat 18-24 for four years, then 88 in 2026 → mean said
     "April peak"). FIX: anomaly-robust baseline — drop the trailing incomplete
     year AND use the MEDIAN across years per week, not the mean. A single spike
     year can't move the median of five.
  2. peak/trough amplitude was unstable (summer trough≈0 → ratio explodes,
     christmas=62 vs yoga=6). FIX: amplitude = (peak-trough)/mean  [= seas_var],
     comparable across keywords.
  3. Week-level phase σ was too noisy (yoga's real April wobble → σ 9.9 wks,
     rejected). FIX: measure phase consistency at MONTH granularity.
  4. A spike and a season look identical on amplitude alone. FIX: the primary
     seasonal gate is REPEAT-ACROSS-YEARS (recurrence), with amplitude + phase
     as supporting filters. A breakout flag is computed separately so the two
     are never confused.

Validated targets (the trial regression set):
  christmas lights → SEASONAL (recurs every Dec, all years)
  hiking backpack  → BREAKOUT, NOT seasonal (one 2026 spike over a flat base)
  yoga mat         → BREAKOUT, NOT seasonal (one 2026 spike, already crashing)
"""
from __future__ import annotations

import math
from datetime import date
from statistics import mean, median, pstdev

# ── thresholds (calibrated against the trial regression set; tune in one place) ─
SEASONAL_AMP_MIN = 0.60        # (peak-trough)/mean on the robust climatology
SEASONAL_PHASE_MONTHS_MAX = 1.5   # circular stdev of yearly peak-MONTHS
SEASONAL_YEARS_MIN = 3         # need the spike in ≥3 distinct years to be a season
EVERGREEN_COV_MAX = 0.20       # detrended coefficient of variation
EVERGREEN_AMP_MAX = 0.50       # evergreen must also be seasonally flat
SURGE_PEAK_X = 2.0             # recent multi-week peak ≥ 2× robust per-week baseline
SURGE_RECENT_WEEKS = 12        # "recent" window for a multi-week surge on weekly data
REALTIME_SPIKE_X = 2.0         # daily/hourly spike vs trailing baseline (1-7 day)
REALTIME_SPIKE_DAYS = 7        # the 1-7 day real-time window
RAMP_THRESH_FRAC = 0.20        # ramp starts at trough + 20% of amplitude


# ── date helpers ──────────────────────────────────────────────────────────────
def _iso_week(d: str) -> int:
    y, m, dd = (int(x) for x in d.split("-")[:3])
    w = date(y, m, dd).isocalendar()[1]
    return 52 if w >= 53 else w


def _month(d: str) -> int:
    return int(d.split("-")[1])


def _year(d: str) -> int:
    return int(d.split("-")[0])


def week_to_month_name(w: int) -> str:
    names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    if not w:
        return "?"
    return names[min(11, int((w - 1) // 4.345))]


# ── robust climatology (the core fix) ─────────────────────────────────────────
def group_by_year(series: list[float], dates: list[str]) -> dict[int, list[tuple[int, float]]]:
    """{year: [(week_of_year, value), ...]} — used to test recurrence per year."""
    out: dict[int, list[tuple[int, float]]] = {}
    for v, d in zip(series, dates):
        if not d:
            continue
        out.setdefault(_year(d), []).append((_iso_week(d), v))
    return out


def complete_years(by_year: dict[int, list], min_weeks: int = 40) -> list[int]:
    """Years with enough coverage to trust (drops the trailing incomplete year)."""
    return sorted(y for y, pts in by_year.items() if len(pts) >= min_weeks)


def robust_climatology(series: list[float], dates: list[str]) -> list[float]:
    """Average-year curve using the MEDIAN across years per ISO week, computed
    ONLY over complete years. The median + dropping the incomplete current year
    is what stops a single spike year from inventing a fake season.
    Returns 52 values (index 0 = week 1), circular 3-week smoothed.
    """
    by_year = group_by_year(series, dates)
    keep = set(complete_years(by_year))
    # collect per-week values across complete years only
    per_week: dict[int, list[float]] = {w: [] for w in range(1, 53)}
    for y, pts in by_year.items():
        if y not in keep:
            continue
        for w, v in pts:
            per_week[w].append(v)
    raw = {w: (median(vs) if vs else None) for w, vs in per_week.items()}
    # fill holes circularly with nearest defined week
    filled = []
    for w in range(1, 53):
        if raw[w] is not None:
            filled.append(raw[w]); continue
        for off in range(1, 27):
            a = raw[((w - 1 + off) % 52) + 1]
            b = raw[((w - 1 - off) % 52) + 1]
            if a is not None:
                filled.append(a); break
            if b is not None:
                filled.append(b); break
        else:
            filled.append(0.0)
    # circular 3-week smooth
    return [mean([filled[(i - 1) % 52], filled[i], filled[(i + 1) % 52]])
            for i in range(52)]


def peak_trough_ramp(smooth: list[float]) -> dict:
    peak_i = max(range(52), key=lambda i: smooth[i])
    trough_i = min(range(52), key=lambda i: smooth[i])
    peak_v, trough_v = smooth[peak_i], smooth[trough_i]
    m = mean(smooth)
    amplitude = (peak_v - trough_v) / m if m else 0.0   # = seasonality_variance, comparable
    thresh = trough_v + RAMP_THRESH_FRAC * (peak_v - trough_v)
    ramp_i = trough_i
    for off in range(1, 53):
        i = (trough_i + off) % 52
        if smooth[i] >= thresh:
            ramp_i = i; break
    return {"peak_wk": peak_i + 1, "trough_wk": trough_i + 1, "ramp_wk": ramp_i + 1,
            "amplitude": amplitude}


# ── recurrence: does the peak land in the same MONTH across years? ─────────────
def annual_peak_months(series: list[float], dates: list[str]) -> list[int]:
    by_year = group_by_year(series, dates)
    months = []
    for y in complete_years(by_year):
        pts = by_year[y]
        # week→month: take the month of the peak week's mid-date approximation
        peak_w = max(pts, key=lambda t: t[1])[0]
        months.append(min(12, max(1, int((peak_w - 1) // 4.345) + 1)))
    return months


def circular_stdev_units(values: list[int], period: int) -> float:
    """Circular stdev on a wrap-around scale (months: period=12)."""
    if len(values) < 2:
        return 0.0
    angles = [2 * math.pi * v / period for v in values]
    C = mean(math.cos(a) for a in angles)
    S = mean(math.sin(a) for a in angles)
    R = math.hypot(C, S)
    if R >= 1.0:
        return 0.0
    return math.sqrt(-2 * math.log(R)) * period / (2 * math.pi)


# ── evergreen: flatness after removing trend ──────────────────────────────────
def detrended_cov(series: list[float]) -> float:
    n = len(series)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx, my = mean(xs), mean(series)
    den = sum((x - mx) ** 2 for x in xs)
    slope = (sum((xs[i] - mx) * (series[i] - my) for i in range(n)) / den) if den else 0.0
    resid = [series[i] - (my + slope * (xs[i] - mx)) for i in range(n)]
    return (pstdev(resid) / my) if my else 0.0


# ── RECENT_SURGE: multi-week spike over the robust baseline (weekly data) ──────
def surge_signal(series: list[float], dates: list[str], smooth: list[float]) -> dict:
    """Multi-WEEK surge detector on 5y weekly data. Compares the recent ~12-week
    window against the per-week robust climatology baseline. A surge = recent
    peak ≫ what that week historically is. Separates hiking/yoga's 2026 spring
    spike from christmas's EXPECTED Dec rise (which matches its own baseline).

    NOTE: this is NOT the 1-7 day REALTIME_BREAKOUT — weekly data can't resolve
    that. Real-time spikes need realtime_breakout() on a fresh daily/hourly pull.
    """
    if len(series) < SURGE_RECENT_WEEKS + 4:
        return {"is_surge": False, "recent_x": None, "peak_date": None}
    recent = series[-SURGE_RECENT_WEEKS:]
    recent_dates = dates[-SURGE_RECENT_WEEKS:]
    peak_v = max(recent)
    peak_d = recent_dates[recent.index(peak_v)]
    expected = [smooth[(_iso_week(d) - 1) % 52] for d in recent_dates if d]
    base = median(expected) if expected else mean(smooth)
    recent_x = round(peak_v / base, 2) if base > 0 else None
    is_surge = recent_x is not None and recent_x >= SURGE_PEAK_X
    return {"is_surge": is_surge, "recent_x": recent_x,
            "peak_date": peak_d, "baseline_expected": round(base, 1),
            "recent_peak": peak_v}


def realtime_breakout(daily_series: list[float], daily_dates: list[str]) -> dict:
    """1-7 DAY real-time spike detector. Runs on a FRESH past_7_days (hourly) or
    past_90_days (daily) pull — NOT the 5y weekly series. Compares the last
    REALTIME_SPIKE_DAYS against the trailing baseline of the same fresh pull.

    Tiers:
      BREAKOUT_TODAY     — spike inside the last ~2 days
      BREAKOUT_THIS_WEEK — last 1-7 days elevated, persisted ≥2 points
    """
    n = len(daily_series)
    if n < REALTIME_SPIKE_DAYS + 7:
        return {"tier": None, "spike_x": None, "is_realtime_breakout": False}
    recent = daily_series[-REALTIME_SPIKE_DAYS:]
    baseline = daily_series[:-REALTIME_SPIKE_DAYS]
    base = median(baseline) if baseline else 0.0
    peak_v = max(recent)
    spike_x = round(peak_v / base, 2) if base > 0 else None
    elevated = [v for v in recent if base > 0 and v >= SURGE_PEAK_X * base]
    persisted = len(elevated) >= 2
    is_bo = spike_x is not None and spike_x >= REALTIME_SPIKE_X and persisted
    tier = None
    if is_bo:
        # if the peak is in the last 2 points → TODAY, else THIS_WEEK
        last2_peak = max(daily_series[-2:]) if n >= 2 else peak_v
        tier = "BREAKOUT_TODAY" if last2_peak >= SURGE_PEAK_X * base else "BREAKOUT_THIS_WEEK"
    return {"tier": tier, "spike_x": spike_x, "is_realtime_breakout": is_bo,
            "baseline": round(base, 1), "recent_peak": peak_v}


# ── SUSTAINED_RISE: ET-style multi-year compounding growth ────────────────────
# This is the mode Exploding Topics sells. Their core signal (verified via Semrush
# KB 2026-06-15) is "steady compounding Google search-volume growth over months/
# years". We reproduce it with a log-linear regression on a UNIFORM monthly series.
#
# Data source is SOURCE-ADAPTIVE (the "smart detection" for coverage gaps):
#   PRIMARY  = SV monthly_searches (absolute, uniform monthly grid, up to 4y)
#   FALLBACK = Trends weekly series resampled to monthly-by-date — used when SV is
#              missing (regulated products like nicotine, or brand-new terms that
#              Google Ads doesn't report SV for but Trends does).
# Either way the regression runs on a clean uniform monthly grid.

SUSTAINED_SLOPE_MIN = 0.02     # per-month log slope (~27%/yr) to count as rising
SUSTAINED_R2_MIN = 0.70        # how STEADY the climb is (vs choppy/spiky). The key gate.
SUSTAINED_R2_CHOPPY = 0.45     # rising but noisy
SUSTAINED_EXPLODING_ANN = 0.80 # ≥80%/yr annualized = "exploding" tier


def _monthly_from_sv(monthly_searches: list[dict]) -> list[float]:
    """Absolute monthly volumes, oldest→newest, floored at 1 for log."""
    if not monthly_searches:
        return []
    pts = sorted(monthly_searches, key=lambda m: (m.get("year", 0), m.get("month", 0)))
    return [max(m.get("search_volume") or 0, 1) for m in pts]


def _monthly_from_weekly(series: list[float], dates: list[str]) -> list[float]:
    """Resample a (possibly gappy) weekly Trends series onto a uniform monthly grid
    keyed by ACTUAL DATE. Missing months (sub-threshold weeks DFS dropped) are
    filled with the floor — which correctly encodes 'near-zero interest then', the
    new-product fingerprint. This is the Trends fallback for SV-missing keywords."""
    if not series or not dates:
        return []
    by_ym: dict[tuple, list[float]] = {}
    for v, d in zip(series, dates):
        if not d:
            continue
        y, m = int(d.split("-")[0]), int(d.split("-")[1])
        by_ym.setdefault((y, m), []).append(v)
    if not by_ym:
        return []
    keys = sorted(by_ym)
    (y0, m0), (y1, m1) = keys[0], keys[-1]
    out = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        vals = by_ym.get((y, m))
        out.append(max(mean(vals), 1.0) if vals else 1.0)
        m += 1
        if m > 12:
            m = 1; y += 1
    return out


def _acceleration_coeff(monthly: list[float]) -> float:
    """Exploding Topics' 'exponent' term: the t² coefficient of a quadratic fit on
    the RAW (not log) series, normalized by the series mean. >0 = accelerating
    (true 'exploding'), ~0 = linear ('regular'/steady), <0 = decelerating. This is
    ET's documented discriminator between 'exploding' and 'regular'. Verified from
    their page data 2026-06-15 (value = gradient·t + yIntercept + exponent·t²)."""
    n = len(monthly)
    if n < 6:
        return 0.0
    # least-squares quadratic y = a + b t + c t^2 via normal equations (small n)
    xs = list(range(n))
    Sy = sum(monthly); mean_y = Sy / n or 1.0
    # build sums
    S = [sum(x ** k for x in xs) for k in range(5)]          # S0..S4
    Sxy = [sum((xs[i] ** k) * monthly[i] for i in range(n)) for k in range(3)]
    # solve 3x3 [[S0,S1,S2],[S1,S2,S3],[S2,S3,S4]] · [a,b,c] = [Sxy0,Sxy1,Sxy2]
    import itertools
    A = [[S[0], S[1], S[2]], [S[1], S[2], S[3]], [S[2], S[3], S[4]]]
    Bv = [Sxy[0], Sxy[1], Sxy[2]]
    # Cramer's rule
    def det3(m):
        return (m[0][0]*(m[1][1]*m[2][2]-m[1][2]*m[2][1])
                - m[0][1]*(m[1][0]*m[2][2]-m[1][2]*m[2][0])
                + m[0][2]*(m[1][0]*m[2][1]-m[1][1]*m[2][0]))
    D = det3(A)
    if abs(D) < 1e-12:
        return 0.0
    Ac = [row[:] for row in A]
    for r in range(3):
        Ac[r][2] = Bv[r]
    c = det3(Ac) / D                  # the t² coefficient
    return c / mean_y                 # normalize so it's comparable across keywords


def sustained_rise(monthly: list[float], window: int = 0) -> dict:
    """ET-style trend detector. Inspired by Exploding Topics' documented method
    (decoded from their page data 2026-06-15: regression with gradient + exponent
    t² term over a 24-month window). We run:
      • log-linear (exponential) slope + R²  — steadiness + compounding rate
      • quadratic t² 'acceleration' coeff    — ET's exploding-vs-regular discriminator

    WINDOW NOTE (validated 2026-06-15): ET uses a strict 24-month window, but we
    default to the FULL series (window=0). Empirically, the 24-month window MISSED
    late-S-curve risers — creatine gummies / suri toothbrush did their explosive
    growth in 2022-24, so the recent 24mo looks flat and they dropped out. The
    full-series log-linear caught them (11/11 vs ET's own picks). The 24mo
    acceleration is still computed (on the recent window) as ET's discriminator.
    Pass window=24 to replicate ET exactly when you want recency over lifetime."""
    if window and len(monthly) > window:
        monthly = monthly[-window:]
    n = len(monthly)
    if n < 24:
        return {"is_sustained_rise": False, "reason": f"{n} months (<24)", "stage": None}
    accel = _acceleration_coeff(monthly[-24:] if len(monthly) >= 24 else monthly)
    ly = [math.log(v) for v in monthly]
    xs = list(range(n))
    mx, my = mean(xs), mean(ly)
    den = sum((x - mx) ** 2 for x in xs)
    slope = sum((xs[i] - mx) * (ly[i] - my) for i in range(n)) / den if den else 0.0
    yhat = [my + slope * (x - mx) for x in xs]
    ss_res = sum((ly[i] - yhat[i]) ** 2 for i in range(n))
    ss_tot = sum((v - my) ** 2 for v in ly) or 1e-9
    r2 = 1 - ss_res / ss_tot
    ann = math.exp(slope * 12) - 1
    fold = monthly[-1] / mean(monthly[:6]) if mean(monthly[:6]) > 0 else None
    # S-curve stage from the last 6 months' own slope
    recent = ly[-6:]
    rx = list(range(len(recent)))
    rmx, rmy = mean(rx), mean(recent)
    rden = sum((x - rmx) ** 2 for x in rx)
    rslope = sum((rx[i] - rmx) * (recent[i] - rmy) for i in range(len(recent))) / rden if rden else 0.0
    stage = "rising" if rslope > 0.01 else "cooling" if rslope < -0.01 else "plateau"

    is_rise = slope >= SUSTAINED_SLOPE_MIN and r2 >= SUSTAINED_R2_MIN and stage != "cooling"
    is_choppy_rise = slope >= SUSTAINED_SLOPE_MIN and r2 >= SUSTAINED_R2_CHOPPY and not is_rise
    # PEAKED: it rose meaningfully over the window (fold-growth real) but the last
    # 6 months are cooling — it's rolling over. Don't launch into a fading trend.
    is_peaked = (stage == "cooling" and fold is not None and fold >= 1.5
                 and slope >= 0)
    # ET's exploding-vs-regular split = ACCELERATION (their t² exponent). A trend is
    # "exploding" if it's accelerating (accel > 0) OR very high compounding rate;
    # "steady" (ET's "regular") if rising but ~linear.
    tier = None
    if is_rise:
        tier = "exploding" if (accel > 0 or ann >= SUSTAINED_EXPLODING_ANN) else "steady"
    return {
        "is_sustained_rise": is_rise,
        "is_choppy_rise": is_choppy_rise,
        "is_peaked": is_peaked,
        "tier": tier, "stage": stage,
        "log_slope": round(slope, 4), "r2": round(r2, 2),
        "acceleration": round(accel, 4),
        "annualized_growth_pct": round(ann * 100),
        "fold_growth": round(fold, 1) if fold else None,
        "months": n, "window": n,
    }


def sustained_rise_adaptive(monthly_searches: list[dict] | None,
                            weekly_series: list[float] | None,
                            weekly_dates: list[str] | None) -> dict:
    """Source-adaptive: prefer SV monthly (clean/absolute); fall back to Trends
    weekly→monthly when SV is missing. Returns the sustained_rise dict + a 'source'
    field so the caller knows which fed it."""
    sv_monthly = _monthly_from_sv(monthly_searches or [])
    if len(sv_monthly) >= 24:
        r = sustained_rise(sv_monthly)
        r["source"] = "sv_monthly"
        return r
    wk_monthly = _monthly_from_weekly(weekly_series or [], weekly_dates or [])
    if len(wk_monthly) >= 24:
        r = sustained_rise(wk_monthly)
        r["source"] = "trends_weekly_resampled"
        return r
    return {"is_sustained_rise": False, "reason": "no usable monthly series", "stage": None,
            "source": None}


# ── lead-time grading for an approaching season ───────────────────────────────
def lead_time(cur_wk: int, ramp_wk: int, peak_wk: int) -> tuple[str, str]:
    d_ramp = (ramp_wk - cur_wk) % 52
    d_peak = (peak_wk - cur_wk) % 52
    if d_ramp < d_peak:  # before the season, ramp ahead
        if d_ramp <= 1:
            return "OK_IN_TREND", f"ramp in {d_ramp}w — launch now, minimal build-up"
        if d_ramp <= 3:
            return "GOOD", f"ramp in {d_ramp}w — solid runway"
        if d_ramp <= 8:
            return "OPTIMAL", f"ramp in {d_ramp}w — full build-up (index+ads+reviews season first)"
        return "OFF_SEASON_WAIT", f"ramp not for {d_ramp}w — optimal window opens in ~{d_ramp-8}w"
    else:                # in-season climb
        if d_peak >= 3:
            return "RAMPING", f"in-season, peak in {d_peak}w — launch now to catch peak"
        if d_peak >= 1:
            return "AT_PEAK_SOON", f"peak in {d_peak}w — little runway"
        return "TOO_LATE", "at peak — no runway, wait next cycle"


# ── top-level classifier ──────────────────────────────────────────────────────
def classify(rec: dict, today: date | None = None) -> dict:
    today = today or date.today()
    kw, geo = rec.get("keyword"), rec.get("geo")
    series = rec.get("raw_series") or []
    dates = rec.get("raw_dates") or []
    cur_wk = _iso_week(today.isoformat())

    # ── These run FIRST and DON'T need 100 weekly points ──────────────────────
    # SELLABILITY gate — dropshippable product, or a rising person/event/concept?
    ci = rec.get("competition_index")
    is_sellable = True if ci is None else ci >= 25

    # SUSTAINED_RISE (ET-style) — source-adaptive: SV monthly (clean) if present,
    # else Trends weekly resampled to monthly. Works even when weekly < 100 pts
    # (explosive new products born from zero — boneless couch, shoe washing bag).
    sr = sustained_rise_adaptive(rec.get("monthly_searches"), series, dates)

    # ── Weekly-dependent detectors: only if we have enough weekly history ──────
    have_weekly = len(series) >= 100
    if have_weekly:
        smooth = robust_climatology(series, dates)
        ptr = peak_trough_ramp(smooth)
        peak_months = annual_peak_months(series, dates)
        phase_std = circular_stdev_units(peak_months, 12)
        n_years = len(peak_months)
        cov = detrended_cov(series)
        surge = surge_signal(series, dates, smooth)
        is_seasonal = (ptr["amplitude"] >= SEASONAL_AMP_MIN
                       and phase_std <= SEASONAL_PHASE_MONTHS_MAX
                       and n_years >= SEASONAL_YEARS_MIN)
        is_evergreen = (cov <= EVERGREEN_COV_MAX
                        and ptr["amplitude"] <= EVERGREEN_AMP_MAX
                        and not surge["is_surge"])
        lead, lead_reason = lead_time(cur_wk, ptr["ramp_wk"], ptr["peak_wk"]) if is_seasonal else (None, None)
    else:
        smooth = [0.0] * 52
        ptr = {"peak_wk": 0, "ramp_wk": 0, "trough_wk": 0, "amplitude": 0.0}
        phase_std = 0.0; n_years = 0; cov = 0.0
        surge = {"is_surge": False, "recent_x": None, "peak_date": None}
        is_seasonal = is_evergreen = False
        lead, lead_reason = (None, None)

    # optional fresh daily series for the TRUE 1-7 day real-time breakout layer.
    rt = {"tier": None, "spike_x": None, "is_realtime_breakout": False}
    if rec.get("daily_series"):
        rt = realtime_breakout(rec["daily_series"], rec.get("daily_dates") or [])

    # If we have NEITHER usable weekly NOR a sustained-rise verdict, bail.
    if not have_weekly and not sr.get("source"):
        return {"keyword": kw, "geo": geo, "primary": "INSUFFICIENT_DATA",
                "note": f"{len(series)} weekly pts, no monthly series"}

    # precedence: NOT_SELLABLE short-circuits everything (don't recommend a movie).
    #   then realtime-breakout > recent-surge > sustained-rise > approaching-season
    #   > evergreen > off-season-seasonal > flat
    # A confirmed steady multi-year climb is a DURABLE trend, not a "recent surge to
    # verify" — so it outranks the weekly-data surge (which is prone to the
    # provisional-edge artifact). Confirmed = clean SV monthly, OR the Trends-weekly
    # fallback when it's VERY steady (r2 ≥ 0.80, high bar for the noisier source).
    sr_confirmed = sr.get("is_sustained_rise") and (
        sr.get("source") == "sv_monthly"
        or (sr.get("source") == "trends_weekly_resampled" and (sr.get("r2") or 0) >= 0.80)
    )
    # When CLEAN SV monthly data exists, it is the source of truth — it overrides
    # the weekly surge signal (which is prone to the provisional-edge artifact).
    # So a keyword that SV says is flat/peaked won't be mislabeled RECENT_SURGE.
    sv_clean = sr.get("source") == "sv_monthly"
    surge_trusted = surge["is_surge"] and not sv_clean
    if not is_sellable:
        primary = "NOT_SELLABLE"
    elif rt["is_realtime_breakout"]:
        primary = f"REALTIME_BREAKOUT · {rt['tier']}"
    elif sr_confirmed:
        primary = f"SUSTAINED_RISE · {sr['tier']} · {sr['stage']}"
    elif sr.get("is_peaked"):
        primary = "PEAKED · cooling"
    elif surge_trusted and not is_seasonal:
        primary = "RECENT_SURGE"
    elif sr.get("is_sustained_rise"):
        primary = f"SUSTAINED_RISE · {sr['tier']} · {sr['stage']}"
    elif is_seasonal and lead in ("OPTIMAL", "GOOD", "OK_IN_TREND", "RAMPING", "AT_PEAK_SOON"):
        primary = f"SEASONAL · {lead}"
    elif is_evergreen:
        primary = "EVERGREEN"
    elif is_seasonal:
        primary = f"SEASONAL · {lead}"
    elif surge_trusted:
        primary = "RECENT_SURGE"
    elif sr.get("is_choppy_rise"):
        primary = "SUSTAINED_RISE · choppy"
    else:
        primary = "FLAT / UNCLASSIFIED"

    return {
        "keyword": kw, "geo": geo, "primary": primary,
        "is_sellable": is_sellable, "competition_index": ci,
        "is_seasonal": is_seasonal, "is_evergreen": is_evergreen,
        "is_recent_surge": surge["is_surge"],
        "is_realtime_breakout": rt["is_realtime_breakout"],
        "is_sustained_rise": sr.get("is_sustained_rise", False),
        "is_peaked": sr.get("is_peaked", False),
        "sustained_tier": sr.get("tier"), "sustained_stage": sr.get("stage"),
        "sustained_r2": sr.get("r2"), "sustained_ann_growth": sr.get("annualized_growth_pct"),
        "sustained_source": sr.get("source"),
        "realtime_tier": rt["tier"], "realtime_spike_x": rt["spike_x"],
        "amplitude": round(ptr["amplitude"], 2),
        "phase_stdev_months": round(phase_std, 2),
        "years_used": n_years,
        "detrended_cov": round(cov, 3),
        "surge_x": surge["recent_x"], "surge_peak_date": surge.get("peak_date"),
        "peak_wk": ptr["peak_wk"], "ramp_wk": ptr["ramp_wk"], "trough_wk": ptr["trough_wk"],
        "peak_month": week_to_month_name(ptr["peak_wk"]),
        "ramp_month": week_to_month_name(ptr["ramp_wk"]),
        "current_wk": cur_wk,
        "lead_time": lead, "lead_reason": lead_reason,
    }
