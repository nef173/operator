"""BrightData Web-Unlocker Google-Shopping scan — the FALLBACK when the DataForSEO Merchant scan
fails (HTTP 402 out-of-credit, or any transport error).

DataForSEO stays PRIMARY (richer: product images, product URLs, the sponsored `shop_ad_aclk` flag,
search volume). This is the resilience layer so Google-Shopping finding survives a DFS outage: it
fetches the Google Shopping tab (`udm=28`) through the BD Web Unlocker as MARKDOWN and parses the PLA
product grid (title / price / seller / rating / discount) + Google's category rows.

Known limits vs DFS: the markdown carries no product image or product URL (those live in the HTML),
so BD-fallback products have `image_url=None`/`url=None` — the finding lane still surfaces them by
title/price/seller, and the seller→domain resolver recovers a store link. Output shape is IDENTICAL
to `shopping_scan_dfs.extract_merchant` ({products:[…], carousels:[…]}) so the rest of the scan
pipeline (summarize → paid-scan.json) is unchanged.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

_BD_REQUEST = "https://api.brightdata.com/request"
# udm=28 = Google's Shopping vertical (the PLA product grid — the same surface DFS Merchant scans).
_URL_TPL = "https://www.google.com/search?udm=28&q={q}&gl={geo}&hl=en"

# A BARE price line ("$18.99" or sale+compare "$15.99$30"); NOT a filter link like "[Under $15](…)".
_PRICE = re.compile(r"^\$\s?([\d,]+(?:\.\d{1,2})?)(?:\s?\$\s?([\d,]+(?:\.\d{1,2})?))?$")
_RATING = re.compile(r"(\d(?:\.\d)?)\((\d[\d.,]*[KM]?)\)")
_DISCOUNT = re.compile(r"(\d+)%\s*OFF", re.I)
# Google's category-row headers render as "<segment>\n\n[ More ](/search…)".
_CATEGORY = re.compile(r"\n([^\n\[\]$]{5,70})\n\s*\n\[\s*\n+\s*More", re.M)
_SKIP_TITLES = {"see more", "more", "more results", "sort by:", "relevance", "all products", "try"}


def _num(s: object) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch_markdown(keyword: str, token: str, zone: str, geo: str = "US", timeout: int = 90) -> str:
    """Google Shopping tab → markdown via the BD Web Unlocker (unlocks Google's bot wall)."""
    url = _URL_TPL.format(q=urllib.parse.quote_plus(keyword), geo=(geo or "US").lower())
    body = json.dumps({"zone": zone, "url": url, "format": "raw",
                       "data_format": "markdown", "country": (geo or "US").lower()}).encode()
    req = urllib.request.Request(
        _BD_REQUEST, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted BD endpoint)
        return r.read().decode("utf-8", "replace")


def _bad_title(t: str) -> bool:
    tl = t.lower()
    return (not t or len(t) < 3 or "](" in t or t.startswith(("[", "$"))
            or tl in _SKIP_TITLES or tl.startswith(("free ", "get it", "delete", "see "))
            or bool(_RATING.match(t)) or bool(_DISCOUNT.match(t)))


def parse(md: str) -> dict:
    """Parse the Shopping-tab markdown into the extract_merchant shape ({products, carousels}).

    Google renders each product as separate blank-line-delimited fields (title / price / seller /
    delivery / returns / rating / discount), so parse the FLAT line list with the bare price line as
    the anchor: title = the line just before the price, seller = the first line after, rating +
    discount = scanned from the lines up to the next product's price."""
    md = md or ""
    category_rows: list[str] = []
    for seg in _CATEGORY.findall(md):
        seg = seg.strip("* ").strip()
        if seg and seg.lower() not in _SKIP_TITLES and seg not in category_rows:
            category_rows.append(seg)

    lines = [ln.strip() for ln in md.splitlines() if ln.strip()]
    price_at = [i for i, ln in enumerate(lines) if _PRICE.match(ln)]
    products: list[dict] = []
    seen: set[tuple] = set()
    for k, i in enumerate(price_at):
        if i == 0:
            continue
        title = lines[i - 1].strip("* ").strip()
        if _bad_title(title):
            continue
        m = _PRICE.match(lines[i])
        price, regular = _num(m.group(1)), (_num(m.group(2)) if m.group(2) else None)
        nxt = price_at[k + 1] if k + 1 < len(price_at) else len(lines)
        tail = lines[i + 1:nxt]  # seller + extras, up to (not incl.) the next product's price
        # The seller is the first store-like line after the price — but its exact position varies in
        # the server-side markdown (delivery/returns/rating lines can precede it), so scan a few lines
        # for the first line that isn't a price / link / delivery-returns / rating / discount phrase.
        seller = None
        for c in tail[:5]:
            cl = c.lower()
            if (_PRICE.match(c) or "](" in c or c.startswith("$")
                    or _RATING.fullmatch(c) or _DISCOUNT.fullmatch(c)
                    or cl.startswith(("free ", "get it", "save ", "delivery", "sponsored", "in stock"))
                    or cl.endswith((" returns", " delivery", " ago"))
                    or cl in ("more", "see more")):
                continue
            seller = re.sub(r"\s*&\s*more$", "", c).strip() or None
            break
        rest = " ".join(tail)
        rmatch, dmatch = _RATING.search(rest), _DISCOUNT.search(rest)
        key = (title.lower(), price)
        if key in seen:
            continue
        seen.add(key)
        products.append({
            "title": title, "seller": seller, "domain": None, "image_url": None,
            "price_current": price, "price_regular": regular,
            "discount_pct": _num(dmatch.group(1)) if dmatch else None,
            "currency": "USD", "rating": _num(rmatch.group(1)) if rmatch else None,
            "reviews": rmatch.group(2) if rmatch else None, "sponsored": False, "url": None,
        })
    return {"products": products, "carousels": category_rows}


def scan(keyword: str, token: str, zone: str, geo: str = "US", timeout: int = 90) -> dict:
    """BD Google-Shopping scan → {products, carousels}. [] on missing creds / any error."""
    kw = (keyword or "").strip()
    if not (kw and token and zone):
        return {"products": [], "carousels": []}
    try:
        return parse(fetch_markdown(kw, token, zone, geo=geo, timeout=timeout))
    except Exception:  # noqa: BLE001 — fallback is best-effort; caller records the failure
        return {"products": [], "carousels": []}
