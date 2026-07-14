"""Server-side Temu product finding for the marketplace lane — Bright Data Temu DATASET method
(operator's choice; NOT Apify, NOT the local AdsPower CDP method which can't run on the server).

Reuses the vendored `temu_dataset.py` (BD Web-Scraper/Dataset API: trigger discover-by-keyword →
poll snapshot → fetch rows). Hands-off, server-runnable. Needs, in Connections:
  • BRIGHTDATA_DATASET_TOKEN    — a Dataset-scoped BD token (falls back to BRIGHTDATA_API_TOKEN;
                                   the plain Web-Unlocker token may 403 the Dataset API)
  • BRIGHTDATA_TEMU_DATASET     — the Temu dataset id (gd_…). OPTIONAL: when blank, this module
                                   AUTO-RESOLVES it by calling the BD `GET /datasets/list` endpoint
                                   and picking the account's subscribed Temu dataset by name — so a
                                   fresh install works as long as the operator has subscribed to ANY
                                   Temu dataset in the BD Web-Scraper library (no id to copy/paste).
RESEARCH/discovery source only; the private agent is the supplier.

The Dataset API is ASYNC (a snapshot can take minutes). `wait_s` bounds the inline poll for the
'Find products' drill-in; on timeout it returns [] (the lane degrades to AliExpress-only) rather
than hanging the request. Results are cached 7d by the caller, so a slow first fetch is one-time.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Reuse the vendored temu_dataset.py (probe BOTH layouts — the Docker image strips `steps/`).
_API_ROOT = Path(__file__).resolve().parent.parent
for _d in (_API_ROOT / "steps" / "01-niche-discovery" / "scripts",
           _API_ROOT / "01-niche-discovery" / "scripts"):
    if _d.is_dir() and str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
try:
    import temu_dataset as _canonical  # noqa: E402  (path injected above)
except Exception:  # noqa: BLE001 — vendored step absent → lane degrades to []
    _canonical = None

# BD Marketplace Dataset API — `GET /datasets/list` returns every dataset the account is subscribed
# to as [{id, name, size}]. We use it to auto-discover the operator's Temu dataset id by name when
# BRIGHTDATA_TEMU_DATASET is blank, so the lane works without a hand-pasted gd_… id.
_DATASETS_LIST = "https://api.brightdata.com/datasets/list"
# Cache the resolved (token → dataset_id) so we don't hit /datasets/list on every keyword fetch.
_DATASET_CACHE: dict[str, str] = {}


def resolve_dataset_id(token: str) -> str | None:
    """Auto-discover a Temu dataset id from the BD account via `GET /datasets/list` — pick the first
    dataset whose name mentions Temu. Cached per token. Returns the gd_… id, or None on any failure /
    no Temu dataset subscribed (the caller then degrades to AliExpress-only)."""
    if not token:
        return None
    if token in _DATASET_CACHE:
        return _DATASET_CACHE[token] or None
    req = urllib.request.Request(
        _DATASETS_LIST, method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (trusted BD endpoint)
            rows = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, ValueError, OSError):
        return None  # transport / auth / parse error → no auto-resolve (advisory lane)
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        did = str(row.get("id") or "")
        if did and "temu" in name.lower():
            _DATASET_CACHE[token] = did
            return did
    _DATASET_CACHE[token] = ""  # remember "none found" so we don't re-list every fetch
    return None


def _num(*vals) -> float | None:
    for v in vals:
        if v is None:
            continue
        m = re.search(r"[\d.]+", str(v).replace(",", ""))
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                continue
    return None


def _sold_to_int(*vals) -> int | None:
    for v in vals:
        if v is None:
            continue
        m = re.search(r"([\d.]+)\s*([kKmM]?)", str(v).replace(",", ""))
        if not m:
            continue
        try:
            n = float(m.group(1))
        except ValueError:
            continue
        return int(n * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1))
    return None


def _row(it: dict) -> dict:
    """Normalize a BD Temu-dataset record → the lane's product shape. Field names vary by dataset,
    so probe the common ones (verify/adjust against the operator's actual subscribed dataset)."""
    gid = str(it.get("goods_id") or it.get("product_id") or it.get("id") or "").strip()
    url = it.get("url") or it.get("product_url") or it.get("link") or \
        (f"https://www.temu.com/goods.html?goods_id={gid}" if gid else None)
    return {
        "id": gid or None,
        "title": it.get("title") or it.get("product_name") or it.get("name"),
        "price": _num(it.get("price"), it.get("sale_price"), it.get("final_price")),
        "compare_at": _num(it.get("market_price"), it.get("original_price"), it.get("list_price")),
        "sold": _sold_to_int(it.get("sold_count"), it.get("sales"), it.get("units_sold"),
                             it.get("sold"), it.get("sales_tip")),
        "rating": _num(it.get("rating"), it.get("stars")),
        "reviews": it.get("review_count") or it.get("reviews") or it.get("reviews_count"),
        "image": it.get("image_url") or it.get("image") or it.get("thumbnail") or it.get("main_image"),
        "url": url,
    }


def search(term: str, token: str, dataset_id: str = "", geo: str = "US",
           max_items: int = 20, wait_s: int = 120) -> list[dict]:
    """Trigger the BD Temu dataset (discover-by-keyword) → poll (bounded) → fetch → normalized rows,
    sorted by SOLD desc. `dataset_id` is the gd_… id; when blank it is AUTO-RESOLVED from the BD
    account via `resolve_dataset_id(token)`. [] on ANY failure / no dataset / timeout (advisory lane)."""
    if not (term or "").strip() or not token or _canonical is None:
        return []
    ds = (dataset_id or "").strip() or resolve_dataset_id(token)
    if not ds:
        return []  # no Temu dataset configured AND none auto-discoverable → AliExpress-only
    try:
        sid = _canonical.trigger(ds, token, [{"keyword": term}], by="keyword")
        if not _canonical.wait(sid, token, timeout=wait_s):
            return []
        rows = _canonical.fetch(sid, token)
    except (SystemExit, Exception):  # noqa: BLE001 — trigger/wait raise SystemExit on failure/timeout
        return []
    out = [_row(it) for it in (rows or []) if isinstance(it, dict)]
    out = [r for r in out if r.get("title")][:max_items]
    out.sort(key=lambda p: -(p.get("sold") or 0))
    return out


# ── Web-Unlocker path (the account has only a URL-collect Temu dataset, which can't discover-by-
# keyword → empty snapshots). Same synchronous BD Web Unlocker fetch AliExpress uses, on Temu's
# search page. Temu is heavily bot-protected, so this is EVIDENCE-FIRST: probe() reports whether the
# fetch actually returns extractable product JSON before we rely on it. ───────────────────────────
_BD_REQUEST = "https://api.brightdata.com/request"
_TEMU_SEARCH_TPL = "https://www.temu.com/search_result.html?search_key={q}"


def fetch_search_html(term: str, token: str, zone: str, geo: str = "US", timeout: int = 90) -> str:
    """Fetch Temu's search page HTML via the BD Web Unlocker (country-scoped). Raises on transport
    error (caller degrades to [])."""
    url = _TEMU_SEARCH_TPL.format(q=urllib.parse.quote_plus(term))
    body = json.dumps({"zone": zone, "url": url, "format": "raw",
                       "country": (geo or "US").lower()}).encode()
    req = urllib.request.Request(
        _BD_REQUEST, data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted BD endpoint)
        return r.read().decode("utf-8", "replace")


def probe(term: str, token: str, zone: str, geo: str = "US") -> dict:
    """Evidence probe — fetch Temu's search HTML + report size and whether it carries extractable
    product markers, so we know if the Web-Unlocker path is viable BEFORE building a parser on it.
    Longer timeout (180s): Temu's anti-bot makes the Web Unlocker work hard; we're checking whether
    it EVER returns product data, not whether it's fast (a slow source runs via the background warm)."""
    try:
        html = fetch_search_html(term, token, zone, geo=geo, timeout=180)
    except Exception as e:  # noqa: BLE001
        return {"error": repr(e)[:200]}
    markers = {m: (m in html) for m in
               ("goods_id", "goodsId", "priceInfo", "price_info", "__NEXT_DATA__",
                "window.rawData", "\"goodsList\"", "link_url", "goods_name", "salesTip")}
    return {"len": len(html), "markers": markers, "head": html[:240].replace("\n", " ")}
