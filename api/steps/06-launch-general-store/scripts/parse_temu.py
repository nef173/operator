#!/usr/bin/env python3
"""
FAST Temu candidate extractor — the Temu sibling of parse_ae_listing.py.

WHY: Temu's `search_result.html` is blocked (Empty/empty), but THREE Temu page
types DO render through the Bright Data **MCP** Web-Unlocker zone and each one
returns a ~40-product grid with English titles + price + sold-count + RRP
(compare-at) + a real kwcdn image:
  1. product pages   `.../<slug>-g-<id>.html`        (renders a 40-item "similar items" feed)
  2. search-agg pages `.../<slug>-<digits>-s.html`     (Temu's own search aggregation)
  3. channel pages    `.../channel/best-sellers.html`  (movers; 7/14/30-day tabs render)

Discover real Temu URLs first with the MCP `search_engine` tool
(`site:temu.com <term>`, geo=us), then `scrape_batch` up to 10 of them, then run
this parser on the saved file.

GEO — US PINNING NOW WORKS (2026-06-17): the MCP scrape tools expose NO country
param (random residential exit), BUT the scripted Web-Unlocker HTTP API path
(brightdata_client.fetch / direct POST https://api.brightdata.com/request with
{"zone":"web_unlocker1","country":"us","data_format":"markdown"}) returns a
true US exit and renders Temu. The earlier "Empty-200" was NOT a non-render zone
— it was the zone's access_params BLOCKED-IPS list denying our own residential
outbound IP (error header x-brd-err-code: client_10050). FIX = remove the
blocked IPs and keep BOTH Allowed-IPs and Blocked-IPs EMPTY on every zone
(token auth, not IP auth) so a rotating residential IP never gets blocked.
For random-geo discovery the MCP path is still fine (English titles + sold-counts
survive; only price CURRENCY varies). Use the scripted US-pinned path when you
need USD prices / true US movers ranking.

This script does NOT call the network. It PARSES the file the MCP tool saved.

USAGE:
  python parse_temu.py <saved-tool-result.txt> [--min-sold N] [--top N] [--save out.json]
"""
from __future__ import annotations
import argparse, json, re, pathlib


def _load_pages(path: str) -> list[dict]:
    """Return [{url, content}] from whatever the MCP saved (same shapes as the
    AliExpress parser): outer [{type,text}] whose text is JSON [{url,content}];
    a bare [{url,content}]; a single {url,content}; or raw markdown."""
    raw = pathlib.Path(path).read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [{"url": "(raw)", "content": raw}]
    if isinstance(data, list) and data and isinstance(data[0], dict) and "text" in data[0]:
        pages = []
        for blk in data:
            t = blk.get("text", "")
            try:
                inner = json.loads(t)
                pages.extend(inner if isinstance(inner, list) else [inner])
            except json.JSONDecodeError:
                pages.append({"url": "(raw)", "content": t})
        return pages
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return [{"url": "(raw)", "content": raw}]


# A Temu product link: ](/<locale>/<slug>-g-<id>.html "Title")  — title group optional
_LINK = re.compile(r'\]\((/[^)]*?-g-(\d+)\.html)(?:\s+"([^"]*)")?\)')
# Money: €16.90 / $16.90 / CA$16.90 / US $16.90 / ₩13,187  (comma OR dot decimals)
_MONEY = re.compile(r'(?:US\s*\$|CA\$|[€£\$₩])\s*([\d][\d.,]*\d|\d)')
_SOLD = re.compile(r'([\d.,]+)\s*([kKmM]?)\+?\s*sold', re.I)
_RRP = re.compile(r'(?:RRP|Lowest recent price:)\s*(?:RRP)?\s*(?:US\s*\$|CA\$|[€£\$₩])\s*([\d][\d.,]*\d|\d)', re.I)
# kwcdn product image (skip data:image gif placeholders + tiny slim pngs)
_IMG = re.compile(r'https://(?:img|aimg)\.kwcdn\.com/[^\s\)"\']+?\.(?:jpg|jpeg|png|webp|avif)', re.I)
_CUR = re.compile(r'US\s*\$|CA\$|[€£\$₩]')


def _to_float(s: str) -> float | None:
    """Temu mixes '16.90' and '16,90' and '13,187'. Heuristic: if both , and .
    present, the last one is the decimal sep; else if only ',' and it's followed
    by exactly 2 digits at the end treat as decimal, otherwise thousands."""
    s = s.strip()
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):       # 1.234,56  (EU)
            s = s.replace(".", "").replace(",", ".")
        else:                                  # 1,234.56  (US)
            s = s.replace(",", "")
    elif "," in s:
        frac = s.rsplit(",", 1)[1]
        s = s.replace(",", ".") if len(frac) == 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _sold_to_int(num: str, unit: str) -> int:
    u = unit.lower()
    # When a K/M multiplier follows, the separator is a DECIMAL point, not a
    # thousands group ("1,2K"/"1.2K" = 1.2K = 1200) — so normalise comma->dot
    # before parsing instead of letting _to_float treat it as thousands.
    if u in ("k", "m"):
        n = float(num.replace(",", ".")) if num else 0.0
        n *= 1_000 if u == "k" else 1_000_000
    else:
        n = _to_float(num) or 0
    return int(n)


def parse_temu(md: str) -> list[dict]:
    """Split the markdown on each product link; for each card pull the
    title (from the link's quote), then price / sold / RRP / image from the
    window of text immediately FOLLOWING the link (Temu prints those after)."""
    out, seen = [], set()
    links = list(_LINK.finditer(md))
    cur_sym = (_CUR.search(md) or [None])
    page_currency = cur_sym.group(0) if hasattr(cur_sym, "group") else None
    for i, m in enumerate(links):
        item_id = m.group(2)
        if item_id in seen:
            continue
        seen.add(item_id)
        title = (m.group(3) or "").strip()
        # body = text from end of this link to start of next link
        end = links[i + 1].start() if i + 1 < len(links) else min(m.end() + 1200, len(md))
        body = md[m.end():end]
        # also a small lookback for the eager image that precedes the link
        look = md[max(0, m.start() - 1500):m.start()]

        rrp_m = _RRP.search(body)
        compare_at = _to_float(rrp_m.group(1)) if rrp_m else None
        # sale price = first money token in body that is NOT the RRP value
        prices = [_to_float(x) for x in _MONEY.findall(body)]
        prices = [p for p in prices if p is not None and p != compare_at]
        price = prices[0] if prices else None
        # if RRP missing but two distinct prices, treat max as compare-at
        if compare_at is None and len(set(prices)) >= 2:
            compare_at = max(prices) if max(prices) > min(prices) else None
            price = min(prices)
        sold_m = _SOLD.search(body)
        sold = _sold_to_int(*sold_m.groups()) if sold_m else 0
        imgs = _IMG.findall(look) + _IMG.findall(body)
        image = imgs[0] if imgs else None

        out.append({
            "item_id": item_id,
            "url": f"https://www.temu.com{m.group(1)}",
            "title": re.sub(r"\s+", " ", title)[:160],
            "price": price,
            "compare_at": compare_at,
            "currency": page_currency,
            "sold": sold,
            "image": image,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--min-sold", type=int, default=0)
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    pages = _load_pages(args.file)
    all_rows, seen = [], set()
    for pg in pages:
        for row in parse_temu(pg.get("content", "")):
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
        cur = r["currency"] or ""
        print(f"  {r['sold']:>8} sold  {cur}{r['price'] or '?':<7} (was {cur}{r['compare_at'] or '-'})  {r['item_id']}  {r['title'][:64]}")
    if args.save:
        pathlib.Path(args.save).write_text(json.dumps(rows, indent=2))
        print(f"\nsaved {len(rows)} -> {args.save}")


if __name__ == "__main__":
    main()
