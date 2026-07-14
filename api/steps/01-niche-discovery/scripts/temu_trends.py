#!/usr/bin/env python3
"""
Temu CATEGORY best-sellers + 7-day trend report via Apify (crw/temu-category-trend-report).
Companion to temu_apify.py (which is keyword-search best-sellers). This one = the global
"Best-Selling Items by category" view with built-in 7-day momentum deltas.

Returns per product: rank, title, price/original/discount, sales_volume + sales_tip,
rating, review_count, and 7-DAY DELTAS (rank_7d_delta, sales_7d_delta, price_7d_delta,
appearances_7d, days_in_category), momentum_status, image (thumb_url), goods_url.

CONFIG: APIFY_TOKEN in .apify.env (or env).

USAGE:
  python temu_trends.py "Arts, Crafts & Sewing" --top 30 --save out.json
  python temu_trends.py --list                       # show all categories
  python temu_trends.py 1493 --sort momentum         # by id; sort by 7d sales gain
"""
from __future__ import annotations
import argparse, json, os, sys, time, urllib.request, pathlib

ACTOR = "crw~temu-category-trend-report"
API = "https://api.apify.com/v2"
CATEGORIES = {
    "appliances": "990", "arts, crafts & sewing": "1493", "automotive": "580",
    "baby & maternity": "1167", "beachwear": "7166", "beauty & health": "25",
    "books & media": "7085", "business, industry & science": "259",
    "cell phones & accessories": "2640", "electronics & accessories": "248",
    "food & grocery": "7084", "health & household": "871", "home & kitchen": "36",
    "home gadgets & office": "202", "jewelry accessories": "352", "kids fashion": "218",
    "kids shoes": "1553", "men's bags & wallets": "731", "men's big & tall": "1232",
    "men's clothing": "67", "men's shoes": "1536", "men's underwear & sleepwear": "114",
    "musical instruments": "628", "patio, lawn & garden": "885", "pet supplies": "320",
    "smart home": "1422", "sports & outdoors": "178", "tools & home improvement": "893",
    "toys & games": "204", "women's clothing": "28", "women's curve clothing": "589",
    "women's lingerie & lounge": "1107", "women's shoes": "95",
}

def token():
    t = os.environ.get("APIFY_TOKEN")
    if not t:
        env = pathlib.Path(__file__).with_name(".apify.env")
        if env.exists():
            for ln in env.read_text().splitlines():
                if ln.startswith("APIFY_TOKEN"): t = ln.split("=", 1)[1].strip()
    if not t: raise SystemExit("No APIFY_TOKEN.")
    return t

def resolve(cat):
    if cat in CATEGORIES.values(): return cat
    key = cat.strip().lower()
    if key in CATEGORIES: return CATEGORIES[key]
    hits = [v for k, v in CATEGORIES.items() if key in k]
    if len(hits) == 1: return hits[0]
    raise SystemExit(f"Category '{cat}' not found/ambiguous. Run --list to see options.")

def run(cat_id, tok, wait=420):
    r = urllib.request.Request(f"{API}/acts/{ACTOR}/runs?token={tok}",
        data=json.dumps({"category_id": cat_id, "region": "US"}).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    rid = json.loads(urllib.request.urlopen(r, timeout=60).read())["data"]["id"]
    print(f"  run {rid} …", file=sys.stderr)
    t0 = time.time()
    while time.time() - t0 < wait:
        st = json.loads(urllib.request.urlopen(f"{API}/actor-runs/{rid}?token={tok}", timeout=60).read())["data"]
        if st["status"] == "SUCCEEDED":
            ds = st["defaultDatasetId"]
            return json.loads(urllib.request.urlopen(f"{API}/datasets/{ds}/items?token={tok}&clean=true", timeout=60).read())
        if st["status"] in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise SystemExit(f"actor {st['status']} (run {rid})")
        time.sleep(8)
    raise SystemExit("timed out")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("category", nargs="?", help="category name or id")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--sort", choices=["rank", "momentum"], default="rank",
                    help="rank = current best-sellers; momentum = biggest 7-day sales gainers")
    ap.add_argument("--save", default="")
    args = ap.parse_args()
    if args.list or not args.category:
        print("Categories:")
        for name, cid in CATEGORIES.items(): print(f"  {cid:>6}  {name.title()}")
        return
    rows = run(resolve(args.category), token())
    if args.sort == "momentum":
        rows.sort(key=lambda r: r.get("sales_7d_delta") or 0, reverse=True)
    else:
        rows.sort(key=lambda r: r.get("rank") or 9999)
    rows = rows[:args.top]
    cat = rows[0].get("category_name", args.category) if rows else args.category
    print(f"=== {cat}: {len(rows)} best-sellers [{args.sort}] (US) ===", file=sys.stderr)
    print(f"{'#':>3} {'title':44} {'price':>7} {'sold':>7} {'7d-sales':>9} {'mom':>8} img")
    for r in rows:
        print(f"{str(r.get('rank','')):>3} {str(r.get('title',''))[:44]:44} {str(r.get('price_str','')):>7} "
              f"{str(r.get('sales_tip_text','')):>7} {str(r.get('sales_7d_delta','')):>9} {str(r.get('momentum_status',''))[:8]:>8} "
              f"{'Y' if r.get('thumb_url') else '-'}")
    if args.save:
        pathlib.Path(args.save).write_text(json.dumps(rows, indent=2))
        print(f"saved {len(rows)} -> {args.save}", file=sys.stderr)

if __name__ == "__main__":
    main()
