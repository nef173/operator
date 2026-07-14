"""alerts — proactive performance alerts across every store.

Scans each store's (always-fresh) Product Performance snapshot for products that need attention and
ranks them by € impact, so problems find the operator instead of the operator hunting for them.
Computed live on read — the snapshots are kept current by the 6h worker + order webhooks, so the
digest is always up to date without its own storage or schedule.

Conditions (on the 30-day window — recent health is what's actionable; falls back to all-time when
the 30d slice is empty):
  bleeding      profit < 0 with real revenue          — losing money on the product       (high)
  wasting_ads   ROAS < 1.5 with real ad spend         — ad budget not paying back          (high)
  refund_spike  refunds > 20% of revenue              — a quality / listing / sizing issue (medium)
  thin_margin   0 <= margin < 10% with real revenue   — barely profitable                  (medium)
  winner        ROAS >= 3 with strong revenue         — scale opportunity (a GOOD alert)   (positive)
"""
from __future__ import annotations

from . import readers

# Thresholds — MATERIAL floors so the digest is the handful worth acting on, not every marginal SKU.
_MIN_REV = 100.0         # ignore trickle products (revenue floor to raise any alert)
_MIN_LOSS = 200.0        # only flag "bleeding" when the loss is at least this (€ impact)
_MIN_AD = 150.0          # ad-spend floor for the ROAS-waste alert
_ROAS_WASTE = 1.5        # ROAS below this (with material spend) = wasting ad budget
_ROAS_WIN = 3.0          # ROAS at/above this (with strong revenue) = scale it
_WIN_REV = 750.0         # revenue floor for a "winner"
_REFUND_MIN = 100.0      # refunds must be at least this € to be a "spike" (not just a % on tiny rev)
_REFUND_FRAC = 0.20      # refunds above this fraction of revenue = spike
_THIN_MARGIN = 10.0      # margin % below this (but >= 0) = thin
_THIN_MIN_REV = 400.0    # only flag thin margin on a product doing real revenue
_MAX_ITEMS = 150         # cap the digest list (already ranked by € impact)

_SEV_RANK = {"high": 0, "medium": 1, "positive": 2}


def _roas(w: dict) -> float | None:
    cost = float(w.get("cost") or 0)
    return (float(w.get("cv") or 0) / cost) if cost > 0 else None


def _alerts_for(store: str, p: dict) -> list[dict]:
    """Zero or more alert dicts for one product (30d window, all-time fallback)."""
    wins = p.get("windows") or {}
    w = wins.get("30") or {}
    if not (float(w.get("cv") or 0) > 0 or float(w.get("cost") or 0) > 0):
        w = wins.get("all") or {}          # no recent activity → judge on lifetime
    cv = float(w.get("cv") or 0)
    refunds = float(w.get("refunds") or 0)
    cost = float(w.get("cost") or 0)
    profit = w.get("profit")
    margin = w.get("margin_pct")
    roas = _roas(w)
    out: list[dict] = []

    def add(kind, severity, headline, impact):
        out.append({
            "store": store, "product_id": p.get("product_id"), "title": p.get("title"),
            "status": p.get("status"), "kind": kind, "severity": severity,
            "headline": headline, "impact": round(float(impact), 2),
            "roas": round(roas, 2) if roas is not None else None,
            "revenue": round(cv, 2), "ad_spend": round(cost, 2),
            "profit": round(float(profit), 2) if profit is not None else None,
            "margin_pct": margin, "refunds": round(refunds, 2),
        })

    if cv < _MIN_REV and cost < _MIN_AD:
        return out  # too small to matter

    if profit is not None and profit <= -_MIN_LOSS and cv >= _MIN_REV:
        add("bleeding", "high", f"Losing money — profit {profit:,.0f}", -profit)
    elif roas is not None and roas < _ROAS_WASTE and cost >= _MIN_AD:
        add("wasting_ads", "high", f"ROAS {roas:.2f}× on {cost:,.0f} ad spend", cost)

    if refunds >= _REFUND_MIN and cv >= _MIN_REV and refunds > _REFUND_FRAC * cv:
        add("refund_spike", "medium", f"Refunds {refunds / cv * 100:.0f}% of revenue", refunds)

    if margin is not None and 0 <= margin < _THIN_MARGIN and cv >= _THIN_MIN_REV and not any(a["kind"] == "bleeding" for a in out):
        add("thin_margin", "medium", f"Thin margin {margin:.1f}%", cv)

    if roas is not None and roas >= _ROAS_WIN and cv >= _WIN_REV:
        add("winner", "positive", f"Winner — ROAS {roas:.2f}× on {cv:,.0f} revenue", cv)

    return out


def _is_active(p: dict) -> bool:
    """Snapshot stores status uppercased (ACTIVE / DRAFT / ARCHIVED). Only live products count."""
    return str(p.get("status") or "").strip().upper() == "ACTIVE"


def scan_store(store: str) -> list[dict]:
    """All alerts for one store. Only ACTIVE products are scanned — draft/archived products aren't
    live, so their ad spend / refunds aren't actionable (and the operator already archived the ones
    they killed). Hidden/excluded products are already filtered out of the view upstream."""
    try:
        view = readers.optimization_view(store)
    except Exception:  # noqa: BLE001 — one store's read must not sink the digest
        return []
    prods = [p for p in (view.get("products") or []) if p.get("product_id") and not p.get("hidden")]
    # Filter to ACTIVE only. Fallback: if a snapshot carries no status at all (predates status
    # capture), don't blank the digest — scan what we have rather than silently returning nothing.
    has_status = any(str(p.get("status") or "").strip() for p in prods)
    scan = [p for p in prods if _is_active(p)] if has_status else prods
    items: list[dict] = []
    for p in scan:
        items.extend(_alerts_for(store, p))
    return items


def scan_one(store: str) -> dict:
    """Single-store digest — the 'fix these first' work view inside Product Optimization. Same shape
    as scan_all (counts + ranked items) but scoped to one store."""
    items = scan_store(store)
    items.sort(key=lambda a: (_SEV_RANK.get(a["severity"], 9), -a["impact"]))
    counts = {sev: sum(1 for a in items if a["severity"] == sev) for sev in ("high", "medium", "positive")}
    return {
        "ok": True,
        "store": store,
        "counts": counts,
        "total": len(items),
        "needs_attention": counts["high"],
        "items": items[:_MAX_ITEMS],
        "shown": min(len(items), _MAX_ITEMS),
    }


def scan_all() -> dict:
    """Cross-store digest: ranked items + per-store and per-severity counts."""
    stores = readers.list_stores()
    items: list[dict] = []
    for s in stores:
        items.extend(scan_store(s))
    # Rank: severity (high → medium → positive), then € impact desc.
    items.sort(key=lambda a: (_SEV_RANK.get(a["severity"], 9), -a["impact"]))
    by_store: dict[str, dict] = {}
    for a in items:
        b = by_store.setdefault(a["store"], {"store": a["store"], "high": 0, "medium": 0, "positive": 0})
        b[a["severity"]] = b.get(a["severity"], 0) + 1
    counts = {
        "high": sum(1 for a in items if a["severity"] == "high"),
        "medium": sum(1 for a in items if a["severity"] == "medium"),
        "positive": sum(1 for a in items if a["severity"] == "positive"),
    }
    return {
        "ok": True,
        "counts": counts,                                        # true totals across all stores
        "total": len(items),
        "needs_attention": counts["high"],                      # badge = the urgent (money-losing) count
        "by_store": sorted(by_store.values(), key=lambda x: -(x["high"] * 10 + x["medium"])),
        "items": items[:_MAX_ITEMS],                             # ranked by € impact, capped for display
        "shown": min(len(items), _MAX_ITEMS),
    }
