#!/usr/bin/env python3
"""
apify_image_1688.py — PHOTO-IN reverse-image sourcing on 1688/Alibaba via Apify.

The bulk keyword engine is `alibaba_bulk.py` (text -> Alibaba offers). THIS tool is
the IMAGE lane: "I have a product photo (from a competitor listing / AliExpress /
Temu / an ad) — find that exact product on a Chinese supplier site + its wholesale
price + its full image gallery." No source URL, no keyword needed — just the image.

WHY this two-step shape (decided 2026-06-18 after hunting the whole Apify store):
There is NO clean Apify-native "1688 search-by-image" actor. The 1688 actors are all
keyword/offerId-in. The one purpose-built 拍立淘 (by-image) path is a paid managed API
(TMAPI) behind a login. So the reliable Apify-only photo-in pipeline is two actors:

  STEP A  zen-studio/google-lens-visual-search   ($7.99/1k images)
          imageUrl  ->  visual/exact matches {title, link, source, thumbnail}
          We keep ONLY matches whose link/source is a Chinese-supplier domain
          (1688 / alibaba / made-in-china / dhgate). NO prices here — just the
          matching LISTING URLs (and, for 1688, the parseable offerId).

  STEP B  zen-studio/1688-wholesale-scraper       ($3.99/1k products)   [--enrich]
          offerId  ->  50+ fields incl. tiered wholesale PRICE, supplier, and the
          FULL image gallery. We call it once, batched, for every 1688 offerId
          Step A found, to turn bare URLs into real priced candidates.

Both actors are run through the canonical Apify run-sync-get-dataset-items endpoint
(one HTTP call each, server-side). Step A is fanned out per product image in
parallel; Step B is ONE batched call for all offerIds.

AUTH: APIFY_TOKEN in env or this folder's .env  (Apify Console -> Settings -> API).

USAGE
  # photo -> matching CN-supplier listings (Step A only)
  python apify_image_1688.py --in products.json --out search_results.json

  # photo -> matching listings + real 1688 price/images (Step A + Step B enrich)
  python apify_image_1688.py --in products.json --out search_results.json --enrich

INPUT  products.json  (same shape lens_search.py / match_china.py use):
  [{"name": "...", "image": "<url>"  (or "images": ["<url>", ...]),
    "source": "meta|aliexpress|amazon|...", "slug": "..."}]

OUTPUT search_results.json  (the shape match_china.py consumes — pipe it straight in):
  [{"name", "slug", "source":"1688|alibaba|...", "site",
    "images": [<our query image(s)>],
    "candidates": [{"offer_id","title","price","currency","moq","supplier",
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
import urllib.parse
import urllib.request

APIFY_BASE = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
LENS_ACTOR = "zen-studio~google-lens-visual-search"
WS_1688_ACTOR = "zen-studio~1688-wholesale-scraper"

# Chinese-supplier domains we keep from the Lens match list. Everything else (Amazon,
# Etsy, random blogs the photo also appears on) is dropped — we only want sourceable
# CN listings.
SUPPLIER_DOMAINS = {
    "1688.com": "1688",
    "alibaba.com": "alibaba",
    "made-in-china.com": "made-in-china",
    "dhgate.com": "dhgate",
    "aliexpress.com": "aliexpress",
}
# offerId out of a 1688 detail URL:  detail.1688.com/offer/960367187839.html
OFFER_1688 = re.compile(r"1688\.com/offer/(\d{6,})\.html")
# product id out of an alibaba detail URL:  /product-detail/Foo_1601022642230.html
OFFER_ALIBABA = re.compile(r"/product-detail/[^?\s]*?_(\d{6,})\.html")


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


def apify_token() -> str:
    t = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN")
    if t:
        return t
    env = pathlib.Path(__file__).resolve().parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(("APIFY_TOKEN=", "APIFY_API_TOKEN=")):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("No APIFY_TOKEN found (env or china-source-match/scripts/.env).")


def _post(actor: str, body: dict, token: str, timeout: int = 300) -> list:
    """One Apify run-sync-get-dataset-items call -> the run's dataset items (a list)."""
    url = APIFY_BASE.format(actor=actor) + "?token=" + urllib.parse.quote(token)
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read() or "[]")
    return out if isinstance(out, list) else (out.get("items") or [])


def _domain_of(link: str) -> str | None:
    host = urllib.parse.urlparse(link or "").netloc.lower()
    for dom in SUPPLIER_DOMAINS:
        if host == dom or host.endswith("." + dom):
            return dom
    return None


def lens_matches(image_url: str, token: str, max_results: int) -> list[dict]:
    """STEP A: imageUrl -> CN-supplier matches only. Each -> a bare candidate
    {offer_id?, title, url, image, _site}; price/supplier filled by Step B."""
    items = _post(LENS_ACTOR, {
        "imageUrl": image_url,
        "searchType": "all",          # visual + exact + shoppable
        "includeAllTabs": True,
        "maxResults": max_results,
    }, token)
    # The actor returns one run-object holding visualMatches/relatedLinks lists, OR a
    # flat list of match rows depending on version — handle both defensively.
    rows: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        nested = (it.get("visualMatches") or it.get("exactMatches")
                  or it.get("matches") or it.get("relatedLinks"))
        rows.extend(nested if isinstance(nested, list) else [it])

    cands, seen = [], set()
    for m in rows:
        if not isinstance(m, dict):
            continue
        link = m.get("link") or m.get("url") or ""
        dom = _domain_of(link) or _domain_of(m.get("source") or "")
        if not dom:
            continue
        om = OFFER_1688.search(link) or OFFER_ALIBABA.search(link)
        oid = om.group(1) if om else (link.rsplit("/", 1)[-1][:40] or None)
        if not oid or oid in seen:
            continue
        seen.add(oid)
        cands.append({
            "offer_id": oid,
            "title": (m.get("title") or "")[:200],
            "price": None, "currency": None, "moq": None, "supplier": None,
            "url": link,
            "image": m.get("thumbnail") or m.get("image") or "",
            "_site": SUPPLIER_DOMAINS[dom],
        })
    return cands


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    m = re.search(r"(\d+(?:\.\d+)?)", str(v).replace(",", ""))
    return float(m.group(1)) if m else None


def enrich_1688(offer_ids: list[str], token: str) -> dict[str, dict]:
    """STEP B: ONE batched 1688-wholesale-scraper call for all offerIds -> a map
    offer_id -> {price, currency, moq, supplier, image, title}."""
    if not offer_ids:
        return {}
    items = _post(WS_1688_ACTOR, {"offerIds": offer_ids}, token, timeout=600)
    out: dict[str, dict] = {}
    for p in items:
        if not isinstance(p, dict):
            continue
        oid = str(p.get("offerId") or p.get("offer_id") or "")
        if not oid:
            continue
        price = p.get("price")
        if isinstance(price, dict):
            pval = _num(price.get("value") or price.get("min") or price.get("begin"))
            pcur = price.get("currency") or "CNY"
        elif isinstance(price, list) and price:                # tiered price breaks
            first = price[0] if isinstance(price[0], dict) else {}
            pval, pcur = _num(first.get("price") or first.get("value")), "CNY"
        else:
            pval, pcur = _num(price), "CNY"
        gallery = p.get("images") or p.get("imageGallery") or []
        img = gallery[0] if isinstance(gallery, list) and gallery else p.get("image")
        out[oid] = {
            "title": (p.get("title") or p.get("subject") or "")[:200] or None,
            "price": pval, "currency": pcur,
            "moq": _num(p.get("minOrderQuantity") or p.get("moq")),
            "supplier": p.get("companyName") or p.get("supplier") or p.get("sellerName"),
            "image": img,
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True,
                    help="products.json [{name, image|images, source, slug}]")
    ap.add_argument("--out", default="search_results.json")
    ap.add_argument("--enrich", action="store_true",
                    help="Step B: turn 1688 offerIds into real price/images "
                         "(one batched 1688-wholesale-scraper call)")
    ap.add_argument("--max-results", type=int, default=40,
                    help="max Lens matches per image before domain-filtering")
    ap.add_argument("--workers", type=int, default=8,
                    help="parallel Step-A Lens calls (one per product image)")
    args = ap.parse_args()

    token = apify_token()
    products = json.loads(pathlib.Path(args.inp).read_text())
    if not isinstance(products, list):
        raise SystemExit("products.json must be a JSON array.")

    def query_image(prod: dict) -> str | None:
        if prod.get("images"):
            return prod["images"][0]
        return prod.get("image")

    results = [{"name": p.get("name", ""), "slug": p.get("slug") or slugify(p.get("name", "")),
                "source": p.get("source", ""), "site": "",
                "images": (p.get("images") or ([p["image"]] if p.get("image") else [])),
                "candidates": []} for p in products]

    # STEP A — parallel Lens reverse-image search, one per product image.
    def run_a(i_prod):
        i, prod = i_prod
        img = query_image(prod)
        if not img:
            return i, []
        try:
            return i, lens_matches(img, token, args.max_results)
        except Exception as e:                                   # one bad image != fail all
            print(f"  ! lens failed for {prod.get('name','?')[:30]}: {e}")
            return i, []

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, cands in ex.map(run_a, list(enumerate(products))):
            results[i]["candidates"] = cands
            if cands:
                results[i]["site"] = cands[0]["_site"]

    # STEP B — one batched 1688 price/image enrich for every 1688 offerId found.
    if args.enrich:
        ids_1688 = sorted({c["offer_id"] for r in results for c in r["candidates"]
                           if c.get("_site") == "1688"})
        print(f"enriching {len(ids_1688)} 1688 offerIds (one batched call)…")
        enriched = enrich_1688(ids_1688, token)
        for r in results:
            for c in r["candidates"]:
                e = enriched.get(c["offer_id"])
                if e:
                    c.update({k: v for k, v in e.items() if v is not None})

    for r in results:                                            # drop the internal _site
        for c in r["candidates"]:
            c.pop("_site", None)

    pathlib.Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    for r in results:
        print(f"{len(r['candidates']):3d} candidates  {r['name'][:40]}")
    tot = sum(len(r["candidates"]) for r in results)
    hit = sum(1 for r in results if r["candidates"])
    print(f"\n{tot} candidates, {hit}/{len(results)} products had >=1 -> {args.out}")
    print("Next (exact-match gate): python match_china.py --in", args.out,
          "--judge openrouter --min-conf 0.85 --out matched.json")


if __name__ == "__main__":
    main()
