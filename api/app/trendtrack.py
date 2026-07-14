"""TrendTrack public REST API client (api.trendtrack.io) — the in-app path for the premium
spy/discovery source.

TrendTrack used to be MCP-only (reachable only from an agent's Claude Code session), so the
hosted worker fell back to DataForSEO for store discovery. TrendTrack now ships a public REST API
(Bearer-token, JSON), so the hosted app can call it directly — this module is that client.

Auth: a workspace-scoped key (`tt_live_…`) entered in Settings → Connections → Data
(TRENDTRACK_API_TOKEN). Create it in TrendTrack: workspace settings → enable Public API access →
"Get API key".

Credits: metered endpoints report usage in response HEADERS — X-Credits-Used / X-Credits-Remaining
/ X-Usage-Cost. `/v1/me` and `/v1/lookup` are unmetered (zero-cost). We surface creditsRemaining on
every call so callers can credit-gate a batch (same discipline as the MCP workflow).

Stdlib only (urllib). Best-effort: every function returns a dict with `ok`; it never raises into a
request handler.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import connections

_BASE = "https://api.trendtrack.io"
_TIMEOUT = 30


def _token() -> str | None:
    return connections.runtime_get("TRENDTRACK_API_TOKEN")


def has_token() -> bool:
    return bool((_token() or "").strip())


def _call(method: str, path: str, body: dict | None = None) -> dict:
    """One REST call. Returns {ok, status, data, credits_remaining, cost, error}. Never raises."""
    tok = (_token() or "").strip()
    if not tok:
        return {"ok": False, "error": "TrendTrack API token not set (Settings → Connections → Data)."}
    url = _BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            hdrs = resp.headers
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                payload = {"_raw": raw[:2000]}
            return {
                "ok": True,
                "status": resp.status,
                "data": payload,
                "credits_used": _num(hdrs.get("X-Credits-Used")),
                "credits_remaining": _num(hdrs.get("X-Credits-Remaining")),
                "cost": _num(hdrs.get("X-Usage-Cost")),
            }
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            detail = ""
        msg = "invalid or unauthorized token" if e.code in (401, 403) else f"HTTP {e.code}"
        return {"ok": False, "status": e.code, "error": f"{msg}{': ' + detail if detail else ''}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"ok": False, "error": f"TrendTrack unreachable: {e}"}


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Unmetered ────────────────────────────────────────────────────────────────
def me() -> dict:
    """Validate the credential → the workspace it resolves to. Zero credits."""
    return _call("GET", "/v1/me")


def usage() -> dict:
    """Remaining credits + billing period."""
    return _call("GET", "/v1/usage")


def lookup(query: str) -> dict:
    """Resolve a brand name / domain / FB page / IG handle to TrendTrack ids. Zero credits."""
    return _call("GET", f"/v1/lookup?query={urllib.parse.quote(query)}")


# ── Metered ──────────────────────────────────────────────────────────────────
def shops_query(body: dict) -> dict:
    """Raw POST /v1/shops/query passthrough (search Shopify stores + store-level analytics)."""
    return _call("POST", "/v1/shops/query", body)


def search_shops(
    *,
    markets: list[str] | None = None,
    dtc_region: str = "us",
    min_monthly_visits: int | None = 5000,
    min_growth_30d: float | None = None,
    sort_by: str = "growth30d",
    limit: int = 50,
    extra: dict | None = None,
) -> dict:
    """Growing US-market storefronts — the clean discovery feed (operator mandate: always target
    US). Verified against the live /v1/shops/query schema: `mainMarketCountries` is the audience-
    market filter (uppercase ISO), `dtcRegion` is the DTC preset (all|us|eu), `sortBy=growth30d`
    orders by 30-day traffic growth. `extra` merges extra filters (categoryIds / shopifyAppIds /
    trafficGrowth conditions)."""
    body: dict = {
        "mainMarketCountries": markets or ["US"],
        "dtcRegion": dtc_region,
        "sortBy": sort_by,
        "order": "desc",
        "limit": limit,
    }
    if min_monthly_visits is not None:
        body["minMonthlyVisits"] = min_monthly_visits
    if min_growth_30d is not None:
        # trafficGrowth conditions: value is a PERCENTAGE; greater = gte.
        body["trafficGrowth"] = [{"period": "last30d", "comparison": "greater", "value": min_growth_30d}]
    if extra:
        body.update(extra)
    return shops_query(body)


def similar_shops(identifier: str, limit: int = 25) -> dict:
    """Lookalike shops for a known competitor domain/id (GET /v1/shops/{identifier}/similar)."""
    return _call("GET", f"/v1/shops/{urllib.parse.quote(identifier, safe='')}/similar?limit={limit}")


def search_ads(search: str, *, limit: int = 50, offset: int = 0,
               sort_by: str = "relevance", order: str = "desc") -> dict:
    """Meta/Facebook ad creatives for a term. GET /v1/ads requires `search`. sortBy must be one of
    relevance / newest / longestRunning / reach (the API rejects anything else with a 400;
    'longestRunning' = proven-winner longevity, 'reach' is useless for US ads which report reach:0)."""
    from urllib.parse import urlencode
    qs = urlencode({"search": search, "limit": limit, "offset": offset, "sortBy": sort_by, "order": order})
    return _call("GET", f"/v1/ads?{qs}")


# ── Keyword → dropship competitors (the Find-Products source) ─────────────────
# Meta ads for a keyword reveal the actual DROPSHIP advertisers running it, each with a store, a
# creative image, and a LANDING (product) page — a far better competitor source than Google
# Shopping's retail feed (which is brands/marketplaces with no direct store URL). Duplicate-creative
# count + ad longevity are proven-winner signals.
def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _days_since(v) -> int | None:
    """Days since an ISO date / epoch — ad longevity (older first-seen = longer-proven winner)."""
    import datetime as _dt
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            n = float(v)
            ts = _dt.datetime.fromtimestamp(n / 1000 if n > 1e11 else n, _dt.timezone.utc)
        else:
            ts = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        return max(0, (_dt.datetime.now(_dt.timezone.utc) - ts).days)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _is_us_targeted(ad: dict) -> bool:
    """Operator mandate 'always US'. Keep only US-shown ads: drop EU-flagged ads that don't ALSO
    target US; otherwise require 'US' in targetedCountries / mainCountry (fail-open when no audience
    data). Meta only publishes reach for EU ads, so relevance-sort still returns EU — filter here."""
    aud = ad.get("audience") if isinstance(ad.get("audience"), dict) else {}
    flags = ad.get("flags") if isinstance(ad.get("flags"), dict) else {}
    countries = aud.get("targetedCountries") or []
    has_us = "US" in countries or aud.get("mainCountry") == "US"
    if flags.get("isEuAd") is True:
        return has_us
    return has_us or not countries  # non-EU with US, or no audience data at all


def _ad_to_competitor(ad: dict) -> dict | None:
    """Normalize one TrendTrack ad → a found-product competitor card. Real /v1/ads shape:
    advertiser{name,reach30d}, content{body,landingPageUrl,landingPageDomain}, media{thumbnailUrl},
    metrics{duplicates}. The landing page IS the competitor's product page (operator's ask)."""
    if not isinstance(ad, dict):
        return None
    adv = ad.get("advertiser") if isinstance(ad.get("advertiser"), dict) else {}
    content = ad.get("content") if isinstance(ad.get("content"), dict) else {}
    media = ad.get("media") if isinstance(ad.get("media"), dict) else {}
    metrics = ad.get("metrics") if isinstance(ad.get("metrics"), dict) else {}
    store = adv.get("name")
    domain = content.get("landingPageDomain")
    url = content.get("landingPageUrl")
    image = media.get("thumbnailUrl") or media.get("imageUrl")
    body = content.get("body") or ""
    title = content.get("title") or (body.splitlines()[0].strip() if body else None) or store
    if not (store or url or image or domain):
        return None
    if isinstance(title, str) and len(title) > 90:
        title = title[:90].rstrip() + "…"
    longevity = _int(ad.get("daysRunning"))
    if not longevity:
        longevity = _days_since(ad.get("firstSeenAt") or ad.get("createdAt"))
    return {
        "title": title,
        "image": image or None,
        "store_name": store or domain,
        "domain": domain or None,
        # landingPageUrl = the competitor's actual PRODUCT PAGE (never a google redirect);
        # fall back to the store domain.
        "url": url or (f"https://{domain}" if domain else None),
        "ad_longevity_days": longevity,
        "dup_creatives": _int(metrics.get("duplicates")),
        "reach_30d": _int(adv.get("reach30d")),
        "source": "trendtrack_ads",
        "validated": False,
    }


def _ads_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("ads", "data", "results", "items", "records"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def competitors_for_keyword(keyword: str, limit: int = 40) -> dict:
    """Dropship competitor advertisers running Meta ads for `keyword` → normalized found-product
    cards (store + creative image + landing/product page + ad-longevity + dup-creative winner
    signal). Best-effort — {ok, products, credits_remaining, error}."""
    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "products": [], "error": "empty keyword"}
    if not has_token():
        return {"ok": False, "products": [], "error": "no token"}
    # Pull extra (we filter to US-shown ads client-side, so over-fetch to keep a full page).
    r = search_ads(kw, limit=max(limit * 3, 60), sort_by="relevance")
    if not r.get("ok"):
        return {"ok": False, "products": [], "error": r.get("error"), "status": r.get("status")}
    ads = _ads_list(r.get("data"))
    us_ads = [a for a in ads if _is_us_targeted(a)]
    seen, products = set(), []
    for a in us_ads:
        c = _ad_to_competitor(a)
        if not c:
            continue
        key = (c.get("domain") or c.get("url") or c.get("store_name") or "").strip().lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        products.append(c)
    # Proven winners first: longest-running ads lead (higher ad-longevity = more validated).
    products.sort(key=lambda p: -(p.get("ad_longevity_days") or 0))
    return {"ok": True, "products": products[:limit], "raw_count": len(ads), "us_count": len(us_ads),
            "credits_remaining": r.get("credits_remaining")}


# ── App-facing status (drives the Connections badge + credit display) ─────────
def status() -> dict:
    """Is the token set + valid, and how many credits remain. Used by the Connections UI and the
    discover-general-stores preflight to decide whether to call TrendTrack directly."""
    if not has_token():
        return {"ok": True, "connected": False, "reason": "No TrendTrack API token set."}
    m = me()
    if not m.get("ok"):
        return {"ok": True, "connected": False, "reason": m.get("error") or "token check failed"}
    u = usage()
    mdata = m.get("data") or {}
    ws = mdata.get("workspace") or {}
    workspace = ws.get("name") if isinstance(ws, dict) else ws
    plan = None
    credits_remaining = None
    if u.get("ok"):
        ud = u.get("data") or {}
        quota = ud.get("includedQuota") or {}
        credits_remaining = quota.get("remaining")
        plan = (ud.get("billing") or {}).get("planCode")
    return {
        "ok": True,
        "connected": True,
        "workspace": workspace,
        "plan": plan,
        "credits_remaining": credits_remaining,
    }
