"""Automation rules for the Product Performance page.

A rule = "WHEN <conditions> on <window/scope> → <action>". The engine is ANALYSIS-first: it
evaluates every enabled rule against the (already reconciled) optimization snapshot and reports
which products WOULD trigger which action + why — it NEVER auto-executes. The operator reviews the
matches and applies (or later opts into auto-apply per action). This mirrors the ads scale loop's
analysis-first posture, and it's what makes it safe to act on: the numbers underneath are tied to
the P&L, split per market, and carry PM's per-market COGS.

Scopes:
  • total        → evaluate the product's window totals.
  • any_market   → matches if ANY single market (country) satisfies all conditions.
  • all_markets  → matches if EVERY market the product sells in satisfies all conditions (≥1 market).

Metrics read from a window dict (optimization_view already derives net/profit/margin): ad_spend,
roas, sales (conv value), qty, margin, profit, refunds, net, orders. roas is derived (cv/cost).
"""
from __future__ import annotations

import datetime as _dt
import json

from . import db

WINDOWS = ("7", "14", "30", "all")
SCOPES = ("total", "any_market", "all_markets")
METRICS = ("ad_spend", "sales", "qty", "refunds", "orders", "net", "profit", "margin", "roas")
OPS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}
# action key → human label (the operator applies these; no auto-execute)
ACTIONS = {
    "draft": "Set to Draft",
    "exclude": "Exclude from optimization",
    "lower_price": "Lower price",
    "optimize_title": "Optimize title",
    "optimize_product": "Optimize product (PDP)",
    "flag": "Flag / note",
}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _metric(win: dict, key: str):
    """Value of `key` from a window dict, or None when not computable (so a rule never fires on
    a product with no data — e.g. roas when nothing was spent)."""
    if not isinstance(win, dict):
        return None
    cv = float(win.get("cv") or 0)
    cost = float(win.get("cost") or 0)
    if key == "ad_spend":
        return cost
    if key == "sales":
        return cv
    if key == "qty":
        return float(win.get("qty") or 0)
    if key == "refunds":
        return float(win.get("refunds") or 0)
    if key == "orders":
        return float(win.get("orders") or 0)
    if key == "net":
        return float(win.get("net") or 0)
    if key == "profit":
        return win.get("profit")  # may be None
    if key == "margin":
        return win.get("margin_pct")  # may be None
    if key == "roas":
        return (cv / cost) if cost > 0 else None
    return None


def _conditions_ok(win: dict, conditions: list) -> bool:
    """All conditions (AND) satisfied by this window. A condition with an incomputable metric
    (None) is NOT satisfied — we don't act on products with no data for the tested metric."""
    for c in conditions or []:
        val = _metric(win, str(c.get("metric") or ""))
        op = OPS.get(str(c.get("op") or ""))
        try:
            target = float(c.get("value"))
        except (TypeError, ValueError):
            return False
        if val is None or op is None or not op(float(val), target):
            return False
    return True


# ── Global on/off master switch ───────────────────────────────────────────────────────────────
def get_enabled(store: str) -> bool:
    """Whether automation is globally ON for this store (default ON)."""
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT enabled FROM pm_automation_settings WHERE store_key = ?", (store,)).fetchone()
        return bool(int(row["enabled"])) if row else True
    except Exception:  # noqa: BLE001
        return True


def set_enabled(store: str, enabled: bool) -> dict:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO pm_automation_settings (store_key, enabled, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (store_key) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
            (store, 1 if enabled else 0, _now()))
    return {"ok": True, "enabled": bool(enabled)}


# ── CRUD ────────────────────────────────────────────────────────────────────────────────────
def list_rules(store: str) -> dict:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM pm_automation_rules WHERE store_key = ? ORDER BY id", (store,)
        ).fetchall()]
    for r in rows:
        r["enabled"] = bool(r["enabled"])
        r["window"] = r.pop("win", "30")  # DB column is `win` (window is a reserved word in Postgres)
        try:
            r["conditions"] = json.loads(r["conditions"] or "[]")
        except (ValueError, TypeError):
            r["conditions"] = []
    return {"ok": True, "store": store, "automation_enabled": get_enabled(store), "rules": rows}


def save_rule(store: str, rule: dict) -> dict:
    """Create (no id) or update (id present) a rule. Validates window / scope / action / ops."""
    name = str(rule.get("name") or "").strip() or "Untitled rule"
    window = str(rule.get("window") or "30")
    scope = str(rule.get("scope") or "total")
    action = str(rule.get("action") or "")
    conditions = rule.get("conditions") or []
    if window not in WINDOWS:
        return {"ok": False, "error": f"window must be one of {WINDOWS}"}
    if scope not in SCOPES:
        return {"ok": False, "error": f"scope must be one of {SCOPES}"}
    if action not in ACTIONS:
        return {"ok": False, "error": f"action must be one of {sorted(ACTIONS)}"}
    if not isinstance(conditions, list) or not conditions:
        return {"ok": False, "error": "at least one condition is required"}
    for c in conditions:
        if str(c.get("metric") or "") not in METRICS:
            return {"ok": False, "error": f"unknown metric: {c.get('metric')}"}
        if str(c.get("op") or "") not in OPS:
            return {"ok": False, "error": f"unknown op: {c.get('op')}"}
        try:
            float(c.get("value"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "each condition needs a numeric value"}
    enabled = 1 if rule.get("enabled", True) else 0
    cond_json = json.dumps(conditions)
    now = _now()
    with db.connect() as conn:
        rid = rule.get("id")
        if rid:
            conn.execute(
                "UPDATE pm_automation_rules SET name=?, enabled=?, win=?, scope=?, conditions=?, "
                "action=?, updated_at=? WHERE store_key=? AND id=?",
                (name, enabled, window, scope, cond_json, action, now, store, int(rid)),
            )
        else:
            cur = conn.execute(
                "INSERT INTO pm_automation_rules "
                "(store_key, name, enabled, win, scope, conditions, action, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (store, name, enabled, window, scope, cond_json, action, now, now),
            )
            rid = cur.lastrowid
    return {"ok": True, "id": rid}


def delete_rule(store: str, rule_id: int) -> dict:
    with db.connect() as conn:
        conn.execute("DELETE FROM pm_automation_rules WHERE store_key = ? AND id = ?", (store, int(rule_id)))
    return {"ok": True}


# ── Evaluation (analysis only) ────────────────────────────────────────────────────────────────
def evaluate(store: str) -> dict:
    """Run every ENABLED rule against the current optimization snapshot. Returns the products that
    WOULD trigger each rule (with the market when the scope is per-market + the values that matched).
    Never executes — the operator applies from the matches list."""
    if not get_enabled(store):
        return {"ok": True, "store": store, "evaluated_at": _now(), "paused": True,
                "currency": None, "rule_count": 0, "product_count": 0, "match_count": 0, "matches": []}
    from . import readers
    snap = readers.optimization_view(store)
    products = [p for p in (snap.get("products") or []) if not p.get("hidden")]
    rules = list_rules(store)["rules"]
    matches: list[dict] = []
    for rule in rules:
        if not rule.get("enabled"):
            continue
        win_key = rule["window"]
        scope = rule["scope"]
        conds = rule["conditions"]
        for p in products:
            hit_market = None
            values = None
            if scope == "total":
                w = (p.get("windows") or {}).get(win_key) or {}
                if _conditions_ok(w, conds):
                    values = _snapshot_values(w)
            else:
                mkts = {c: (wins.get(win_key) or {}) for c, wins in (p.get("markets") or {}).items()}
                mkts = {c: w for c, w in mkts.items() if w}
                if not mkts:
                    continue
                oks = [c for c, w in mkts.items() if _conditions_ok(w, conds)]
                if scope == "any_market" and oks:
                    hit_market = oks[0]
                    values = _snapshot_values(mkts[hit_market])
                elif scope == "all_markets" and len(oks) == len(mkts):
                    hit_market = "all"
                    values = _snapshot_values(next(iter(mkts.values())))
            if values is not None:
                matches.append({
                    "rule_id": rule["id"], "rule_name": rule["name"], "action": rule["action"],
                    "action_label": ACTIONS.get(rule["action"], rule["action"]),
                    "window": win_key, "scope": scope, "market": hit_market,
                    "product_id": p.get("product_id"), "title": p.get("title"),
                    "image": p.get("image"), "status": p.get("status"),
                    "values": values,
                })
    return {
        "ok": True, "store": store, "evaluated_at": _now(),
        "currency": snap.get("currency"), "rule_count": sum(1 for r in rules if r.get("enabled")),
        "product_count": len(products), "match_count": len(matches), "matches": matches,
    }


# ── Apply an action + activity log ──────────────────────────────────────────────────────────
def _log(store, rule_name, pid, title, market, action, detail, result, vals) -> None:
    try:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO pm_automation_log "
                "(store_key, at, rule_name, product_id, product_title, market, action, detail, result, vals) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (store, _now(), rule_name, pid, title, market, action, detail, result,
                 json.dumps(vals) if vals is not None else None))
    except Exception:  # noqa: BLE001 — logging must never break the action
        pass


def apply_action(store: str, product_id: str, action: str, rule_name=None, product_title=None,
                 market=None, vals=None) -> dict:
    """Apply ONE automation action for a product and log it. draft/exclude EXECUTE for real
    (Shopify status / app flag); the content actions (lower_price / optimize_*) are recorded and
    the product is FLAGGED for the operator (no destructive auto-edit until wired). Always logged."""
    from . import optimization
    pid = str(product_id or "").rsplit("/", 1)[-1].strip()
    if not pid:
        return {"ok": False, "error": "product_id required"}
    result, detail = "logged", ""
    if action == "draft":
        r = optimization.set_products_status(store, [pid], "DRAFT")
        ok = bool(r.get("ok"))
        result = "applied" if ok else "failed"
        detail = "Shopify status → DRAFT" if ok else f"Shopify draft failed: {((r.get('failed') or [{}])[0]).get('error', '')}"
    elif action == "exclude":
        optimization.set_flag(store, pid, hidden=True)
        result, detail = "applied", "Excluded from the optimization view"
    else:
        # lower_price / optimize_title / optimize_product — flag for the operator (no auto-edit yet).
        mk = f", market {market}" if market and market != "all" else ""
        optimization.set_flag(store, pid, note=f"Automation: {ACTIONS.get(action, action)} (rule: {rule_name or '—'}{mk})")
        result, detail = "flagged", f"Flagged for {ACTIONS.get(action, action)} — apply manually"
    _log(store, rule_name, pid, product_title, market, action, detail, result, vals)
    return {"ok": result != "failed", "result": result, "detail": detail}


def get_log(store: str, limit: int = 100) -> dict:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT at, rule_name, product_id, product_title, market, action, detail, result, vals "
            "FROM pm_automation_log WHERE store_key = ? ORDER BY id DESC LIMIT ?",
            (store, max(1, min(int(limit), 500)))).fetchall()]
    for r in rows:
        r["action_label"] = ACTIONS.get(r.get("action"), r.get("action"))
        try:
            r["vals"] = json.loads(r["vals"]) if r.get("vals") else None
        except (ValueError, TypeError):
            r["vals"] = None
    return {"ok": True, "store": store, "entries": rows}


def _snapshot_values(w: dict) -> dict:
    cv = float(w.get("cv") or 0)
    cost = float(w.get("cost") or 0)
    return {
        "ad_spend": round(cost, 2), "sales": round(cv, 2), "qty": int(w.get("qty") or 0),
        "refunds": round(float(w.get("refunds") or 0), 2), "profit": w.get("profit"),
        "margin": w.get("margin_pct"), "roas": round(cv / cost, 2) if cost > 0 else None,
    }
