"""Product Optimization snapshot — join Shopify revenue to (later) Google Ads spend per product.

This is the read-only cockpit behind Step 09 (scale: keep / cull / find winners). It answers,
per product, across rolling 7 / 14 / 30-day windows: how many units sold, how much revenue, how
much was refunded — the Shopify side of the ROAS picture. The Google Ads spend side is layered in
a later phase (the GOOGLE_ADS_* creds already live in connections); until then this surface is
honest Shopify-only, never a fake zero.

Design (mirrors the rest of the app):
  * SNAPSHOT model, not live-on-pageload — a worker job pulls the data and writes
    general-stores/<store>/optimization.json, exactly like trends.json. The page reads the
    snapshot (fast) and shows "last synced N ago", just like the Pythago reference.
  * Best-effort, stdlib-only — reuses the same Admin GraphQL transport shape as shopify.py.
    A missing scope / token returns {ok: False, error} rather than raising.
  * Currency is the STORE's own currency (read from the pulled profile) — we never sum across
    currencies (that's why the reference shows Ads-EUR and Shopify-AUD separately).

Credentials flow entirely through the connections seams (the single setup surface):
  connections.shopify_for(store) → admin_token + shop_domain  (the Shopify read)
  connections.store_profile(store).currency                   (label the money correctly)
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import time
import urllib.error
import urllib.request

from . import config, connections
from .shopify import _API_VERSION, _admin_endpoint  # reuse the pinned version + endpoint builder

_HTTP_TIMEOUT = 30
_PAGE_SIZE = 100          # Shopify orders page size (100 keeps each GraphQL query under the cost cap)
# Runaway backstop only — NOT a real cap: 20000 pages × 100 = 2,000,000 orders. The pull stops the
# instant `hasNextPage` is false, so a store with 35k orders does 350 pages and stops. Bumped from
# 200 (which capped decorsdeluxe at 20k → "totals are a floor"); the sync is backgrounded + the
# _graphql retry paces itself against Shopify's cost-throttle, so a full all-time pull is safe.
_MAX_PAGES = 20000
_THROTTLE_RETRIES = 8     # on a Shopify cost-throttle / 429, wait for the leaky bucket + retry
WINDOWS = [7, 14, 30]     # the rolling day-windows the table shows (subsets of the all-time pull)
ALL = "all"               # the lifetime bucket — every pulled order counts toward it (all-time view)

# One orders page: every line item with its product + variant + money, plus refunds. We pull
# ALL-TIME (optionally bounded below by the store's data_start_date) and page NEWEST-FIRST
# (`reverse: true`) so the rolling 7/14/30 windows stay exact even if the safety cap truncates the
# oldest tail. `discountedTotalSet` is the line's revenue in shop currency; refunds are summed
# separately so net = revenue − refunds.
_ORDERS_QUERY = """
query OperatorOrders($q: String!, $cursor: String) {
  orders(first: %d, query: $q, after: $cursor, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      name
      createdAt
      shippingAddress { countryCodeV2 }
      lineItems(first: 50) {
        nodes {
          quantity
          discountedTotalSet { shopMoney { amount currencyCode } }
          variant { id title sku }
          product { id title status featuredImage { url } totalVariants publishedAt tags }
        }
      }
      refunds {
        createdAt
        totalRefundedSet { shopMoney { amount } }
      }
    }
  }
}
""" % _PAGE_SIZE


def _throttle_wait(cost: dict | None, attempt: int) -> float:
    """Seconds to wait before retrying a throttled query. If Shopify told us the cost + refill rate,
    wait exactly long enough for the leaky bucket to hold the requested cost; else exponential
    back-off. Capped at 10s so one bad page never stalls a long pull for minutes."""
    if cost:
        ts = cost.get("throttleStatus") or {}
        needed = float(cost.get("requestedQueryCost") or 100)
        avail = float(ts.get("currentlyAvailable") or 0)
        restore = float(ts.get("restoreRate") or 50) or 50.0
        if needed > avail:
            return min(10.0, max(1.0, (needed - avail) / restore))
    return min(10.0, float(2 ** attempt))


def _graphql(shop_domain: str, token: str, query: str, variables: dict, _attempt: int = 0) -> dict:
    """POST a GraphQL doc with variables. Retries on Shopify's cost-throttle (GraphQL THROTTLED or
    HTTP 429), waiting for the leaky bucket to refill — so an uncapped all-time order pull paces
    itself instead of dying mid-way. Raises RuntimeError on any other transport/HTTP/GraphQL error."""
    req = urllib.request.Request(
        _admin_endpoint(shop_domain),
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Shopify-Access-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310 (operator's own store)
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429 and _attempt < _THROTTLE_RETRIES:  # Too Many Requests → back off + retry
            time.sleep(_throttle_wait(None, _attempt))
            return _graphql(shop_domain, token, query, variables, _attempt + 1)
        detail = e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else str(e)
        raise RuntimeError(f"Shopify Admin API {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach Shopify Admin API: {e.reason}")
    if isinstance(body, dict) and body.get("errors"):
        errs = body["errors"]
        throttled = isinstance(errs, list) and any(
            isinstance(e, dict) and (((e.get("extensions") or {}).get("code") == "THROTTLED")
                                     or "throttl" in str(e.get("message", "")).lower())
            for e in errs)
        if throttled and _attempt < _THROTTLE_RETRIES:
            cost = (body.get("extensions") or {}).get("cost") if isinstance(body.get("extensions"), dict) else None
            time.sleep(_throttle_wait(cost, _attempt))
            return _graphql(shop_domain, token, query, variables, _attempt + 1)
        msg = errs[0].get("message") if isinstance(errs, list) and errs else str(errs)
        raise RuntimeError(f"Shopify GraphQL error: {msg}")
    return (body or {}).get("data") or {}


def _parse_iso(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    try:
        # Shopify returns RFC3339 like "2026-06-23T10:00:00Z"
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _empty_window() -> dict:
    # cog = units × latest landed cost from Product Management's invoice-derived per-variant
    # costs (pm_product_costs_market). 0 until an invoice covering the variant lands.
    return {"qty": 0, "cv": 0.0, "refunds": 0.0, "orders": 0, "cog": 0.0}


def _blank_product(node_product: dict) -> dict:
    return {
        "product_id": node_product.get("id"),
        "title": node_product.get("title") or "—",
        "status": (node_product.get("status") or "").upper() or None,
        "image": ((node_product.get("featuredImage") or {}) or {}).get("url"),
        "variants_count": node_product.get("totalVariants"),
        "published_at": node_product.get("publishedAt"),
        "tags": node_product.get("tags") or [],
        "_variant_ids": set(),  # stripped before serialize; used to refine the count from actual sales
        "windows": {**{str(w): _empty_window() for w in WINDOWS}, ALL: _empty_window()},
        # Per-country (market) breakdown — same window shape, one entry per country the product
        # sold in. Powers the row's globe-expand + the automation's "check all markets" rules.
        "markets": {},  # {countryCode: {window: {qty, cv, refunds, orders, cog, cost}}}
    }


def _blank_market_windows() -> dict:
    return {**{str(w): _empty_window() for w in WINDOWS}, ALL: _empty_window()}


def build_snapshot(store: str) -> dict:
    """Pull Shopify orders+refunds ALL-TIME (bounded below by the store's data_start_date when set)
    and aggregate per product into the 7/14/30-day rolling columns PLUS an all-time lifetime bucket.
    Returns {ok, snapshot?, error?} — best-effort, never raises for a partial/credential failure."""
    creds = connections.shopify_for(store)
    token = (creds.get("admin_token") or "").strip()
    shop_domain = (creds.get("shop_domain") or "").strip()
    if not token or not shop_domain:
        return {
            "ok": False,
            "error": creds.get("auth_error") or (
                "Set this store's Shopify Client ID + Secret and its myshopify domain first "
                "(Connections · Stores · Store tab)."
            ),
        }

    now = _dt.datetime.now(_dt.timezone.utc)
    # ALL-TIME pull, optionally bounded below by the store's data_start_date (a rebrand / ownership
    # cut-off). Blank = no floor = full order history. The query is empty when there's no floor so
    # Shopify returns every order; otherwise it's bounded to created_at >= the cut-off.
    start_date = (connections.store_setting(store, "data_start_date") or "").strip()
    floor = _parse_iso(start_date) or _parse_iso(f"{start_date}T00:00:00Z") if start_date else None
    q = f"created_at:>={floor.strftime('%Y-%m-%dT%H:%M:%SZ')}" if floor else ""
    # window cutoffs: a sale counts toward window W if it happened on/after (now − W days)
    cutoffs = {w: now - _dt.timedelta(days=w) for w in WINDOWS}

    products: dict[str, dict] = {}
    totals = {**{str(w): _empty_window() for w in WINDOWS}, ALL: _empty_window()}
    order_count = 0
    cursor: str | None = None

    # PM's landed costs (EUR) per variant — invoice-derived, the same numbers the P&L and the
    # margin tabs price against. Joined per line item below so every product window carries a
    # real COGS column; this is what makes per-product PROFIT possible in Product Performance.
    variant_costs: dict[str, float] = {}
    sku_costs: dict[str, float] = {}
    market_costs: dict[str, dict[str, float]] = {}  # {variant_id: {country: landed cost}} — PM per-market overrides
    order_costs: dict[str, float] = {}
    day_costs: dict[str, float] = {}
    # Deferred day-level COGS allocation (see below): lines from orders WITHOUT their own
    # invoice total, waiting for the day pool split after the scan.
    cog_pending: list[tuple] = []
    day_amt_pool: dict[str, float] = {}
    day_order_costed: dict[str, float] = {}
    earliest_order: _dt.datetime | None = None  # oldest captured order → all-time ad-spend floor
    try:
        from . import db as _db, product_mgmt as _pm
        with _db.connect() as _conn:
            variant_costs = _pm._variant_costs(_conn, store)
            # Per-(variant, country) landed cost from PM's per-market overrides — so each MARKET's
            # COGS uses ITS own cost (a variant can cost more to land in GB than DE). variant_costs
            # already collapses these to one best cost for the product total; here we keep the split.
            for r in _conn.execute(
                "SELECT variant_id, country, cost_override_eur FROM pm_product_costs_market "
                "WHERE store_key = ? AND cost_override_eur IS NOT NULL AND country <> '_ALL'",
                (store,),
            ).fetchall():
                vid_c = str(r["variant_id"] or "").rsplit("/", 1)[-1]
                if vid_c:
                    market_costs.setdefault(vid_c, {})[str(r["country"])] = float(r["cost_override_eur"])
            # SKU-level rolling unit cost from the invoice lines themselves — covers history
            # migrated from the standalone, whose lines carry SKUs but no variant ids.
            for r in _conn.execute(
                "SELECT sku, SUM(bill_cost_eur) AS cost, SUM(qty) AS units FROM pm_invoice_lines "
                "WHERE store_key = ? AND line_type = 'charge' AND sku IS NOT NULL AND sku <> '' "
                "GROUP BY sku", (store,),
            ).fetchall():
                units = float(r["units"] or 0)
                if units > 0:
                    sku_costs[str(r["sku"]).strip()] = float(r["cost"] or 0) / units
            # Per-ORDER invoice cost — the strongest source: the standalone-migrated lines
            # carry ONLY the order number (no sku/variant), but the billed total per order is
            # exact. Keyed by bare order number ('#1042' → '1042').
            for r in _conn.execute(
                "SELECT order_no, SUM(CASE WHEN line_type = 'charge' THEN bill_cost_eur ELSE 0 END) "
                "- SUM(CASE WHEN line_type = 'refund' THEN refund_amount_eur ELSE 0 END) AS cost "
                "FROM pm_invoice_lines WHERE store_key = ? AND order_no IS NOT NULL "
                "GROUP BY order_no", (store,),
            ).fetchall():
                key = str(r["order_no"] or "").strip().lstrip("#")
                if key and float(r["cost"] or 0) > 0:
                    order_costs[key] = float(r["cost"])
            # Day-level invoice COGS — finance's own per-day source. The history migrated
            # from the standalone carries NO order numbers or SKUs at all, only the order
            # DATE — so the day total is the only exact cost signal for it.
            from . import finance as _fin
            day_costs = _fin._invoice_cog_by_date(store)
    except Exception:  # noqa: BLE001 — costs are additive; never sink the snapshot
        variant_costs, sku_costs, market_costs, order_costs, day_costs = {}, {}, {}, {}, {}

    # Revenue → EUR. shopMoney is the STORE's own currency, but every cost source above (PM
    # invoice COGS) and the ad spend are EUR — so an AUD/GBP store's profit = cv(store cur) −
    # cog(EUR) − ad(EUR) mixed currencies and gave wrong margins. Normalize revenue to EUR at the
    # order's date so the whole snapshot ties out in one currency. EUR stores are a no-op.
    store_cur = None
    try:
        store_cur = ((connections.store_currency(store) or "").upper() or None)
    except Exception:  # noqa: BLE001 — profile optional; snapshot still renders
        store_cur = None
    _rev_fx: dict[str, float] = {}

    def _rev_eur(amount: float, day: str) -> float:
        if not store_cur or store_cur == "EUR":
            return amount
        if day not in _rev_fx:
            from . import finance as _f
            try:
                _rev_fx[day] = _f._fx_rate(store_cur, "EUR", day)
            except Exception:  # noqa: BLE001 — FX best-effort
                _rev_fx[day] = 1.0
        return amount * _rev_fx[day]

    # Pass-through add-ons (Shipping Protection / order-protection / gift wrap / warranty) aren't
    # real catalog products — exclude them at the SOURCE so they never enter products OR totals
    # (keeps the snapshot's totals == sum of the rows the operator sees).
    try:
        from . import product_mgmt as _pmx
        _is_addon = _pmx._is_excluded_title
    except Exception:  # noqa: BLE001
        def _is_addon(_t):  # type: ignore[misc]
            return False

    try:
        for _ in range(_MAX_PAGES):
            data = _graphql(shop_domain, token, _ORDERS_QUERY, {"q": q, "cursor": cursor})
            conn = (data.get("orders") or {})
            for order in conn.get("nodes") or []:
                order_count += 1
                created = _parse_iso(order.get("createdAt"))
                if created is None:
                    continue
                if earliest_order is None or created < earliest_order:
                    earliest_order = created
                # bucket keys this order feeds: each rolling window it falls in, PLUS the ALL bucket
                # (every pulled order counts toward the lifetime/all-time view).
                bkeys = [str(w) for w in WINDOWS if created >= cutoffs[w]] + [ALL]
                country = ((order.get("shippingAddress") or {}).get("countryCodeV2") or "??")
                # revenue + qty from line items. Buffered first so the ORDER-level invoice
                # cost (the exact billed total for this order, when its invoice landed) can be
                # allocated across the lines by revenue share — the same proportional rule the
                # standalone uses. Per-line fallback: variant cost, then SKU rolling cost.
                seen_products_this_order: set[str] = set()
                buffered: list[tuple] = []  # (pid, qty, amt, est_cog)
                for li in ((order.get("lineItems") or {}).get("nodes") or []):
                    prod = li.get("product") or {}
                    pid = prod.get("id")
                    if not pid:
                        continue  # deleted product / custom line — skip rather than guess
                    if _is_addon(prod.get("title")):
                        continue  # shipping protection / gift wrap — not a real product
                    p = products.get(pid)
                    if p is None:
                        p = _blank_product(prod)
                        products[pid] = p
                    vid = (li.get("variant") or {}).get("id")
                    if vid:
                        p["_variant_ids"].add(vid)
                    qty = int(li.get("quantity") or 0)
                    amt = float((((li.get("discountedTotalSet") or {}).get("shopMoney") or {}).get("amount")) or 0)
                    li_sku = str((li.get("variant") or {}).get("sku") or "").strip()
                    vid_num = str(vid or "").rsplit("/", 1)[-1]
                    # PM's per-market landed cost for THIS shipping country wins, so each market's
                    # COGS reflects its own cost; else the variant's best single cost (which already
                    # folds in the _ALL override + store-wide rolling), else the SKU rolling cost.
                    unit_cost = (market_costs.get(vid_num, {}).get(country)
                                 or variant_costs.get(vid_num)
                                 or sku_costs.get(li_sku) or 0.0)
                    buffered.append((pid, qty, amt, qty * unit_cost))
                order_cog = order_costs.get(str(order.get("name") or "").strip().lstrip("#"))
                day = created.date().isoformat()
                amt_sum = sum(b[2] for b in buffered)
                for pid, qty, amt, est_cog in buffered:
                    if order_cog is not None and amt_sum > 0:
                        line_cog = order_cog * (amt / amt_sum)   # exact billed total, revenue-share split
                    elif order_cog is not None and buffered:
                        line_cog = order_cog / len(buffered)
                    else:
                        line_cog = None                           # priced in the day-level pass below
                    p = products[pid]
                    amt_eur = _rev_eur(amt, day)
                    mk = p["markets"].setdefault(country, _blank_market_windows())
                    for k in bkeys:
                        pw = p["windows"][k]
                        pw["qty"] += qty
                        pw["cv"] += amt_eur
                        tw = totals[k]
                        tw["qty"] += qty
                        tw["cv"] += amt_eur
                        mw = mk[k]
                        mw["qty"] += qty
                        mw["cv"] += amt_eur
                    if line_cog is not None:
                        for k in bkeys:
                            p["windows"][k]["cog"] += line_cog
                            totals[k]["cog"] += line_cog
                            mk[k]["cog"] += line_cog
                        day_order_costed[day] = day_order_costed.get(day, 0.0) + line_cog
                    else:
                        cog_pending.append((pid, day, amt, est_cog, tuple(bkeys), country))
                        day_amt_pool[day] = day_amt_pool.get(day, 0.0) + amt
                    if pid not in seen_products_this_order:
                        seen_products_this_order.add(pid)
                        for k in bkeys:
                            p["windows"][k]["orders"] += 1
                if seen_products_this_order:  # skip orders that were only add-ons / deleted products
                    for k in bkeys:
                        totals[k]["orders"] += 1
                # refunds: attributed by refund date to the window it falls in (order-level total —
                # Shopify doesn't cheaply give per-line refund here; order-level is the honest read).
                # The ALL bucket always takes the refund; rolling windows take it if it falls in range.
                for rf in (order.get("refunds") or []):
                    r_created = _parse_iso(rf.get("createdAt")) or created
                    r_amt = float((((rf.get("totalRefundedSet") or {}).get("shopMoney") or {}).get("amount")) or 0)
                    if r_amt <= 0:
                        continue
                    r_keys = [str(w) for w in WINDOWS if r_created >= cutoffs[w]] + [ALL]
                    r_amt_eur = _rev_eur(r_amt, r_created.date().isoformat())
                    # Distribute the order refund across ITS products by revenue share (same rule as
                    # COGS) so refunds reconcile with the rows — instead of dumping the whole refund
                    # on the order's first product (which lost it when that product was deleted/an
                    # add-on). totals.refunds is recomputed as Σ(products) at the end. Orders whose
                    # products were all deleted/add-ons contribute no captured revenue either, so
                    # dropping their refund keeps the snapshot internally consistent.
                    if amt_sum > 0:
                        for (b_pid, _q, b_amt, _ec) in buffered:
                            share = r_amt_eur * (b_amt / amt_sum)
                            bmk = products[b_pid]["markets"].setdefault(country, _blank_market_windows())
                            for k in r_keys:
                                products[b_pid]["windows"][k]["refunds"] += share
                                bmk[k]["refunds"] += share
                    elif buffered:
                        share = r_amt_eur / len(buffered)
                        for (b_pid, _q, _a, _ec) in buffered:
                            bmk = products[b_pid]["markets"].setdefault(country, _blank_market_windows())
                            for k in r_keys:
                                products[b_pid]["windows"][k]["refunds"] += share
                                bmk[k]["refunds"] += share
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
    except Exception as e:  # noqa: BLE001 — best-effort; report, don't crash the job
        return {"ok": False, "error": str(e)}

    # ── Day-level COGS allocation pass ─────────────────────────────────────────────────────
    # Lines whose order carried no invoice total of its own get priced from the DAY's invoice
    # COGS (the exact per-day number the P&L uses), split across the day's un-costed revenue
    # by revenue share. Order-level costs already booked are subtracted from the day pool so
    # nothing double-counts. Falls back to the per-variant/SKU estimate when the day has no
    # invoice data at all. This is what prices the standalone-migrated history, which carries
    # only order DATES (no order numbers, no SKUs).
    for pid, day, amt, est_cog, bkeys, country in cog_pending:
        day_pool = max(0.0, day_costs.get(day, 0.0) - day_order_costed.get(day, 0.0))
        pool_amt = day_amt_pool.get(day, 0.0)
        line_cog = day_pool * (amt / pool_amt) if (day_pool > 0 and pool_amt > 0) else est_cog
        if line_cog <= 0:
            continue
        p = products.get(pid)
        mk = p["markets"].setdefault(country, _blank_market_windows()) if p is not None else None
        for k in bkeys:
            if p is not None:
                p["windows"][k]["cog"] += line_cog
                if mk is not None:
                    mk[k]["cog"] += line_cog
            totals[k]["cog"] += line_cog

    # Snapshot is EUR-normalized (revenue converted at order-date FX above; COGS + ad spend are
    # already EUR), so profit = revenue − COGS − ad spend ties out in one currency for every store.
    currency = "EUR"

    def _pid_num(gid: str) -> str:
        return gid.rsplit("/", 1)[-1] if gid else ""

    # ── Google Ads layer: per-product metrics from the in-account Ads Script (no OAuth needed) ──
    # Merge clicks / impressions / conversions / conv_value + the script's OWN attributed cost onto
    # each product's windows (conv_value is the ad key, distinct from the window's Shopify `cv`).
    ads_by_product: dict = {}
    try:
        from . import finance as _fin_ads
        ads_cutoffs = {w: cutoffs[w].strftime("%Y-%m-%d") for w in WINDOWS}
        ads_by_product = _fin_ads.ads_product_windows(store, ads_cutoffs)
    except Exception:  # noqa: BLE001 — ads layer is additive; never sink the Shopify snapshot
        ads_by_product = {}

    rows = []
    for pid, p in products.items():
        vc = p.get("variants_count") or (len(p["_variant_ids"]) or None)
        p.pop("_variant_ids", None)
        p["variants_count"] = vc
        ads = ads_by_product.get(_pid_num(pid))
        if ads:
            for k, w in p["windows"].items():
                s = ads.get(k) or {}
                w["clicks"] = int(w.get("clicks") or 0) + int(s.get("clicks") or 0)
                w["impressions"] = int(w.get("impressions") or 0) + int(s.get("impressions") or 0)
                w["conversions"] = float(w.get("conversions") or 0) + float(s.get("conversions") or 0)
                w["conv_value"] = float(w.get("conv_value") or 0) + float(s.get("conv_value") or 0)
                w["cost"] = float(w.get("cost") or 0) + float(s.get("cost") or 0)  # script-attributed
        rows.append(p)

    # ── Ad spend RECONCILED to the canonical P&L source (fin_ad_spend) ──────────────────────────
    # The total ad spend per window = the exact number the P&L uses. The Ads-Script per-product cost
    # merged above is only the ATTRIBUTED portion — brand/PMax spend + any product the script missed
    # go unattributed. Spread that remainder across products by revenue share so Σ(per-product cost)
    # == the real account spend and per-product ROAS is honest (never the inflated/deflated split of
    # attributed-only). This also closes the "recent window shows €0 ad spend" gap — the total comes
    # from fin_ad_spend even when the per-product script lags a day.
    def _ad_eur(amount, curcode, day) -> float:
        amt = float(amount or 0)
        cur = (curcode or "EUR").upper()
        if amt == 0 or cur == "EUR":
            return amt
        try:
            from . import finance as _f
            return amt * _f._fx_rate(cur, "EUR", day)
        except Exception:  # noqa: BLE001 — FX best-effort
            return amt

    total_ad: dict[str, float] = {}
    try:
        from . import db as _dbx
        with _dbx.connect() as _c:
            for w in WINDOWS:
                cut = cutoffs[w].strftime("%Y-%m-%d")
                total_ad[str(w)] = sum(
                    _ad_eur(r["amount"], r["currency"], r["date"])
                    for r in _c.execute(
                        "SELECT amount, currency, date FROM fin_ad_spend WHERE store_key = ? AND date >= ?",
                        (store, cut)).fetchall())
            # Bound all-time ad spend to the OLDEST captured order's date, so the all-time bucket's
            # revenue and ad spend cover the same period (the live order pull may not reach back as
            # far as fin_ad_spend does — e.g. a bought store whose pre-takeover orders aren't in the
            # live Shopify list but whose daily aggregates persist). Keeps all-time ROAS honest.
            all_floor = earliest_order.date().isoformat() if earliest_order else "0001-01-01"
            total_ad[ALL] = sum(
                _ad_eur(r["amount"], r["currency"], r["date"])
                for r in _c.execute(
                    "SELECT amount, currency, date FROM fin_ad_spend WHERE store_key = ? AND date >= ?",
                    (store, all_floor)).fetchall())
    except Exception:  # noqa: BLE001 — no fin_ad_spend → per-product ad cost stays the script value
        total_ad = {}

    for k in [str(w) for w in WINDOWS] + [ALL]:
        tot = float(total_ad.get(k) or 0)
        if tot <= 0:
            continue
        attributed = sum(float(p["windows"][k].get("cost") or 0) for p in rows)
        remainder = tot - attributed
        total_cv = sum(float(p["windows"][k].get("cv") or 0) for p in rows)
        if remainder > 0 and total_cv > 0:
            for p in rows:
                pcv = float(p["windows"][k].get("cv") or 0)
                if pcv > 0:
                    p["windows"][k]["cost"] = round(
                        float(p["windows"][k].get("cost") or 0) + remainder * pcv / total_cv, 2)
        # remainder < 0 (script over-reported vs the account total) → keep the script cost as-is.

    # ── Per-MARKET (country) finalize: prune empty markets + split each product's reconciled ad
    # cost across its markets by that market's revenue share (no per-country ad data exists, so
    # revenue share is the honest allocation — same rule as the product split). ──────────────────
    for p in rows:
        markets = p.get("markets") or {}
        for c in [c for c, mw in markets.items()
                  if float(mw[ALL].get("cv") or 0) <= 0 and float(mw[ALL].get("refunds") or 0) <= 0]:
            del markets[c]
        for k in [str(w) for w in WINDOWS] + [ALL]:
            pc = float(p["windows"][k].get("cost") or 0)
            mcv_total = sum(float(m[k].get("cv") or 0) for m in markets.values())
            for m in markets.values():
                mcv = float(m[k].get("cv") or 0)
                m[k]["cost"] = round(pc * mcv / mcv_total, 2) if (pc > 0 and mcv_total > 0) else 0.0

    reconciled = any(float(total_ad.get(k) or 0) > 0 for k in total_ad)
    ads_connected = bool(creds.get("google_ads_customer_id")) or reconciled or bool(ads_by_product)
    ads_source = "reconciled" if reconciled else ("script" if ads_by_product else None)

    # ── Totals = SUM of the (real) product rows, so the "total" ALWAYS ties to what's shown ────
    # (orders is left as the scan count — one order with 3 products is ONE order, not three.)
    for k in [str(w) for w in WINDOWS] + [ALL]:
        t = totals[k]
        t["qty"] = int(sum(int(p["windows"][k].get("qty") or 0) for p in rows))
        t["cv"] = round(sum(float(p["windows"][k].get("cv") or 0) for p in rows), 2)
        t["refunds"] = round(sum(float(p["windows"][k].get("refunds") or 0) for p in rows), 2)
        t["cog"] = round(sum(float(p["windows"][k].get("cog") or 0) for p in rows), 2)
        t["cost"] = round(sum(float(p["windows"][k].get("cost") or 0) for p in rows), 2)
        t["clicks"] = int(sum(int(p["windows"][k].get("clicks") or 0) for p in rows))
        t["impressions"] = int(sum(int(p["windows"][k].get("impressions") or 0) for p in rows))
        t["conversions"] = round(sum(float(p["windows"][k].get("conversions") or 0) for p in rows), 2)
        t["conv_value"] = round(sum(float(p["windows"][k].get("conv_value") or 0) for p in rows), 2)

    # default sort: highest all-time revenue first (the operator's "what's earning" view)
    rows.sort(key=lambda r: r["windows"][ALL]["cv"], reverse=True)

    snapshot = {
        "store": store,
        "synced_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "currency": currency,
        "data_start_date": (start_date or None),  # the pull floor (blank = all history)
        "windows": WINDOWS,
        "orders_scanned": order_count,
        "truncated": order_count >= _MAX_PAGES * _PAGE_SIZE,
        # "connected" = the ads DATA layer is present: either the per-store Google OAuth id is
        # set (API path, later) or the Ads Script has pushed per-product rows (the live path).
        "ads_connected": ads_connected,
        "ads_source": ads_source,
        "totals": totals,
        "products": rows,
        "error": None,
    }
    return {"ok": True, "snapshot": snapshot}


_STATUS_MUTATION = """
mutation SetStatus($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id status }
    userErrors { field message }
  }
}
"""

_VALID_STATUSES = ("ACTIVE", "DRAFT", "ARCHIVED")


def set_products_status(store: str, product_ids: list[str], status: str,
                        prev: dict | None = None, titles: dict | None = None) -> dict:
    """Bulk product-status change (the Pythago 'Set to Active/Draft/Archived' action) via
    Shopify productUpdate. Best-effort per product — one failure doesn't stop the rest.
    When `prev` {numeric_pid: old_status} is supplied, each successful change is written to the
    mutation history so it can be reverted. Returns {ok, updated, failed: [{id, error}]}."""
    status = (status or "").upper()
    if status not in _VALID_STATUSES:
        return {"ok": False, "error": f"status must be one of {_VALID_STATUSES}"}
    creds = connections.shopify_for(store)
    token = (creds.get("admin_token") or "").strip()
    shop_domain = (creds.get("shop_domain") or "").strip()
    if not token or not shop_domain:
        return {"ok": False, "error": creds.get("auth_error") or "store not connected to Shopify"}
    prev = prev or {}
    titles = titles or {}
    updated = 0
    failed: list[dict] = []
    hist: list[tuple] = []  # (pid, title, old_status) for successful changes with a known prev
    changed: dict[str, str] = {}  # numeric_pid → new status, for the in-place snapshot patch
    for pid in product_ids[:200]:  # sanity cap per call
        gid = pid if str(pid).startswith("gid://") else f"gid://shopify/Product/{pid}"
        num = str(pid).rsplit("/", 1)[-1]
        try:
            data = _graphql(shop_domain, token, _STATUS_MUTATION,
                            {"input": {"id": gid, "status": status}})
            pu = data.get("productUpdate") or {}
            errs = pu.get("userErrors") or []
            if errs:
                failed.append({"id": pid, "error": errs[0].get("message")})
            elif not ((pu.get("product") or {}).get("id")):
                # No userError but no product back = the update didn't take (bad id / permission).
                failed.append({"id": pid, "error": "Shopify returned no product — change did not apply."})
            else:
                updated += 1
                changed[num] = status
                old = prev.get(num) or prev.get(str(pid))
                if old and str(old).upper() != status:
                    hist.append((num, titles.get(num) or titles.get(str(pid)), str(old).upper()))
        except Exception as e:  # noqa: BLE001 — collect, keep going
            failed.append({"id": pid, "error": str(e)[:120]})
    # Patch the snapshot in place so the UI shows the new status on reload — no full re-sync needed.
    _patch_snapshot_status(store, changed)
    if hist:
        try:
            from . import db
            with db.connect() as conn:
                for num, title, old in hist:
                    _record_history(conn, store, num, title, "status", old, status,
                                    f"Status → {status.title()}")
        except Exception:  # noqa: BLE001 — history is additive; never fail the status change
            pass
    return {"ok": not failed, "updated": updated, "failed": failed}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_history(conn, store, pid, title, field, old_val, new_val, label) -> None:
    """Append a mutation-history row (best-effort; caller already holds the connection)."""
    conn.execute(
        "INSERT INTO pm_optimization_history "
        "(store_key, at, product_id, product_title, field, old_val, new_val, label, reverted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (store, _now(), pid, title, field, old_val, new_val, label))


def _tags_list(raw) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return [str(t) for t in v] if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


# ── Per-product flags: Exclude + Note + Tags (server-persisted; read by the view + Automation) ──
def set_flag(store: str, product_id: str, hidden=None, note=None, tags=None, title=None) -> dict:
    """Upsert a product's optimization flag. Only the fields passed (hidden / note / tags) change;
    the rest keep their stored value. Every actual change is written to the mutation history so it
    can be reverted. product_id is normalized to its numeric tail."""
    pid = str(product_id or "").rsplit("/", 1)[-1].strip()
    if not pid:
        return {"ok": False, "error": "product_id required"}
    from . import db
    now = _now()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT hidden, note, tags FROM pm_optimization_flags WHERE store_key = ? AND product_id = ?",
            (store, pid)).fetchone()
        cur_hidden = int(row["hidden"]) if row else 0
        cur_note = row["note"] if row else None
        cur_tags = _tags_list(row["tags"]) if row else []
        new_hidden = (1 if hidden else 0) if hidden is not None else cur_hidden
        new_note = note if note is not None else cur_note
        new_note = (str(new_note).strip() or None) if new_note is not None else None
        new_tags = ([str(t).strip() for t in tags if str(t).strip()] if tags is not None else cur_tags)
        tags_json = json.dumps(new_tags)
        # History — only for fields that actually changed.
        if hidden is not None and new_hidden != cur_hidden:
            _record_history(conn, store, pid, title, "hidden", str(cur_hidden), str(new_hidden),
                            "Excluded" if new_hidden else "Included")
        if note is not None and new_note != cur_note:
            _record_history(conn, store, pid, title, "note", cur_note, new_note,
                            "Note removed" if not new_note else ("Note added" if not cur_note else "Note edited"))
        if tags is not None and new_tags != cur_tags:
            _record_history(conn, store, pid, title, "tags", json.dumps(cur_tags), tags_json, "Tags updated")
        conn.execute(
            "INSERT INTO pm_optimization_flags (store_key, product_id, hidden, note, tags, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (store_key, product_id) DO UPDATE SET "
            "hidden = excluded.hidden, note = excluded.note, tags = excluded.tags, "
            "updated_at = excluded.updated_at",
            (store, pid, new_hidden, new_note, tags_json, now))
    return {"ok": True, "product_id": pid, "hidden": bool(new_hidden), "note": new_note, "tags": new_tags}


def set_tags(store: str, product_id: str, tags: list, title=None) -> dict:
    """Set the full app-side tag list for a product (Pythago per-row Add/Remove tag)."""
    return set_flag(store, product_id, tags=(tags or []), title=title)


def get_flags(store: str) -> dict:
    """{numeric_product_id: {hidden, note, tags}} for merging into the snapshot view."""
    out: dict = {}
    try:
        from . import db
        with db.connect() as conn:
            for r in conn.execute(
                "SELECT product_id, hidden, note, tags FROM pm_optimization_flags WHERE store_key = ?",
                (store,)).fetchall():
                out[str(r["product_id"])] = {
                    "hidden": bool(r["hidden"]), "note": r["note"], "tags": _tags_list(r["tags"])}
    except Exception:  # noqa: BLE001 — flags are additive; never break the view
        pass
    return out


# ── Mutation history + revert (Pythago per-row "history" icon) ────────────────────────────────
def get_history(store: str, product_id: str, limit: int = 40) -> list:
    """Recent mutation-history entries for a product, newest first."""
    pid = str(product_id or "").rsplit("/", 1)[-1].strip()
    out: list = []
    if not pid:
        return out
    try:
        from . import db
        with db.connect() as conn:
            for r in conn.execute(
                "SELECT id, at, field, old_val, new_val, label, reverted FROM pm_optimization_history "
                "WHERE store_key = ? AND product_id = ? ORDER BY id DESC LIMIT ?",
                (store, pid, int(limit))).fetchall():
                out.append({"id": r["id"], "at": r["at"], "field": r["field"],
                            "old_val": r["old_val"], "new_val": r["new_val"],
                            "label": r["label"], "reverted": bool(r["reverted"])})
    except Exception:  # noqa: BLE001
        pass
    return out


def revert_history(store: str, entry_id: int) -> dict:
    """Undo a single mutation: re-apply its old_val to the field, then mark the entry reverted.
    Re-applying goes through the normal setter, so the revert is itself logged."""
    from . import db
    with db.connect() as conn:
        row = conn.execute(
            "SELECT product_id, product_title, field, old_val, reverted FROM pm_optimization_history "
            "WHERE store_key = ? AND id = ?", (store, int(entry_id))).fetchone()
        if row is None:
            return {"ok": False, "error": "history entry not found"}
        if int(row["reverted"] or 0):
            return {"ok": False, "error": "already reverted"}
        pid, title, field, old_val = row["product_id"], row["product_title"], row["field"], row["old_val"]
    if field == "hidden":
        set_flag(store, pid, hidden=(str(old_val) == "1"), title=title)
    elif field == "note":
        set_flag(store, pid, note=(old_val or ""), title=title)
    elif field == "tags":
        set_tags(store, pid, _tags_list(old_val), title=title)
    elif field == "status":
        if old_val:
            set_products_status(store, [pid], old_val)
    else:
        return {"ok": False, "error": f"cannot revert field '{field}'"}
    with db.connect() as conn:
        conn.execute("UPDATE pm_optimization_history SET reverted = 1 WHERE store_key = ? AND id = ?",
                     (store, int(entry_id)))
    return {"ok": True, "reverted_field": field}


# ── Saved filter presets (server-side; was localStorage) ──────────────────────────────────────
def list_saved_filters(store: str) -> list:
    out: list = []
    try:
        from . import db
        with db.connect() as conn:
            for r in conn.execute(
                "SELECT name, state FROM pm_saved_filters WHERE store_key = ? ORDER BY created_at",
                (store,)).fetchall():
                try:
                    out.append({"name": r["name"], "state": json.loads(r["state"])})
                except Exception:  # noqa: BLE001 — skip a corrupt row, keep the rest
                    pass
    except Exception:  # noqa: BLE001
        pass
    return out


def save_filter(store: str, name: str, state: dict) -> dict:
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name required"}
    from . import db
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO pm_saved_filters (store_key, name, state, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (store_key, name) DO UPDATE SET state = excluded.state",
            (store, name, json.dumps(state or {}), _now()))
    return {"ok": True, "name": name}


def delete_filter(store: str, name: str) -> dict:
    from . import db
    with db.connect() as conn:
        conn.execute("DELETE FROM pm_saved_filters WHERE store_key = ? AND name = ?",
                     (store, (name or "").strip()))
    return {"ok": True}


def snapshot_path(store: str):
    """Where the snapshot lives: general-stores/<store>/optimization.json."""
    return config.general_stores_dir() / store / "optimization.json"


def _patch_snapshot_status(store: str, changes: dict) -> None:
    """Best-effort: patch the on-disk snapshot's product `status` for products we just changed on
    Shopify, so the UI reflects it on the next read WITHOUT a full (order-pulling) re-sync. `changes`
    maps a numeric product id → new UPPERCASE status. No-op if there's no snapshot yet."""
    if not changes:
        return
    path = snapshot_path(store)
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — no snapshot / unreadable → nothing to patch
        return
    prods = snap.get("products")
    if not isinstance(prods, list):
        return
    touched = False
    for p in prods:
        num = str((p or {}).get("product_id") or "").rsplit("/", 1)[-1]
        if num in changes:
            p["status"] = changes[num]
            touched = True
    if touched:
        try:
            path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        except OSError:  # noqa: BLE001 — the Shopify write already succeeded; a re-sync will reconcile
            pass


# ONE snapshot build per store at a time — across EVERY caller (manual /sync route, worker
# store-data-sync, webhook run_incremental, sync-all). Concurrent all-time pulls of the same store
# drain that store's shared Shopify rate-limit bucket faster than it refills, so they all throttle
# and fail (observed on decorsdeluxe: a webhook-sync + a manual sync collided → "Throttled"). A
# store already syncing simply skips — the in-flight pull will produce the fresh snapshot anyway.
_sync_inflight_lock = threading.Lock()
_sync_inflight: set[str] = set()


def sync(store: str) -> dict:
    """Build the snapshot and persist it. Returns {ok, path?, synced_at?, products?, skipped?, error?}.
    Per-store coalesced: if a build for this store is already running, this call skips immediately."""
    with _sync_inflight_lock:
        if store in _sync_inflight:
            return {"ok": True, "skipped": True, "reason": "a snapshot sync for this store is already running"}
        _sync_inflight.add(store)
    try:
        result = build_snapshot(store)
        if not result.get("ok"):
            return result
        snap = result["snapshot"]
        path = snapshot_path(store)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snap, indent=2), encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": f"built snapshot but could not write {path.name}: {e}"}
        return {"ok": True, "path": str(path), "synced_at": snap["synced_at"], "products": len(snap["products"])}
    finally:
        with _sync_inflight_lock:
            _sync_inflight.discard(store)
