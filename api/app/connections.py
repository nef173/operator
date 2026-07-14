"""Connections — where the operator wires the app to the outside world.

Two kinds of credential live here, both persisted in the DB (`app_settings` key
`connections`, so they survive a Railway redeploy once DATABASE_URL points at Neon):

  * Shopify              — ONE app-wide Client ID + Secret (global), plus each store's
    myshopify domain (per store). The store's 24h Admin API token is MINTED on demand from
    the Client ID/Secret via Shopify's client-credentials grant — there is no per-store
    `shpat_` token to paste. This is what lets a job authenticate to that store.
  * Global API keys      — DataForSEO, Bright Data, Gemini (the locked vision/image model,
    which also powers the in-app Assistant gateway), OpenRouter (the OpenAI-compatible hub
    that serves the chat copilot — incl. Claude/GPT — when no Gemini key is set).

Security posture: this is a single-operator self-hosted control panel. Secrets are
stored as-is in the operator's own database and are NEVER returned raw over the API —
`public_view()` masks every secret to a `••••last4` preview plus a `configured` flag.
Writes (`update`) only touch the fields actually supplied, so you can change one key
without re-typing the others. `as_env()` is the seam a job runner uses to inject these
into a subprocess environment (the pipeline scripts already read these env names).

THIS MODULE IS THE SINGLE SOURCE OF TRUTH for every external credential the app uses.
Settings → Connections configures it once and it fans out to four consumers — nothing
downstream should read a credential from `os.environ` directly. The four seams:

  * `as_env()`            — global API keys → `jobs._base_env()` → every SUBPROCESS job
                            (pipeline scripts read the env names verbatim). Precedence is
                            `{**as_env(), **os.environ}`, so a real process env var wins.
  * `runtime_get(env)`    — a single global key for IN-PROCESS readers (the Assistant +
                            VisionScan image-QA call their LLM gateway from inside FastAPI,
                            not via a subprocess). Same precedence (process env wins, then DB).
  * `shopify_for(store)`  — resolved Shopify creds for one store: the shop domain plus a live
                            Admin token (auto-minted from the app-wide Client ID/Secret, cached
                            ~23h; a manually-set legacy `admin_token` overrides).
  * `google_env_for(store)` — per-store Google ids (Ads customer + Merchant Center) mapped
                            onto the env names downstream Ads/GMC jobs read; merged over
                            `as_env()` in `jobs._run_auto`.

ADDING A NEW CREDENTIAL (the extensibility contract): append one row to `API_FIELDS`
(account-wide) or `STORE_FIELDS` (per store) — the field registry is the allow-list, the UI
renders it automatically (grouped by `group`), and `public_view`/`update` pick it up with no
other change. Then route the consumer through the matching seam above — a subprocess job gets
it free via `as_env()`; an in-process reader must call `runtime_get(env)` (never read
`os.environ[...]` directly, or it bypasses the one setup surface). OAuth/MCP integrations that
don't fit the paste-a-key model go in `INTEGRATIONS` and get their own client module
(see `markifact.py`), surfaced as info/connect rows rather than editable secret fields.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import google_oauth, runlog

_SETTING_KEY = "connections"

# The global API keys the pipeline understands, by the exact env-var name each script
# reads. `secret=False` fields (usernames / public ids) are shown in full; everything
# else is masked. These are account-wide (not per store) — one set powers every store.
# The Google group is the Google Ads OAuth *app* credential + the manager (MCC) login id;
# the *which account to act on* customer id is per store (see STORE_FIELDS below).
API_FIELDS: list[dict] = [
    # ── Shopify: NO app-wide Client ID/Secret field. The "one app, all stores" idea is
    # structurally impossible — each store is its own Shopify organization, and a dev-dashboard
    # app "can only be installed on stores that are part of the same organization". Every store
    # therefore carries its OWN client_id/client_secret (STORE_FIELDS, entered on the store row).
    # shopify_app_client() still honors a shell SHOPIFY_CLIENT_ID/SECRET env pair as a fallback.
    {"env": "DATAFORSEO_USERNAME", "label": "DataForSEO Username", "group": "Data", "secret": False},
    {"env": "DATAFORSEO_PASSWORD", "label": "DataForSEO Password", "group": "Data", "secret": True},
    {"env": "BRIGHTDATA_API_TOKEN", "label": "Bright Data Token", "group": "Data", "secret": True},
    {"env": "BRIGHTDATA_TEMU_DATASET", "label": "Bright Data Temu Dataset ID (Temu product finding)", "group": "Data", "secret": False, "placeholder": "gd_… — subscribe to a Temu dataset in the BD Web-Scraper library"},
    {"env": "BRIGHTDATA_DATASET_TOKEN", "label": "Bright Data Dataset-scoped Token (Temu; falls back to the BD Token)", "group": "Data", "secret": True, "placeholder": "only if the plain BD token 403s the Dataset API"},
    {"env": "TMAPI_TOKEN", "label": "TMAPI Token (1688 sourcing)", "group": "Data", "secret": True},
    {"env": "APIFY_TOKEN", "label": "Apify Token (Temu product finding)", "group": "Data", "secret": True, "placeholder": "apify_api_… — runs the crw/temu-products-scraper actor (BrightData can't scrape Temu)"},
    # TrendTrack public REST API (api.trendtrack.io) — the premium spy/discovery source (scaling US
    # Shopify stores, Meta ads, winning products). Was MCP-only (agent session); the public API lets
    # the hosted worker call it directly. Create a workspace-scoped key: TrendTrack → workspace
    # settings → enable Public API access → "Get API key" (a `tt_live_…` token). Paste it here.
    {"env": "TRENDTRACK_API_TOKEN", "label": "TrendTrack API Token (spy / discovery)", "group": "Data", "secret": True, "placeholder": "tt_live_… — enable Public API in TrendTrack workspace settings"},
    # ── Bright Data — the rest are NOT extra "keys". The single token above is your one Bright
    # Data API key; it also covers the dataset-token spelling via the alias below. The SERP zone /
    # CN proxy are separate Bright Data products with their own login.
    # (The Temu/Amazon "dataset ID" fields were removed 2026-07-08 — no app path used them: Temu &
    # Amazon flow through candidate_queue add-signal lanes + supplier_import.py, which use the
    # Bright Data API token above, not the Dataset API.)
    # Bright Data SERP zone — powers the Google Lens reverse-image search in china-source-match
    # (a SERP zone, NOT the residential proxy). Customer id + zone are public-ish; the zone
    # password is the secret.
    {"env": "BRIGHTDATA_CUSTOMER_ID", "label": "Bright Data Customer ID (SERP zone, optional)", "group": "Data", "secret": False},
    {"env": "BRIGHTDATA_SERP_ZONE", "label": "Bright Data SERP Zone name (optional)", "group": "Data", "secret": False},
    {"env": "BRIGHTDATA_SERP_ZONE_PASSWORD", "label": "Bright Data SERP Zone Password (optional)", "group": "Data", "secret": True},
    {"env": "BRIGHTDATA_BROWSER_CDP", "label": "Bright Data Scraping-Browser CDP (Google Sponsored-PLA capture — auto-provisioned)", "group": "Data", "secret": True, "placeholder": "wss://brd-customer-…@brd.superproxy.io:9222 — auto-filled by the BD provision button once Customer ID is set"},
    # CN residential proxy URL for 1688 mtop scraping (embeds its own creds → secret).
    {"env": "BD_CN_PROXY", "label": "Bright Data CN Proxy URL (1688, optional)", "group": "Data", "secret": True},
    {"env": "OPENROUTER_API_KEY", "label": "OpenRouter API Key", "group": "AI", "secret": True},
    # Gemini is the LOCKED model for vision (image QA), image generation (nano_banana = Gemini 2.5
    # Flash Image) and the in-app Assistant — but the operator does NOT need a separate Gemini key:
    # if only OpenRouter is set, `_assistant_gateway()` routes every Gemini call through OpenRouter
    # (model `google/gemini-2.5-flash`). This field is therefore OPTIONAL — fill it only to hit the
    # Gemini API directly (slightly cheaper, and the direct-transport path for image gen). Either way
    # the key is entered once; runtime_get() reuses it for the in-process Assistant/Vision gateway.
    {"env": "GEMINI_API_KEY", "label": "Gemini API Key (optional — OpenRouter serves Gemini if blank)", "group": "AI", "secret": True},
    # The EASY Merchant Center connection. For stores you own/admin, a service account is far
    # simpler than the 3-legged OAuth flow below: create a service account in Google Cloud,
    # enable the Merchant API, add its email as a user in Merchant Center → Account access, and
    # paste the downloaded JSON key here. No consent popup, no refresh tokens. The GMC reader
    # jobs (account issues / product disapprovals / reports) authenticate server-to-server with
    # this key via `as_env()`. Multiline because a service-account key is a JSON document.
    {"env": "GOOGLE_SERVICE_ACCOUNT_JSON", "label": "Service Account JSON key", "group": "Google Merchant Center", "secret": True, "multiline": True, "placeholder": "Paste the full service-account JSON key file contents here ({ \"type\": \"service_account\", … })"},
    {"env": "GOOGLE_ADS_DEVELOPER_TOKEN", "label": "Google Ads Developer Token", "group": "Google Ads", "secret": True},
    {"env": "GOOGLE_ADS_CLIENT_ID", "label": "Google Ads OAuth Client ID", "group": "Google Ads", "secret": False},
    {"env": "GOOGLE_ADS_CLIENT_SECRET", "label": "Google Ads OAuth Client Secret", "group": "Google Ads", "secret": True},
    {"env": "GOOGLE_ADS_REFRESH_TOKEN", "label": "Google Ads Refresh Token", "group": "Google Ads", "secret": True},
    {"env": "GOOGLE_ADS_LOGIN_CUSTOMER_ID", "label": "Google Ads Manager (MCC) ID", "group": "Google Ads", "secret": False, "placeholder": "123-456-7890"},
    # CJ Dropshipping — the fallback supplier path (used only for SKUs the private agent / 1688 can't carry).
    {"env": "CJ_EMAIL", "label": "CJ Dropshipping Email", "group": "Sourcing", "secret": False},
    {"env": "CJ_API_KEY", "label": "CJ Dropshipping API Key", "group": "Sourcing", "secret": True},
    # (AdsPower removed 2026-07-08 — the Google-Shopping competitor scan now runs entirely
    # server-side from DataForSEO's structured API; no local real-Chrome profile is needed.)
    # ── Chat escalation (Orders & Issues) — the provider adapter posts a 1:1 message when a dispute is
    # escalated. BOTH Slack and Pumble are supported via one adapter (chat_adapter.py); `CHAT_PROVIDER`
    # selects which. Slack = Bot token (xoxb-…) → chat.postMessage; Pumble = an addon API key →
    # sendMessage. The default channel is where an escalation lands when a dispute has no channel of
    # its own. Full two-way thread mirroring (webhooks/files/reactions) is deferred infra — this is the
    # one-way "ping a human" path the escalate button needs.
    {"env": "CHAT_PROVIDER", "label": "Chat provider — 'slack' or 'pumble'", "group": "Chat", "secret": False, "placeholder": "slack"},
    {"env": "SLACK_BOT_TOKEN", "label": "Slack Bot Token (xoxb-…)", "group": "Chat", "secret": True},
    {"env": "SLACK_DEFAULT_CHANNEL", "label": "Slack default channel (id or #name)", "group": "Chat", "secret": False, "placeholder": "#disputes"},
    {"env": "PUMBLE_API_KEY", "label": "Pumble API Key (addon)", "group": "Chat", "secret": True},
    {"env": "PUMBLE_DEFAULT_CHANNEL", "label": "Pumble default channel", "group": "Chat", "secret": False, "placeholder": "disputes"},
    # Trustpilot reputation — Trustpilot blocks direct server access, so store scores are read
    # through the OpenWeb Ninja Trustpilot API (free tier ~100 req/month, no card; scores cached
    # 24h). Entered once; trustpilot.py reads it via runtime_get(). See the Trustpilot page in the
    # Multimarket (store-management) app.
    {"env": "OPENWEB_NINJA_API_KEY", "label": "OpenWeb Ninja API Key (Trustpilot)", "group": "Trustpilot", "secret": True, "placeholder": "for live Trustpilot scores"},
]

# Per-store credential fields, grouped into the "setup view" sub-sections each store has.
# `group` drives the UI grouping (Shopify vs Google). Shopify creds are what let a job
# push to a store; the Google per-store ids are the Ads customer + Merchant Center this
# store acts on. The store-scoped values all live under one dict per store (storage key
# `shopify`, kept for back-compat), so `shopify_for(store)` keeps working unchanged.
STORE_FIELDS: list[dict] = [
    # Each store carries its OWN Shopify custom-app Client ID + Secret (below) — the 24h Admin token
    # is minted from them via the client-credentials grant. `admin_token` remains an OPTIONAL manual
    # override for any legacy store still using a `shpat_` custom-app token; leave it blank to use
    # the auto-minted token.
    {"key": "display_name", "label": "Store name (display)", "group": "Store", "secret": False, "placeholder": "e.g. Decors Deluxe"},
    {"key": "shop_domain", "label": "myshopify domain", "group": "Store", "secret": False, "placeholder": "your-store.myshopify.com"},
    # This store's OWN custom-app credentials. Create a custom app in the store's Shopify admin
    # (Settings → Apps → Develop apps), paste the copied scopes into it, then paste its Client ID +
    # Secret here. The 24h Admin token is minted from THESE via the client-credentials grant.
    # `admin_token` below is an optional direct override (a `shpat_` token) that skips minting.
    {"key": "client_id", "label": "Shopify Client ID (this store's custom app)", "group": "Store", "secret": False, "placeholder": "edc2c2144d1d0ca…"},
    {"key": "client_secret", "label": "Shopify Client Secret", "group": "Store", "secret": True, "placeholder": "shpss_… (this store's app secret)"},
    {"key": "admin_token", "label": "Admin API token (optional — auto-minted from Client ID/Secret)", "group": "Store", "secret": True, "placeholder": "leave blank — minted automatically"},
    # Data cut-off: how far back the app pulls this store's data (orders, ads, parcels, P&L).
    # BLANK = ALL history (no cut-off — the default). Enter an ISO date (YYYY-MM-DD) ONLY when a
    # store should be read from a special start date onward (e.g. a rebrand or ownership-change date).
    {"key": "data_start_date", "label": "Data start date (blank = all history)", "group": "Store", "secret": False, "placeholder": "all history — leave blank"},
    # These are NOT how you connect Google — they only say WHICH account to act on after the
    # OAuth "Connect Google" flow (the `google` integration below) is authorized. The UI surfaces
    # them as a picker populated from `google_oauth.list_accounts()`, with manual entry as a fallback.
    {"key": "google_ads_customer_id", "label": "Google Ads Customer ID", "group": "Google", "secret": False, "placeholder": "123-456-7890"},
    {"key": "merchant_center_id", "label": "Merchant Center ID", "group": "Google", "secret": False, "placeholder": ""},
    # ── Finance / P&L per-store parameters (calcPL knobs). These are NOT credentials — they are the
    # per-store profit-model settings the Finance app's calcPL reads (COGS formula rate + invoice
    # coverage floor + payment-processor fee model). Kept here so the ONE Settings → Connections
    # surface is also where Finance is configured per store; finance.py reads them via store_setting()
    # and falls back to sensible defaults when blank. Non-secret → shown in full.
    {"key": "cog_rate", "label": "Finance · COGS formula rate (fallback, e.g. 0.30)", "group": "Finance", "secret": False, "placeholder": "0.30"},
    {"key": "cog_floor_pct", "label": "Finance · Invoice-coverage floor (e.g. 0.25)", "group": "Finance", "secret": False, "placeholder": "0.25"},
    {"key": "tx_rate", "label": "Finance · Transaction fee rate (e.g. 0.056)", "group": "Finance", "secret": False, "placeholder": "0.056"},
    {"key": "tx_fix_fee", "label": "Finance · Fixed fee per order (e.g. 0.25)", "group": "Finance", "secret": False, "placeholder": "0.25"},
    {"key": "report_currency", "label": "Finance · Reporting currency (e.g. EUR)", "group": "Finance", "secret": False, "placeholder": "EUR"},
    # ── Orders & Issues per-store — ParcelPanel API key (each store has its own ParcelPanel account).
    # The parcel-tracking sync reads this to pull in-transit parcels; blank = that store's parcel sync
    # is skipped (the rest of the Issues app still works from Shopify orders + manual disputes).
    {"key": "parcelpanel_api_key", "label": "Issues · ParcelPanel API key (optional)", "group": "Orders & Issues", "secret": True, "placeholder": ""},
]
# Map per-store Google ids onto the env names downstream jobs read.
_STORE_GOOGLE_ENV = {
    "google_ads_customer_id": "GOOGLE_ADS_CUSTOMER_ID",
    "merchant_center_id": "GOOGLE_MERCHANT_CENTER_ID",
}
# Integrations that DON'T fit the paste-a-key model — they authenticate via OAuth and are
# reached over a hosted endpoint, not an API key. They still belong in the one setup surface
# (so the operator sees the whole wiring in one place), but they're rendered as info/connect
# rows, not editable secret fields. Verified 2026-06-21:
#   * Google Ads native API/MCP is strictly READ-ONLY (GAQL/reporting) — covered by the
#     GOOGLE_ADS_* key fields above; good for the 09 Scale reads, not for writes.
#   * Markifact has NO plain REST API — it's a remote MCP (OAuth 2.1 + PKCE, no key) that
#     carries the WRITE layer (create Search/PMax, edit creative, negatives, pause, budgets,
#     human-in-the-loop). The MCP *is* the API. This is the campaign-write path; it stays
#     manual/deferred until the OAuth-MCP client flow is built (ads execution is deferred).
INTEGRATIONS: list[dict] = [
    {
        "id": "google",
        "name": "Google (Ads + Merchant Center)",
        "kind": "oauth",
        "endpoint": "accounts.google.com / googleads + content APIs",
        "auth": "OAuth 2.0 + PKCE (uses the Google Ads OAuth client id/secret above)",
        "purpose": (
            "The REAL connection to Google. Authorize once — the refresh token authorizes "
            "every Ads/GMC job. The per-store Ads/Merchant ids only pick WHICH account to act on."
        ),
        "status": "oauth",  # live status (connected / client_ready) attached in public_view
    },
]

SHOPIFY_FIELDS = STORE_FIELDS  # back-compat alias for any importer of the old name
_STORE_KEYS = {f["key"] for f in STORE_FIELDS}
_SHOPIFY_KEYS = _STORE_KEYS  # back-compat alias
_API_ENVS = {f["env"] for f in API_FIELDS}
_API_SECRET = {f["env"] for f in API_FIELDS if f["secret"]}

# Some scripts read the SAME secret under a different spelling than the registry's canonical
# env name. Rather than show the operator two fields for one value, the registry stores it once
# and `as_env()` also exports it under each alias (via setdefault, so an explicitly-set real
# shell value of the alias is never clobbered). Bright Data: the registry key is
# `BRIGHTDATA_API_TOKEN`, but several pipeline scripts read `BRIGHT_DATA_API_TOKEN`.
_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    # Bright Data: the registry key is `BRIGHTDATA_API_TOKEN`, but several pipeline scripts read
    # `BRIGHT_DATA_API_TOKEN`, and the Dataset-token field is documented as "falls back to the token
    # above" — so the one token also satisfies the dataset-token spelling. Enter the Bright Data
    # token ONCE; it covers all three names (an explicitly-set dataset token still wins via setdefault).
    "BRIGHTDATA_API_TOKEN": ("BRIGHT_DATA_API_TOKEN", "BRIGHT_DATA_DATASET_TOKEN"),
}


def _raw() -> dict:
    data = runlog.setting_get(_SETTING_KEY) or {}
    if not isinstance(data, dict):
        return {"api": {}, "shopify": {}, "profile": {}}
    data.setdefault("api", {})
    data.setdefault("shopify", {})
    data.setdefault("profile", {})  # per-store store-level facts pulled from Shopify (language/currency/…)
    return data


def _mask(value: str | None, secret: bool) -> str:
    """Masked preview for the UI. Non-secret -> shown in full. Secret -> ••••last4."""
    if not value:
        return ""
    if not secret:
        return value
    tail = value[-4:] if len(value) > 4 else ""
    return f"••••{tail}" if tail else "••••"


def public_view(stores: list[str]) -> dict:
    """The masked, render-safe view. Per API field and per (store × Shopify field):
    `configured` (is a value set) + `masked` (safe preview). Never the raw secret."""
    raw = _raw()
    api_vals = raw.get("api", {})
    shop_vals = raw.get("shopify", {})
    profile_vals = raw.get("profile", {})

    api = []
    for f in API_FIELDS:
        v = api_vals.get(f["env"]) or ""
        api.append({
            "env": f["env"], "label": f["label"], "group": f["group"], "secret": f["secret"],
            "configured": bool(v), "masked": _mask(v, f["secret"]),
            "placeholder": f.get("placeholder", ""), "multiline": f.get("multiline", False),
        })

    shopify = []
    for store in stores:
        sv = shop_vals.get(store, {}) if isinstance(shop_vals.get(store), dict) else {}
        fields = []
        for f in STORE_FIELDS:
            v = sv.get(f["key"]) or ""
            fields.append({
                "key": f["key"], "label": f["label"], "group": f["group"], "secret": f["secret"],
                "placeholder": f.get("placeholder", ""), "configured": bool(v), "masked": _mask(v, f["secret"]),
            })
        prof = profile_vals.get(store) if isinstance(profile_vals.get(store), dict) else None
        shopify.append({
            "store": store,
            # A store is "connected" if it can authenticate — either a legacy manual admin_token OR
            # (its shop_domain + the app-wide Client ID/Secret, which mint a token on demand). No
            # network call here (public_view runs on every settings load).
            "connected": store_has_shopify_auth(store, sv),
            "ads_ready": bool(sv.get("google_ads_customer_id")),
            "fields": fields,
            # Pulled store-level facts (language/currency/timezone/markets/catalog). Not secret —
            # returned in full so the UI can show them and confirm what's flowing downstream.
            "profile": prof,
        })
    # Attach live status to the integrations that have a real client (Google OAuth).
    integrations = []
    for it in INTEGRATIONS:
        row = dict(it)
        if it["id"] == "google":
            try:
                st = google_oauth.public_status()
            except Exception:
                st = {"connected": False, "client_ready": False}
            row["connected"] = bool(st.get("connected"))
            row["status"] = "connected" if st.get("connected") else (
                "client ready" if st.get("client_ready") else "needs client"
            )
            row["detail"] = st
        integrations.append(row)
    return {"api": api, "shopify": shopify, "integrations": integrations}


def update(payload: dict) -> dict:
    """Merge supplied values into the store. Only keys present (and non-empty) are written,
    so updating one field never wipes the rest. Empty-string explicitly CLEARS a field
    (lets the operator remove a credential). Returns the fresh masked view's raw store."""
    raw = _raw()
    api_vals = dict(raw.get("api", {}))
    shop_vals = {k: dict(v) for k, v in raw.get("shopify", {}).items() if isinstance(v, dict)}

    incoming_api = payload.get("api") or {}
    for env, val in incoming_api.items():
        if env not in _API_ENVS:
            continue  # ignore unknown keys — the field registry is the allow-list
        if val is None:
            continue
        val = str(val).strip()
        if val == "":
            api_vals.pop(env, None)
        else:
            api_vals[env] = val

    incoming_shop = payload.get("shopify") or {}
    for store, fields in incoming_shop.items():
        if not isinstance(fields, dict):
            continue
        cur = dict(shop_vals.get(store, {}))
        for key, val in fields.items():
            if key not in _SHOPIFY_KEYS or val is None:
                continue
            val = str(val).strip()
            if val == "":
                cur.pop(key, None)
            else:
                cur[key] = val
        if cur:
            shop_vals[store] = cur
        else:
            shop_vals.pop(store, None)

    # Preserve the pulled store-profile section — `update` only touches credentials.
    runlog.setting_set(_SETTING_KEY, {"api": api_vals, "shopify": shop_vals, "profile": raw.get("profile", {})})

    # ALWAYS auto-sync: any store whose creds changed on ANY write path (Connections UI,
    # the pl-dashboard migration, module callers) gets an immediate background data sync
    # the moment it can authenticate — the operator never has to click Sync after adding
    # a store. Lazy import (store_sync imports this module); no-op-safe (skips if no auth).
    try:
        from . import store_sync
        for store in incoming_shop:
            if isinstance(incoming_shop.get(store), dict) and store_has_shopify_auth(
                store, shop_vals.get(store)
            ):
                store_sync.run_async(store)
    except Exception:  # noqa: BLE001 — a sync trigger must never fail a credentials write
        pass
    return {"api": api_vals, "shopify": shop_vals}


def forget_store(store: str) -> None:
    """Drop ALL persisted per-store data (credentials + pulled profile) for a deleted store,
    so a re-added store of the same key starts clean. No-op if the store has no saved data."""
    raw = _raw()
    shop_vals = {k: v for k, v in raw.get("shopify", {}).items() if k != store}
    prof_vals = {k: v for k, v in raw.get("profile", {}).items() if k != store}
    runlog.setting_set(_SETTING_KEY, {"api": raw.get("api", {}), "shopify": shop_vals, "profile": prof_vals})


def as_env() -> dict[str, str]:
    """The global API keys as an env dict, for a job runner to inject into a subprocess.
    Per-store Shopify creds are resolved separately (they're store-scoped). Empty values
    are skipped so we never clobber a real shell env var with a blank."""
    raw = _raw()
    out = {k: str(v) for k, v in (raw.get("api") or {}).items() if k in _API_ENVS and v}
    for canonical, aliases in _ENV_ALIASES.items():
        val = out.get(canonical)
        if val:
            for alias in aliases:
                out.setdefault(alias, val)
    return out


# The in-app Assistant + VisionScan + translate call an OpenAI-compatible gateway. Rather than make
# the operator paste the same key a second/third time, the gateway DERIVES from a key they ALREADY
# entered — there's no separate "Assistant LLM API Key" field. Resolution order (first key wins):
#   1. Gemini  — also the LOCKED vision/image model; its OpenAI-compatible surface is the base below.
#   2. OpenRouter — the universal OpenAI-compatible hub (serves Claude/GPT too) for the chat copilot
#      when no Gemini key is set. Vision QA still wants Gemini, but chat degrades to OpenRouter.
# An explicit shell ASSISTANT_LLM_* always overrides this derivation.
_GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_OPENROUTER_DEFAULT_MODEL = "google/gemini-2.5-flash"


def _assistant_gateway() -> dict[str, str]:
    """The in-app Assistant/Vision LLM gateway, derived from a key the operator ALREADY entered —
    so the same secret is never typed twice. Gemini first (it's also the locked vision model), else
    OpenRouter. Returns {} when neither key is set (callers then degrade gracefully)."""
    gem = os.environ.get("GEMINI_API_KEY") or as_env().get("GEMINI_API_KEY")
    if gem:
        return {
            "ASSISTANT_LLM_API_KEY": gem,
            "ASSISTANT_LLM_BASE_URL": _GEMINI_OPENAI_BASE,
            "ASSISTANT_LLM_MODEL": _GEMINI_DEFAULT_MODEL,
        }
    orouter = os.environ.get("OPENROUTER_API_KEY") or as_env().get("OPENROUTER_API_KEY")
    if orouter:
        return {
            "ASSISTANT_LLM_API_KEY": orouter,
            "ASSISTANT_LLM_BASE_URL": _OPENROUTER_BASE,
            "ASSISTANT_LLM_MODEL": _OPENROUTER_DEFAULT_MODEL,
        }
    return {}


# The operator's Costs-page model SELECTOR (costs.agent_model) spelled for OpenRouter. Only the
# CHAT assistant follows the selector — vision QA, translate and multimarket stay on the locked
# cheap Gemini Flash tier regardless (feedback_gemini_is_the_vision_model), so picking a flagship
# text model never silently 10x-es the per-image / per-translation cost.
_OPENROUTER_MODEL_IDS = {
    "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
    "gpt-5-mini": "openai/gpt-5-mini",
    "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
    "gpt-5-4": "openai/gpt-5.4",
    "gpt-5-5": "openai/gpt-5.5",
    "claude-opus-4-8": "anthropic/claude-opus-4.8",
}


def agent_text_model() -> str | None:
    """The CHAT-assistant text model: the operator's selected agent model (Settings → Costs),
    spelled for whichever gateway is active. Through OpenRouter the selector maps to its
    OpenRouter id; on the Gemini-direct surface only Gemini is servable, so the gateway default
    stays. An explicit ASSISTANT_LLM_MODEL (env or stored) always wins. None when no gateway."""
    explicit = os.environ.get("ASSISTANT_LLM_MODEL") or as_env().get("ASSISTANT_LLM_MODEL")
    if explicit:
        return explicit
    gw = _assistant_gateway()
    if not gw:
        return None
    if "openrouter" in (gw.get("ASSISTANT_LLM_BASE_URL") or "").lower():
        from . import costs  # lazy — costs imports runlog/config only, but keep the seam thin
        mapped = _OPENROUTER_MODEL_IDS.get(costs.agent_model())
        if mapped:
            return mapped
    return gw.get("ASSISTANT_LLM_MODEL")


def runtime_get(env: str) -> str | None:
    """Resolve ONE configured value for IN-PROCESS readers (the Assistant + VisionScan QA call
    their LLM gateway from the FastAPI process, not via a subprocess `as_env()` injection). Real
    process env wins — same precedence as `jobs._base_env()` (`{**as_env(), **os.environ}`) — then
    the DB-stored connection. This is the seam that lets a key set in Settings → Connections power
    the in-app assistant/vision layer without a hand-set shell env var. Returns None if unset.

    Single-entry rule: the Assistant/Vision LLM gateway has no UI field of its own — its base URL,
    API key, and model all DERIVE from a provider key the operator already entered (Gemini, else
    OpenRouter), so the same key is never re-typed."""
    direct = os.environ.get(env) or as_env().get(env)
    if direct:
        return direct
    if env in ("ASSISTANT_LLM_API_KEY", "ASSISTANT_LLM_BASE_URL", "ASSISTANT_LLM_MODEL"):
        return _assistant_gateway().get(env)
    return None


# ── Shopify Admin token minting (client-credentials grant) ──────────────────────────────────
# Shopify's own-organization apps mint a 24h Admin API access token by POSTing the app-wide
# Client ID + Secret to a store's token endpoint. We cache the minted token per store (keyed by a
# fingerprint of shop_domain + client id, so a credential change invalidates it) and refresh when
# less than ~1h of the 24h life remains. All-stdlib urllib, mirroring shopify.py's transport.
_SHOPIFY_TOKEN_ENDPOINT = "/admin/oauth/access_token"
_TOKEN_REFRESH_BUFFER = 3600  # seconds before expiry at which we proactively re-mint
_token_cache: dict[str, dict] = {}  # store -> {"token", "expires_at", "fingerprint"}


def _shop_host(shop_domain: str) -> str:
    return (shop_domain or "").strip().replace("https://", "").replace("http://", "").rstrip("/")


def shopify_app_client() -> tuple[str, str]:
    """The app-wide Shopify Client ID + Secret (client-credentials grant). Process env wins over the
    DB-stored connection (same precedence as every other seam). ('', '') when unset."""
    api = _raw().get("api", {}) or {}
    cid = os.environ.get("SHOPIFY_CLIENT_ID") or api.get("SHOPIFY_CLIENT_ID") or ""
    secret = os.environ.get("SHOPIFY_CLIENT_SECRET") or api.get("SHOPIFY_CLIENT_SECRET") or ""
    return str(cid).strip(), str(secret).strip()


def _mint_admin_token(shop_domain: str, client_id: str, client_secret: str) -> dict:
    """Exchange the app's Client ID + Secret for a 24h Admin API token for one owned store via the
    client-credentials grant. Returns {"token", "expires_at"} on success, else {"error"}. Never
    raises — the caller keeps the no-raise contract of shopify_for()."""
    url = f"https://{_shop_host(shop_domain)}{_SHOPIFY_TOKEN_ENDPOINT}"
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (operator's own store)
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else str(e)
        return {"error": f"Shopify token grant {e.code}: {detail}"}
    except urllib.error.URLError as e:
        return {"error": f"could not reach Shopify token endpoint: {e.reason}"}
    except Exception as e:  # noqa: BLE001 — malformed JSON / anything else → report, don't crash
        return {"error": f"Shopify token grant failed: {e}"}
    tok = (body or {}).get("access_token")
    if not tok:
        return {"error": f"Shopify token grant returned no access_token: {str(body)[:160]}"}
    ttl = int((body or {}).get("expires_in") or 86399)
    return {"token": str(tok), "expires_at": time.time() + ttl}


def clear_token_cache(store: str | None = None) -> None:
    """Drop the cached minted Admin token so the next call re-mints. Call after the store's
    Shopify app scopes change (a reinstall grants new scopes, but the ~24h-cached token still
    carries the old ones). No arg clears every store."""
    if store is None:
        _token_cache.clear()
    else:
        _token_cache.pop(store, None)


def _client_for_store(sv: dict) -> tuple[str, str]:
    """This store's own custom-app Client ID + Secret if BOTH are set, otherwise the app-wide pair.
    Lets each store authenticate as its own custom app while keeping the single-app fallback."""
    cid = str(sv.get("client_id") or "").strip()
    secret = str(sv.get("client_secret") or "").strip()
    if cid and secret:
        return cid, secret
    return shopify_app_client()


def client_secret_for(store: str) -> str:
    """This store's Shopify app Client Secret (own if set, else app-wide) — the key Shopify signs
    webhooks with (X-Shopify-Hmac-Sha256). Empty string when none is configured."""
    sv = (_raw().get("shopify") or {}).get(store) or {}
    if not isinstance(sv, dict):
        return ""
    return _client_for_store(sv)[1]


def store_for_shop_domain(shop_domain: str) -> str | None:
    """Map an incoming X-Shopify-Shop-Domain (e.g. 'foo.myshopify.com') back to its store key."""
    host = _shop_host(shop_domain).lower()
    if not host:
        return None
    for store, sv in (_raw().get("shopify") or {}).items():
        if isinstance(sv, dict) and _shop_host(sv.get("shop_domain") or "").lower() == host:
            return store
    return None


def _resolve_admin_token(store: str, shop_domain: str, sv: dict) -> dict:
    """Live Admin token for a store, minting + caching via the client-credentials grant. Uses this
    store's own Client ID/Secret when set, else the app-wide pair. Returns {"token"} on success,
    {"error"} on a mint failure, or {} when no client creds or shop domain are set (nothing to try)."""
    cid, secret = _client_for_store(sv)
    if not (shop_domain and cid and secret):
        return {}
    fingerprint = f"{_shop_host(shop_domain)}|{cid}"
    now = time.time()
    cached = _token_cache.get(store)
    if (cached and cached.get("fingerprint") == fingerprint
            and (cached.get("expires_at", 0) - _TOKEN_REFRESH_BUFFER) > now):
        return {"token": cached["token"]}
    minted = _mint_admin_token(shop_domain, cid, secret)
    if minted.get("token"):
        _token_cache[store] = {
            "token": minted["token"], "expires_at": minted["expires_at"], "fingerprint": fingerprint,
        }
        return {"token": minted["token"]}
    return {"error": minted.get("error")}


def store_has_shopify_auth(store: str, sv: dict | None = None) -> bool:
    """Whether a store CAN authenticate to Shopify — WITHOUT a network call (so it's safe in
    public_view / setup-guide gating). True if a legacy admin_token is set, OR the store has a
    shop_domain and the app-wide Client ID + Secret are both configured."""
    if sv is None:
        sv = (_raw().get("shopify") or {}).get(store) or {}
    if not isinstance(sv, dict):
        return False
    if (sv.get("admin_token") or "").strip():
        return True
    cid, secret = _client_for_store(sv)
    return bool((sv.get("shop_domain") or "").strip() and cid and secret)


def shopify_for(store: str) -> dict:
    """Resolved Shopify creds for one store (for a job/reader to authenticate). Includes the shop
    domain plus a live `admin_token`: a manually-set legacy token wins, otherwise one is minted from
    the app-wide Client ID/Secret via the client-credentials grant (cached ~23h). On a mint failure
    the returned dict carries `auth_error` instead of `admin_token`. Returns {} if nothing is set.
    Internal use only — never serialize this to the API response (it contains a live token)."""
    raw = _raw()
    sv = (raw.get("shopify") or {}).get(store)
    out = dict(sv) if isinstance(sv, dict) else {}
    # Legacy manual token (a `shpat_` custom-app token) is an explicit override — use it as-is.
    if (out.get("admin_token") or "").strip():
        return out
    shop_domain = (out.get("shop_domain") or "").strip()
    if shop_domain:
        res = _resolve_admin_token(store, shop_domain, out)
        if res.get("token"):
            out["admin_token"] = res["token"]
        elif res.get("error"):
            out["auth_error"] = res["error"]
    return out


# The Admin API scopes every part of the app needs on a store — the SINGLE SOURCE OF TRUTH
# (the Settings UI fetches this list via /api/shopify/scopes so the copy-box can never drift).
# Grouped by which app surface consumes each scope; keep in sync when a new surface calls Shopify.
REQUIRED_SHOPIFY_SCOPES: list[str] = [
    # Listing / catalog build + Product-Mgmt cost push
    "read_products", "write_products",
    "read_inventory", "write_inventory",
    "read_price_rules", "read_discounts", "read_product_listings",
    # NOTE: the market price-push suite (priceListFixedPricesAdd) needs NO extra scope — Shopify
    # authorizes price-list / catalog mutations under write_products + write_markets (both below).
    # There is no read_price_lists/write_price_lists OAuth scope; a real push to lumoira confirmed
    # it works with exactly these scopes.
    # Publish live to the Google & YouTube sales channel
    "read_publications", "write_publications",
    # Store profile + multi-market localization (Multimarket WRITES: enable store languages,
    # create markets + web presences, push shipping profiles and templated legal policies)
    "read_locales", "write_locales",
    "read_markets", "write_markets",
    "read_translations", "write_translations",
    "read_shipping", "write_shipping",
    "read_legal_policies", "write_legal_policies",
    # Images / theme content
    "read_files", "write_files", "read_content", "write_content",
    # Finance / P&L
    "read_orders", "read_shopify_payments_payouts", "read_shopify_payments_disputes",
    # Orders & Issues
    "read_customers", "read_fulfillments", "write_fulfillments",
    "read_merchant_managed_fulfillment_orders", "read_assigned_fulfillment_orders",
]


def _granted_scopes(shop_domain: str, token: str) -> tuple[list[str], str | None]:
    """The scopes actually GRANTED to this store's Admin token, via Shopify's
    /admin/oauth/access_scopes.json. Returns (granted_handles, error). Never raises."""
    url = f"https://{_shop_host(shop_domain)}/admin/oauth/access_scopes.json"
    req = urllib.request.Request(
        url, method="GET",
        headers={"X-Shopify-Access-Token": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (operator's own store)
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else str(e)
        return [], f"access_scopes {e.code}: {detail}"
    except urllib.error.URLError as e:
        return [], f"could not reach Shopify: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return [], f"access_scopes read failed: {e}"
    handles = [str(s.get("handle") or "").strip() for s in (body or {}).get("access_scopes", [])]
    return [h for h in handles if h], None


def verify_store(store: str) -> dict:
    """Check a store's Shopify connection end-to-end WITHOUT mutating anything: can we mint/resolve
    a token, and does that token carry every scope the app needs? Returns a JSON-safe verdict
    (no token) — {ok, connected, shop_domain, granted, missing, extra, auth_error}. Never raises."""
    creds = shopify_for(store)
    shop_domain = (creds.get("shop_domain") or "").strip()
    token = (creds.get("admin_token") or "").strip()
    auth_error = creds.get("auth_error")
    required = list(REQUIRED_SHOPIFY_SCOPES)
    if not shop_domain:
        return {"ok": False, "connected": False, "shop_domain": "", "granted": [],
                "missing": required, "extra": [], "auth_error": "no shop domain set"}
    if not token:
        return {"ok": False, "connected": False, "shop_domain": shop_domain, "granted": [],
                "missing": required, "extra": [],
                "auth_error": auth_error or "could not authenticate (check Client ID/Secret + install the app on this store)"}
    granted, err = _granted_scopes(shop_domain, token)
    if err:
        return {"ok": False, "connected": True, "shop_domain": shop_domain, "granted": [],
                "missing": required, "extra": [], "auth_error": err}
    gset = set(granted)
    missing = [s for s in required if s not in gset]
    extra = [s for s in granted if s not in set(required)]
    return {
        "ok": not missing, "connected": True, "shop_domain": shop_domain,
        "granted": sorted(granted), "missing": missing, "extra": sorted(extra), "auth_error": None,
    }


def google_env_for(store: str) -> dict[str, str]:
    """The store-scoped Google ids as an env dict (Ads customer id + Merchant Center id),
    mapped onto the env names downstream Ads/GMC jobs read. Merge over `as_env()` so a job
    gets global OAuth app creds + this store's target account. {} if nothing is set."""
    sv = shopify_for(store)
    out: dict[str, str] = {}
    for key, env in _STORE_GOOGLE_ENV.items():
        v = sv.get(key)
        if v:
            out[env] = str(v)
    return out


# ----------------------------------------------------- store profile (Shopify-pulled facts)
# These store-level FACTS (native language, currency, timezone, markets, catalog size) are pulled
# from Shopify by `shopify.pull_profile(store)` and SAVED here, so the single setup surface is also
# the single read surface. Downstream readers MUST go through these seams (e.g. image_qa's gallery-
# language check, pricing's currency, scheduling's timezone) rather than re-querying Shopify or
# falling back to a global hardcode.
def set_profile(store: str, profile: dict) -> dict:
    """Persist (merge) the pulled store-level facts for one store. Returns the saved profile.
    Merge — not replace — so a partial pull never wipes previously-pulled fields."""
    raw = _raw()
    profiles = {k: dict(v) for k, v in raw.get("profile", {}).items() if isinstance(v, dict)}
    cur = dict(profiles.get(store, {}))
    for k, v in (profile or {}).items():
        if v is not None:
            cur[k] = v
    profiles[store] = cur
    runlog.setting_set(_SETTING_KEY, {
        "api": raw.get("api", {}), "shopify": raw.get("shopify", {}), "profile": profiles,
    })
    return cur


def store_label(store: str) -> str:
    """The human display label for a store — its operator-set `display_name` if any, else the
    store key itself. This is the ONE place the slug→name mapping lives, so every display surface
    (the global StoreSelect shared by every sub-app, dashboards, headings) shows the same name.
    The slug (`store`) stays the immutable internal key used for data/paths/API; renaming a store =
    editing its display_name here, and it fans out to every surface that renders `store_label()`."""
    dn = store_setting(store, "display_name")
    return dn if dn else store


def store_labels(stores: list[str]) -> dict[str, str]:
    """slug → display label for a list of stores (see `store_label`)."""
    return {s: store_label(s) for s in stores}


def store_setting(store: str, key: str, default: str | None = None) -> str | None:
    """Read ONE per-store field (a STORE_FIELDS value) WITHOUT a network call — unlike
    `shopify_for()`, this never mints a Shopify token. Used by in-process readers (e.g. Finance's
    calcPL knobs cog_rate / tx_rate) that only need a stored config value, not live auth."""
    sv = (_raw().get("shopify") or {}).get(store)
    if not isinstance(sv, dict):
        return default
    v = sv.get(key)
    return str(v) if v not in (None, "") else default


def store_profile_for(store: str) -> dict:
    """The full saved store-level profile for one store ({} if never pulled)."""
    raw = _raw()
    p = (raw.get("profile") or {}).get(store)
    return dict(p) if isinstance(p, dict) else {}


def store_language(store: str) -> str | None:
    """The store's native language (display name, e.g. 'English' / 'Deutsch'), or None if not pulled.
    The VisionScan gallery-language check reads this so 'foreign text' is judged against the actual
    store language instead of a global default."""
    return store_profile_for(store).get("language") or None


def store_currency(store: str) -> str | None:
    """The store's currency code (e.g. 'USD' / 'EUR'), or None if not pulled — for pricing + feed."""
    return store_profile_for(store).get("currency") or None


def store_timezone(store: str) -> str | None:
    """The store's IANA timezone (e.g. 'America/New_York'), or None if not pulled — for scheduling."""
    return store_profile_for(store).get("timezone") or None
