"""store_sync — the automatic per-store Shopify data sync (no more manual-only buttons).

One call pulls everything a connected store's sub-apps read from Shopify:

    profile   shopify.pull_profile      store language/currency/markets → Connections profile
    finance   finance.sync_daily        daily revenue/refund aggregates → fin_shopify_daily
    products  product_mgmt.sync_orders  per-order variant lines         → pm_shopify_orders
    issues    issues.sync_orders        orders + items for disputes     → dm_orders/dm_order_items

Fired from THREE places, so the data stays fresh with zero clicks:
  * the 24/7 worker's `store-data-sync` step (auto, daily — see worker._SYNTHETIC_STEPS),
  * immediately after the operator saves a store's creds in Connections (background thread),
  * the sub-apps' existing manual Sync buttons (unchanged — they are now "refresh now").

Failure posture: each pull is best-effort and isolated — one failing pull never blocks the
others. The combined verdict is recorded in the run ledger (action `store-data-sync`) with a
per-part breakdown in `detail`, and surfaced to the UI via `status(store)` (Connections shows
last-sync + failures next to each store; the Activity log keeps the history). A store with no
Shopify auth is SKIPPED with an honest reason — that's a setup state, not an error flood.
"""
from __future__ import annotations

import threading

from . import connections, runlog

ACTION = "store-data-sync"

# (part-name, function) — pulls only; nothing here writes to Shopify. LEAN build: only the store
# PROFILE is synced (currency / shop name / markets). The finance / orders / variants / issues /
# optimization pulls belonged to the ops sub-apps that this build does not carry.
_PARTS = ("profile",)


def _pulls(store: str) -> list[tuple[str, dict]]:
    """Run every pull for one store, returning (part, result) pairs. Imports are lazy so a
    circular import can never wedge app startup (worker → store_sync → shopify → connections)."""
    from . import shopify

    out: list[tuple[str, dict]] = []
    for part, fn in (
        ("profile", lambda: shopify.pull_profile(store)),
    ):
        try:
            res = fn() or {}
        except Exception as e:  # noqa: BLE001 — one pull's crash must not sink the rest
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        out.append((part, res))
    return out


def run(store: str) -> dict:
    """Sync one store now. Returns {ok, skipped?, parts: {part: {ok, error?}}, failures}."""
    if not connections.store_has_shopify_auth(store):
        # Not an error — the store simply isn't connected yet. Recorded so the UI can say so.
        runlog.record(store, ACTION, "all", "skipped", detail="store not connected to Shopify yet")
        return {"ok": True, "skipped": True, "reason": "store not connected to Shopify yet",
                "parts": {}, "failures": []}
    parts: dict[str, dict] = {}
    failures: list[str] = []
    for part, res in _pulls(store):
        ok = bool(res.get("ok"))
        parts[part] = {"ok": ok, **({"error": res.get("error")} if not ok else {})}
        if not ok:
            failures.append(f"{part}: {res.get('error') or 'failed'}")
    status = "done" if not failures else "failed"
    detail = "all parts synced" if not failures else " · ".join(failures)[:500]
    runlog.record(store, ACTION, "all", status, detail=detail)
    return {"ok": not failures, "skipped": False, "parts": parts, "failures": failures}


def run_async(store: str) -> None:
    """Fire-and-forget sync (used right after the operator saves a store's creds, so the
    sub-apps fill with real data without waiting for the next worker cadence)."""
    threading.Thread(target=run, args=(store,), daemon=True, name=f"store-sync-{store}").start()


def run_incremental(store: str) -> dict:
    """No-op in the LEAN build. It used to sync the order-affected subset (finance + orders +
    issues + the Product Performance snapshot) on a Shopify order webhook — all of which belonged
    to the ops sub-apps this build does not carry. Kept as a safe stub so the webhook path (if
    wired) never errors."""
    return {"ok": True, "skipped": True}


def run_all() -> dict:
    """Sync EVERY registered store now, one after another (sequential — gentle on Shopify + compute).
    Powers the global 'Sync all stores now' control. Best-effort per store; a failure on one never
    stops the rest. Returns {ok, count, synced, skipped, stores: [{store, ok, skipped?, failures}]}."""
    from . import readers

    stores = readers.list_stores()
    results: list[dict] = []
    synced = skipped = 0
    for store in stores:
        try:
            r = run(store)
        except Exception as e:  # noqa: BLE001 — isolate one store's crash
            r = {"ok": False, "skipped": False, "failures": [f"{type(e).__name__}: {e}"]}
        if r.get("skipped"):
            skipped += 1
        elif r.get("ok"):
            synced += 1
        results.append({"store": store, "ok": bool(r.get("ok")), "skipped": bool(r.get("skipped")),
                        "failures": r.get("failures") or []})
    runlog.record(None, "sync-all-stores", "all",
                  "done" if all(x["ok"] for x in results) else "failed",
                  detail=f"{synced} synced, {skipped} skipped, {len(stores)} total")
    return {"ok": True, "count": len(stores), "synced": synced, "skipped": skipped, "stores": results}


_sync_all_inflight = threading.Lock()
_sync_all_running = {"v": False}


def run_all_async() -> bool:
    """Kick run_all() in a background thread. Returns False if one is already running (collapsed)."""
    with _sync_all_inflight:
        if _sync_all_running["v"]:
            return False
        _sync_all_running["v"] = True

    def _job() -> None:
        try:
            run_all()
        finally:
            with _sync_all_inflight:
                _sync_all_running["v"] = False

    threading.Thread(target=_job, daemon=True, name="sync-all-stores").start()
    return True


def is_running_all() -> bool:
    """True while a run_all_async() pass is in flight (for the UI's live status)."""
    return _sync_all_running["v"]


def status(store: str) -> dict | None:
    """The last sync verdict for one store, for the Connections/System UI:
    {at, status: done|failed|skipped, detail} — or None if it never ran."""
    row = runlog.last_run(store, ACTION)
    if not row:
        return None
    return {"at": row.get("ts"), "status": row.get("status"), "detail": row.get("detail")}
