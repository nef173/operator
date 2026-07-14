"""Users & Access — the per-person, per-app RBAC model ported from NN Operations'
Master Settings → Users & Access.

NN's model: each PERSON has a NAME / position / photo / password, plus a map of which
apps they can open and their ROLE inside each app (Owner / Admin / Representative). There
is NO separate "username" and NO email — the person's NAME is their sign-in identity, just
like NN's "Invite person" (Name + Position + Password + App access).

Because real auth (Clerk) is still deferred, the "logged-in" user is simulated by an
`active_user_id` the operator can switch — every downstream gate (shell app list,
FinanceGuard) reflects the active user's access map.

Backed by the runlog app_settings key→JSON store (key = "users"). Passwords are stored
but NEVER echoed back over the API (only `has_password` is exposed).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from . import runlog

# ── Password hashing (stdlib only — no bcrypt/passlib dep) ─────────────────────────
# Stored form: "pbkdf2$<iterations>$<salt_hex>$<hash_hex>". Legacy rows created before
# real auth stored the password in plaintext; verify_password() still accepts those so
# nobody is locked out, and login upgrades them to a hash on first successful sign-in.
_PBKDF2_ITER = 240_000


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        # Legacy plaintext row — constant-time compare, upgraded on next login.
        return hmac.compare_digest(pw, stored)
    try:
        _, iter_s, salt_hex, hash_hex = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), int(iter_s))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _is_hashed(stored: str) -> bool:
    return bool(stored) and stored.startswith("pbkdf2$")


# ── Recoverable copy (owner-viewable) — encrypted at rest, NOT plaintext ────────────
# The pbkdf2 hash above is what authenticates sign-in and can never be reversed. To let an
# OWNER view/share the password they set for a teammate, we ALSO keep a separately
# encrypted copy (`password_enc`). Encryption is a stdlib SHA-256 keystream cipher + HMAC
# (no external dep for the Railway build). Key precedence: env USERS_SECRET_KEY, else a
# per-install random key persisted once in app_settings. Rows created before this feature
# have no `password_enc` and simply aren't revealable (owner resets to make one viewable).
_ENC_KEY_SETTING = "users_enc_key"


def _enc_key() -> bytes:
    env = os.environ.get("USERS_SECRET_KEY")
    if env:
        return hashlib.sha256(env.encode()).digest()
    st = runlog.setting_get(_ENC_KEY_SETTING)
    if st and st.get("k"):
        return bytes.fromhex(st["k"])
    key = secrets.token_bytes(32)
    runlog.setting_set(_ENC_KEY_SETTING, {"k": key.hex()})
    return key


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def encrypt_secret(pw: str) -> str:
    key = _enc_key()
    nonce = secrets.token_bytes(16)
    data = pw.encode()
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    mac = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]
    return f"sc1${nonce.hex()}${ct.hex()}${mac.hex()}"


def decrypt_secret(blob: str) -> str | None:
    if not blob:
        return None
    try:
        tag, nonce_h, ct_h, mac_h = blob.split("$", 3)
        if tag != "sc1":
            return None
        key = _enc_key()
        nonce, ct = bytes.fromhex(nonce_h), bytes.fromhex(ct_h)
        exp = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(exp, bytes.fromhex(mac_h)):
            return None
        pt = bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))
        return pt.decode()
    except Exception:
        return None

# NN's three per-app roles. `rep` == "Representative" (short id kept for the URL/JSON).
ROLES = ("owner", "admin", "rep")
ROLE_LABELS = {"owner": "Owner", "admin": "Admin", "rep": "Representative"}

# The grantable apps — mirrors web/lib/apps.ts OPERATOR_APPS ids. Keeping this in sync is
# the "one place" contract: a new app appends its id here + a row in apps.ts.
APP_IDS = (
    "research-listing",
    "product-feed",
    "multimarket",
    "finance",
    "product-mgmt",
    "issues",
    "tasks",
    "store-mgmt",
)

_SETTING_KEY = "users"

# NN-style: the app ships WITH a working owner login (no self-serve "create your owner"
# screen). The operator signs in with this default, then renames it + adds everyone else
# from inside the app (Settings → Access). Change it on first sign-in.
_DEFAULT_NAME = "admin"
_DEFAULT_PASSWORD = "admin"


def _seed() -> dict:
    """First-run state: a single Owner with full access AND the default admin/admin login,
    so sign-in works immediately and nothing is hidden until the operator scopes people."""
    uid = "u_owner"
    state = {
        "people": [
            {
                "id": uid,
                "name": _DEFAULT_NAME,
                "position": "",
                "photo": None,
                "password": hash_password(_DEFAULT_PASSWORD),
                "password_enc": encrypt_secret(_DEFAULT_PASSWORD),
                "access": {a: "owner" for a in APP_IDS},
            }
        ],
        "active_user_id": uid,
    }
    runlog.setting_set(_SETTING_KEY, state)
    return state


def _ensure_login(state: dict) -> bool:
    """No-lockout guarantee without a setup screen: if NO person has both a name and a
    password (a fresh seed, or an older empty-owner row from before this default existed),
    stamp the default admin/admin login onto the first person so sign-in always works.
    Returns True if it changed anything."""
    people = state.get("people", [])
    if not people:
        return False
    if any((p.get("name") or "").strip() and p.get("password") for p in people):
        return False
    p = people[0]
    changed = False
    if not (p.get("name") or "").strip():
        p["name"] = _DEFAULT_NAME
        changed = True
    if not p.get("password"):
        p["password"] = hash_password(_DEFAULT_PASSWORD)
        changed = True
    return changed


def _backfill_full_owners(state: dict) -> bool:
    """Self-heal for the extensibility contract: when a NEW app id is appended to APP_IDS,
    a person who is already a full-access owner (owner on every OTHER app) inherits it, so the
    shell never hides a freshly-added app from the owner. Deliberately-scoped admins/reps (who
    lack some other app, or hold it at a lower role) are left untouched. Returns True if changed."""
    changed = False
    for p in state.get("people", []):
        acc = p.setdefault("access", {})
        for aid in APP_IDS:
            if aid in acc:
                continue
            others = [x for x in APP_IDS if x != aid]
            if others and all(acc.get(o) == "owner" for o in others):
                acc[aid] = "owner"
                changed = True
    return changed


def _migrate_username_to_name(state: dict) -> bool:
    """One-time self-heal for rows written by the short-lived username-identity build:
    fold any stored `username` into `name` (name wins if present) and drop the field."""
    changed = False
    for p in state.get("people", []):
        if "username" in p:
            if not (p.get("name") or "").strip():
                p["name"] = p.get("username") or ""
            del p["username"]
            changed = True
    return changed


def _state() -> dict:
    raw = runlog.setting_get(_SETTING_KEY)
    if not raw or not raw.get("people"):
        return _seed()
    changed = _migrate_username_to_name(raw)
    changed = _backfill_full_owners(raw) or changed
    changed = _ensure_login(raw) or changed
    if changed:
        runlog.setting_set(_SETTING_KEY, raw)
    return raw


def _public(p: dict) -> dict:
    """A person WITHOUT the raw password — the only shape the API ever returns."""
    return {
        "id": p["id"],
        # NN-style: the NAME IS the identity (sign-in + display).
        "name": p.get("name", ""),
        "position": p.get("position", ""),
        "photo": p.get("photo"),
        "has_password": bool(p.get("password")),
        # True when a recoverable (encrypted) copy exists, so an owner can reveal it.
        "can_reveal": bool(p.get("password_enc")),
        "access": dict(p.get("access", {})),
    }


def _norm_access(access: dict | None) -> dict:
    """Keep only valid app→role pairs (drops unknown apps / roles)."""
    out: dict[str, str] = {}
    for k, v in (access or {}).items():
        if k in APP_IDS and v in ROLES:
            out[k] = v
    return out


def list_people() -> list[dict]:
    return [_public(p) for p in _state()["people"]]


def active_user() -> dict | None:
    st = _state()
    aid = st.get("active_user_id")
    for p in st["people"]:
        if p["id"] == aid:
            return _public(p)
    return _public(st["people"][0]) if st["people"] else None


def create_person(name: str, password: str = "", access: dict | None = None,
                  position: str = "", photo: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    st = _state()
    if _find_by_name(st, name):
        raise ValueError("a person with that name already exists")
    pid = "u_" + secrets.token_hex(4)
    person = {
        "id": pid,
        "name": name,
        "position": (position or "").strip(),
        "photo": photo,
        # Hash on write so the store never holds a plaintext password for a new person.
        "password": hash_password(password) if password else "",
        # Encrypted (recoverable) copy so an owner can view/re-share it later.
        "password_enc": encrypt_secret(password) if password else "",
        "access": _norm_access(access),
    }
    st["people"].append(person)
    runlog.setting_set(_SETTING_KEY, st)
    return _public(person)


def update_person(pid: str, name: str | None = None, password: str | None = None,
                  access: dict | None = None, position: str | None = None,
                  photo: str | None = None, photo_set: bool = False) -> dict:
    st = _state()
    for p in st["people"]:
        if p["id"] == pid:
            if name is not None and name.strip():
                n = name.strip()
                if _find_by_name(st, n, exclude=pid):
                    raise ValueError("a person with that name already exists")
                p["name"] = n
            if position is not None:
                p["position"] = position.strip()
            if password:  # blank = keep existing
                p["password"] = hash_password(password)
                p["password_enc"] = encrypt_secret(password)
            if photo_set:
                p["photo"] = photo
            if access is not None:
                p["access"] = _norm_access(access)
            runlog.setting_set(_SETTING_KEY, st)
            return _public(p)
    raise KeyError(pid)


# ── Auth support (used by auth.py) ─────────────────────────────────────────────────
def _find_by_name(st: dict, name: str, exclude: str | None = None) -> dict | None:
    name = (name or "").strip().lower()
    if not name:
        return None
    for p in st["people"]:
        if p.get("id") != exclude and (p.get("name", "") or "").strip().lower() == name:
            return p
    return None


def authenticate(name: str, password: str) -> dict | None:
    """Verify name+password. On success, upgrade a legacy plaintext row to a hash and
    return the PUBLIC person. Returns None on any mismatch (caller emits a generic 401)."""
    st = _state()
    p = _find_by_name(st, name)
    if not p or not verify_password(password, p.get("password", "")):
        return None
    changed = False
    if not _is_hashed(p.get("password", "")):
        p["password"] = hash_password(password)
        changed = True
    # Backfill a recoverable copy for rows created before password-viewing existed, using
    # the plaintext we have in-hand on this successful login.
    if not p.get("password_enc"):
        p["password_enc"] = encrypt_secret(password)
        changed = True
    if changed:
        runlog.setting_set(_SETTING_KEY, st)
    return _public(p)


def reveal_password(pid: str) -> str | None:
    """Decrypt the recoverable copy for OWNER-gated viewing. None = no recoverable copy on
    file (legacy row set before this feature, or decrypt failed). Raises KeyError if unknown."""
    for p in _state()["people"]:
        if p["id"] == pid:
            return decrypt_secret(p.get("password_enc", ""))
    raise KeyError(pid)


def is_owner(uid: str) -> bool:
    """True if this person holds the Owner role on at least one app — the gate for viewing
    other people's passwords."""
    for p in _state()["people"]:
        if p["id"] == uid:
            return any(r == "owner" for r in (p.get("access") or {}).values())
    return False


def public_by_id(pid: str) -> dict | None:
    for p in _state()["people"]:
        if p["id"] == pid:
            return _public(p)
    return None


def default_owner_id() -> str | None:
    """The uid a trusted pass-through (NN one-login) should sign in AS. Prefers the current
    active user when they're an owner, else the first owner on file, else the seeded owner —
    so a renamed/re-provisioned owner still resolves without hard-coding the seed uid."""
    people = _state()["people"]
    act = (active_user() or {}).get("id")
    if act and is_owner(act):
        return act
    for p in people:
        if any(r == "owner" for r in (p.get("access") or {}).values()):
            return p["id"]
    return people[0]["id"] if people else None


def bootstrap_owner_from_env() -> str | None:
    """Provision the real owner login from deployment env vars, so a live deploy (Railway)
    can be given the operator's OWN credentials WITHOUT ever putting a password in the repo.

    Reads OWNER_NAME (falls back to OWNER_USERNAME for older deploys) + OWNER_PASSWORD. When
    both are set it makes that name a full-access owner and (re)sets its password —
    idempotent, runs on every boot:
      • a person with that name already exists → refresh password + grant full owner access
      • the seeded default owner is still present (admin / never renamed) → rename it in place
      • otherwise → create the owner
    That owner is also made the active user. Returns the name provisioned, else None.
    Called from the API lifespan at startup."""
    name = (os.environ.get("OWNER_NAME") or os.environ.get("OWNER_USERNAME") or "").strip()
    password = os.environ.get("OWNER_PASSWORD") or ""
    if not name or not password:
        return None
    st = _state()
    people = st.get("people", [])
    full = {a: "owner" for a in APP_IDS}

    existing = _find_by_name(st, name)
    if existing:
        existing["password"] = hash_password(password)
        existing["name"] = name
        existing["access"] = full
        target = existing
    else:
        # Adopt the untouched seed owner (still on the default name) rather than pile a
        # second account next to it; else mint a fresh owner.
        seed = next((p for p in people if (p.get("name") or "").strip().lower() == _DEFAULT_NAME), None)
        if seed is not None:
            seed["name"] = name
            seed["password"] = hash_password(password)
            seed["access"] = full
            target = seed
        else:
            target = {
                "id": "u_" + secrets.token_hex(4),
                "name": name,
                "position": "",
                "photo": None,
                "password": hash_password(password),
                "access": full,
            }
            people.append(target)

    st["active_user_id"] = target["id"]
    runlog.setting_set(_SETTING_KEY, st)
    return name


def delete_person(pid: str) -> bool:
    st = _state()
    before = len(st["people"])
    st["people"] = [p for p in st["people"] if p["id"] != pid]
    if len(st["people"]) == before:
        return False
    if st.get("active_user_id") == pid and st["people"]:
        st["active_user_id"] = st["people"][0]["id"]
    runlog.setting_set(_SETTING_KEY, st)
    return True


def set_active(pid: str) -> dict | None:
    st = _state()
    if not any(p["id"] == pid for p in st["people"]):
        raise KeyError(pid)
    st["active_user_id"] = pid
    runlog.setting_set(_SETTING_KEY, st)
    return active_user()


def access_view() -> dict:
    """Effective view-mode for the active user. Backward compatible with the old
    {role, restricted} shape the shell + FinanceGuard consume, plus the richer RBAC
    (active user, per-app role map, role vocabulary)."""
    u = active_user()
    access = (u or {}).get("access", {})
    restricted = [a for a in APP_IDS if a not in access]
    role = "owner" if access.get("finance") == "owner" else "rep"
    return {
        "role": role,
        "restricted": restricted,
        "user": (
            {
                "id": u["id"],
                "name": u["name"],
                "position": u["position"],
                "photo": u["photo"],
            }
            if u
            else None
        ),
        "apps": access,
        "roles": list(ROLES),
        "role_labels": ROLE_LABELS,
        "app_ids": list(APP_IDS),
    }
