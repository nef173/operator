#!/usr/bin/env python3
"""
alibaba_bulk.py — BULK, PARALLEL China-sourcing product research via Alibaba.com.

This is the scale engine that WORKS on our own Bright Data account. We proved
empirically (2026-06-18) that 1688.com is blocked at every BD layer (Scraper
Studio AI-gen fails at code-gen; Web-Unlocker fetch hangs/empties — x5sec), while
**Alibaba.com is fully fetchable** through the existing `mcp_unlocker` zone
(47KB of real listing: titles, "Min. order", pieces, prices). Alibaba.com is the
same Chinese factories that AliExpress/Temu resellers source from — one tier above
1688, below AE retail — and it's English-native, so English keywords go straight in.

How it scales: you give it a list of ENGLISH keywords; it builds one Alibaba.com
search URL per keyword and hands the WHOLE list to a Bright Data Scraper Studio
collector in a single batch call (`bdata scraper run <collector> --input-file`),
which BD runs in parallel server-side and returns one merged result array. No
browser, no AdsPower, no per-product loop. BD holds the anti-bot + self-healing.

PRE-REQ: build the collector once (Scraper Studio), then reuse its id:
  bdata scraper create "https://www.alibaba.com/trade/search?SearchText=summer+jacket" \
    "<listing description>" --name alibaba-keyword-search
  -> save the printed collector_id (also stored in _scraper/create-alibaba.json)

USAGE
  # 1) just build the URL list (feed to `bdata scraper run --input-file`)
  python alibaba_bulk.py --keywords "summer jacket, knife sharpener, dog cooling mat" \
      --urls-out urls.txt

  # 2) build + run the collector in one batch + normalize to candidates
  python alibaba_bulk.py --keywords-file kws.txt --collector c_xxx --run \
      --out search_results.json

OUTPUT (search_results.json — same shape match_china.py consumes):
  [{"name": <keyword>, "slug", "source":"alibaba", "site":"alibaba",
    "candidates":[{"offer_id","title","price","currency","moq","supplier",
                   "url","image"}, ...]}, ...]

For the true 1688 DOMESTIC FLOOR on a locked SKU, fall back to the Apify
zen-studio/1688 actor (it maintains the x5sec solver BD does not).
"""
from __future__ import annotations
import argparse
import concurrent.futures as cf
import json
import os
import pathlib
import re
import subprocess
import urllib.parse

ALIBABA_SEARCH = "https://www.alibaba.com/trade/search?SearchText={kw}"
TOKEN_ENV = "Store Cloner/.env"   # BRIGHT_DATA_API_TOKEN lives here (relative to project root)


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


def read_keywords(args) -> list[str]:
    kws: list[str] = []
    if args.keywords:
        kws += [k.strip() for k in args.keywords.split(",")]
    if args.keywords_file:
        for line in pathlib.Path(args.keywords_file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                kws.append(line)
    # dedup, keep order
    seen, out = set(), []
    for k in kws:
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def build_urls(keywords: list[str], pages: int = 1) -> tuple[list[str], dict[str, str]]:
    """One search URL per (keyword × page). Returns (urls, url->keyword map).
    Production render returns ~3-4 cards/page (the grid lazy-loads the rest via
    JS that BD's crawl doesn't fully trigger), so depth comes from &page=N — each
    page yields a DISTINCT ~3-4 cards (verified 2026-06-18: pages 1-3 = 9 unique).
    All page-URLs go into the SAME batch call and bucket back to the keyword."""
    urls, url_to_kw = [], {}
    for k in keywords:
        base = ALIBABA_SEARCH.format(kw=urllib.parse.quote_plus(k))
        for p in range(1, max(1, pages) + 1):
            u = base if p == 1 else f"{base}&page={p}"
            urls.append(u)
            url_to_kw[u] = k
    return urls, url_to_kw


def bd_token() -> str:
    t = os.environ.get("BRIGHT_DATA_API_TOKEN") or os.environ.get("BRIGHTDATA_API_TOKEN")
    if t:
        return t
    # fall back to the sibling Store Cloner/.env used across this project
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "Store Cloner" / ".env"
        if cand.exists():
            for line in cand.read_text().splitlines():
                if line.startswith("BRIGHT_DATA_API_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("No BRIGHT_DATA_API_TOKEN found (env or Store Cloner/.env).")


# The WORKING Alibaba keyword-search collector (Scraper Studio, built 2026-06-18).
# Scraper Studio routes through BD's FULL-ACCESS managed crawler, which fetches
# alibaba.com fine — UNLIKE `bdata scrape`/`scrape_as_markdown` (immediate-access
# mode), which alibaba's robots.txt now blocks. So the collector is the path that
# works with NO account change. Override with --collector if you rebuild it.
COLLECTOR_DEFAULT = "c_mqj72pgy2qnjxxluwm"
DETAIL_ID = re.compile(r"/product-detail/[^?\s]*?_(\d{6,})\.html")


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    m = re.search(r"(\d+(?:\.\d+)?)", str(v).replace(",", ""))
    return float(m.group(1)) if m else None


def normalize(products: list[dict]) -> list[dict]:
    """Map the collector's structured products[] into candidate offers.
    Collector fields: title, price{value,currency,symbol}, moq (free text,
    geo-localized e.g. 'Pedido minimo: 200 Unidades'), product_url, image_url,
    supplier_name."""
    cands, seen = [], set()
    for p in products:
        if not isinstance(p, dict):
            continue
        url = p.get("product_url") or p.get("url") or ""
        oid_m = DETAIL_ID.search(url)
        oid = oid_m.group(1) if oid_m else (url.rsplit("/", 1)[-1][:40] or None)
        if not oid or oid in seen:
            continue
        seen.add(oid)
        price = p.get("price") or {}
        if isinstance(price, dict):
            pval, pcur = _num(price.get("value")), price.get("currency")
        else:
            pval, pcur = _num(price), None
        cands.append({
            "offer_id": oid,
            "title": (p.get("title") or "")[:200],
            "price": pval, "currency": pcur,
            "moq": _num(p.get("moq")), "moq_raw": p.get("moq"),
            "supplier": p.get("supplier_name") or p.get("supplier"),
            "url": url,
            "image": p.get("image_url") or p.get("image") or "",
        })
    return cands


def run_collector(collector: str, urls: list[str], token: str,
                  raw_out: str, retries: int) -> list[dict]:
    """ONE batch call: bdata scraper run <collector> --urls <csv> (BD runs all URLs
    server-side in parallel, returns one merged array of {products, input}).
    Retries the WHOLE batch while every group is empty (cold-start can return [])."""
    pathlib.Path(raw_out).parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for attempt in range(1, retries + 2):
        cmd = ["bdata", "scraper", "run", collector, "--urls", ",".join(urls),
               "--json", "-o", raw_out, "-k", token]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        raw = json.loads(pathlib.Path(raw_out).read_text() or "[]")
        rows = raw if isinstance(raw, list) else (raw.get("data") or raw.get("results") or [])
        if any((g or {}).get("products") for g in rows if isinstance(g, dict)):
            break
        print(f"  (batch empty, retry {attempt}/{retries})")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keywords", help="comma-separated English keywords")
    ap.add_argument("--keywords-file", help="one English keyword per line (# comments ok)")
    ap.add_argument("--urls-out", default="urls.txt")
    ap.add_argument("--run", action="store_true",
                    help="also run the collector (one batch call) + normalize to candidates")
    ap.add_argument("--collector", default=COLLECTOR_DEFAULT,
                    help="Scraper Studio collector_id (default = the built alibaba one)")
    ap.add_argument("--retries", type=int, default=2,
                    help="batch retries while every url-group is empty (cold-start)")
    ap.add_argument("--pages", type=int, default=1,
                    help="search pages per keyword (each &page=N adds ~3-4 DISTINCT "
                         "offers; all pages go in the same batch + bucket to the keyword)")
    ap.add_argument("--raw-out", default="_scraper/run-alibaba.json")
    ap.add_argument("--out", default="search_results.json")
    args = ap.parse_args()

    keywords = read_keywords(args)
    if not keywords:
        raise SystemExit("Give --keywords or --keywords-file.")
    urls, url_to_kw = build_urls(keywords, args.pages)
    pathlib.Path(args.urls_out).write_text("\n".join(urls) + "\n")
    print(f"{len(urls)} Alibaba search URLs ({len(keywords)} kw x {args.pages} page) "
          f"-> {args.urls_out}")

    if not args.run:
        print("Next: re-run with --run to run the collector batch + normalize.")
        return

    token = bd_token()
    rows = run_collector(args.collector, urls, token, args.raw_out, args.retries)

    # rows = [{"products":[...], "input":{"url": <search url>}}, ...] — one per URL.
    # url_to_kw maps EVERY page-URL back to its keyword (many pages -> one keyword).
    results = []
    for k in keywords:
        results.append({"name": k, "slug": slugify(k), "source": "alibaba",
                        "site": "alibaba", "candidates": []})
    by_kw = {k: r for k, r in zip(keywords, results)}
    leftover_products = []
    for g in rows if isinstance(rows, list) else []:
        if not isinstance(g, dict):
            continue
        src = (g.get("input") or {}).get("url")
        prods = g.get("products") or []
        if src in url_to_kw:
            by_kw[url_to_kw[src]]["candidates"].extend(normalize(prods))
        else:
            leftover_products.extend(prods)
    if leftover_products and len(keywords) == 1:
        results[0]["candidates"].extend(normalize(leftover_products))

    # dedup per keyword by offer_id (same offer can recur across pages of one kw)
    for r in results:
        seen, uniq = set(), []
        for c in r["candidates"]:
            oid = c.get("offer_id")
            if oid and oid in seen:
                continue
            seen.add(oid)
            uniq.append(c)
        r["candidates"] = uniq

    pathlib.Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    for r in results:
        print(f"{len(r['candidates']):3d} offers  {r['name'][:40]}")
    tot = sum(len(r["candidates"]) for r in results)
    hit = sum(1 for r in results if r["candidates"])
    print(f"\n{tot} candidates, {hit}/{len(keywords)} keywords had >=1 -> {args.out}")
    if tot == 0:
        print("0 results: collector returned empty for every URL. Raise --retries, or "
              "the collector may need a re-heal (bdata scraper heal", args.collector + ").")
    print("Next (optional exact-match gate): python match_china.py --in", args.out,
          "--judge openrouter --min-conf 0.85 --out matched.json")


if __name__ == "__main__":
    main()
