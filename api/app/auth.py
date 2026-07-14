"""Real sign-in on top of the existing per-person RBAC (`users.py`).

Model: NAME + password → an opaque bearer TOKEN the web client stores and sends as
`Authorization: Bearer <token>` on every request. Chosen over a cookie because web and
api are separate Railway origins — cross-site cookies are fragile, a header is not.
NN-style: the person's NAME is the sign-in identity (no username, no email).

Sessions live in the runlog app_settings key→JSON store (key "auth_sessions"): only the
SHA-256 of each token is persisted, so a leak of the settings row can't be replayed. On
a valid login the person also becomes the global active user, so the shell + FinanceGuard
+ /api/access reflect who is signed in with zero changes to their existing reads.

No-lockout contract: the app ALWAYS ships with a working owner login (users.py seeds
admin/admin and backfills any empty-owner row), so there's no self-serve setup screen —
sign-in always works, and owners create/rename everyone else from inside the app.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from . import runlog, users

_SESSIONS_KEY = "auth_sessions"
_TTL_SECONDS = 30 * 24 * 3600  # 30 days — a long-lived operator console, not a bank
_MAX_SESSIONS = 200  # bound the JSON blob (Railway-cost guardrail); prune oldest beyond this


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load() -> dict:
    return runlog.setting_get(_SESSIONS_KEY) or {}


def _save(sessions: dict) -> None:
    runlog.setting_set(_SESSIONS_KEY, sessions)


def _prune(sessions: dict) -> dict:
    """Drop expired sessions, then cap the total (newest kept) so the row can't grow
    unbounded across months of logins."""
    now = time.time()
    live = {h: s for h, s in sessions.items() if (s.get("exp", 0) > now)}
    if len(live) > _MAX_SESSIONS:
        newest = sorted(live.items(), key=lambda kv: kv[1].get("created", 0), reverse=True)
        live = dict(newest[:_MAX_SESSIONS])
    return live


def mint(uid: str) -> dict | None:
    """Issue a bearer session for an already-trusted uid (no password). Shared by password
    login and the NN pass-through below. Returns {token, user} or None if uid is unknown."""
    person = users.public_by_id(uid)
    if not person:
        return None
    token = secrets.token_urlsafe(32)
    now = time.time()
    sessions = _prune(_load())
    sessions[_token_hash(token)] = {"uid": uid, "created": now, "exp": now + _TTL_SECONDS}
    _save(sessions)
    try:
        users.set_active(uid)
    except KeyError:
        pass
    return {"token": token, "user": person}


def login(name: str, password: str) -> dict | None:
    """Return {token, user} on success, else None (caller emits a generic 401)."""
    person = users.authenticate(name, password)
    if not person:
        return None
    return mint(person["id"])


# ── One-login pass-through from the NN shell ────────────────────────────────────────
# The whole app is embedded (iframe) behind NN's own login, so a SECOND sign-in here is
# pure friction. NN signs a short-lived token with a secret it shares ONLY with this API
# (OPERATOR_SSO_SECRET, never shipped to the browser) and hands it to the frame in the URL
# hash; we verify it and mint an owner bearer. The public API stays locked — a bearer is
# only ever minted for a caller that already holds the shared secret (i.e. the NN server).
_SSO_MAX_SKEW = 600  # a presented token may claim at most 10 min of life (forgery-window cap)


def _sso_secret() -> str:
    return (os.environ.get("OPERATOR_SSO_SECRET") or "").strip()


def sso_enabled() -> bool:
    return bool(_sso_secret())


def verify_sso(token: str | None) -> bool:
    """token = '<exp>.<hexsig>' where hexsig = HMAC-SHA256(secret, str(exp)). Valid when the
    signature matches (constant-time) AND exp is in the future but not absurdly far ahead."""
    secret = _sso_secret()
    if not secret or not token or "." not in token:
        return False
    exp_str, _, sig = token.partition(".")
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), exp_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig.strip()):
        return False
    now = time.time()
    return now <= exp <= now + _SSO_MAX_SKEW


def resolve(token: str | None) -> dict | None:
    """Bearer token → PUBLIC user, or None if missing/expired/unknown."""
    if not token:
        return None
    sessions = _load()
    sess = sessions.get(_token_hash(token))
    if not sess or sess.get("exp", 0) <= time.time():
        return None
    return users.public_by_id(sess.get("uid"))


def logout(token: str | None) -> None:
    if not token:
        return
    sessions = _load()
    if sessions.pop(_token_hash(token), None) is not None:
        _save(sessions)


def token_from_header(authorization: str | None) -> str | None:
    """Extract the bearer token from an `Authorization: Bearer <t>` header value."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None
