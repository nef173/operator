"""Connector API health — surfaces WHY a data source isn't working (out of credit / auth failed /
no creds / unreachable) so the operator can SEE it in the app and fix it, instead of the failure
being buried in a silent failed job. The trigger case: DataForSEO's Google-Shopping Merchant scan
returns HTTP 402 when the account balance runs out, which killed the whole Google-Shopping finding
lane invisibly.

Read in-process via connections.runtime_get (os.environ is empty on the live Railway deploy).
Best-effort with short timeouts — a health probe must never hang or throw."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

from . import connections

_T = 12  # per-probe timeout (s)


def _ok(key: str, name: str, message: str, **extra) -> dict:
    return {"key": key, "name": name, "ok": True, "status": "ok", "message": message, "fix": "", **extra}


def _bad(key: str, name: str, status: str, message: str, fix: str = "", **extra) -> dict:
    return {"key": key, "name": name, "ok": False, "status": status, "message": message, "fix": fix, **extra}


def _get(env: str) -> str:
    return (connections.runtime_get(env) or "").strip()


def _dfs() -> dict:
    """DataForSEO — the balance check. Powers keyword research, SERP, the resolver AND the Google-
    Shopping Merchant scan. A low/zero balance is what makes Merchant scans 402 → Google Shopping 0."""
    name = "DataForSEO"
    u, p = _get("DATAFORSEO_USERNAME"), _get("DATAFORSEO_PASSWORD")
    if not (u and p):
        return _bad("dataforseo", name, "no_creds", "No DataForSEO credentials set.",
                    "Add DataForSEO Username + Password under Connections → Data.")
    auth = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
    try:
        req = urllib.request.Request("https://api.dataforseo.com/v3/appendix/user_data",
                                     headers={"Authorization": auth})
        with urllib.request.urlopen(req, timeout=_T) as r:  # noqa: S310 (trusted DFS endpoint)
            d = json.loads(r.read().decode("utf-8", "replace"))
        res = (((d.get("tasks") or [{}])[0].get("result") or [{}])[0]) or {}
        bal = (res.get("money") or {}).get("balance")
        if isinstance(bal, (int, float)):
            if bal <= 1:
                return _bad("dataforseo", name, "out_of_credit",
                            f"Balance ${bal:.2f} — too low. Google-Shopping (Merchant) scans and other "
                            f"DataForSEO calls fail with HTTP 402, so the Google-Shopping finding lane "
                            f"returns 0.",
                            "Top up your DataForSEO account at dataforseo.com → Dashboard → Add funds.",
                            balance=round(bal, 2))
            return _ok("dataforseo", name, f"OK — balance ${bal:.2f}.", balance=round(bal, 2))
        return _ok("dataforseo", name, "Reachable (balance not reported).")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return _bad("dataforseo", name, "auth_failed", f"Auth rejected (HTTP {e.code}).",
                        "Re-check your DataForSEO Username + Password under Connections → Data.")
        if e.code == 402:
            return _bad("dataforseo", name, "out_of_credit", "HTTP 402 — out of credit.",
                        "Top up your DataForSEO account at dataforseo.com.")
        return _bad("dataforseo", name, "error", f"Unexpected HTTP {e.code}.", "")
    except Exception as e:  # noqa: BLE001
        return _bad("dataforseo", name, "error", f"Unreachable ({type(e).__name__}).", "")


def _brightdata() -> dict:
    """Bright Data — AliExpress finding + the Google-Shopping Web-Unlocker fallback. Validate the token
    against the account status endpoint (cheap, no scrape cost)."""
    name = "Bright Data"
    tok = _get("BRIGHTDATA_API_TOKEN")
    if not tok:
        return _bad("brightdata", name, "no_creds", "No Bright Data token set.",
                    "Add the Bright Data Token under Connections → Data.")
    try:
        req = urllib.request.Request("https://api.brightdata.com/status",
                                     headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=_T) as r:  # noqa: S310
            r.read()
        return _ok("brightdata", name, "Token valid — AliExpress + Google-Shopping fallback ready.")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return _bad("brightdata", name, "auth_failed", f"Token rejected (HTTP {e.code}).",
                        "Re-check the Bright Data Token under Connections → Data.")
        return _ok("brightdata", name, "Token present (status probe inconclusive).")
    except Exception:  # noqa: BLE001 — probe failure ≠ token invalid; report configured
        return _ok("brightdata", name, "Token present (status endpoint unreachable).")


def _tmapi() -> dict:
    """TMAPI — the 1688 sourcing/finding lane. Token presence (a live search would cost a call)."""
    name = "TMAPI (1688)"
    if not _get("TMAPI_TOKEN"):
        return _bad("tmapi", name, "no_creds", "No TMAPI token set — the 1688 finding lane is off.",
                    "Add the TMAPI Token under Connections → Data.")
    return _ok("tmapi", name, "Token present — 1688 finding lane enabled.")


def _trendtrack() -> dict:
    """TrendTrack — spy roster + Meta-ads discovery. Token presence."""
    name = "TrendTrack"
    if not _get("TRENDTRACK_API_TOKEN"):
        return _bad("trendtrack", name, "no_creds", "No TrendTrack token — spy/discovery is off.",
                    "Add the TrendTrack API Token under Connections → Data (enable Public API in TrendTrack).")
    return _ok("trendtrack", name, "Token present — spy + discovery enabled.")


def _apify() -> dict:
    """Apify — the Temu finding actor (crw/temu-products-scraper). BrightData can't scrape Temu, so
    this is the only working Temu source. Validate the token via a cheap whoami probe + report plan."""
    name = "Apify (Temu)"
    tok = _get("APIFY_TOKEN")
    if not tok:
        return _bad("apify", name, "no_creds", "No Apify token — Temu finding is off (BrightData can't scrape Temu).",
                    "Add the Apify Token under Connections → Data.")
    try:
        req = urllib.request.Request(f"https://api.apify.com/v2/users/me?token={tok}")
        with urllib.request.urlopen(req, timeout=_T) as r:  # noqa: S310
            d = (json.loads(r.read().decode("utf-8", "replace")).get("data") or {})
        plan = (d.get("plan") or {}).get("id") or "?"
        return _ok("apify", name, f"Token valid (plan {plan}) — Temu finding via the crw actor enabled.")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return _bad("apify", name, "auth_failed", f"Token rejected (HTTP {e.code}).",
                        "Re-check the Apify Token under Connections → Data.")
        return _ok("apify", name, "Token present (probe inconclusive).")
    except Exception:  # noqa: BLE001
        return _ok("apify", name, "Token present (Apify unreachable).")


def _bestseller_fetch() -> dict:
    """Competitor best-seller fetch (`/collections/all?sort_by=best-selling`) — surfaces WHEN/WHY it
    fell back to the merchandising signal (datacenter-IP bot-block / rate-limit / no residential proxy)
    so the operator can see it, not just silently degrade."""
    try:
        from . import readers
        h = readers.bestseller_fetch_health()
    except Exception as exc:  # noqa: BLE001
        return _bad("bestseller_fetch", "Competitor best-seller fetch", "error", f"probe failed: {exc}"[:160])
    fix = ("" if h.get("proxy_configured") or h.get("ok")
           else "Set BD_CN_PROXY in Connections (it's re-routed to a rotating US residential IP) so "
                "the plain fetch has a fallback when a store bot-blocks the datacenter IP.")
    common = {"recent_failures": h.get("recent_failures"), "proxy_configured": h.get("proxy_configured")}
    if h.get("ok"):
        return _ok("bestseller_fetch", "Competitor best-seller fetch", h.get("message", "OK"), **common)
    # degraded = the scan still works via the merchandising fallback → ok=True (visible, not a hard fail)
    return {"key": "bestseller_fetch", "name": "Competitor best-seller fetch", "ok": True,
            "status": "degraded", "message": h.get("message", "some stores fell back"), "fix": fix, **common}


def health() -> dict:
    """All connector-API health checks, worst-first. `ok` is the overall pass; each check carries a
    status (ok / out_of_credit / auth_failed / no_creds / error), a plain-English message, and a fix."""
    checks = [_dfs(), _brightdata(), _tmapi(), _trendtrack(), _apify(), _bestseller_fetch()]
    order = {"out_of_credit": 0, "auth_failed": 1, "error": 2, "no_creds": 3, "degraded": 4, "ok": 5}
    checks.sort(key=lambda c: order.get(c.get("status"), 9))
    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "problems": [c for c in checks if not c["ok"]]}
