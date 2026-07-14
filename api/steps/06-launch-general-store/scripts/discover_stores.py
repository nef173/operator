#!/usr/bin/env python3
"""
discover_stores.py — find general Google-Shopping stores to feed the Best-Seller Spy.

TrendTrack `search_shops` is the discovery engine, but it's an MCP (only the agent
can call it, not a script) AND its results are huge. So the division of labour is:

  1. AGENT calls TrendTrack search_shops (filtered to US market + min_monthly_visits)
     and SAVES the raw JSON dump(s) to a file.
  2. THIS SCRIPT ingests those dump(s), applies the precise filters that matter for
     the spy, dedupes against stores.txt, ranks, and emits candidates.
  3. manage_stores.py --add validates each is a live Shopify store + appends.

Filters (all tunable):
  --min-visits      monthly visits floor (default 30000)
  --mom             require month-over-month traffic INCREASE >= this fraction
                    (default 0.0 = any increase; 0.10 = +10% MoM). Computed from
                    traffic.history (last month vs previous month).
  --mom-months      how many trailing months must be ascending (default 1 = just
                    last>prev; 2 = last two steps ascending = more durable)
  --max-meta-ads    keep "Google" stores: cap active Meta ads (default 50;
                    high Meta ads = a Meta-driven store, not a Google store)
  --min-products    general store = broad catalog (default 100; general stores
                    carry hundreds-thousands of SKUs)
  --min-us          minimum US traffic share 0-1 (default 0.5)

Usage:
  # agent saved a dump to /tmp/tt_dump.json:
  discover_stores.py --in /tmp/tt_dump.json --min-visits 30000 --mom 0.0
  discover_stores.py --in /tmp/tt_dump.json --mom 0.10 --mom-months 2 --out candidates.tsv
  discover_stores.py --in dump1.json dump2.json --emit-add   # prints a ready manage_stores cmd
"""
import argparse
import json
import os
import sys


def load_shops(paths):
    """Accept TrendTrack search_shops dumps (full response) or bare arrays."""
    shops = []
    for p in paths:
        with open(p) as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            rows = doc.get("data") or doc.get("shops") or []
        elif isinstance(doc, list):
            rows = doc
        else:
            rows = []
        shops.extend(rows)
    return shops


import re

# A Google-Shopping product-feed app (Simprosys Multi Feeds etc.) is a CONFIRMING
# signal that a store runs the Google channel. TrendTrack surfaces installed apps
# inconsistently (operator: "may not always show, but most of time must"), so this
# is informational — surfaced as a column, never a hard filter.
FEED_APP_TOKENS = re.compile(
    r"\b(simprosys|multiple? ?google ?shopping ?feeds?|google ?shopping ?feed|"
    r"multi ?feeds?|feed ?for ?google ?shopping|datafeedwatch|data ?feed ?watch|"
    r"cedcommerce|adnabu|nabu|sales ?& ?orders|koongo|feedarmy|socialshop|"
    r"clever ?ads|productfeeds?|shopping ?feed|merchant ?center ?feed|"
    r"google ?& ?youtube|google ?channel)\b", re.I)


def shop_apps(shop):
    """Best-effort list of installed app names from a TrendTrack shop record."""
    for key in ("apps", "installedApps", "technologies", "techStack"):
        v = shop.get(key)
        if isinstance(v, list):
            return [a.get("name", a) if isinstance(a, dict) else a for a in v]
    return []


def feed_app_of(shop):
    """Return the matched Google-feed app name, or '' if none/unknown."""
    for a in shop_apps(shop):
        m = FEED_APP_TOKENS.search(str(a or ""))
        if m:
            return (a if isinstance(a, str) else m.group(0)).strip()
    return ""


def us_share(shop):
    tc = (shop.get("traffic") or {}).get("topCountries") or []
    for c in tc:
        if c.get("countryCode") == "US":
            return c.get("share", 0) or 0
    return 0


def mom_series(history):
    """Return list of month-over-month ratios (newest last). history newest last."""
    vals = [h.get("value", 0) for h in (history or [])]
    out = []
    for a, b in zip(vals, vals[1:]):
        out.append((b - a) / a if a else None)
    return vals, out


def ascending_ok(vals, moms, min_mom, months):
    """Last `months` steps each rising AND last step >= min_mom."""
    if len(moms) < months:
        return False
    tail = moms[-months:]
    if any(m is None for m in tail):
        return False
    if any(m <= 0 for m in tail):
        return False
    return moms[-1] >= min_mom


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inputs", nargs="+", required=True,
                    help="TrendTrack search_shops dump file(s)")
    ap.add_argument("--min-visits", type=int, default=30000)
    ap.add_argument("--mom", type=float, default=0.0,
                    help="required month-over-month increase fraction (0.10 = +10%%)")
    ap.add_argument("--mom-months", type=int, default=1,
                    help="trailing months that must be ascending")
    ap.add_argument("--max-meta-ads", type=int, default=50)
    ap.add_argument("--min-products", type=int, default=100)
    ap.add_argument("--min-us", type=float, default=0.5)
    ap.add_argument("--stores", default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "stores.txt"))
    ap.add_argument("--out", default=None, help="write TSV here (default stdout)")
    ap.add_argument("--emit-add", action="store_true",
                    help="print a ready-to-run manage_stores.py --add command")
    args = ap.parse_args()

    # existing tracked domains
    tracked = set()
    if os.path.isfile(args.stores):
        with open(args.stores) as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    tracked.add(ln.lower())

    shops = load_shops(args.inputs)
    seen, cands = set(), []
    for s in shops:
        dom = (s.get("domain") or "").lower().strip()
        if not dom or dom in seen or dom in tracked:
            continue
        seen.add(dom)
        tr = s.get("traffic") or {}
        visits = tr.get("monthlyVisits") or 0
        ads = (s.get("advertising") or {}).get("activeAds") or 0
        prods = (s.get("catalog") or {}).get("productsCount") or 0
        us = us_share(s)
        vals, moms = mom_series(tr.get("history"))

        if visits < args.min_visits:
            continue
        if not ascending_ok(vals, moms, args.mom, args.mom_months):
            continue
        if ads > args.max_meta_ads:
            continue
        if prods < args.min_products:
            continue
        if us < args.min_us:
            continue

        cands.append({
            "domain": dom,
            "visits": visits,
            "mom_last": round((moms[-1] or 0) * 100),
            "us": round(us * 100),
            "meta_ads": ads,
            "products": prods,
            "feed_app": feed_app_of(s) or "?",
            "created": (s.get("createdAt") or "")[:7],
            "history": vals,
        })

    cands.sort(key=lambda c: c["visits"], reverse=True)

    lines = ["domain\tvisits\tMoM%\tUS%\tmeta_ads\tproducts\tfeed_app\tcreated"]
    for c in cands:
        lines.append(f"{c['domain']}\t{c['visits']}\t+{c['mom_last']}%\t"
                     f"{c['us']}%\t{c['meta_ads']}\t{c['products']}\t{c['feed_app']}\t{c['created']}")
    text = "\n".join(lines)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"wrote {args.out} ({len(cands)} candidates)", file=sys.stderr)
    else:
        print(text)

    print(f"\n{len(cands)} candidates pass "
          f"(visits>={args.min_visits}, MoM>=+{int(args.mom*100)}% x{args.mom_months}mo, "
          f"meta_ads<={args.max_meta_ads}, products>={args.min_products}, "
          f"US>={int(args.min_us*100)}%)", file=sys.stderr)
    if args.emit_add and cands:
        doms = " ".join(c["domain"] for c in cands)
        print(f"\n.venv/bin/python manage_stores.py --add {doms} "
              f'--note "TrendTrack discovery" --snapshot-now', file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
