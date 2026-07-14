#!/usr/bin/env python3
"""
tmapi_1688.py — NATIVE 1688 sourcing-research via TMAPI (tmapi.top).

This is the ONLY purpose-built native-1688 path in china-source-match. Unlike the
Apify/Bright-Data lanes (which screen-scrape Alibaba.com or Google Lens), TMAPI is a
managed API that runs 1688's OWN 拍立淘 (search-by-image / image-search), keyword
search, and item-detail behind one apiToken — so it returns true 1688 domestic
listings with tiered wholesale prices, MOQ, supplier, and the full image gallery.

ROLE (unchanged sourcing model): 1688 results are RESEARCH-ONLY — a reference price
FLOOR + spec-truth + "does a CN factory make this", NOT a supplier and NOT true COGS
(the operator's private agent adds her markup + shipping on top). Cite the offer_id
as a research reference; the private agent is the actual supplier.

WHY TMAPI over the Apify two-step (decided 2026-06-18): there is NO Apify-native
1688-by-image actor; the Apify photo lane needs Google-Lens + a separate enrich actor
($7.99/1k + $3.99/1k) and never returns the native 拍立淘 match set. TMAPI does it in
ONE call and also gives keyword search + full item detail under the same token.

API MECHANICS (reverse-engineered from tmapi.top/openapi.json, all verified live):
  base host  https://api.tmapi.top   (GET unless noted; auth = ?apiToken=<token>)
  - GET  /1688/item_detail            item_id=, language=en, scene=drop_shipping
  - GET  /1688/search/items           keyword=, page=, page_size= (<=20)
  - GET  /1688/search/image           img_url=  (Ali-hosted URL ONLY)
  - POST /1688/tools/image/convert_url  body {url, search_api_endpoint=/search/image}
        -> converts a NON-Ali photo (Amazon/Temu/competitor/Meta-ad) into an
           Ali-hosted URL so /search/image can match it. Image-search REJECTS any
           non-Ali url ("Only images from Alibaba-affiliated platforms..."), so this
           client auto-converts whenever the input host isn't an Ali CDN.
  SSL: api.tmapi.top sits behind a Tencent QCloud CDN whose cert is *.cdn.myqcloud.com
       -> hostname mismatch -> we MUST disable cert verification for this host.

AUTH: TMAPI_TOKEN in env or this folder's .env (TMAPI Console -> Account Center).
The 1688 bundle is metered per CALL (~3 calls = 1 validated product). Other platforms
(amazon/* etc.) are SEPARATE balances and return 439 "Insufficient API balance".

USAGE
  # one offer's full detail (spec-truth + tiered price + gallery)
  python tmapi_1688.py --detail 960367187839

  # keyword -> 1688 offers (research breadth)
  python tmapi_1688.py --keyword "dog cooling mat" --page-size 20

  # ONE photo -> native 1688 image matches (auto-converts a non-Ali url)
  python tmapi_1688.py --image "https://m.media-amazon.com/images/I/....jpg"

  # BATCH photo lane (the shape match_china.py consumes — pipe it straight in)
  python tmapi_1688.py --in products.json --out search_results.json

INPUT  products.json  (same shape lens_search.py / apify_image_1688.py use):
  [{"name": "...", "image": "<url>" (or "images": ["<url>", ...]),
    "source": "meta|aliexpress|amazon|temu|...", "slug": "..."}]

OUTPUT search_results.json:
  [{"name","slug","source":"1688","site":"1688","images":[<query image(s)>],
    "candidates":[{"offer_id","title","price","currency","moq","supplier",
                   "url","image"}, ...]}, ...]

Next (exact-match gate):
  python match_china.py --in search_results.json --judge openrouter --min-conf 0.85
"""
from __future__ import annotations
import argparse
import concurrent.futures as cf
import json
import os
import pathlib
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.tmapi.top"
# Ali CDN / domain hosts whose images /search/image accepts WITHOUT conversion.
ALI_HOSTS = ("alicdn.com", "alibaba.com", "1688.com", "aliimg.com")

# api.tmapi.top is fronted by a Tencent QCloud CDN with a shared *.cdn.myqcloud.com
# cert -> hostname mismatch. Verification must be disabled for THIS host only.
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


def tmapi_token() -> str:
    t = os.environ.get("TMAPI_TOKEN")
    if t:
        return t
    env = pathlib.Path(__file__).resolve().parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("TMAPI_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("No TMAPI_TOKEN found (env or china-source-match/scripts/.env).")


def _request(path: str, token: str, params: dict | None = None,
             body: dict | None = None, timeout: int = 60) -> dict:
    """One TMAPI call -> parsed JSON dict. GET unless `body` is given (then POST)."""
    q = dict(params or {})
    q["apiToken"] = token
    url = BASE + path + "?" + urllib.parse.urlencode(q)
    if body is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"code": e.code, "msg": raw[:300]}


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    m = re.search(r"(\d+(?:\.\d+)?)", str(v).replace(",", ""))
    return float(m.group(1)) if m else None


def _is_ali(url: str) -> bool:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    return any(host == h or host.endswith("." + h) for h in ALI_HOSTS)


def convert_url(non_ali_url: str, token: str) -> str | None:
    """POST /1688/tools/image/convert_url — turn a NON-Ali photo into an Ali-hosted
    URL that /search/image will accept. Returns the converted url, or None."""
    r = _request("/1688/tools/image/convert_url", token,
                 body={"url": non_ali_url, "search_api_endpoint": "/search/image"})
    if r.get("code") != 200:
        return None
    d = r.get("data")
    if isinstance(d, dict):
        return d.get("url") or d.get("img_url") or d.get("image_url")
    return d if isinstance(d, str) else None


def _normalize(it: dict) -> dict | None:
    """A keyword/image search item -> the standard candidate shape. Both endpoints
    share: item_id, product_url, title, img, price, price_info, currency, moq,
    shop_info{shop_name}."""
    if not isinstance(it, dict):
        return None
    oid = str(it.get("item_id") or "")
    if not oid:
        return None
    pinfo = it.get("price_info") or {}
    price = _num(it.get("price") or pinfo.get("price") or pinfo.get("sale_price"))
    shop = it.get("shop_info") or {}
    return {
        "offer_id": oid,
        "title": (it.get("title") or "")[:200],
        "price": price,
        "currency": it.get("currency") or "CNY",
        "moq": _num(it.get("moq") or it.get("quantity_begin")),
        "supplier": shop.get("shop_name") or shop.get("seller_login_id"),
        "url": it.get("product_url") or f"https://detail.1688.com/offer/{oid}.html",
        "image": it.get("img") or "",
    }


def search_image(img_url: str, token: str, page_size: int = 20) -> list[dict]:
    """ONE photo -> native 1688 image matches. Auto-converts a non-Ali url first."""
    q_url = img_url
    if not _is_ali(img_url):
        conv = convert_url(img_url, token)
        if not conv:
            return []
        q_url = conv
    r = _request("/1688/search/image", token,
                 {"img_url": q_url, "page_size": min(page_size, 20)})
    items = (r.get("data") or {}).get("items") or []
    out = [c for it in items if (c := _normalize(it))]
    return out


def search_keyword(keyword: str, token: str, page: int = 1,
                   page_size: int = 20) -> list[dict]:
    r = _request("/1688/search/items", token,
                 {"keyword": keyword, "page": page,
                  "page_size": min(page_size, 20), "language": "en"})
    items = (r.get("data") or {}).get("items") or []
    return [c for it in items if (c := _normalize(it))]


def item_detail(item_id: str, token: str) -> dict:
    r = _request("/1688/item_detail", token,
                 {"item_id": str(item_id), "language": "en",
                  "scene": "drop_shipping"})
    return r.get("data") or {}


def enrich(cand: dict, token: str) -> dict:
    """Attach the FULL gallery + variants (color/size SKUs) + specs to a candidate
    via item_detail. A search result carries only ONE thumbnail; a multi-variant
    listing (e.g. 80 SKUs across 10 colors) hides the matching colorway behind a hero
    image of a DIFFERENT variant — so judging the thumbnail alone false-negatives a
    real match. Enriched candidates let the match judge see every variant + spec."""
    oid = cand.get("offer_id")
    if not oid:
        return cand
    try:
        d = item_detail(oid, token)
    except Exception:
        return cand
    if not d:
        return cand
    variants = []
    for p in d.get("sku_props") or []:
        name = p.get("prop_name") or p.get("name") or ""
        vals = [v.get("name") or v.get("value") for v in (p.get("values") or [])]
        vals = [v for v in vals if v]
        if name and vals:
            variants.append({"name": name, "values": vals})
    specs = {}
    for pr in d.get("product_props") or []:
        if isinstance(pr, dict):
            specs.update(pr)
    pinfo = d.get("price_info") or {}
    cand.update({
        "title": d.get("title") or cand.get("title"),
        "images": (d.get("main_imgs") or [])[:8],      # full gallery, hero first
        "variants": variants,
        "specs": specs,
        "price_min": _num(pinfo.get("price_min")),
        "price_max": _num(pinfo.get("price_max")),
        "sold": _num(d.get("sale_count")),
        "stock": _num(d.get("stock")),
    })
    return cand


def enrich_all(cands: list[dict], token: str, workers: int = 4) -> list[dict]:
    """Enrich a candidate list in parallel (one item_detail call each)."""
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(lambda c: enrich(c, token), cands))


def _query_image(prod: dict) -> str | None:
    if prod.get("images"):
        return prod["images"][0]
    return prod.get("image")


def run_batch(products: list[dict], token: str, page_size: int,
              workers: int, do_enrich: bool = False) -> list[dict]:
    results = [{"name": p.get("name", ""),
                "slug": p.get("slug") or slugify(p.get("name", "")),
                "source": "1688", "site": "1688",
                "images": (p.get("images") or
                           ([p["image"]] if p.get("image") else [])),
                "candidates": []} for p in products]

    def run_one(i_prod):
        i, prod = i_prod
        img = _query_image(prod)
        if not img:
            return i, []
        try:
            return i, search_image(img, token, page_size)
        except Exception as e:                       # one bad image != fail all
            print(f"  ! image search failed for {prod.get('name', '?')[:30]}: {e}")
            return i, []

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, cands in ex.map(run_one, list(enumerate(products))):
            results[i]["candidates"] = cands

    if do_enrich:
        for r in results:
            if r["candidates"]:
                r["candidates"] = enrich_all(r["candidates"], token, workers)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--detail", metavar="ITEM_ID",
                   help="fetch one 1688 offer's full detail (spec-truth)")
    g.add_argument("--keyword", help="keyword -> 1688 offers")
    g.add_argument("--image", help="ONE photo url -> native 1688 image matches")
    g.add_argument("--in", dest="inp",
                   help="batch products.json [{name, image|images, source, slug}]")
    ap.add_argument("--out", default="search_results.json")
    ap.add_argument("--page-size", type=int, default=20, help="max 20")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel image-search calls in batch mode")
    ap.add_argument("--enrich", action="store_true",
                    help="attach full gallery + variants + specs to each candidate "
                         "(one item_detail call/candidate) so the match judge sees "
                         "every colorway, not just the search thumbnail. RECOMMENDED "
                         "before match_china.py for multi-variant listings.")
    args = ap.parse_args()

    token = tmapi_token()

    if args.detail:
        d = item_detail(args.detail, token)
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return

    if args.keyword:
        cands = search_keyword(args.keyword, token, page_size=args.page_size)
        if args.enrich:
            cands = enrich_all(cands, token, args.workers)
        print(json.dumps(cands, indent=2, ensure_ascii=False))
        print(f"\n{len(cands)} offers for '{args.keyword}'")
        return

    if args.image:
        cands = search_image(args.image, token, page_size=args.page_size)
        if args.enrich:
            cands = enrich_all(cands, token, args.workers)
        print(json.dumps(cands, indent=2, ensure_ascii=False))
        print(f"\n{len(cands)} 1688 image matches")
        return

    # batch photo lane
    products = json.loads(pathlib.Path(args.inp).read_text())
    if not isinstance(products, list):
        raise SystemExit("products.json must be a JSON array.")
    results = run_batch(products, token, args.page_size, args.workers,
                        do_enrich=args.enrich)
    pathlib.Path(args.out).write_text(
        json.dumps(results, indent=2, ensure_ascii=False))
    for r in results:
        print(f"{len(r['candidates']):3d} candidates  {r['name'][:40]}")
    tot = sum(len(r["candidates"]) for r in results)
    hit = sum(1 for r in results if r["candidates"])
    print(f"\n{tot} candidates, {hit}/{len(results)} products had >=1 -> {args.out}")
    print("Next (exact-match gate): python match_china.py --in", args.out,
          "--judge openrouter --min-conf 0.85 --out matched.json")


if __name__ == "__main__":
    main()
