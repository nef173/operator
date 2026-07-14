#!/usr/bin/env python3
"""
FAST AliExpress candidate extractor for the batch-supplier-sourcing pipeline.

WHY: AliExpress ITEM pages are silently blocked on the Web-Unlocker (Empty 200),
and the one-by-one scraping-browser is the throughput bottleneck. But AliExpress
SEARCH/LISTING pages (`/w/wholesale-<term>.html`) render fine through the Bright
Data **MCP** Web-Unlocker zone, and `scrape_batch` renders up to 10 of them per
call (~15-20 products each => ~150-200 candidates per call, in seconds).

This script does NOT call the network. It PARSES the file that the MCP
`scrape_batch` / `scrape_as_markdown` tool auto-saves when the result is large,
turning ~450KB of listing markdown into a compact, deduped candidate table:
  {item_id, title, price, compare_at, sold, currency, url, image}

PIPELINE:
  1. agent calls MCP scrape_batch on N `/w/wholesale-<form-factor>.html` URLs
     (sort by orders: append `?SortType=total_tranpro_desc`)
  2. MCP saves the big result to tool-results/<...>.txt
  3. python parse_ae_listing.py <that-file> --min-sold 50 --save candidates.json

USAGE:
  python parse_ae_listing.py <saved-tool-result.txt> [--min-sold N] [--save out.json] [--top N]
  # also accepts a raw scrape_as_markdown text file or a JSON file.
"""
from __future__ import annotations
import argparse, json, re, sys, pathlib


def _load_pages(path: str) -> list[dict]:
    """Return [{url, content}] from whatever the MCP saved.
    Handles: outer [{type,text}] wrapper whose text is a JSON string of
    [{url,content}]; a bare [{url,content}] JSON; or raw markdown text."""
    raw = pathlib.Path(path).read_text()
    # try outer JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [{"url": "(raw)", "content": raw}]

    # outer [{type,text}] -> text is itself JSON
    if isinstance(data, list) and data and isinstance(data[0], dict) and "text" in data[0]:
        pages = []
        for blk in data:
            t = blk.get("text", "")
            try:
                inner = json.loads(t)
                if isinstance(inner, list):
                    pages.extend(inner)
                elif isinstance(inner, dict):
                    pages.append(inner)
            except json.JSONDecodeError:
                pages.append({"url": "(raw)", "content": t})
        return pages
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return [{"url": "(raw)", "content": raw}]


_SOLD = re.compile(r"([\d,.]+)\s*([kK]?)\+?\s*sold", re.I)
_PRICE = re.compile(r"(?:C?\$|US\s*\$|€|£)\s*([\d,]+\.\d{2})")
_ITEM = re.compile(r"/item/(\d+)\.html")
_IMG = re.compile(r"https://(?:ae01\.alicdn\.com|ae-pic-a1\.aliexpress-media\.com)/kf/[^\s\)\"']+?\.(?:jpg|png|webp|avif)", re.I)


def _sold_to_int(num: str, k: str) -> int:
    n = float(num.replace(",", ""))
    if k.lower() == "k":
        n *= 1000
    return int(n)


def parse_page(md: str) -> list[dict]:
    """Split a listing markdown into product cards by the /item/<id>.html links,
    then pull title/price/compare/sold/image from the text block of each card."""
    out, seen = [], set()
    # Each product is a markdown link block ending in (//www.aliexpress.com/item/<id>.html?...)
    # Split on the item links, keep the text that PRECEDED each link (that's the card body).
    parts = re.split(r"\]\(//www\.aliexpress\.com/item/(\d+)\.html", md)
    # parts = [pre, id1, between1, id2, between2, ...] -> card text for id_k is parts[2k-1? ] ; simpler: iterate
    # Rebuild: the card BODY for id at index i is the text chunk parts[i*2] (before that id)
    for k in range(1, len(parts), 2):
        item_id = parts[k]
        body = parts[k - 1]
        # For every card after the first, parts[k-1] begins with the PREVIOUS link's
        # URL tail "?params)"; the first ')' closes that link, and everything after it
        # is THIS card's markdown. (The first card has no preceding link.)
        if k > 1:
            cut = body.find(")")
            if cut != -1:
                body = body[cut + 1:]
        if item_id in seen:
            continue
        seen.add(item_id)

        prices = _PRICE.findall(body)
        price = compare_at = None
        if prices:
            vals = [float(p.replace(",", "")) for p in prices]
            price = min(vals)
            hi = max(vals)
            compare_at = hi if hi > price else None
        sold_m = _SOLD.search(body)
        sold = _sold_to_int(*sold_m.groups()) if sold_m else 0
        # Prefer a REAL product thumbnail (has a size/quality suffix); skip lazy
        # placeholder sprites (the repeated tiny S5efe...e2e hash with no size tag).
        imgs = [u for u in _IMG.findall(body) if "_480x" in u or "_640x" in u or "_960x" in u or "q75" in u]
        imgs = imgs or _IMG.findall(body)
        # title = the AliExpress card opens "[Title Title Title ...]" repeated; the
        # readable title is the longest alphabetic run before the first '](' .
        head = body.split("](", 1)[0]
        cand = re.findall(r"[A-Za-z][A-Za-z0-9 ,&/'\-]{20,}", head)
        title = max(cand, key=len).strip() if cand else ""
        title = re.sub(r"\s+", " ", title)[:160]

        out.append({
            "item_id": item_id,
            "url": f"https://www.aliexpress.com/item/{item_id}.html",
            "title": title,
            "price": price,
            "compare_at": compare_at,
            "sold": sold,
            "image": imgs[0] if imgs else None,
        })
    return out


_PID = re.compile(r'"productId":"(\d+)"')


def parse_html_island(html: str) -> list[dict]:
    """PREFERRED source: AliExpress listing HTML embeds a per-product JSON island
    (60 products/page) with REAL image URLs — unlike markdown, where product
    thumbnails are lazy-loaded and only sprite PNGs render. For each productId,
    pull image.imgUrl, title.displayTitle, the prices block (sale = min, compare-at
    = max), and trade.tradeDesc (sold)."""
    out = []
    starts = [m.start() for m in _PID.finditer(html)]
    for idx, s in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else min(s + 4000, len(html))
        w = html[s:end]
        item_id = _PID.search(w).group(1)
        img = re.search(r'"image":\{"imgUrl":"([^"]+)"', w)
        image = img.group(1) if img else None
        if image and image.startswith("//"):
            image = "https:" + image
        title = re.search(r'"displayTitle":"([^"]+)"', w)
        title = title.group(1) if title else ""
        try:
            title = title.encode().decode("unicode_escape")
        except (UnicodeDecodeError, ValueError):
            pass  # malformed escape (e.g. a lone trailing backslash) — keep the raw title, don't crash
        # prices block: collect minPrice numbers (sale = min, original/compare = max)
        pblock = re.search(r'"prices":\{.*?(?="trade"|"image"|\}\},)', w, re.S)
        pvals = [float(x) for x in re.findall(r'"minPrice":([\d.]+)', pblock.group(0))] if pblock else []
        if not pvals:
            pvals = [float(x) for x in re.findall(r'"minPrice":([\d.]+)', w)]
        price = min(pvals) if pvals else None
        compare_at = max(pvals) if pvals and max(pvals) > min(pvals) else None
        sold_m = re.search(r'"tradeDesc":"([\d,.]+)\s*([kK]?)\+?\s*sold', w, re.I)
        sold = _sold_to_int(*sold_m.groups()) if sold_m else 0
        out.append({
            "item_id": item_id,
            "url": f"https://www.aliexpress.com/item/{item_id}.html",
            "title": re.sub(r"\s+", " ", title)[:160],
            "price": price, "compare_at": compare_at, "sold": sold,
            "image": image,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--min-sold", type=int, default=0)
    ap.add_argument("--top", type=int, default=0, help="keep only top-N by sold")
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    pages = _load_pages(args.file)
    all_rows, seen = [], set()
    for pg in pages:
        content = pg.get("content", "")
        rows = parse_html_island(content) if '"productId":"' in content else parse_page(content)
        for row in rows:
            if row["item_id"] in seen:
                continue
            seen.add(row["item_id"])
            all_rows.append(row)

    rows = [r for r in all_rows if r["sold"] >= args.min_sold]
    rows.sort(key=lambda r: r["sold"], reverse=True)
    if args.top:
        rows = rows[: args.top]

    print(f"pages={len(pages)}  unique_products={len(all_rows)}  after_min_sold({args.min_sold})={len(rows)}")
    for r in rows[:60]:
        print(f"  {r['sold']:>7} sold  ${r['price'] or '?':<7} (was ${r['compare_at'] or '-'})  {r['item_id']}  {r['title'][:70]}")
    if args.save:
        pathlib.Path(args.save).write_text(json.dumps(rows, indent=2))
        print(f"\nsaved {len(rows)} -> {args.save}")


if __name__ == "__main__":
    main()
