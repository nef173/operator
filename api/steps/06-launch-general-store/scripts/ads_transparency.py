#!/usr/bin/env python3
"""
ads_transparency.py — Google Ads Transparency Center footprint per competitor domain.

For the Best-Seller Spy (Step 06): query Google's PUBLIC Ads Transparency Center
SearchCreatives RPC for a domain and count how many ads it is currently running, split
by creative format. For a pure-Google Shopping dropship competitor the IMAGE-format
creatives ARE the Shopping / PLA product ads — the catalog they put real paid spend
behind. (decorsdeluxe.com — the AC-breakout case study — shows ~300 such ads.)

Why this matters to the spy: it tells you which tracked stores are ACTIVE Google
advertisers (the positive complement to the META-ADS=0 pure-Google gate), and roughly
how big their advertised catalog is — a winner signal you can't get from best-seller
order alone.

No auth, no AdsPower, no Bright Data, no API key, NO COST — Google's own public endpoint,
stdlib urllib only. Pages 40 creatives at a time via the response continuation token,
bounded by --max-pages.

Usage:
  ads_transparency.py --domain decorsdeluxe.com
  ads_transparency.py --domain decorsdeluxe.com --max-pages 10 --json
  ads_transparency.py --roster --out ads_transparency.json --max-pages 6 --workers 6
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

RPC_URL = "https://adstransparency.google.com/anji/_/rpc/SearchService/SearchCreatives?authuser=0"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Region code in the SearchCreatives request field 7.3. 1024 = "anywhere" (matches the
# Transparency Center default "Ads In anywhere"), which gives the full advertised count.
REGION_ANYWHERE = 1024

PAGE_SIZE = 40

# Creative format codes (response field "4" on each creative).
FORMAT_LABEL = {1: "text", 2: "image", 3: "video"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _request(domain: str, page_token: str | None, region: int) -> dict:
    req: dict = {
        "2": PAGE_SIZE,
        "3": {"12": {"1": domain, "2": True}},
        "7": {"1": 1, "2": 1, "3": region},
    }
    if page_token:
        req["4"] = page_token
    body = urllib.parse.urlencode({"f.req": json.dumps(req)}).encode()
    r = urllib.request.Request(
        RPC_URL, data=body,
        headers={"content-type": "application/x-www-form-urlencoded", "user-agent": UA},
    )
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.load(resp)


def scan_domain(domain: str, max_pages: int = 6, region: int = REGION_ANYWHERE) -> dict:
    """Count + classify a domain's live Transparency Center creatives.

    Returns a stable shape even on error:
      {domain, google_ads_count, capped, by_format{image,video,text,other},
       shopping_ads, video_ads, text_ads, other_ads, last_shown, checked_at, error}
    `shopping_ads` = image-format creatives (the PLA product ads). `capped` = True if the
    cap was hit and the true count is higher (report as "N+")."""
    domain = (domain or "").strip().lower()
    out: dict = {
        "domain": domain,
        "google_ads_count": 0,
        "capped": False,
        "by_format": {"image": 0, "video": 0, "text": 0, "other": 0},
        "shopping_ads": 0,
        "video_ads": 0,
        "text_ads": 0,
        "other_ads": 0,
        "last_shown": None,
        "checked_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "error": None,
    }
    token: str | None = None
    seen: set[str] = set()
    last_ts = 0
    try:
        for _ in range(max(1, max_pages)):
            doc = _request(domain, token, region)
            creatives = doc.get("1") or []
            for c in creatives:
                if not isinstance(c, dict):
                    continue
                cid = c.get("2") or c.get("1")
                if cid in seen:
                    continue
                seen.add(cid)
                label = FORMAT_LABEL.get(c.get("4"), "other")
                out["by_format"][label] += 1
                # field 6.1 = first-shown epoch seconds, field 7.1 = last-shown epoch
                # seconds (sub-key 2 is the nanosecond fraction, not a timestamp).
                stamps = c.get("7") or c.get("6")
                if isinstance(stamps, dict):
                    try:
                        ts = int(stamps.get("1") or 0)
                        last_ts = max(last_ts, ts)
                    except (TypeError, ValueError):
                        pass
            token = doc.get("2")
            if not token or not creatives:
                break
        else:
            # loop exhausted the page cap while a token still remained
            out["capped"] = bool(token)
    except Exception as e:  # network / JSON / HTTP — best-effort, never raise
        out["error"] = f"{type(e).__name__}: {e}"[:300]

    bf = out["by_format"]
    out["google_ads_count"] = len(seen)
    out["shopping_ads"] = bf["image"]
    out["video_ads"] = bf["video"]
    out["text_ads"] = bf["text"]
    out["other_ads"] = bf["other"]
    if last_ts:
        out["last_shown"] = dt.datetime.fromtimestamp(last_ts, dt.timezone.utc).strftime("%Y-%m-%d")
    return out


def read_roster(stores_path: str) -> list[str]:
    out: list[str] = []
    try:
        for ln in open(stores_path, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(ln.lower())
    except OSError as e:
        log(f"could not read {stores_path}: {e}")
    return out


def scan_roster(domains: list[str], max_pages: int, workers: int,
                region: int = REGION_ANYWHERE) -> dict:
    stores: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(scan_domain, d, max_pages, region): d for d in domains}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                res = fut.result()
            except Exception as e:  # pragma: no cover - scan_domain already guards
                res = {"domain": d, "google_ads_count": 0, "error": str(e)[:300]}
            stores[d] = res
            cnt = res.get("google_ads_count", 0)
            cap = "+" if res.get("capped") else ""
            err = f"  ERROR {res['error']}" if res.get("error") else ""
            log(f"  {d:<28} {cnt}{cap} ads "
                f"(shopping={res.get('shopping_ads', 0)}){err}")
    return {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": "anywhere" if region == REGION_ANYWHERE else str(region),
        "max_pages": max_pages,
        "source": "google_ads_transparency_center",
        "stores": stores,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--domain", help="scan a single domain and print the result")
    g.add_argument("--roster", action="store_true",
                   help="scan every domain in --stores and write --out")
    ap.add_argument("--stores",
                    default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "stores.txt"),
                    help="roster file (one domain per line; # = comment)")
    ap.add_argument("--out", default="ads_transparency.json",
                    help="output JSON path for --roster")
    ap.add_argument("--max-pages", type=int, default=6,
                    help="page cap (40 creatives/page); default 6 = up to 240 ads")
    ap.add_argument("--workers", type=int, default=6, help="parallel domains for --roster")
    ap.add_argument("--region", type=int, default=REGION_ANYWHERE,
                    help="Transparency Center region code (default 1024 = anywhere)")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON (single-domain mode); default is a human line")
    args = ap.parse_args()

    if args.domain:
        res = scan_domain(args.domain, args.max_pages, args.region)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            cap = "+" if res["capped"] else ""
            print(f"{res['domain']}: {res['google_ads_count']}{cap} Google ads  "
                  f"(shopping/PLA={res['shopping_ads']}, video={res['video_ads']}, "
                  f"text={res['text_ads']})  last shown {res['last_shown'] or '—'}"
                  + (f"  ERROR {res['error']}" if res["error"] else ""))
        return 0

    domains = read_roster(args.stores)
    if not domains:
        log("no domains in roster — nothing to scan")
        return 1
    log(f"scanning {len(domains)} domains (max-pages={args.max_pages}, workers={args.workers})…")
    data = scan_roster(domains, args.max_pages, args.workers, args.region)
    out_path = args.out
    if not os.path.isabs(out_path):
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), out_path)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    running = sum(1 for s in data["stores"].values() if (s.get("google_ads_count") or 0) > 0)
    log(f"wrote {out_path} — {running}/{len(domains)} domains running Google ads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
