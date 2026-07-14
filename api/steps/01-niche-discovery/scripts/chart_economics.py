#!/usr/bin/env python3
"""
Generate a 6-month playbook-ladder revenue/ad-spend/profit projection.
Emits PNG chart, markdown table snippet, or both.

Uses the Google Shopping Playbook formula:
  ad_cost_per_sale = CPC / CVR    (clicks-per-sale × CPC, CVR-aware)
  profit_per_sale  = retail − COGS − ad_cost_per_sale

Daily budget follows the playbook ladder:
  M1: $50/day · M2: $100/day · M3: $250/day · M4–M6: $400/day

Examples:
  # Pure-product report-style markdown table (operator preference per memory):
  python chart_economics.py --keyword "weighted sleep mask" \\
      --cpc 1.63 --retail 89.99 --cogs 15 \\
      --cvr 0.03 --no-bundles \\
      --out-md dossiers/premium-sleep-mask/economics-snippet.md

  # Re-run when COGS verifies (change --cogs only):
  python chart_economics.py --keyword "weighted sleep mask" \\
      --cpc 1.63 --retail 89.99 --cogs 18 \\
      --cvr 0.03 --no-bundles \\
      --out-md /tmp/econ.md

  # Legacy bundle-case PNG chart:
  python chart_economics.py --keyword "weighted sleep mask" \\
      --cpc 1.63 --retail 89.99 --cogs 15 \\
      --bundle-uplift 1.8 \\
      --out-png dossiers/premium-sleep-mask/economics-projection.png
"""

import argparse
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"]


# Playbook budget ladder — $/day per month
LADDER = [
    ("M1", 50),
    ("M2", 100),
    ("M3", 250),
    ("M4", 400),
    ("M5", 400),
    ("M6", 400),
]

# First few months have a learning-phase efficiency penalty
EFFICIENCY = [0.65, 0.80, 0.92, 1.00, 1.00, 1.00]


def project(cpc: float, retail: float, cogs: float, min_profit: float,
            bundle_uplift: float, cvr: float) -> dict:
    """Compute monthly projections for the playbook ladder.

    CVR-aware: ad_cost_per_sale = cpc / cvr. At cvr=0.04 (playbook default),
    this is cpc * 25 (matches the playbook formula's '25 clicks'). At cvr=0.03,
    it's cpc * 33.33 — fewer sales per ad dollar.

    Bundle math: when a customer buys a multi-pack bundle, revenue scales by
    bundle_uplift (AOV up) and COGS scales by bundle_uplift (more units shipped),
    but ad cost stays the same (one customer acquired regardless of basket size).
    """
    if cvr <= 0:
        raise ValueError(f"cvr must be > 0, got {cvr}")
    clicks_per_sale = 1.0 / cvr
    ad_cost_per_sale = cpc * clicks_per_sale

    # Per-acquired-customer profit
    profit_base = retail - cogs - ad_cost_per_sale
    profit_bundle = (retail - cogs) * bundle_uplift - ad_cost_per_sale

    months = []
    for (label, daily_budget), eff in zip(LADDER, EFFICIENCY):
        monthly_budget = daily_budget * 30
        effective_budget = monthly_budget * eff
        clicks = effective_budget / cpc
        sales = clicks * cvr  # equivalent to effective_budget / ad_cost_per_sale
        ad_spend = effective_budget

        # Base case (no bundles)
        revenue_base = sales * retail
        cogs_base = sales * cogs
        profit_base_m = revenue_base - cogs_base - ad_spend

        # Bundle case
        revenue_bundle = sales * retail * bundle_uplift
        cogs_bundle = sales * cogs * bundle_uplift
        profit_bundle_m = revenue_bundle - cogs_bundle - ad_spend

        months.append({
            "label": label,
            "daily_budget": daily_budget,
            "ad_spend": ad_spend,
            "clicks": clicks,
            "sales": sales,
            "revenue": revenue_base,
            "revenue_bundle": revenue_bundle,
            "cogs": cogs_base,
            "cogs_bundle": cogs_bundle,
            "profit_base": profit_base_m,
            "profit_bundle": profit_bundle_m,
        })
    return {
        "params": {
            "cpc": cpc, "retail": retail, "cogs": cogs,
            "min_profit": min_profit, "bundle_uplift": bundle_uplift,
            "cvr": cvr, "clicks_per_sale": clicks_per_sale,
            "ad_cost_per_sale": ad_cost_per_sale,
            "profit_per_order_base": profit_base,
            "profit_per_order_bundle": profit_bundle,
        },
        "months": months,
    }


def render_chart(proj: dict, keyword: str, out_path: str) -> None:
    months = proj["months"]
    params = proj["params"]
    labels = [m["label"] for m in months]
    n = len(labels)
    xs = np.arange(n)
    width = 0.38

    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(16, 6.5), dpi=120,
        gridspec_kw={"width_ratios": [1.6, 1]},
    )
    fig.patch.set_facecolor("white")

    # ===== LEFT PANEL: stacked-bar revenue waterfall per month =====
    ad_spend = np.array([m["ad_spend"] for m in months])
    cogs_arr = np.array([m["cogs_bundle"] for m in months])
    profit_arr = np.array([m["profit_bundle"] for m in months])
    revenue_arr = np.array([m["revenue_bundle"] for m in months])

    ax1.bar(xs, ad_spend, width=width * 2, color="#EA4335",
            label="Ad spend", edgecolor="white", linewidth=0.5)
    ax1.bar(xs, cogs_arr, bottom=ad_spend, width=width * 2, color="#FBBC04",
            label="COGS", edgecolor="white", linewidth=0.5)
    ax1.bar(xs, profit_arr, bottom=ad_spend + cogs_arr, width=width * 2,
            color="#34A853", label="Profit", edgecolor="white", linewidth=0.5)

    # Annotate total revenue on top of each bar
    for i, m in enumerate(months):
        total = m["revenue_bundle"]
        ax1.text(i, total + max(revenue_arr) * 0.02,
                 f"${total/1000:.1f}K", ha="center", va="bottom",
                 fontsize=10, color="#202124", fontweight="600")
        ax1.text(i, ad_spend[i] / 2, f"${m['daily_budget']}/d", ha="center",
                 va="center", fontsize=9, color="white", fontweight="600")

    ax1.set_xticks(xs)
    ax1.set_xticklabels(labels, fontsize=11)
    ax1.set_ylabel("Monthly revenue ($)", fontsize=11, color="#5F6368")
    ax1.set_title(f"6-month playbook ladder projection — \"{keyword}\"",
                  fontsize=14, color="#202124", fontweight="600", loc="left", pad=15)
    ax1.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f"${x/1000:.0f}K"))
    ax1.legend(loc="upper left", fontsize=10, frameon=False)
    ax1.grid(True, axis="y", linestyle="-", color="#E8EAED", linewidth=0.7)
    ax1.set_axisbelow(True)
    for side in ("top", "right"):
        ax1.spines[side].set_visible(False)
    ax1.spines["left"].set_color("#DADCE0")
    ax1.spines["bottom"].set_color("#DADCE0")
    ax1.tick_params(colors="#5F6368")

    # ===== RIGHT PANEL: cumulative profit lines =====
    cum_base = np.cumsum([m["profit_base"] for m in months])
    cum_bundle = np.cumsum([m["profit_bundle"] for m in months])

    ax2.plot(xs, cum_bundle, marker="o", markersize=8, linewidth=2.5,
             color="#34A853", label=f"With bundles (×{params['bundle_uplift']:.1f} AOV)")
    ax2.plot(xs, cum_base, marker="o", markersize=8, linewidth=2.5,
             color="#FBBC04", linestyle="--",
             label="Base case (no bundles)")

    # Annotate endpoints
    ax2.annotate(f"${cum_bundle[-1]/1000:.0f}K", xy=(xs[-1], cum_bundle[-1]),
                 xytext=(8, 5), textcoords="offset points",
                 fontsize=12, fontweight="700", color="#34A853")
    ax2.annotate(f"${cum_base[-1]/1000:.0f}K", xy=(xs[-1], cum_base[-1]),
                 xytext=(8, -15), textcoords="offset points",
                 fontsize=12, fontweight="700", color="#FBBC04")

    ax2.set_xticks(xs)
    ax2.set_xticklabels(labels, fontsize=11)
    ax2.set_ylabel("Cumulative profit ($)", fontsize=11, color="#5F6368")
    ax2.set_title("Cumulative profit (6mo)",
                  fontsize=14, color="#202124", fontweight="600", loc="left", pad=15)
    ax2.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f"${x/1000:.0f}K"))
    ax2.legend(loc="upper left", fontsize=10, frameon=False)
    ax2.grid(True, axis="y", linestyle="-", color="#E8EAED", linewidth=0.7)
    ax2.set_axisbelow(True)
    for side in ("top", "right"):
        ax2.spines[side].set_visible(False)
    ax2.spines["left"].set_color("#DADCE0")
    ax2.spines["bottom"].set_color("#DADCE0")
    ax2.tick_params(colors="#5F6368")

    # Assumptions footer — escape $ so matplotlib doesn't math-mode the text
    def e(v): return f"\\${v}"
    assumptions = (
        f"Inputs: CPC {e(format(params['cpc'], '.2f'))} | "
        f"retail {e(format(params['retail'], '.2f'))} | "
        f"COGS {e(format(params['cogs'], '.2f'))} | "
        f"ad cost/sale {e(format(params['ad_cost_per_sale'], '.2f'))} | "
        f"profit/order base {e(format(params['profit_per_order_base'], '.2f'))} -> "
        f"with bundles {e(format(params['profit_per_order_bundle'], '.2f'))}      "
        f"Ladder: {e(50)} -> {e(100)} -> {e(250)} -> {e(400)}/day | "
        "learning-phase efficiency: 65/80/92/100%"
    )
    fig.text(0.5, 0.02, assumptions, ha="center", fontsize=9,
             color="#80868b", style="italic")

    plt.subplots_adjust(top=0.92, bottom=0.12, left=0.05, right=0.97, wspace=0.20)
    plt.savefig(out_path, dpi=120, facecolor="white")
    plt.close(fig)
    print(f"Wrote {out_path}", file=sys.stderr)


def render_md(proj: dict, keyword: str, no_bundles: bool, out_path: str,
              margin_target: float = 0.25) -> None:
    """Emit a markdown table snippet for the §Economics section of a niche report.

    When `no_bundles` is True, emits the pure-product format (operator preference per
    `feedback_economics_format.md` memory). When False, includes both base + bundle
    profit columns.

    `margin_target` (default 0.25 = 25% net margin) drives the "Ad spend ceiling"
    block — shows max ad cost per sale to hit that margin, and what CVR or retail
    would need to be to satisfy it at the current CPC/COGS.
    """
    p = proj["params"]
    months = proj["months"]
    cvr_pct = p["cvr"] * 100
    cps = p["clicks_per_sale"]

    lines: list[str] = []
    lines.append(f"## 6. Economics — 6-month playbook ladder projection "
                 f"({'pure product, no bundles' if no_bundles else 'with bundle uplift'})")
    lines.append("")
    bundle_note = "" if no_bundles else f" · bundle uplift **×{p['bundle_uplift']}**"
    lines.append(
        f"**Inputs**: CPC **${p['cpc']:.2f}** (real DFS) · "
        f"retail **${p['retail']:.2f}** · "
        f"COGS **${p['cogs']:.2f}** · "
        f"CVR **{cvr_pct:.0f}%** · "
        f"learning-phase efficiency 65/80/92/100%{bundle_note}"
    )
    lines.append("")

    # Unit economics
    lines.append("### Unit economics")
    lines.append("")
    lines.append("```")
    lines.append(f"Ad cost per sale = CPC / CVR = ${p['cpc']:.2f} × {cps:.2f} "
                 f"= ${p['ad_cost_per_sale']:.2f}")
    lines.append(f"Profit per sale (base) = retail − COGS − ad cost "
                 f"= ${p['retail']:.2f} − ${p['cogs']:.2f} − ${p['ad_cost_per_sale']:.2f} "
                 f"= ${p['profit_per_order_base']:.2f}")
    if not no_bundles:
        lines.append(f"Profit per sale (bundles) = (retail − COGS) × {p['bundle_uplift']} − ad cost "
                     f"= ${p['profit_per_order_bundle']:.2f}")
    lines.append("```")
    lines.append("")
    if p["profit_per_order_base"] < p["min_profit"]:
        gap = p["min_profit"] - p["profit_per_order_base"]
        # Required retail for $30/sale min profit at this CVR + COGS
        req_retail = p["cogs"] + p["ad_cost_per_sale"] + p["min_profit"]
        lines.append(
            f"At {cvr_pct:.0f}% CVR (not the playbook's 4%), profit/sale is "
            f"**${p['profit_per_order_base']:.2f}** — **below** the playbook's "
            f"${p['min_profit']:.0f} min-profit floor by **${gap:.2f}**. To hit "
            f"${p['min_profit']:.0f}/sale at {cvr_pct:.0f}% CVR, retail needs to rise "
            f"to **${req_retail:.2f}**."
        )
        lines.append("")

    # Ad spend ceiling for target margin
    margin_pct = margin_target * 100
    max_ad_per_sale = p["retail"] * (1 - margin_target) - p["cogs"]
    current_margin = p["profit_per_order_base"] / p["retail"]
    current_margin_pct = current_margin * 100
    margin_gap_dollars = p["ad_cost_per_sale"] - max_ad_per_sale  # positive if over ceiling
    # What CVR is needed at current CPC?
    if max_ad_per_sale > 0:
        required_cvr = p["cpc"] / max_ad_per_sale
        required_cpc = max_ad_per_sale * p["cvr"]
    else:
        required_cvr = None
        required_cpc = None
    # What retail is needed at current ad cost + COGS?
    # margin_target = (retail - cogs - ad_cost) / retail
    # retail * (1 - margin_target) = cogs + ad_cost
    required_retail = (p["cogs"] + p["ad_cost_per_sale"]) / (1 - margin_target)
    # Max monthly ad spend at M4 steady-state to maintain margin
    # = sales × max_ad_per_sale
    m4_sales = months[3]["sales"]
    max_monthly_ad_at_margin = m4_sales * max_ad_per_sale

    lines.append(f"### Ad spend ceiling for {margin_pct:.0f}% net profit margin")
    lines.append("")
    lines.append("```")
    lines.append(
        f"Max ad spend per sale = retail × (1 − margin_target) − COGS"
    )
    lines.append(
        f"                      = ${p['retail']:.2f} × {1 - margin_target:.2f} − ${p['cogs']:.2f}"
    )
    lines.append(
        f"                      = ${max_ad_per_sale:.2f}"
    )
    lines.append("")
    lines.append(
        f"Current ad spend per sale (CPC ${p['cpc']:.2f} / CVR {cvr_pct:.0f}%) = ${p['ad_cost_per_sale']:.2f}"
    )
    lines.append(
        f"Current net margin (at ${p['retail']:.2f} retail, ${p['cogs']:.2f} COGS) = "
        f"{current_margin_pct:.1f}%"
    )
    lines.append("```")
    lines.append("")
    if p["ad_cost_per_sale"] <= max_ad_per_sale:
        # Within ceiling
        headroom = max_ad_per_sale - p["ad_cost_per_sale"]
        lines.append(
            f"✅ **Within ceiling.** Current ad cost ${p['ad_cost_per_sale']:.2f}/sale is "
            f"**${headroom:.2f} below** the ${max_ad_per_sale:.2f} ceiling for "
            f"{margin_pct:.0f}% margin. Current net margin = **{current_margin_pct:.1f}%**."
        )
    else:
        # Over ceiling — need to fix one of three levers
        lines.append(
            f"❌ **Over the {margin_pct:.0f}%-margin ceiling by ${margin_gap_dollars:.2f}/sale.** "
            f"Current ad cost ${p['ad_cost_per_sale']:.2f}/sale vs ceiling ${max_ad_per_sale:.2f}/sale "
            f"→ current net margin is **{current_margin_pct:.1f}%**, below target."
        )
        lines.append("")
        lines.append(f"**To hit {margin_pct:.0f}% margin, change ONE lever:**")
        lines.append("")
        lines.append("| Lever | Current | Target |")
        lines.append("|---|---:|---:|")
        if required_cvr is not None and required_cvr <= 1.0:
            lines.append(
                f"| Raise CVR (keeping CPC + retail + COGS) | "
                f"{cvr_pct:.1f}% | **{required_cvr*100:.2f}%** |"
            )
        if required_cpc is not None and required_cpc > 0:
            lines.append(
                f"| Lower CPC (keeping CVR + retail + COGS) | "
                f"${p['cpc']:.2f} | **${required_cpc:.2f}** |"
            )
        lines.append(
            f"| Raise retail (keeping CPC + CVR + COGS) | "
            f"${p['retail']:.2f} | **${required_retail:.2f}** |"
        )
    lines.append("")
    lines.append(
        f"**Max monthly ad spend at M4 cruise budget** "
        f"({m4_sales:.0f} sales/mo × ${max_ad_per_sale:.2f} ceiling) = "
        f"**${max_monthly_ad_at_margin:,.0f}/month** before margin slips below "
        f"{margin_pct:.0f}%. Current M4 ad spend = ${months[3]['ad_spend']:,.0f}/month."
    )
    lines.append("")

    # CPA-sensitivity scenarios
    lines.append("### CPA scenarios (sensitivity to ad cost per acquisition)")
    lines.append("")
    lines.append(
        "How profit changes as **CPA = ad cost per sale = CPC ÷ CVR** moves. "
        "Lower CPA means better targeting, higher CVR, or cheaper CPC. M4 column "
        "uses the playbook's M4 cruise budget split across whatever sales the CPA buys."
    )
    lines.append("")
    lines.append("| CPA | CVR (at current CPC) | Profit/sale | Margin | Sales/mo (M4) | Monthly profit (M4) | 6mo cumulative |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")

    breakeven_cpa = p["retail"] - p["cogs"]
    current_cpa = p["ad_cost_per_sale"]
    cpa_scenarios = sorted(set([30.0, 40.0, 50.0, round(current_cpa, 2), 60.0, 70.0]))
    m4_budget = months[3]["ad_spend"]

    def cumulative_at_cpa(cpa: float) -> float:
        if cpa <= 0:
            return 0.0
        prof_per_sale = p["retail"] - p["cogs"] - cpa
        return sum((m["ad_spend"] / cpa) * prof_per_sale for m in months)

    for cpa in cpa_scenarios:
        prof_per_sale = p["retail"] - p["cogs"] - cpa
        margin_pct_row = (prof_per_sale / p["retail"] * 100) if p["retail"] > 0 else 0
        sales_m4 = (m4_budget / cpa) if cpa > 0 else 0
        monthly_m4 = sales_m4 * prof_per_sale
        cumulative_6mo = cumulative_at_cpa(cpa)
        cvr_at_cpc = (p["cpc"] / cpa * 100) if cpa > 0 else 0
        is_current = abs(cpa - current_cpa) < 0.01
        b = "**" if is_current else ""
        label = f"${cpa:,.2f}"
        if is_current:
            label += " (current)"
        lines.append(
            f"| {b}{label}{b} | {b}{cvr_at_cpc:.2f}%{b} | "
            f"{b}${prof_per_sale:,.2f}{b} | {b}{margin_pct_row:.1f}%{b} | "
            f"{b}{sales_m4:,.0f}{b} | {b}${monthly_m4:,.0f}{b} | "
            f"{b}~${cumulative_6mo/1000:,.1f}K{b} |"
        )
    lines.append("")
    lines.append(
        f"**Break-even CPA** = retail − COGS = **${breakeven_cpa:.2f}**. "
        f"Above this you lose money on every sale (negative profit/sale)."
    )
    lines.append("")

    # Monthly projection table
    lines.append(f"### Monthly projection — pure product at ${p['retail']:.2f} retail, "
                 f"${p['cogs']:.2f} COGS, {cvr_pct:.0f}% CVR")
    lines.append("")
    if no_bundles:
        lines.append("| Month | Daily $ | Ad spend | Clicks | Sales | Revenue | COGS | Profit | Margin |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        tot_ad = tot_clicks = tot_sales = tot_rev = tot_cogs = tot_profit = 0.0
        for m in months:
            tot_ad += m["ad_spend"]
            tot_clicks += m["clicks"]
            tot_sales += m["sales"]
            tot_rev += m["revenue"]
            tot_cogs += m["cogs"]
            tot_profit += m["profit_base"]
            margin_m = (m["profit_base"] / m["revenue"] * 100) if m["revenue"] > 0 else 0
            lines.append(
                f"| {m['label']} | ${m['daily_budget']} | ${m['ad_spend']:,.0f} | "
                f"{m['clicks']:,.0f} | {m['sales']:,.0f} | ${m['revenue']:,.0f} | "
                f"${m['cogs']:,.0f} | **${m['profit_base']:,.0f}** | **{margin_m:.1f}%** |"
            )
        total_margin = (tot_profit / tot_rev * 100) if tot_rev > 0 else 0
        lines.append(
            f"| **6mo total** | — | **${tot_ad:,.0f}** | **{tot_clicks:,.0f}** | "
            f"**{tot_sales:,.0f}** | **${tot_rev:,.0f}** | **${tot_cogs:,.0f}** | "
            f"**${tot_profit:,.0f}** | **{total_margin:.1f}%** |"
        )
    else:
        lines.append("| Month | Daily $ | Ad spend | Clicks | Sales | Revenue (base) | "
                     "Profit base | Profit bundles |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        tot_ad = tot_clicks = tot_sales = tot_rev = tot_pb = tot_pbu = 0.0
        for m in months:
            tot_ad += m["ad_spend"]
            tot_clicks += m["clicks"]
            tot_sales += m["sales"]
            tot_rev += m["revenue"]
            tot_pb += m["profit_base"]
            tot_pbu += m["profit_bundle"]
            lines.append(
                f"| {m['label']} | ${m['daily_budget']} | ${m['ad_spend']:,.0f} | "
                f"{m['clicks']:,.0f} | {m['sales']:,.0f} | ${m['revenue']:,.0f} | "
                f"**${m['profit_base']:,.0f}** | **${m['profit_bundle']:,.0f}** |"
            )
        lines.append(
            f"| **6mo total** | — | **${tot_ad:,.0f}** | **{tot_clicks:,.0f}** | "
            f"**{tot_sales:,.0f}** | **${tot_rev:,.0f}** | "
            f"**${tot_pb:,.0f}** | **${tot_pbu:,.0f}** |"
        )
    lines.append("")

    # COGS sensitivity (steady state M4–M6, no bundles)
    lines.append("### COGS sensitivity (M4 steady state, no bundles)")
    lines.append("")
    lines.append("| COGS | Profit/sale | Monthly profit (M4) | 6mo cumulative |")
    lines.append("|---|---:|---:|---:|")
    # Recompute the ladder for each COGS scenario
    m4_sales = months[3]["sales"]
    # cumulative-month-multiplier (sum of efficiency factors normalized to M4 sales)
    eff_factors = [m["sales"] / m4_sales for m in months]  # M1..M6 relative to M4
    total_eff = sum(eff_factors)
    for c in [10, 15, 20, 25, 30]:
        prof_per_sale = p["retail"] - c - p["ad_cost_per_sale"]
        monthly_m4 = m4_sales * prof_per_sale
        cumulative = m4_sales * prof_per_sale * total_eff
        bold = "**" if c == p["cogs"] else ""
        lines.append(
            f"| {bold}${c}{bold} | {bold}${prof_per_sale:,.2f}{bold} | "
            f"{bold}${monthly_m4:,.0f}{bold} | "
            f"{bold}~${cumulative/1000:,.1f}K{bold} |"
        )
    lines.append("")

    # Threshold note
    breakeven_cogs = p["retail"] - p["ad_cost_per_sale"] - 5
    lines.append(
        f"**Critical threshold**: above **${breakeven_cogs:.0f} COGS** at "
        f"{cvr_pct:.0f}% CVR + ${p['retail']:.2f} retail, the math approaches break-even "
        f"(profit/sale falls below $5, no cushion for refunds/chargebacks). "
        f"Verify COGS via AliExpress samples — non-negotiable."
    )
    lines.append("")
    lines.append("> _To change inputs (COGS, retail, CVR, etc.): prompt Claude Code._")

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote {out_path} (markdown snippet, {len(lines)} lines)", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keyword", required=True, help="Main keyword for chart title")
    ap.add_argument("--cpc", type=float, required=True, help="Average CPC in USD")
    ap.add_argument("--retail", type=float, required=True, help="Retail price")
    ap.add_argument("--cogs", type=float, required=True, help="COGS estimate")
    ap.add_argument("--cvr", type=float, default=0.04,
                    help="Conversion rate as decimal (default 0.04 = playbook 4%%; "
                         "use 0.03 for conservative pure-product analysis)")
    ap.add_argument("--min-profit", type=float, default=30.0,
                    help="Minimum profit per order (playbook default $30)")
    ap.add_argument("--bundle-uplift", type=float, default=1.8,
                    help="Bundle AOV multiplier (default 1.8); ignored if --no-bundles")
    ap.add_argument("--no-bundles", action="store_true",
                    help="Pure-product math only; markdown output omits bundle columns")
    ap.add_argument("--margin-target", type=float, default=0.25,
                    help="Target net profit margin (default 0.25 = 25%%) for the ad-spend-ceiling block")
    ap.add_argument("--out-png", help="Optional: emit PNG chart to this path")
    ap.add_argument("--out-md", help="Optional: emit markdown table snippet to this path")
    ap.add_argument("--out", help="DEPRECATED: alias for --out-png")
    args = ap.parse_args()

    # Backward compat for --out
    out_png = args.out_png or args.out
    if not out_png and not args.out_md:
        ap.error("at least one of --out-png or --out-md must be provided")

    proj = project(args.cpc, args.retail, args.cogs,
                   args.min_profit, args.bundle_uplift, args.cvr)
    p = proj["params"]

    # Print summary table to stderr (always)
    print(f"\nPlaybook ladder projection — {args.keyword}", file=sys.stderr)
    print(f"  CPC ${args.cpc} / CVR {args.cvr*100:.0f}% = "
          f"${p['ad_cost_per_sale']:.2f} ad cost/sale ({p['clicks_per_sale']:.1f} clicks/sale)",
          file=sys.stderr)
    print(f"  Retail ${args.retail} − COGS ${args.cogs} − ad ${p['ad_cost_per_sale']:.2f} = "
          f"${p['profit_per_order_base']:.2f} profit/order base", file=sys.stderr)
    if not args.no_bundles:
        print(f"  With ×{args.bundle_uplift} bundle uplift: "
              f"${p['profit_per_order_bundle']:.2f} profit/order", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  {'Mo':<3} {'Bud/d':>6} {'AdSpd':>7} {'Clicks':>7} {'Sales':>6} "
          f"{'Rev':>8} {'COGS':>7} {'Profit':>8}", file=sys.stderr)
    for m in proj["months"]:
        print(f"  {m['label']:<3} ${m['daily_budget']:>5} ${m['ad_spend']:>6.0f} "
              f"{m['clicks']:>7.0f} {m['sales']:>6.0f} ${m['revenue']:>7.0f} "
              f"${m['cogs']:>6.0f} ${m['profit_base']:>7.0f}", file=sys.stderr)

    if out_png:
        render_chart(proj, args.keyword, out_png)
    if args.out_md:
        render_md(proj, args.keyword, args.no_bundles, args.out_md,
                  margin_target=args.margin_target)


if __name__ == "__main__":
    main()
