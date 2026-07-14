#!/usr/bin/env python3
"""
Fully-automated Temu scraping via the BrightData **Web Scraper / Dataset API**.

BrightData runs the Temu collector server-side — it handles the trusted session,
the anti-content token, AND the CAPTCHA. So this is hands-off: no AdsPower, no
sign-in, no CAPTCHA solving on our end. We just trigger → poll → download.

PREREQUISITES (one-time, on the BrightData dashboard — see README at bottom):
  1. A token with **Dataset API** permission (the Web-Unlocker token is NOT enough —
     verified: /customer/balance returns 403 "API key lacks permissions").
     Create/upgrade at  https://brightdata.com/cp/setting/users
  2. Subscribe to a **Temu** dataset (Web Scraper library → search "Temu") and copy
     its dataset_id (looks like `gd_xxxxxxxxxxxx`).

CONFIG (env or flags):
  BRIGHT_DATA_DATASET_TOKEN   dataset-scoped token (falls back to BRIGHT_DATA_API_TOKEN)
  BRIGHT_DATA_TEMU_DATASET    the Temu dataset_id (or pass --dataset)

USAGE:
  python temu_dataset.py --dataset gd_xxxx --by keyword "diamond painting kit"
  python temu_dataset.py --dataset gd_xxxx --by url "https://www.temu.com/goods.html?goods_id=..."
  (--by keyword uses discover_new/keyword; --by url collects specific product URLs)
"""
from __future__ import annotations
import argparse, json, os, sys, time, urllib.request, urllib.parse, pathlib

API = "https://api.brightdata.com/datasets/v3"

def _token():
    t = os.environ.get("BRIGHT_DATA_DATASET_TOKEN") or os.environ.get("BRIGHT_DATA_API_TOKEN")
    if not t:
        # last resort: read Store Cloner/.env
        env = pathlib.Path(__file__).resolve().parents[2] / "Store Cloner" / ".env"
        if env.exists():
            for ln in env.read_text().splitlines():
                if ln.startswith("BRIGHT_DATA_DATASET_TOKEN") or ln.startswith("BRIGHT_DATA_API_TOKEN"):
                    t = ln.split("=", 1)[1].strip()
    if not t:
        raise SystemExit("No BrightData token (set BRIGHT_DATA_DATASET_TOKEN).")
    return t

def _req(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Authorization", f"Bearer {token}")
    r.add_header("Content-Type", "application/json")
    try:
        return json.loads(urllib.request.urlopen(r, timeout=60).read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"BrightData {e.code}: {e.read().decode()[:300]}")

def trigger(dataset_id, token, inputs, by):
    # discover-by-keyword vs collect-by-url
    q = {"dataset_id": dataset_id, "include_errors": "true"}
    if by == "keyword":
        q.update({"type": "discover_new", "discover_by": "keyword"})
    url = f"{API}/trigger?{urllib.parse.urlencode(q)}"
    out = _req("POST", url, token, inputs)
    sid = out.get("snapshot_id") or out.get("snapshotId")
    if not sid:
        raise SystemExit(f"No snapshot_id returned: {out}")
    return sid

def wait(sid, token, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = _req("GET", f"{API}/progress/{sid}", token)
        st = p.get("status")
        print(f"  snapshot {sid}: {st} ({p.get('rows', p.get('records','?'))} rows)", file=sys.stderr)
        if st == "ready":
            return True
        if st in ("failed", "error"):
            raise SystemExit(f"snapshot failed: {p}")
        time.sleep(10)
    raise SystemExit("timed out waiting for snapshot")

def fetch(sid, token):
    return _req("GET", f"{API}/snapshot/{sid}?format=json", token)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="keyword(s) or url(s)")
    ap.add_argument("--dataset", default=os.environ.get("BRIGHT_DATA_TEMU_DATASET", ""))
    ap.add_argument("--by", choices=["keyword", "url"], default="keyword")
    ap.add_argument("--save", default="")
    args = ap.parse_args()
    if not args.dataset:
        raise SystemExit("Pass --dataset gd_xxxx (the Temu dataset_id) or set BRIGHT_DATA_TEMU_DATASET.")
    token = _token()
    key = "keyword" if args.by == "keyword" else "url"
    inputs = [{key: v} for v in args.inputs]
    print(f"triggering {args.dataset} ({args.by}): {args.inputs}", file=sys.stderr)
    sid = trigger(args.dataset, token, inputs, args.by)
    wait(sid, token)
    rows = fetch(sid, token)
    print(f"=== {len(rows)} Temu records ===", file=sys.stderr)
    for r in (rows if isinstance(rows, list) else [])[:40]:
        title = r.get("title") or r.get("goods_name") or r.get("name") or ""
        price = r.get("price") or r.get("final_price") or ""
        sold = r.get("sold") or r.get("sales") or r.get("units_sold") or ""
        print(f"  {str(title)[:50]:50}  {str(price):>9}  sold {sold}")
    if args.save:
        pathlib.Path(args.save).write_text(json.dumps(rows, indent=2))
        print(f"saved -> {args.save}", file=sys.stderr)

if __name__ == "__main__":
    main()
