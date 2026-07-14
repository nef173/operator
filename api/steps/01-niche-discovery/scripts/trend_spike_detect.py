#!/usr/bin/env python3
"""
Trend Spike Detection
─────────────────────
Reads existing trends.json (output of trends_dfs.py) and classifies each keyword's
recent demand pattern as one of:

  SPIKE_ACTIVE     — peak within last 4 weeks, current still ≥1.5× pre-spike baseline
  SPIKE_RECENT     — peak within last 16 weeks, retracing but current still >baseline
  SUSTAINED_SHIFT  — post-spike baseline ≥1.3× pre-spike baseline (real demand expansion)
  FLASH            — spike happened but fully reverted to baseline
  FLAT             — no significant spike in last 16 weeks

Why this exists (the miss it fixes — see feedback_trend_spike_detection_skill.md):
The discover-niches Stage 4 trend_verdict logic averages 5-year slope, which
dilutes recent 4-week breakouts. Dog stroller April 2026 spiked from baseline 15
to peak 100 (6.7×) while trend_verdict read "flat" because the 5-year smooth
diluted the recent explosion. This detector explicitly looks at the LAST 16
weeks as a separate signal, compared to a pre-spike baseline window.

Usage:
  # Inspect all keywords in a trends.json:
  python trend_spike_detect.py --trends-json dossiers/<niche>/trends.json

  # Filter to one keyword:
  python trend_spike_detect.py --trends-json ... --keyword "dog stroller"

  # Human-readable markdown summary:
  python trend_spike_detect.py --trends-json ... --keyword "dog stroller" --markdown

  # JSON output to file:
  python trend_spike_detect.py --trends-json ... --out spike-report.json
"""
import argparse
import json
import sys
from statistics import mean


# Window sizes (in weeks). Tuned to match the dog-stroller catalyst pattern
# (Jan 2026 NYT article → March-April 2026 spike). 16-week recent window captures
# the full ramp + peak + retrace; 36-week pre-window captures a clean baseline
# without overlap.
RECENT_WINDOW = 16
PRE_WINDOW_END = 20  # gap of 4 weeks between pre-window and recent-window
PRE_WINDOW_START = 56  # 36 weeks of pre-baseline data
MIN_DATA_POINTS = 60  # need at least this many weeks of data to classify

# Thresholds
SUSTAINED_PEAK_X = 2.0     # peak vs pre-baseline to consider a spike
SUSTAINED_POST_X = 1.3     # post-spike baseline vs pre-baseline = sustained shift
ACTIVE_CURRENT_X = 1.5     # current vs pre-baseline for SPIKE_ACTIVE
RECENT_CURRENT_X = 1.2     # current vs pre-baseline for SPIKE_RECENT
ACTIVE_PEAK_WEEKS = 4      # peak within last N weeks = ACTIVE


def classify_spike(series: list[float], dates: list[str]) -> dict:
    """Return verdict + metrics for one keyword's interest series."""
    n = len(series)
    if n < MIN_DATA_POINTS:
        return {
            "verdict": "INSUFFICIENT_DATA",
            "reason": f"only {n} data points (need ≥{MIN_DATA_POINTS})",
            "recommendation": "no_action",
            "investigation_required": False,
        }

    # Slice windows
    recent = series[-RECENT_WINDOW:]
    recent_dates = dates[-RECENT_WINDOW:]
    pre_window = series[-PRE_WINDOW_START:-PRE_WINDOW_END]
    pre_baseline = mean(pre_window) if pre_window else 0.0

    # Peak in recent window
    peak_value = max(recent)
    peak_idx = recent.index(peak_value)
    peak_date = recent_dates[peak_idx]
    weeks_since_peak = (len(recent) - 1) - peak_idx

    # Current
    current_value = series[-1]
    current_date = dates[-1]

    # Post-spike baseline: avg of recent weeks AFTER (peak_idx + 4)
    # Meaningful only when peak was at least 4 weeks ago AND we have ≥4 post-peak observations
    post_baseline = None
    post_window = recent[peak_idx + 4:]
    if weeks_since_peak >= 4 and len(post_window) >= 4:
        post_baseline = mean(post_window)

    # Ratios (guard against divide-by-zero)
    def ratio(num, den):
        return round(num / den, 2) if den and den > 0 else None

    current_vs_baseline = ratio(current_value, pre_baseline)
    peak_vs_baseline = ratio(peak_value, pre_baseline)
    post_vs_pre = ratio(post_baseline, pre_baseline) if post_baseline is not None else None

    # Classification (priority order; first match wins)
    if (post_vs_pre is not None and post_vs_pre >= SUSTAINED_POST_X
            and peak_vs_baseline is not None and peak_vs_baseline >= SUSTAINED_PEAK_X):
        verdict = "SUSTAINED_SHIFT"
        reason = (
            f"post-spike baseline {post_baseline:.0f} is {post_vs_pre}× pre-spike "
            f"baseline {pre_baseline:.0f} (peak {peak_value:.0f}, {weeks_since_peak}w ago)"
        )
        recommendation = "launch_asap"
        investigation_required = True

    elif (weeks_since_peak <= ACTIVE_PEAK_WEEKS
          and current_vs_baseline is not None and current_vs_baseline >= ACTIVE_CURRENT_X):
        verdict = "SPIKE_ACTIVE"
        reason = (
            f"peak {peak_value:.0f} only {weeks_since_peak}w ago; "
            f"current {current_value:.0f} = {current_vs_baseline}× baseline {pre_baseline:.0f}"
        )
        recommendation = "launch_asap_validate_persistence"
        investigation_required = True

    elif (peak_vs_baseline is not None and peak_vs_baseline >= SUSTAINED_PEAK_X
          and current_vs_baseline is not None and current_vs_baseline >= RECENT_CURRENT_X):
        verdict = "SPIKE_RECENT"
        reason = (
            f"peak {peak_value:.0f} {weeks_since_peak}w ago ({peak_vs_baseline}× baseline); "
            f"retracing — current {current_value:.0f} = {current_vs_baseline}× baseline"
        )
        recommendation = (
            "launch_asap_capture_afterglow"
            if current_vs_baseline >= 1.5 else "validate_baseline_persistence"
        )
        investigation_required = True

    elif (peak_vs_baseline is not None and peak_vs_baseline >= SUSTAINED_PEAK_X
          and current_vs_baseline is not None and current_vs_baseline < RECENT_CURRENT_X):
        verdict = "FLASH"
        reason = (
            f"spike to {peak_value:.0f} ({peak_vs_baseline}×) reverted to "
            f"{current_value:.0f} (back at baseline {pre_baseline:.0f})"
        )
        recommendation = "skip"
        investigation_required = False

    else:
        verdict = "FLAT"
        reason = f"no spike: peak {peak_value:.0f} vs baseline {pre_baseline:.0f} ({peak_vs_baseline}×)"
        recommendation = "no_action"
        investigation_required = False

    return {
        "verdict": verdict,
        "reason": reason,
        "recommendation": recommendation,
        "investigation_required": investigation_required,
        "metrics": {
            "data_points": n,
            "pre_spike_baseline": round(pre_baseline, 2),
            "peak_value": peak_value,
            "peak_date": peak_date,
            "weeks_since_peak": weeks_since_peak,
            "current_value": current_value,
            "current_date": current_date,
            "current_vs_baseline_ratio": current_vs_baseline,
            "peak_vs_baseline_ratio": peak_vs_baseline,
            "post_spike_baseline": round(post_baseline, 2) if post_baseline else None,
            "post_vs_pre_ratio": post_vs_pre,
        },
        "recent_window": [(d, v) for d, v in zip(recent_dates, recent)],
    }


def print_markdown_summary(keyword: str, geo: str, result: dict) -> None:
    """Print a human-readable spike report."""
    v = result["verdict"]
    m = result.get("metrics") or {}
    print(f"\n## Spike Check — {keyword} ({geo})")
    print()
    print(f"**Verdict:** `{v}`")
    print(f"**Reason:** {result['reason']}")
    print(f"**Recommendation:** `{result['recommendation']}`")
    print(f"**Catalyst investigation needed:** {'YES' if result.get('investigation_required') else 'no'}")
    print()
    if not m:
        return
    print("### Metrics")
    print(f"- Pre-spike baseline: **{m['pre_spike_baseline']}** (36-week pre-window)")
    print(f"- Peak: **{m['peak_value']}** on **{m['peak_date']}** ({m['weeks_since_peak']}w ago)")
    print(f"- Peak vs baseline: **{m['peak_vs_baseline_ratio']}×**")
    print(f"- Current ({m['current_date']}): **{m['current_value']}** ({m['current_vs_baseline_ratio']}× baseline)")
    if m.get("post_spike_baseline"):
        print(f"- Post-spike baseline (≥4w after peak): **{m['post_spike_baseline']}** ({m['post_vs_pre_ratio']}× pre-spike) — sustained-shift evidence")
    print()
    print("### Recent 16-week window")
    print()
    print("| Date | Interest | Bar |")
    print("|---|---|---|")
    for d, val in result["recent_window"]:
        bar = "█" * int(val / 2) if val else ""
        print(f"| {d} | {val:.0f} | {bar} |")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--trends-json", required=True,
                   help="Path to trends.json (output of trends_dfs.py)")
    p.add_argument("--keyword", default=None,
                   help="Filter to one keyword (case-insensitive)")
    p.add_argument("--out", default=None,
                   help="Output JSON path (otherwise stdout)")
    p.add_argument("--markdown", action="store_true",
                   help="Print markdown summary instead of JSON")
    args = p.parse_args()

    with open(args.trends_json) as f:
        records = json.load(f)
    if not isinstance(records, list):
        print("Error: expected trends.json to be a list of records", file=sys.stderr)
        return 1

    results = []
    for rec in records:
        kw = rec.get("keyword", "")
        geo = rec.get("geo", "US")
        if args.keyword and kw.lower() != args.keyword.lower():
            continue
        series = rec.get("raw_series") or []
        dates = rec.get("raw_dates") or []
        result = classify_spike(series, dates)
        result["keyword"] = kw
        result["geo"] = geo
        results.append(result)

    if args.markdown:
        for r in results:
            print_markdown_summary(r["keyword"], r["geo"], r)
        return 0

    out_data = results[0] if len(results) == 1 else results
    js = json.dumps(out_data, indent=2, default=str)
    if args.out:
        with open(args.out, "w") as f:
            f.write(js)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(js)
    return 0


if __name__ == "__main__":
    sys.exit(main())
