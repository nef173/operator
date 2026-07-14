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


def login(name: str, password: str) -> dict | None:
    """Return {token, user} on success, else None (caller emits a generic 401)."""
    person = users.authenticate(name, password)
    if not person:
        return None
    token = secrets.token_urlsafe(32)
    now = time.time()
    sessions = _prune(_load())
    sessions[_token_hash(token)] = {"uid": person["id"], "created": now, "exp": now + _TTL_SECONDS}
    _save(sessions)
    try:
        users.set_active(person["id"])
    except KeyError:
        pass
    return {"token": token, "user": person}


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
