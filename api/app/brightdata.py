"""Bright Data auto-setup — turn the ONE account API token into every Bright Data resource the
pipeline needs, so the operator never hand-creates a zone or pastes a zone password.

The old Connections page asked for a pile of Bright Data fields (SERP customer id + zone + zone
password, a CN residential proxy URL, plus dataset ids). Those are NOT extra keys — they're
*resources that must exist inside the operator's Bright Data account*. Bright Data exposes a Zone
Management API (Bearer-auth with the same account token), so this module provisions them for the
operator from the single token:

    list zones  ->  ensure a SERP zone + a CN residential zone exist (create if missing)  ->  read
    each zone's password  ->  write SERP zone/password + the assembled CN proxy URL back into
    Connections automatically.

What it CANNOT auto-create: the account *customer id* (Bright Data has no documented endpoint to
read it) and the marketplace *dataset ids* (those are Web-Scraper-library subscriptions, and the
app already reaches Amazon via the Bright Data MCP without one). Both are reported in `needs` so the
operator knows the single remaining paste — everything else is set up by the click.

Deterministic stdlib client (urllib). Creating a zone is a billable side effect, so this runs only
when the operator explicitly triggers setup (a button), never silently on token paste.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import connections

_API = "https://api.brightdata.com"
_HTTP_TIMEOUT = 30

# The two zones the pipeline needs, by purpose. Names are stable so re-running setup is idempotent
# (we adopt an existing zone of the right type before creating one).
_SERP_ZONE = "gstores_serp"   # Google Lens / SERP — china-source-match/lens_search.py
_CN_ZONE = "gstores_cn"       # CN residential proxy for 1688 mtop — china-source-match/mtop_1688.py
_BROWSER_ZONE = "gstores_browser"  # BD Scraping Browser (CDP) — Google Sponsored-PLA capture
# Bright Data superproxy host:port for proxy-protocol zones (residential/unlocker/serp).
_SUPERPROXY = "brd.superproxy.io:33335"
_SUPERPROXY_BROWSER = "brd.superproxy.io:9222"  # Scraping-Browser CDP (wss) host:port


def _token() -> str:
    """The one Bright Data account token, from Connections (the single source of truth)."""
    return (connections.as_env().get("BRIGHTDATA_API_TOKEN") or "").strip()


# --------------------------------------------------------------------------- http
def _request(method: str, path: str, *, body: bytes | None = None) -> tuple[int, str]:
    token = _token()
    if not token:
        raise RuntimeError("No Bright Data token set in Connections.")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{_API}{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode("utf-8", "replace") if e.fp else "")


def _get(path: str) -> tuple[int, object]:
    status, text = _request("GET", path)
    try:
        return status, (json.loads(text) if text else None)
    except json.JSONDecodeError:
        return status, text


def _list_zones() -> list[dict]:
    """`GET /zone/get_active_zones` -> [{"name","type",...}]. Empty list if the call fails so the
    caller treats 'unknown' as 'create it' (create is idempotent on our stable names)."""
    status, data = _get("/zone/get_active_zones")
    if status != 200 or not isinstance(data, list):
        return []
    return [z for z in data if isinstance(z, dict)]


def _zone_password(name: str) -> str | None:
    """`GET /zone/passwords?zone=<name>` -> the first password, or None."""
    status, data = _get(f"/zone/passwords?zone={urllib.parse.quote(name)}")
    if status != 200:
        return None
    if isinstance(data, dict):
        pw = data.get("passwords") or data.get("password")
        if isinstance(pw, list):
            return str(pw[0]) if pw else None
        return str(pw) if pw else None
    return None


def _create_zone(name: str, ztype: str, plan: dict) -> tuple[bool, str]:
    """`POST /zone` with `{"zone":{"name","type"},"plan":{...}}`. Returns (created_ok, detail).
    A 409/'already exists' is treated as success (idempotent)."""
    body = json.dumps({"zone": {"name": name, "type": ztype}, "plan": plan}).encode()
    status, text = _request("POST", "/zone", body=body)
    if status == 200:
        return True, "created"
    low = (text or "").lower()
    if status in (409, 422) and ("exist" in low or "already" in low):
        return True, "already existed"
    return False, f"{status}: {text[:160]}"


# ----------------------------------------------------------------------- provision
def _ensure_zone(name: str, ztype: str, plan: dict, existing: list[dict]) -> tuple[bool, str]:
    """Adopt an existing zone of the right type (prefer our canonical name) or create ours."""
    for z in existing:
        if z.get("name") == name:
            return True, "already existed"
    # No zone by our name — create it (Bright Data allows many zones; ours is namespaced).
    return _create_zone(name, ztype, plan)


def provision() -> dict:
    """Create/adopt the SERP + CN zones, read their passwords, and write what we resolved back into
    Connections. Returns a report: {ok, created, resolved (masked), needs}. Idempotent."""
    if not _token():
        return {"ok": False, "error": "Set the Bright Data Token in Connections first."}

    existing = _list_zones()
    created: list[str] = []
    detail: dict[str, str] = {}

    serp_ok, serp_msg = _ensure_zone(
        _SERP_ZONE, "serp",
        {"country": "us", "serp": {"google": True}}, existing,
    )
    detail[_SERP_ZONE] = serp_msg
    if serp_ok and "created" == serp_msg:
        created.append(_SERP_ZONE)

    cn_ok, cn_msg = _ensure_zone(
        _CN_ZONE, "resi",
        {"country": "cn"}, existing,
    )
    detail[_CN_ZONE] = cn_msg
    if cn_ok and "created" == cn_msg:
        created.append(_CN_ZONE)

    # Scraping Browser (CDP) for Google Sponsored-PLA capture — adopt the operator's existing
    # browser_api zone if they already have one, else create ours.
    br_zone = next((z.get("name") for z in existing
                    if z.get("type") in ("browser_api", "browser")), None) or _BROWSER_ZONE
    br_ok, br_msg = _ensure_zone(br_zone, "browser_api", {}, existing)
    detail[br_zone] = br_msg
    if br_ok and "created" == br_msg:
        created.append(br_zone)

    # Read passwords (best-effort) and write resolved creds back into Connections.
    serp_pw = _zone_password(_SERP_ZONE) if serp_ok else None
    cn_pw = _zone_password(_CN_ZONE) if cn_ok else None
    br_pw = _zone_password(br_zone) if br_ok else None

    api_writes: dict[str, str] = {}
    if serp_ok:
        api_writes["BRIGHTDATA_SERP_ZONE"] = _SERP_ZONE
    if serp_pw:
        api_writes["BRIGHTDATA_SERP_ZONE_PASSWORD"] = serp_pw

    # The customer id is the one value Bright Data won't hand back over the API. If the operator has
    # already pasted it, assemble the full CN proxy URL too; otherwise leave a clear placeholder.
    cust = (connections.as_env().get("BRIGHTDATA_CUSTOMER_ID") or "").strip()
    needs: list[str] = []
    if cn_ok and cn_pw and cust:
        api_writes["BD_CN_PROXY"] = (
            f"http://brd-customer-{cust}-zone-{_CN_ZONE}-country-cn:{cn_pw}@{_SUPERPROXY}"
        )
    if br_ok and br_pw and cust:
        api_writes["BRIGHTDATA_BROWSER_CDP"] = (
            f"wss://brd-customer-{cust}-zone-{br_zone}:{br_pw}@{_SUPERPROXY_BROWSER}"
        )
    if not cust:
        needs.append(
            "Bright Data Customer ID — paste it once (Bright Data dashboard → Account settings; "
            "it's the 'brd-customer-XXXX' id). It's the only value the API can't return; once set, "
            "re-run setup and the CN proxy URL fills itself."
        )

    if api_writes:
        connections.update({"api": api_writes})

    return {
        "ok": serp_ok or cn_ok,
        "created": created,
        "zones": detail,
        "resolved": {
            "BRIGHTDATA_SERP_ZONE": _SERP_ZONE if serp_ok else None,
            "BRIGHTDATA_SERP_ZONE_PASSWORD": "set" if serp_pw else None,
            "BD_CN_PROXY": "set" if api_writes.get("BD_CN_PROXY") else None,
            "BRIGHTDATA_BROWSER_CDP": "set" if api_writes.get("BRIGHTDATA_BROWSER_CDP") else None,
        },
        "needs": needs,
    }


def browser_cdp_endpoint() -> str | None:
    """The BD Scraping-Browser CDP wss endpoint for Google Sponsored-PLA capture. Prefers the resolved
    value in Connections (BRIGHTDATA_BROWSER_CDP, written by provision()); else assembles it live from
    an adopted/created browser zone's password + BRIGHTDATA_CUSTOMER_ID. None if not provisionable."""
    pre = (connections.as_env().get("BRIGHTDATA_BROWSER_CDP") or "").strip()
    if pre:
        return pre
    cust = (connections.as_env().get("BRIGHTDATA_CUSTOMER_ID") or "").strip()
    if not (cust and _token()):
        return None
    existing = _list_zones()
    br_zone = next((z.get("name") for z in existing
                    if z.get("type") in ("browser_api", "browser")), None) or _BROWSER_ZONE
    pw = _zone_password(br_zone)
    return f"wss://brd-customer-{cust}-zone-{br_zone}:{pw}@{_SUPERPROXY_BROWSER}" if pw else None


def status() -> dict:
    """Masked, render-safe summary for the Connections UI — what's provisioned, what's still needed.
    Never returns a password."""
    has_token = bool(_token())
    env = connections.as_env()
    zones = [z.get("name") for z in (_list_zones() if has_token else [])]
    return {
        "has_token": has_token,
        "serp_zone": env.get("BRIGHTDATA_SERP_ZONE") or None,
        "serp_password_set": bool(env.get("BRIGHTDATA_SERP_ZONE_PASSWORD")),
        "customer_id_set": bool(env.get("BRIGHTDATA_CUSTOMER_ID")),
        "cn_proxy_set": bool(env.get("BD_CN_PROXY")),
        "zones": zones,
    }
