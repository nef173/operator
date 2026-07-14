"""Google OAuth — the PROPER way to connect Google Ads + Merchant Center.

The point of this module: a Merchant Center ID or Google Ads Customer ID is NOT a
connection — it's only the *address* of an account. Google grants the app nothing until
the account owner has AUTHORIZED it. So the real connection is an OAuth 2.0 consent flow
that yields a long-lived **refresh token**; the ids then just say *which* of the accounts
that login can reach we should act on.

Flow (mirrors the Markifact OAuth-MCP client, adapted for Google's quirks):
  1. The operator pastes their Google Cloud OAuth **client id + secret** once (the
     GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET fields in Connections). Google has NO
     dynamic client registration — unlike Markifact — so a pre-created OAuth client is a
     hard precondition. `public_status().client_ready` reflects whether it's set.
  2. The operator clicks "Connect Google" → we build the consent URL (scopes `adwords` +
     `content`, `access_type=offline` + `prompt=consent` so Google returns a refresh token)
     and they approve it in a popup.
  3. The callback exchanges the code (PKCE S256 + the client secret) for tokens. We persist
     them AND bridge the refresh token into the connections store under
     `GOOGLE_ADS_REFRESH_TOKEN`, so every downstream Ads/GMC job that reads that env name via
     `connections.as_env()` is authorized with zero extra wiring.
  4. `list_accounts()` then calls Google to enumerate the Ads customers + Merchant Center
     accounts this login can reach — that's what turns the per-store id fields from a
     hand-typed string into a pick-from-dropdown account selector.

State lives in the DB (`app_settings` key `google_oauth`) so it survives a redeploy. Tokens
and the code_verifier are NEVER returned by `public_status()`.

`connections` is imported lazily inside functions to avoid an import cycle (connections.py
attaches this module's status into its public view).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from . import runlog

_SETTING_KEY = "google_oauth"

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
# Ads write/read + Merchant Center (Content API) + identity (so the picker can show the email).
SCOPES = [
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/content",
    "openid",
    "email",
]
_ADS_LIST_CUSTOMERS = "https://googleads.googleapis.com/v21/customers:listAccessibleCustomers"
# New Merchant API (replaces the sunset Content-API-for-Shopping v2.1 authinfo, dead 2026-08-18).
# Lists every Merchant Center account this login can reach; quota is charged per user.
_MERCHANT_LIST_ACCOUNTS = "https://merchantapi.googleapis.com/accounts/v1/accounts?pageSize=500"
_HTTP_TIMEOUT = 30
_REFRESH_SKEW = 60  # refresh this many seconds before expiry (clock-skew margin)


# --------------------------------------------------------------------------- state
def _state() -> dict:
    data = runlog.setting_get(_SETTING_KEY)
    return data if isinstance(data, dict) else {}


def _save(state: dict) -> None:
    runlog.setting_set(_SETTING_KEY, state)


def _patch(**fields) -> dict:
    state = _state()
    state.update(fields)
    _save(state)
    return state


# --------------------------------------------------------------------------- creds
def _client_id() -> str | None:
    from . import connections
    return connections.runtime_get("GOOGLE_ADS_CLIENT_ID")


def _client_secret() -> str | None:
    from . import connections
    return connections.runtime_get("GOOGLE_ADS_CLIENT_SECRET")


def _developer_token() -> str | None:
    from . import connections
    return connections.runtime_get("GOOGLE_ADS_DEVELOPER_TOKEN")


def _login_customer_id() -> str | None:
    from . import connections
    return connections.runtime_get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")


# --------------------------------------------------------------------------- http
def _request(method: str, url: str, *, headers: dict | None = None,
             body: bytes | None = None) -> tuple[int, dict, str]:
    """Minimal stdlib HTTP. Returns (status, lower-cased headers, text). Never raises on a
    4xx/5xx — the caller inspects status — only on transport failure."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, hdrs, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") if e.fp else ""
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
        return e.code, hdrs, raw


def _post_form(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    status, _, text = _request(
        "POST", url, body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {"raw": text}
    if status >= 400:
        raise RuntimeError(
            f"POST {url} -> {status}: {data.get('error_description') or data.get('error') or text[:200]}"
        )
    return data


def _get_json(url: str, headers: dict) -> dict:
    status, _, text = _request("GET", url, headers={"Accept": "application/json", **headers})
    if status >= 400:
        raise RuntimeError(f"GET {url} -> {status}: {text[:200]}")
    return json.loads(text) if text else {}


# ------------------------------------------------------------------------- oauth
def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def start_oauth(redirect_uri: str) -> dict:
    """Begin the handshake: require the client creds, generate PKCE + state, persist the
    pending leg, return the Google consent URL for the operator to open in their browser."""
    client_id = _client_id()
    if not client_id or not _client_secret():
        raise RuntimeError(
            "Set the Google Ads OAuth Client ID + Client Secret first (API keys · Google Ads) — "
            "Google has no dynamic client registration, so a Google Cloud OAuth client is required."
        )
    verifier, challenge = _pkce()
    state_tok = secrets.token_urlsafe(24)
    _patch(pending={
        "state": state_tok, "code_verifier": verifier,
        "redirect_uri": redirect_uri, "ts": int(time.time()),
    })
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state_tok,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",   # ask for a refresh token
        "prompt": "consent",        # force the refresh token even on re-consent
        "include_granted_scopes": "true",
    }
    url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)
    return {"authorization_url": url, "state": state_tok}


def finish_oauth(code: str, state_tok: str) -> dict:
    """Complete the handshake: validate state, exchange the code (PKCE verifier + client
    secret) for tokens, persist them, and BRIDGE the refresh token into the connections store
    so downstream Ads/GMC jobs are authorized. Returns a SAFE status (never the tokens)."""
    state = _state()
    pending = state.get("pending") or {}
    if not pending or pending.get("state") != state_tok:
        raise RuntimeError("OAuth state mismatch — restart the connection")
    tok = _post_form(TOKEN_ENDPOINT, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "code_verifier": pending["code_verifier"],
    })
    _store_tokens(tok)
    s = _state()
    s.pop("pending", None)
    s.pop("last_error", None)
    s["connected_at"] = int(time.time())
    _save(s)
    return public_status()


def _store_tokens(tok: dict) -> None:
    expires_in = int(tok.get("expires_in") or 3600)
    fields = {
        "access_token": tok.get("access_token"),
        "scope": tok.get("scope", " ".join(SCOPES)),
        "token_type": tok.get("token_type", "Bearer"),
        "expires_at": int(time.time()) + expires_in,
    }
    if tok.get("refresh_token"):
        fields["refresh_token"] = tok["refresh_token"]
        # Bridge into the existing seam: every subprocess job reads GOOGLE_ADS_REFRESH_TOKEN
        # via connections.as_env(), so persisting it here authorizes them with no extra wiring.
        try:
            from . import connections
            connections.update({"api": {"GOOGLE_ADS_REFRESH_TOKEN": tok["refresh_token"]}})
        except Exception:
            pass  # the local copy still works; the bridge is best-effort
    _patch(**fields)


def _refresh() -> None:
    state = _state()
    rt = state.get("refresh_token")
    if not rt:
        raise RuntimeError("not connected — no refresh token; reconnect Google")
    tok = _post_form(TOKEN_ENDPOINT, {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    })
    tok.setdefault("refresh_token", rt)  # Google omits it on refresh; keep the one we have
    _store_tokens(tok)


def _access_token() -> str:
    state = _state()
    if not state.get("access_token") and not state.get("refresh_token"):
        raise RuntimeError("not connected — run the Connect flow first")
    if int(state.get("expires_at", 0)) <= int(time.time()) + _REFRESH_SKEW:
        _refresh()
        state = _state()
    return state["access_token"]


def disconnect() -> dict:
    """Revoke the token with Google (best-effort) and clear all stored Google OAuth creds.
    Also clears the bridged refresh token from connections so jobs stop being authorized."""
    state = _state()
    tok = state.get("refresh_token") or state.get("access_token")
    if tok:
        try:
            _post_form(REVOKE_ENDPOINT, {"token": tok})
        except Exception:
            pass  # best-effort; clear locally regardless
    runlog.setting_set(_SETTING_KEY, {})
    try:
        from . import connections
        connections.update({"api": {"GOOGLE_ADS_REFRESH_TOKEN": ""}})  # "" clears the field
    except Exception:
        pass
    return public_status()


# ----------------------------------------------------------------- account picker
def list_accounts() -> dict:
    """Enumerate the Ads customers + Merchant Center accounts this Google login can reach.
    Best-effort: each side is wrapped so a missing developer token / unscoped account doesn't
    sink the whole call. This is what powers the per-store account-picker dropdowns."""
    token = _access_token()
    ads: list[dict] = []
    ads_error: str | None = None
    merchant: list[dict] = []
    merchant_error: str | None = None

    dev_token = _developer_token()
    if dev_token:
        try:
            headers = {"Authorization": f"Bearer {token}", "developer-token": dev_token}
            login_cid = _login_customer_id()
            if login_cid:
                headers["login-customer-id"] = login_cid.replace("-", "")
            data = _get_json(_ADS_LIST_CUSTOMERS, headers)
            for rn in data.get("resourceNames", []):
                cid = rn.split("/")[-1]
                ads.append({"id": cid, "label": f"{cid[:3]}-{cid[3:6]}-{cid[6:]}" if len(cid) == 10 else cid})
        except Exception as e:
            ads_error = str(e)
    else:
        ads_error = "set the Google Ads Developer Token to list Ads accounts"

    try:
        info = _get_json(_MERCHANT_LIST_ACCOUNTS, {"Authorization": f"Bearer {token}"})
        for acct in info.get("accounts", []):
            # Resource name is "accounts/{id}"; the bare id is what every downstream feed job uses.
            mid = str(acct.get("name", "")).split("/")[-1]
            if mid:
                merchant.append({"id": mid, "label": acct.get("accountName") or mid})
    except Exception as e:
        merchant_error = str(e)

    return {
        "ads_accounts": ads, "ads_error": ads_error,
        "merchant_accounts": merchant, "merchant_error": merchant_error,
    }


# ------------------------------------------------------------------------ status
def is_connected() -> bool:
    return bool(_state().get("refresh_token") or _state().get("access_token"))


def public_status() -> dict:
    """Render-safe status for the Settings UI. NEVER returns tokens or the code_verifier."""
    state = _state()
    expires_at = int(state.get("expires_at", 0))
    scope = state.get("scope") or ""
    return {
        "connected": bool(state.get("refresh_token") or state.get("access_token")),
        "client_ready": bool(_client_id() and _client_secret()),
        "developer_token_set": bool(_developer_token()),
        "scopes": [s for s in scope.split(" ") if s] or None,
        "expires_at": expires_at or None,
        "expires_in": max(0, expires_at - int(time.time())) if expires_at else None,
        "redirect_uri": state.get("redirect_uri"),
        "connected_at": state.get("connected_at"),
        "last_error": state.get("last_error"),
    }
