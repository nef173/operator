#!/usr/bin/env python3
"""
lens_search.py — BULK, server-side reverse-IMAGE search for China sourcing.

Step 1 of the china-source-match pipeline (the fast/bulk engine, replacing the
one-by-one AdsPower browser path). For EVERY researched product image it asks
Bright Data's SERP **Google Lens** endpoint for `exact_matches` /
`visual_matches`, keeps only hits on Chinese-supplier domains (Alibaba.com /
Made-in-China / 1688 / DHgate / AliExpress), and writes the candidate table that
`match_china.py` then VLM-validates.

WHY image, not keyword: products come from AliExpress/Amazon/Temu/ad-spy with
English titles that translate poorly to the Chinese marketplace; the image is the
exact-product signal. WHY Lens (not AdsPower): one HTTP call per image, all
products in PARALLEL, server-side, managed solver — no browser, no login.

WHY Alibaba.com over 1688 here: Google Lens indexes Alibaba.com / Made-in-China
strongly and 1688 weakly (1688 is a domestic CN site Google barely crawls), but
Alibaba.com is the SAME factories your agent sources from — 1688 is just the
cheaper tier of those same suppliers.

INPUT  (products.json):
  [{"name":"...", "image":"<url or local path>",
    "source":"aliexpress|temu|meta|amazon|google", "slug":"..."}, ...]
  (also accepts "images":[...] for multi-image queries)

OUTPUT (search_results.json — consumed verbatim by match_china.py):
  [{"name","slug","source","site":"lens","images":[query imgs],
    "candidates":[{"offer_id","title","price","currency","supplier","url",
                   "image"}, ...]}, ...]

ENV (.env): BRIGHTDATA_CUSTOMER_ID, BRIGHTDATA_SERP_ZONE,
            BRIGHTDATA_SERP_ZONE_PASSWORD   (a SERP zone — NOT the residential zone)

USAGE
  python lens_search.py --in products.json --out search_results.json \
      --tabs exact_matches,visual_matches --workers 8
  python lens_search.py --in products.json --raw-dir ./_lens_raw   # keep raw JSON to tune the parser
"""
from __future__ import annotations
import argparse
import concurrent.futures as cf
import json
import os
import pathlib
import re
import urllib.parse

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

PROXY_HOST = "brd.superproxy.io:33335"

# A hit on one of these domains = "a Chinese factory makes this / my agent can source it".
SUPPLIER_DOMAINS = {
    "alibaba.com": "alibaba",
    "made-in-china.com": "made-in-china",
    "1688.com": "1688",
    "dhgate.com": "dhgate",
    "aliexpress.com": "aliexpress",
    "globalsources.com": "globalsources",
}


def _proxies() -> dict:
    cid = os.environ.get("BRIGHTDATA_CUSTOMER_ID", "")
    zone = os.environ.get("BRIGHTDATA_SERP_ZONE", "")
    pw = os.environ.get("BRIGHTDATA_SERP_ZONE_PASSWORD", "")
    if not (cid and zone and pw):
        raise SystemExit(
            "Missing BD SERP creds. Set BRIGHTDATA_CUSTOMER_ID, BRIGHTDATA_SERP_ZONE, "
            "BRIGHTDATA_SERP_ZONE_PASSWORD in .env (a SERP zone, not the residential one)."
        )
    user = f"brd-customer-{cid}-zone-{zone}"
    auth = f"http://{user}:{pw}@{PROXY_HOST}"
    return {"http": auth, "https": auth}


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


def _supplier_for(url: str) -> str | None:
    host = urllib.parse.urlparse(url).netloc.lower()
    for dom, name in SUPPLIER_DOMAINS.items():
        if host == dom or host.endswith("." + dom):
            return name
    return None


def _lens_call(image: str, tab: str, proxies: dict, raw_dir: pathlib.Path | None,
               slug: str) -> dict:
    """One Google Lens call for one image + one tab. Returns parsed JSON ({} on error).
    Image can be a remote URL (uploadbyurl) or a local path (v3/upload POST)."""
    headers = {"x-unblock-data-format": "parsed_light"}
    try:
        if image.startswith(("http://", "https://", "//")):
            if image.startswith("//"):
                image = "https:" + image
            url = ("https://lens.google.com/uploadbyurl?url="
                   + urllib.parse.quote(image, safe="")
                   + f"&brd_json=1&brd_lens={tab}")
            r = requests.get(url, proxies=proxies, headers=headers, timeout=120, verify=False)
        else:  # local file -> POST upload
            p = pathlib.Path(image)
            if not p.exists():
                return {}
            with p.open("rb") as fh:
                r = requests.post(
                    f"https://lens.google.com/v3/upload?brd_json=1&brd_lens={tab}",
                    files={"encoded_image": (p.name, fh, "image/jpeg")},
                    proxies=proxies, headers=headers, timeout=120, verify=False)
        body = r.text
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"{slug}.{tab}.json").write_text(body)
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return json.loads(body[body.find("{"): body.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return {}


def _iter_matches(parsed: dict, tab: str):
    """Yield raw match dicts from a Lens JSON response, tolerant of schema drift:
    the parser tries the tab key, then a few common container keys."""
    if not isinstance(parsed, dict):
        return
    for key in (tab, "exact_matches", "visual_matches", "products", "matches",
                "organic", "results"):
        v = parsed.get(key)
        if isinstance(v, list):
            for m in v:
                if isinstance(m, dict):
                    yield m


def _norm(m: dict) -> dict | None:
    """Pull a candidate out of one Lens match dict (defensive about field names)."""
    url = (m.get("page_url") or m.get("link") or m.get("url") or m.get("source_url")
           or m.get("redirect_link") or "")
    supplier = _supplier_for(url) if url else None
    if not supplier:
        return None
    price = m.get("price")
    currency = None
    if isinstance(price, dict):
        currency = price.get("currency")
        price = price.get("value") or price.get("extracted_value")
    elif isinstance(price, str):
        mm = re.search(r"([\d,]+\.?\d*)", price.replace(",", ""))
        if "US$" in price or "$" in price:
            currency = "USD"
        price = float(mm.group(1)) if mm else None
    img = (m.get("image") or m.get("thumbnail") or m.get("image_url")
           or m.get("source_icon") or "")
    if isinstance(img, dict):
        img = img.get("link") or img.get("url") or ""
    offer = re.search(r"/(?:offer|item|product[s]?)/(\d+)", url)
    return {
        "offer_id": offer.group(1) if offer else url.rsplit("/", 1)[-1][:40],
        "title": (m.get("title") or m.get("name") or "")[:200],
        "price": price, "currency": currency,
        "supplier": supplier, "url": url,
        "image": img if isinstance(img, str) else "",
    }


def search_product(prod: dict, tabs: list[str], proxies: dict,
                   raw_dir: pathlib.Path | None) -> dict:
    slug = prod.get("slug") or slugify(prod.get("name", "product"))
    imgs = prod.get("images") or ([prod["image"]] if prod.get("image") else [])
    cands, seen = [], set()
    for image in imgs[:2]:           # 1-2 query images is plenty
        for tab in tabs:
            parsed = _lens_call(image, tab, proxies, raw_dir, slug)
            for m in _iter_matches(parsed, tab):
                c = _norm(m)
                if c and c["url"] and c["url"] not in seen:
                    seen.add(c["url"])
                    cands.append(c)
    return {"name": prod.get("name"), "slug": slug, "source": prod.get("source"),
            "site": "lens", "images": imgs, "candidates": cands}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--out", default="search_results.json")
    ap.add_argument("--tabs", default="exact_matches,visual_matches",
                    help="comma list: exact_matches,visual_matches,products")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--raw-dir", default="", help="keep raw Lens JSON here to tune the parser")
    args = ap.parse_args()

    products = json.loads(pathlib.Path(args.infile).read_text())
    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()]
    proxies = _proxies()
    raw_dir = pathlib.Path(args.raw_dir) if args.raw_dir else None

    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(search_product, p, tabs, proxies, raw_dir): p for p in products}
        for fut in cf.as_completed(futs):
            r = fut.result()
            results.append(r)
            n_sup = len({c["supplier"] for c in r["candidates"]})
            print(f"{len(r['candidates']):3d} cands ({n_sup} suppliers)  "
                  f"{(r.get('name') or '')[:50]}")

    pathlib.Path(args.out).write_text(json.dumps(results, indent=2))
    total = sum(len(r["candidates"]) for r in results)
    hit = sum(1 for r in results if r["candidates"])
    print(f"\n{hit}/{len(results)} products have >=1 China-supplier candidate  "
          f"({total} candidates total) -> {args.out}")
    print("Next: python match_china.py --in", args.out,
          "--judge openrouter --min-conf 0.85 --out matched.json")


if __name__ == "__main__":
    main()
