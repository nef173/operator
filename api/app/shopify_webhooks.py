"""shopify_webhooks — real-time order ingestion via Shopify webhooks (push, not polling).

Shopify POSTs an event (order created/updated/cancelled, refund created) to ONE public endpoint the
moment it happens. We verify the HMAC signature (the store's app Client Secret), map the shop domain
back to our store key, and fire a DEBOUNCED per-store refresh — so a burst of orders collapses into a
single sync instead of hammering Shopify. The webhook is the fast TRIGGER; the existing, trusted sync
path (store_sync.run_incremental) does the reconciled fetch (orders → PM/finance/issues + the Product
Performance snapshot with ad-spend reconciliation).

Why a debounced trigger and not a per-order DB write: the optimization snapshot's value is the
RECONCILED view (revenue × windows + ad spend spread by revenue share), which is a whole-store
computation — reproducing it from a single order payload would drift from the canonical builder. The
webhook removes the POLLING DELAY (no waiting for the daily worker); the debounce bounds the cost.

Registration is idempotent and per store (each needs its own Admin token). The callback URL is the
API's own public origin (RAILWAY_PUBLIC_DOMAIN, or WEBHOOK_PUBLIC_BASE override).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time

from . import connections, runlog

# GraphQL WebhookSubscriptionTopic enums we subscribe to — everything that moves revenue/refunds.
_TOPICS = ("ORDERS_CREATE", "ORDERS_UPDATED", "ORDERS_CANCELLED", "REFUNDS_CREATE")

# Collapse a burst of order events into ONE refresh per store per this window (seconds).
_DEBOUNCE = 120

ACTION = "shopify-webhooks"


def _callback_url() -> str:
    """The public URL Shopify POSTs to — this API's own origin + the webhook path."""
    base = (os.environ.get("WEBHOOK_PUBLIC_BASE")
            or connections.runtime_get("WEBHOOK_PUBLIC_BASE") or "").rstrip("/")
    if not base:
        dom = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
        base = f"https://{dom}" if dom else "https://operator-app-production.up.railway.app"
    return f"{base}/api/webhooks/shopify"


def _creds(store: str) -> tuple[str, str]:
    """(shop_domain, admin_token) for a store, or ('','') / raises via caller checks."""
    c = connections.shopify_for(store)
    return (c.get("shop_domain") or "").strip(), (c.get("admin_token") or "").strip()


def _existing(shop: str, token: str) -> list[dict]:
    from . import shopify
    q = ("{ webhookSubscriptions(first: 100) { edges { node { id topic "
         "endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } } } } } }")
    data = shopify._graphql(shop, token, q)
    return [e["node"] for e in ((data.get("webhookSubscriptions") or {}).get("edges") or [])]


def register(store: str) -> dict:
    """Create every order/refund webhook subscription for a store that isn't already pointing at our
    callback. Idempotent. Returns {ok, created, existing, callback, error?}."""
    shop, token = _creds(store)
    if not shop or not token:
        return {"ok": False, "error": connections.shopify_for(store).get("auth_error") or "store not connected to Shopify"}
    if not connections.client_secret_for(store):
        return {"ok": False, "error": "no Client Secret set for this store — needed to verify webhooks"}
    from . import shopify
    cb = _callback_url()
    try:
        subs = _existing(shop, token)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    have = {s["topic"] for s in subs if ((s.get("endpoint") or {}).get("callbackUrl") == cb)}
    created: list[str] = []
    errors: list[str] = []
    for topic in _TOPICS:
        if topic in have:
            continue
        m = ("mutation { webhookSubscriptionCreate(topic: %s, webhookSubscription: "
             "{callbackUrl: %s, format: JSON}) { userErrors { message } "
             "webhookSubscription { id } } }") % (topic, json.dumps(cb))
        try:
            r = shopify._graphql(shop, token, m)
            errs = ((r.get("webhookSubscriptionCreate") or {}).get("userErrors")) or []
            if errs:
                errors.append(f"{topic}: {errs[0].get('message')}")
            else:
                created.append(topic)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{topic}: {str(e)[:120]}")
    status = "done" if not errors else "failed"
    runlog.record(store, ACTION, "register", status,
                  detail=(f"+{len(created)} created, {len(have)} existing"
                          + (f" · errors: {'; '.join(errors)}" if errors else ""))[:400])
    return {"ok": not errors, "created": created, "existing": sorted(have),
            "callback": cb, **({"errors": errors} if errors else {})}


def list_subs(store: str) -> dict:
    """Current webhook subscriptions for a store (topic + callback), for the UI status."""
    shop, token = _creds(store)
    if not shop or not token:
        return {"ok": False, "error": "store not connected", "subscriptions": []}
    try:
        subs = _existing(shop, token)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200], "subscriptions": []}
    cb = _callback_url()
    return {"ok": True, "callback": cb, "subscriptions": [
        {"id": s["id"], "topic": s["topic"],
         "callback": (s.get("endpoint") or {}).get("callbackUrl"),
         "ours": (s.get("endpoint") or {}).get("callbackUrl") == cb}
        for s in subs]}


def unregister(store: str) -> dict:
    """Delete the subscriptions that point at OUR callback (leaves any others untouched)."""
    shop, token = _creds(store)
    if not shop or not token:
        return {"ok": False, "error": "store not connected"}
    from . import shopify
    cb = _callback_url()
    removed = 0
    try:
        for s in _existing(shop, token):
            if (s.get("endpoint") or {}).get("callbackUrl") != cb:
                continue
            m = ('mutation { webhookSubscriptionDelete(id: %s) { userErrors { message } deletedWebhookSubscriptionId } }'
                 % json.dumps(s["id"]))
            shopify._graphql(shop, token, m)
            removed += 1
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200], "removed": removed}
    runlog.record(store, ACTION, "unregister", "done", detail=f"{removed} removed")
    return {"ok": True, "removed": removed}


def register_all() -> dict:
    """Register webhooks for every registered store (best-effort). For the global control."""
    from . import readers
    out = []
    for store in readers.list_stores():
        try:
            r = register(store)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        out.append({"store": store, **r})
    return {"ok": True, "stores": out}


# ── Inbound: verify + debounced trigger ───────────────────────────────────────────────────────
def verify(store: str, raw_body: bytes, hmac_header: str) -> bool:
    """True if `hmac_header` is a valid base64 HMAC-SHA256 of the raw body under the store's app
    Client Secret — i.e. this really came from Shopify for this store."""
    secret = connections.client_secret_for(store)
    if not secret or not hmac_header:
        return False
    digest = base64.b64encode(hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, hmac_header.strip())


_dirty_lock = threading.Lock()
_scheduled: set[str] = set()


def touch(store: str) -> None:
    """Schedule a debounced refresh for `store`. If one is already pending, do nothing (collapse
    the burst). After _DEBOUNCE seconds, run the order-affected sync once."""
    with _dirty_lock:
        if store in _scheduled:
            return
        _scheduled.add(store)

    def _job() -> None:
        time.sleep(_DEBOUNCE)
        with _dirty_lock:
            _scheduled.discard(store)
        from . import store_sync
        try:
            store_sync.run_incremental(store)
        except Exception as e:  # noqa: BLE001 — never let a webhook-driven sync crash a thread loudly
            runlog.record(store, ACTION, "sync", "failed", detail=f"{type(e).__name__}: {e}")

    threading.Thread(target=_job, daemon=True, name=f"wh-sync-{store}").start()


def handle(topic: str, shop_domain: str, raw_body: bytes, hmac_header: str) -> dict:
    """Process one inbound webhook: map shop→store, verify HMAC, schedule the debounced refresh.
    Returns {ok, store?, reason?}. Always cheap — the heavy sync runs later in the debounced job."""
    store = connections.store_for_shop_domain(shop_domain or "")
    if not store:
        return {"ok": False, "reason": f"unknown shop: {shop_domain}"}
    if not verify(store, raw_body, hmac_header):
        return {"ok": False, "reason": "bad hmac"}
    touch(store)
    runlog.record(store, ACTION, "inbound", "done", detail=f"{topic} → refresh scheduled")
    return {"ok": True, "store": store}
