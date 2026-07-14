#!/usr/bin/env python3
"""
bestseller_diff.py — Competitor Best-Seller Spy, diff stage (Step 06).

Compares two daily snapshots for a store (or all stores in a snapshots dir) and
emits the MOVERS: products that newly entered or rose in the best-seller ranking.

Mover classes:
  - new     : in latest, absent in prior (or prior had it below tracked depth)
  - gainer  : rank improved (smaller rank number) by >= --min-jump
  - faller  : rank worsened by >= --min-jump
  - reentry : present now, was present earlier-than-prior but absent in prior
  - steady  : present both days, |delta| < --min-jump  (excluded unless --include-steady)

Winner signal: a "new" or "gainer" whose created_at is recent (fresh product being
scaled) is the strongest list-now candidate -> flagged is_fresh + days_old.

Usage:
  bestseller_diff.py --snapshots snapshots --store creative.lighting
  bestseller_diff.py --snapshots snapshots                 # all stores, latest vs prior
  bestseller_diff.py --snapshots snapshots --prior 2026-06-15 --latest 2026-06-16
  bestseller_diff.py --snapshots snapshots --out movers.json
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def load(path):
    with open(path) as f:
        return json.load(f)


def snapshot_dates(store_dir):
    files = sorted(glob.glob(os.path.join(store_dir, "*.json")))
    return [(os.path.splitext(os.path.basename(p))[0], p) for p in files]


def days_old(created_at, ref_date):
    if not created_at:
        return None
    try:
        created = dt.datetime.fromisoformat(created_at).date()
        return (ref_date - created).days
    except ValueError:
        return None


def diff_store(store_dir, prior_date=None, latest_date=None,
               min_jump=1, fresh_days=45, include_steady=False):
    dates = snapshot_dates(store_dir)
    if len(dates) < 2 and not (prior_date and latest_date):
        return None, "need >=2 snapshots (have %d)" % len(dates)

    by_date = dict(dates)
    if latest_date is None:
        latest_date = dates[-1][0]
    if prior_date is None:
        # most recent date strictly before latest
        earlier = [d for d, _ in dates if d < latest_date]
        if not earlier:
            return None, "no snapshot before %s" % latest_date
        prior_date = earlier[-1]

    if latest_date not in by_date or prior_date not in by_date:
        return None, "missing snapshot (%s or %s)" % (prior_date, latest_date)

    latest = load(by_date[latest_date])
    prior = load(by_date[prior_date])
    ref = dt.date.fromisoformat(latest_date)

    prior_rank = {p["handle"]: p["rank"] for p in prior["products"]}
    # Observed depth of the PRIOR snapshot: a product can only legitimately be
    # "new" if its current rank is within the depth we actually captured prior.
    # Otherwise (current rank deeper than prior depth) we simply never observed
    # that slot yesterday -> it's "unobserved-prior", NOT a real new entry.
    # This prevents a depth change (e.g. 20 -> 30) from faking 10 "new" movers.
    prior_depth = prior.get("count", len(prior["products"]))
    movers = []
    for p in latest["products"]:
        h = p["handle"]
        new_rank = p["rank"]
        old_rank = prior_rank.get(h)
        d_old = days_old(p.get("created_at"), ref)
        is_fresh = d_old is not None and d_old <= fresh_days

        if old_rank is None:
            if new_rank > prior_depth:
                klass, delta = "unobserved_prior", None
            else:
                klass, delta = "new", None
        else:
            delta = old_rank - new_rank  # positive = rose
            if delta >= min_jump:
                klass = "gainer"
            elif delta <= -min_jump:
                klass = "faller"
            else:
                klass = "steady"

        if klass == "steady" and not include_steady:
            continue
        if klass == "unobserved_prior" and not include_steady:
            continue

        movers.append({
            "handle": h,
            "title": p.get("title"),
            "class": klass,
            "rank": new_rank,
            "prior_rank": old_rank,
            "rank_delta": delta,           # +rose / -fell / None=new
            "created_at": p.get("created_at"),
            "days_old": d_old,
            "is_fresh": is_fresh,
            "price": p.get("price"),
            "image": p.get("image"),
            "url": p.get("url"),
        })

    # Sort: new+fresh first, then gainers by biggest rise, then the rest
    klass_rank = {"new": 0, "gainer": 1, "reentry": 2, "faller": 3, "steady": 4}
    movers.sort(key=lambda m: (
        klass_rank.get(m["class"], 9),
        not m["is_fresh"],
        -(m["rank_delta"] or 0) if m["class"] == "gainer" else 0,
        m["rank"],
    ))
    result = {
        "store": latest["store"],
        "prior_date": prior_date,
        "latest_date": latest_date,
        "latest_count": latest["count"],
        "prior_count": prior["count"],
        "comparable_depth": min(prior_depth, latest.get("count", len(latest["products"]))),
        "movers": movers,
        "summary": {
            "new": sum(1 for m in movers if m["class"] == "new"),
            "gainer": sum(1 for m in movers if m["class"] == "gainer"),
            "faller": sum(1 for m in movers if m["class"] == "faller"),
            "fresh_winners": sum(1 for m in movers
                                 if m["is_fresh"] and m["class"] in ("new", "gainer")),
        },
    }
    return result, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", default="snapshots", help="snapshots root dir")
    ap.add_argument("--store", default=None, help="single store (default: all stores)")
    ap.add_argument("--prior", default=None, help="prior date YYYY-MM-DD (default: auto)")
    ap.add_argument("--latest", default=None, help="latest date YYYY-MM-DD (default: newest)")
    ap.add_argument("--min-jump", type=int, default=1, help="min rank change to count")
    ap.add_argument("--fresh-days", type=int, default=45,
                    help="created_at <= N days = fresh winner flag")
    ap.add_argument("--include-steady", action="store_true")
    ap.add_argument("--out", default=None, help="write combined JSON here")
    args = ap.parse_args()

    if args.store:
        stores = [args.store]
    else:
        stores = [os.path.basename(d.rstrip("/"))
                  for d in glob.glob(os.path.join(args.snapshots, "*"))
                  if os.path.isdir(d)]
        # respect stores.txt — don't emit movers for dropped stores' leftover snapshots
        roster_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stores.txt")
        if os.path.isfile(roster_path):
            roster = {ln.strip().lower() for ln in open(roster_path)
                      if ln.strip() and not ln.strip().startswith("#")}
            if roster:
                stores = [s for s in stores if s.lower() in roster]

    all_results = []
    for store in sorted(stores):
        store_dir = os.path.join(args.snapshots, store)
        res, err = diff_store(store_dir, args.prior, args.latest,
                              args.min_jump, args.fresh_days, args.include_steady)
        if err:
            log(f"  ✗ {store}: {err}")
            continue
        s = res["summary"]
        log(f"  ✓ {store} ({res['prior_date']}→{res['latest_date']}): "
            f"{s['new']} new, {s['gainer']} gainers, {s['fresh_winners']} fresh winners")
        all_results.append(res)

    out = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "stores": all_results,
        "totals": {
            "stores_with_movers": len(all_results),
            "new": sum(r["summary"]["new"] for r in all_results),
            "gainers": sum(r["summary"]["gainer"] for r in all_results),
            "fresh_winners": sum(r["summary"]["fresh_winners"] for r in all_results),
        },
    }
    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        log(f"\nwrote {args.out}")
    else:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
