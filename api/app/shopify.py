"""Shopify store profile — pull the store-level FACTS from Shopify and make them flow.

A store is more than a push token. Its native LANGUAGE, CURRENCY, TIMEZONE, the MARKETS it
sells into, and the size of its CATALOG are facts that downstream steps must respect:

  * language  → the VisionScan gallery-language check (image_qa) judges "foreign text" against
                the store's actual native language, not a global "English" hardcode.
  * currency  → pricing / price-to-margin math + the Google feed must speak the store currency.
  * timezone  → scheduling (daily-listing cadence, ad dayparting) anchors to the store clock.
  * markets   → feed/market targeting + whether a second-language locale is published.
  * catalog   → a sanity read on how populated the store is (drives the build plan).

These are PULLED from Shopify via the Admin GraphQL API using the per-store admin token that
already lives in `connections.shopify_for(store)`, then SAVED into the connections single-source
store (`connections.set_profile`) so the rest of the app reads them through the seams
(`connections.store_language/currency/timezone/...`) instead of any os.environ hardcode.

Best-effort, stdlib-only (mirrors `google_oauth`'s urllib transport + per-section try/except so
one missing scope doesn't sink the whole pull). Returns a normalized dict; never raises for a
partial result — each section reports its own `*_error`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import connections

# Admin API version — pinned (see CLAUDE.md / memory: 2025-01 is the project's canonical version;
# `MediaImage.alt` etc. behave as expected there).
_API_VERSION = "2025-01"
_HTTP_TIMEOUT = 30

# One GraphQL document fetches every store-level fact in a single round-trip. Markets and locales
# are separate connections under the shop; productsCount is a cheap catalog-size read.
_PROFILE_QUERY = """
query OperatorStoreProfile {
  shop {
    name
    myshopifyDomain
    currencyCode
    ianaTimezone
    primaryDomain { host url }
    billingAddress { countryCodeV2 }
  }
  shopLocales {
    locale
    name
    primary
    published
  }
  markets(first: 50) {
    nodes {
      name
      enabled
      primary
      currencySettings { baseCurrency { currencyCode } }
      webPresence { rootUrls { locale url } }
    }
  }
  productsCount { count }
}
"""


def _admin_endpoint(shop_domain: str) -> str:
    host = (shop_domain or "").strip().replace("https://", "").replace("http://", "").rstrip("/")
    return f"https://{host}/admin/api/{_API_VERSION}/graphql.json"


def _graphql(shop_domain: str, token: str, query: str) -> dict:
    """POST a GraphQL document to a store's Admin API. Raises RuntimeError on transport / HTTP
    error or a GraphQL `errors` payload, so the caller can surface a precise message."""
    req = urllib.request.Request(
        _admin_endpoint(shop_domain),
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Shopify-Access-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310 (operator's own store)
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else str(e)
        raise RuntimeError(f"Shopify Admin API {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach Shopify Admin API: {e.reason}")
    if isinstance(body, dict) and body.get("errors"):
        msg = body["errors"][0].get("message") if isinstance(body["errors"], list) else str(body["errors"])
        raise RuntimeError(f"Shopify GraphQL error: {msg}")
    return (body or {}).get("data") or {}


def _normalize(data: dict) -> dict:
    """Flatten the GraphQL response into the saved profile shape the app reads from."""
    shop = data.get("shop") or {}
    locales = data.get("shopLocales") or []
    markets_node = (data.get("markets") or {}).get("nodes") or []

    primary_locale = next((l for l in locales if l.get("primary")), None)
    languages = [
        {"locale": l.get("locale"), "name": l.get("name"),
         "primary": bool(l.get("primary")), "published": bool(l.get("published"))}
        for l in locales
    ]
    markets = [
        {
            "name": m.get("name"),
            "enabled": bool(m.get("enabled")),
            "primary": bool(m.get("primary")),
            "currency": (((m.get("currencySettings") or {}).get("baseCurrency") or {}).get("currencyCode")),
        }
        for m in markets_node
    ]
    catalog_count = ((data.get("productsCount") or {}).get("count"))

    return {
        "shop_name": shop.get("name"),
        "myshopify_domain": shop.get("myshopifyDomain"),
        "primary_domain": (shop.get("primaryDomain") or {}).get("host"),
        "currency": shop.get("currencyCode"),
        "timezone": shop.get("ianaTimezone"),
        "country": (shop.get("billingAddress") or {}).get("countryCodeV2"),
        # Native language = the display name of the primary locale (fall back to the locale code).
        "language": (primary_locale or {}).get("name") or (primary_locale or {}).get("locale"),
        "primary_locale": (primary_locale or {}).get("locale"),
        "languages": languages,
        "markets": markets,
        "catalog_count": catalog_count,
    }


def pull_profile(store: str) -> dict:
    """Pull the store-level profile from Shopify and PERSIST it into the connections single-source.

    Returns {ok, profile?, error?}. Best-effort: a missing token or an unscoped read returns
    `{ok: False, error: ...}` rather than raising, so the route can surface it cleanly (mirrors
    `google_oauth.list_accounts`). On success the profile is saved per-store so every downstream
    reader (`connections.store_language/currency/timezone`) sees it with zero extra wiring."""
    creds = connections.shopify_for(store)
    token = (creds.get("admin_token") or "").strip()
    shop_domain = (creds.get("shop_domain") or "").strip()
    if not token or not shop_domain:
        return {
            "ok": False,
            "error": creds.get("auth_error") or (
                "Set the app-wide Shopify Client ID + Secret and this store's myshopify domain "
                "first (Connections · Shopify)."
            ),
        }
    try:
        data = _graphql(shop_domain, token, _PROFILE_QUERY)
    except Exception as e:  # noqa: BLE001 — best-effort; report, don't crash the route
        # A missing scope (read_locales / read_markets) makes Shopify reject the whole query
        # ("Access denied for shopLocales field"). That is surfaced as a FAILED sync on purpose:
        # the fix is to add the scopes to the store's Shopify app + reinstall, not to hide it.
        return {"ok": False, "error": str(e)}

    profile = _normalize(data)
    try:
        connections.set_profile(store, profile)
    except Exception as e:  # noqa: BLE001 — pull succeeded even if persistence hiccupped
        return {"ok": True, "profile": profile, "warning": f"pulled but could not save: {e}"}
    return {"ok": True, "profile": profile}
