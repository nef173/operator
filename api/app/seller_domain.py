"""Seller NAME -> store DOMAIN resolver for the Google Shopping finding lane.

Google Shopping PLAs (via DFS Merchant) expose a seller DISPLAY NAME ("Cozy Earth"), never the
store's domain — so the 2-stage lane (find dropshipper stores -> scan their catalogs) starved to 0
for ~every keyword (verified: DFS returns domain=null for 80/80 listings). This resolves the name
-> domain via a DFS organic SERP ("<seller> official site" -> first result host that isn't a
marketplace / retail giant / social / review site), cached per seller (a brand's domain is stable,
so steady-state cost is ~0; only a brand-new seller hits DFS). Whether the resolved store is a real
DROPSHIPPER vs a brand is decided DOWNSTREAM by readers._is_dropshipper_store — this only recovers
the domain so that stage can run at all.

Creds in-process via connections.runtime_get (os.environ is empty on the live Railway deploy).
"""
from __future__ import annotations

import base64
import json
import re
import urllib.request
from urllib.parse import quote_plus, urlparse

from . import config, connections

_DFS_ORGANIC = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
_BD_REQUEST = "https://api.brightdata.com/request"
_TIMEOUT = 30

# Hosts that are never a dropship COMPETITOR's own store — marketplaces, retail giants, social,
# review/wiki. A substring match is deliberate (amazon.com, amazon.co.uk, smile.amazon.com …).
_NOT_A_STORE = (
    "amazon.", "ebay.", "walmart.", "etsy.", "target.", "aliexpress.", "alibaba.", "temu.",
    "wayfair.", "homedepot.", "lowes.", "bestbuy.", "costco.", "macys.", "nordstrom.", "kohls.",
    "overstock.", "sears.", "newegg.", "wish.com", "shein.", "bedbathandbeyond.",
    "instagram.", "facebook.", "tiktok.", "youtube.", "pinterest.", "twitter.", "x.com", "reddit.",
    "wikipedia.", "yelp.", "trustpilot.", "sitejabber.", "google.", "bing.", "linkedin.", "houzz.",
    "shopify.com", "myshopify.com", "apps.apple.", "play.google.",
)


def _cache_path():
    return config.data_root() / "operator-app" / "api" / "data" / "seller-domain.json"


def _host(u: str) -> str:
    try:
        h = urlparse(u if "://" in u else "https://" + u).netloc.lower().strip()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def _is_store_host(h: str) -> bool:
    return bool(h) and "." in h and " " not in h and not any(b in h for b in _NOT_A_STORE)


def _serp_domain(name: str) -> str | None:
    u = (connections.runtime_get("DATAFORSEO_USERNAME") or "").strip()
    p = (connections.runtime_get("DATAFORSEO_PASSWORD") or "").strip()
    if not (u and p):
        return None
    auth = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
    payload = [{"keyword": f"{name} official site", "location_code": 2840,
                "language_code": "en", "depth": 10}]
    try:
        req = urllib.request.Request(
            _DFS_ORGANIC, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310 (trusted DFS endpoint)
            d = json.loads(r.read().decode("utf-8", "replace"))
        items = (((d.get("tasks") or [{}])[0].get("result") or [{}])[0] or {}).get("items") or []
    except Exception:  # noqa: BLE001 — advisory; resolver failure just leaves the seller unresolved
        return None
    for it in items:
        if not isinstance(it, dict) or it.get("type") != "organic":
            continue
        h = _host(str(it.get("url") or it.get("domain") or ""))
        if _is_store_host(h):
            return h
    return None


def _bd_domain(name: str) -> str | None:
    """Resolve seller name -> domain via the BrightData Web Unlocker (Google-search markdown) instead
    of a DataForSEO SERP call — BD is ~2x cheaper AND keeps this OFF the DataForSEO bill (the resolver
    was a per-find DFS-credit burst). Parses the first store-like host from the SERP markdown. None on
    missing BD creds / any error → DFS fallback."""
    tok = (connections.runtime_get("BRIGHTDATA_API_TOKEN") or "").strip()
    zone = (connections.runtime_get("BRIGHTDATA_SERP_ZONE") or "web_unlocker1").strip()
    if not (tok and zone):
        return None
    url = f"https://www.google.com/search?q={quote_plus(name + ' official site')}&hl=en&gl=us"
    body = json.dumps({"zone": zone, "url": url, "format": "raw",
                       "data_format": "markdown", "country": "us"}).encode()
    try:
        req = urllib.request.Request(
            _BD_REQUEST, data=body, method="POST",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310 (trusted BD endpoint)
            md = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — advisory; fall back to DFS
        return None
    for m in re.finditer(r"https?://([a-z0-9.\-]+\.[a-z]{2,})", md, re.I):
        h = _host(m.group(1))
        if _is_store_host(h):
            return h
    return None


def resolve(seller: str) -> str | None:
    """Seller display-name -> store domain (cached). Tries BrightData first (cheaper + off the DFS
    bill), falls back to a DataForSEO SERP. None if unresolvable / a marketplace / no creds.
    Marketplace-vs-dropshipper-vs-brand is judged downstream."""
    name = re.sub(r"\s+", " ", str(seller or "").strip())
    if len(name) < 2:
        return None
    key = name.lower()
    try:
        cache = json.loads(_cache_path().read_text())
        cache = cache if isinstance(cache, dict) else {}
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key] or None
    dom = _bd_domain(name) or _serp_domain(name)  # BD first (cheaper, off-DFS); DFS fallback
    cache[key] = dom or ""
    try:
        _cache_path().parent.mkdir(parents=True, exist_ok=True)
        _cache_path().write_text(json.dumps(cache))
    except OSError:
        pass
    return dom
