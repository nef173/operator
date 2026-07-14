#!/usr/bin/env python3
"""
Fully-automated Temu research via Apify — the ONLY method that works (2026-06-15).

Temu walls everything else: its API = 403 (anti-content token), search = login wall,
product = slide CAPTCHA. BrightData (Web Unlocker, Browser API, datasets, Scraper Studio)
ALL fail. Most free Apify Temu actors are broken too (amit123 = API 403, piotrv1001 = 422).
The WORKING one = `crw/temu-products-scraper` (updated daily) — region US/GB/BR/JP, sort by
top_sales (= Temu "Best-Selling Items" view), returns price + sold count + rating + reviews
+ IMAGE URL + product/seller links. Apify handles session + CAPTCHA server-side.

CONFIG: APIFY_TOKEN in 01-niche-discovery/scripts/.apify.env (or env var).

USAGE:
  python temu_apify.py "diamond painting kit" --sort top_sales --region US --max 40 --save out.json
  sort: relevance | top_sales (best-selling) | most_recent | price_low_to_high | price_high_to_low
"""
from __future__ import annotations
import argparse, json, os, sys, time, urllib.request, pathlib

ACTOR = "crw~temu-products-scraper"
API = "https://api.apify.com/v2"

def token():
    t = os.environ.get("APIFY_TOKEN")
    if not t:
        env = pathlib.Path(__file__).with_name(".apify.env")
        if env.exists():
            for ln in env.read_text().splitlines():
                if ln.startswith("APIFY_TOKEN"):
                    t = ln.split("=", 1)[1].strip()
    if not t:
        raise SystemExit("No APIFY_TOKEN (set env or .apify.env).")
    return t

def _post(url, body, tok):
    r = urllib.request.Request(url + ("&" if "?" in url else "?") + "token=" + tok,
                               data=json.dumps(body).encode(), method="POST",
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=60).read())

def _get(url, tok):
    sep = "&" if "?" in url else "?"
    return json.loads(urllib.request.urlopen(url + sep + "token=" + tok, timeout=60).read())

def run(keyword, region, sort, max_items, tok, wait=420):
    # `maxItems` is the RUN-level pay-per-result cap — the crw~temu actor now REQUIRES it (400
    # "max-items-must-be-greater-than-zero" without it), separate from the `max_items` input field.
    started = _post(f"{API}/acts/{ACTOR}/runs?maxItems={max_items}", {
        "keyword": keyword, "region": region, "sort": sort, "max_items": max_items}, tok)
    rid = started["data"]["id"]
    print(f"  run {rid} …", file=sys.stderr)
    t0 = time.time()
    while time.time() - t0 < wait:
        st = _get(f"{API}/actor-runs/{rid}", tok)["data"]
        if st["status"] == "SUCCEEDED":
            ds = st["defaultDatasetId"]
            return _get(f"{API}/datasets/{ds}/items?clean=true&limit={max_items}", tok)
        if st["status"] in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise SystemExit(f"actor {st['status']} — check Apify console run {rid}")
        time.sleep(8)
    raise SystemExit("timed out")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword")
    ap.add_argument("--region", default="US", choices=["US", "GB", "BR", "JP"])
    ap.add_argument("--sort", default="top_sales",
                    choices=["relevance", "top_sales", "most_recent", "price_low_to_high", "price_high_to_low"])
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--save", default="")
    args = ap.parse_args()
    rows = run(args.keyword, args.region, args.sort, args.max, token())
    print(f"=== {len(rows)} Temu products — {args.keyword!r} [{args.region}, {args.sort}] ===", file=sys.stderr)
    print(f"{'title':46} {'price':>8} {'was':>8} {'sold':>9}  rating  img")
    for r in rows:
        print(f"{str(r.get('title',''))[:46]:46} {str(r.get('price_str','')):>8} {str(r.get('market_price_str','')):>8} "
              f"{str(r.get('sales_tip','')).replace(' sold',''):>9}  {str(r.get('rating','')):>5}  {'Y' if r.get('image_url') else '-'}")
    if args.save:
        pathlib.Path(args.save).write_text(json.dumps(rows, indent=2))
        print(f"saved {len(rows)} -> {args.save}", file=sys.stderr)

if __name__ == "__main__":
    main()
