"""Temu product finding via an Apify actor — the ONLY working Temu source.

BrightData cannot scrape Temu (PDD anti-bot wall — verified across Web Unlocker, Scraping Browser,
screenshot; see the Temu-not-viable memory). The Apify actor `crw/temu-products-scraper` renders
Temu's keyword search server-side and returns REAL products (title, price + market/compare-at price,
sales volume, rating, reviews, product image, product URL, mall/seller). Confirmed working on the
Apify FREE plan (unlike gio21's paid-gated mock data).

Auth = APIFY_TOKEN (connections). Uses the sync run endpoint (one call, blocks until the actor
finishes) — so it runs in the background WARM path (`_temu_cached` fetch=True), cached 7d, never in
the user-facing instant read. Best-effort: any error / empty / non-FREE gating → []. Note: each run
spends Apify platform credits (FREE = ~$5/mo), so the lane is keyword-cached to keep runs low.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

_ACTOR = "crw~temu-products-scraper"
_SYNC = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={token}"


def _cents(v: object) -> float | None:
    """crw prices are integer-cent strings ('1260' → 12.60)."""
    try:
        return round(int(str(v).strip()) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def _sold(v: object) -> int | None:
    """'4.9K+' / '1,200' / '3.4M' → int, for demand ranking."""
    s = str(v or "").upper().replace("+", "").replace(",", "").strip()
    m = re.match(r"([\d.]+)\s*([KM]?)", s)
    if not m:
        return None
    try:
        return int(float(m.group(1)) * {"K": 1_000, "M": 1_000_000}.get(m.group(2), 1))
    except ValueError:
        return None


def _int(v: object) -> int | None:
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _float(v: object) -> float | None:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _row(it: dict) -> dict:
    return {
        "id": it.get("goods_id"),
        "title": it.get("title"),
        "price": _cents(it.get("price")),
        "compare_at": _cents(it.get("market_price")),
        "sold": _sold(it.get("sales_num") or it.get("sales_tip")),
        "rating": _float(it.get("rating")),
        "reviews": _int(it.get("review_count")),
        "image": it.get("image_url") or it.get("thumb_url"),
        "url": it.get("link_url"),
        "supplier": it.get("mall_id"),
        "currency": it.get("currency") or "USD",
    }


def search(keyword: str, token: str, geo: str = "US", max_items: int = 20, timeout: int = 180) -> list[dict]:
    """Temu keyword search → demand-ranked product rows. [] on missing token / any error / empty /
    FREE-gated mock. Slow (server-side actor run) — call it from the background warm, not the read."""
    kw = (keyword or "").strip()
    if not (kw and token):
        return []
    url = _SYNC.format(actor=_ACTOR, token=urllib.parse.quote(token, safe=""))
    body = json.dumps({"keyword": kw, "region": (geo or "US").upper(),
                       "max_items": max(int(max_items or 20), 10), "sort": "relevance"}).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted Apify endpoint)
            items = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — advisory lane, degrade to empty on any transport/timeout/parse
        return []
    if not isinstance(items, list):
        return []
    rows = [_row(it) for it in items
            if isinstance(it, dict) and it.get("title") and not it.get("error") and not it.get("_mock")]
    rows.sort(key=lambda r: -(r.get("sold") or 0))  # demand-rank (highest sold first)
    return rows[:max_items]
