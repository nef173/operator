"""Server-side AliExpress LISTING fetch for the marketplace finding lane.

This is ONLY the server-side FETCH. The parse + rules are the project's CANONICAL method —
`parse_ae_listing.parse_html_island` (vendored at steps/06-launch-general-store/scripts/, synced
from 06-launch-general-store/scripts/), the single source of truth per
feedback_fast_aliexpress_listing_scrape_not_item_by_item: scrape the listing page sorted by
ORDERS (`/w/wholesale-<term>.html?SortType=total_tranpro_desc`) → per-product JSON island →
{item_id, title, price (sale=min minPrice), compare_at (=max minPrice, COGS basis), sold, image,
url}. We REUSE that parser rather than reimplement it.

The canonical method's FETCH is the agent's Bright Data MCP `scrape_batch` (agent-only — it saves
a file the parser then reads). That can't run in the operator-app RUNTIME (Railway, no agent), so
this module does the same fetch server-side via the BD Web Unlocker HTTP API
(`api.brightdata.com/request`, zone `web_unlocker1`) — item pages are blocked, listing pages
render. AliExpress is a RESEARCH/discovery source only; the private agent is the supplier.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Reuse the CANONICAL AliExpress listing parser (one source of truth for the parse rules). It's
# vendored by sync-steps.sh, but the layout differs between the repo and the image:
#   • repo / local:  <api>/steps/06-launch-general-store/scripts/parse_ae_listing.py
#   • Docker image:  /app/06-launch-general-store/scripts/…  (the Dockerfile STRIPS the `steps/`
#                    prefix: `COPY steps/06-…/scripts ./06-…/scripts`)
# so probe both (parent of app/ is <api> locally and /app in the image).
_API_ROOT = Path(__file__).resolve().parent.parent
_CANONICAL_DIRS = [
    _API_ROOT / "steps" / "06-launch-general-store" / "scripts",   # repo / local
    _API_ROOT / "06-launch-general-store" / "scripts",             # Docker image (steps/ stripped)
]
for _d in _CANONICAL_DIRS:
    if _d.is_dir() and str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
try:
    import parse_ae_listing as _canonical  # noqa: E402  (path injected above)
except Exception:  # noqa: BLE001 — vendored step absent → lane degrades to [] (advisory)
    _canonical = None

_BD_REQUEST = "https://api.brightdata.com/request"
# Sorted by total orders = proven demand first (the canonical method's rule).
_LISTING_TPL = "https://www.aliexpress.com/w/wholesale-{q}.html?SortType=total_tranpro_desc"


def fetch_html(term: str, token: str, zone: str, geo: str = "US", timeout: int = 90) -> str:
    """Fetch the AliExpress listing HTML through the BD Web Unlocker (country-scoped so US prices
    render). Raises on transport error — the caller degrades to [] (advisory lane)."""
    url = _LISTING_TPL.format(q=urllib.parse.quote_plus(term))
    body = json.dumps({
        "zone": zone, "url": url, "format": "raw", "country": (geo or "US").lower(),
    }).encode()
    req = urllib.request.Request(
        _BD_REQUEST, data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (trusted BD endpoint)
        return r.read().decode("utf-8", "replace")


def search(term: str, token: str, zone: str, geo: str = "US", timeout: int = 60,
           attempts: int = 2) -> list[dict]:
    """Fetch the listing → CANONICAL parse_html_island → sort by SOLD (proven demand) desc. Returns
    the canonical rows ({item_id, title, price, compare_at, sold, image, url}) or [] on ANY failure
    so the marketplace lane never breaks the aggregate 'Find products' view.

    Retry policy (latency-bounded — this runs in a SYNC endpoint): a fetch that returns a real,
    substantial page is AUTHORITATIVE — parse it and return, EVEN IF it has 0 products (a genuinely
    empty keyword must NOT burn 3×timeout retrying). Only an empty / anti-bot 'silent block' page
    (<5 KB) is retried. This caps a cold fetch at ~1 page load, not attempts×timeout."""
    if not (term or "").strip() or not token or not zone or _canonical is None:
        return []
    for _ in range(max(1, attempts)):
        try:
            html = fetch_html(term, token, zone, geo=geo, timeout=timeout)
            if len(html) < 5000:
                continue  # empty / anti-bot block → retry
            rows = _canonical.parse_html_island(html)  # real page → authoritative (return even if [])
        except Exception:  # noqa: BLE001 — transport OR parse error → advisory lane degrades to empty
            continue
        rows.sort(key=lambda p: -(p.get("sold") or 0))
        return rows
    return []
