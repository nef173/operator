"""Server-side 1688 / Alibaba PRODUCT-FINDING fetch for the marketplace lane, via TMAPI (tmapi.top).
1688 is a PRODUCT-FINDING source (find products for a trend/keyword to list) — a first-class finder
like AliExpress + Temu, NOT a "COGS/wholesale" tool.

WHY TMAPI (switched 2026-07 from the Bright Data DCA Scraper-Studio collector): the collector path
(`/dca/trigger` → poll `/dca/dataset?id=`) is async, slow (30-100s), and flaky — it kept timing out
and returning 0 products. TMAPI is a managed API that runs 1688's OWN keyword search behind one
apiToken and returns true 1688 domestic listings (title / price / MOQ / supplier / image / url) in a
SINGLE synchronous call. TMAPI_TOKEN is configured in Connections. This module REUSES the canonical
native-1688 client `tmapi_1688.py` (vendored under china-source-match/scripts) — it owns the TMAPI
transport + response normalization; this module only maps its candidate shape to the lane's row shape.

ROLE (unchanged sourcing model): 1688 results are RESEARCH/discovery only — a reference price FLOOR
+ spec-truth + "does a CN factory make this", NOT a supplier and NOT true COGS (the private agent adds
her markup + shipping on top; the LISTING price comes from the research source, never the 1688 number —
see feedback_price_basis_from_research_source_not_1688). Cite the offer_id as a research reference.

NOTE: TMAPI keyword-search prices are in CNY and titles may be Chinese (1688 is a CN domestic B2B
platform) — a known source characteristic, unchanged from the old collector.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Reuse the CANONICAL native-1688 TMAPI client (one source of truth for the TMAPI transport + the
# keyword-search response normalization). It's vendored by the Docker build, but the layout differs
# between the repo and the image:
#   • repo / local:  <api>/steps/05-launch-niche-store/china-source-match/scripts/tmapi_1688.py
#   • Docker image:  /app/05-launch-niche-store/china-source-match/scripts/…  (the Dockerfile STRIPS
#                    the `steps/` prefix: `COPY steps/05-…/scripts ./05-…/scripts`)
# so probe both (parent of app/ is <api> locally and /app in the image).
_API_ROOT = Path(__file__).resolve().parent.parent
for _d in (_API_ROOT / "steps" / "05-launch-niche-store" / "china-source-match" / "scripts",
           _API_ROOT / "05-launch-niche-store" / "china-source-match" / "scripts"):
    if _d.is_dir() and str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
try:
    import tmapi_1688 as _canonical  # noqa: E402  (path injected above)
except Exception:  # noqa: BLE001 — vendored step absent → lane degrades to [] (advisory)
    _canonical = None


def search(term: str, token: str, page_size: int = 20, max_items: int = 20) -> list[dict]:
    """Keyword → native 1688 offers via TMAPI (`GET /1688/search/items`) → normalized rows
    {id, title, price, compare_at, sold, moq, supplier, currency, image, url}. `token` is the
    TMAPI_TOKEN. Returns [] on ANY failure / missing token (advisory lane — the marketplace lane
    then runs without the 1688 rows). Synchronous: one TMAPI call, no polling.

    price = the offer's own 1688 price (a research reference / COGS basis, NOT the listing price).
    compare_at is None (1688 keyword search has no struck compare-at) and sold is None (TMAPI's
    keyword search carries no sale-count — it lives on item_detail only, which the lane doesn't call);
    moq + supplier come straight from the offer, matching the old collector's row shape."""
    if not (term or "").strip() or not token or _canonical is None:
        return []
    try:
        cands = _canonical.search_keyword(term, token, page=1,
                                          page_size=min(page_size, 20))
    except Exception:  # noqa: BLE001 — advisory lane, degrade to empty on any transport/parse error
        return []
    out: list[dict] = []
    for c in (cands or [])[:max_items]:
        out.append({
            "id": c.get("offer_id"),
            "title": c.get("title"),
            "price": c.get("price"),        # the offer's 1688 price = research reference / COGS basis
            "compare_at": None,             # 1688 keyword search has no struck compare-at
            "sold": None,                   # 1688 keyword search has no sold-count (no demand signal)
            "moq": c.get("moq"),
            "supplier": c.get("supplier"),
            "currency": c.get("currency") or "CNY",
            "image": c.get("image") or None,
            "url": c.get("url") or None,
        })
    return out
