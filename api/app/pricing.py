"""Listing price rules + a transparent price suggestion.

The operator's pricing intents (the single source of truth so the rules show up identically
wherever a price is suggested):

  1. vs competing Google Shopping listings  → undercut. Come in just below the lowest
     competing listing — sourced from the competitor Google listing URL we import — so we
     win the click on price first. This is a *setting*, not a hard law: it can be toggled
     off (then price rides purely on the markup floor) and the undercut amount is tunable.
  2. vs the marketplace COGS basis (AliExpress / Temu / 1688 landed)  → keep a markup
     FLOOR (default 4×) so undercutting can never sell the margin away. The exact
     multiplier is still being validated — exposed as `draft`. Applied PER VARIANT/OPTION:
     each variant's own COGS sets its own floor, so a larger/heavier size with a higher
     landed cost simply prices higher.

Every suggested price lands on an allowed charm ending — X2.99 / X4.99 / X7.99 / X9.99
(the ones digit is 2/4/7/9, cents always .99): e.g. 22.99, 24.99, 27.99, 29.99. The
suggestion is deliberately transparent (it returns both candidate numbers + which rule won
+ any conflict) rather than a single opaque figure, so the operator can sanity-check it.
price_suggest is called once per variant — there is no single product-level price.
"""
from __future__ import annotations

import math
import random

from . import runlog

# Marketplace markup multiplier — still being validated, hence status "draft" below.
MARKETPLACE_MARKUP = 4.0

# Allowed charm endings within each $10 band — the ones digit is 2/4/7/9 and the cents are
# always .99, so every price lands on X2.99 / X4.99 / X7.99 / X9.99 (e.g. 22.99, 24.99,
# 27.99, 29.99, 32.99…). This is the operator's price-to-margin charm rule.
CHARM_ENDINGS = (2.99, 4.99, 7.99, 9.99)

# Compare-at ("was" / struck-through) discount tiers. ONE tier is drawn PER PRODUCT and
# applied uniformly to every variant — never mix tiers inside a product (if a product is
# 30% off, all its variants are 30% off). Weighted toward 30/40% off (50% off is rarer).
# Toggleable in the listing plan (set compare prices on/off). Same for both listing paths.
COMPARE_AT_TIERS = [0.30, 0.40, 0.50]
COMPARE_AT_WEIGHTS = [0.40, 0.40, 0.20]  # 30/40 off weighted more than 50 off

# Configurable undercut behaviour. The competitor price basis is the competing Google
# Shopping listing URL the operator imports; undercut can be turned off and the amount tuned.
PRICING_SETTINGS = {
    "undercut": {
        "enabled": True,
        "amount": 0.01,  # come in this much under the lowest competitor, then snap to .99
        "basis": "imported competitor Google listing URL",
        "note": (
            "Setting (not a hard law). On: price just below the lowest competing Google "
            "listing we import. Off: price rides purely on the markup floor."
        ),
    },
    "compare_at": {
        "enabled": True,
        "tiers": [int(t * 100) for t in COMPARE_AT_TIERS],  # [30, 40, 50] percent off
        "weights": COMPARE_AT_WEIGHTS,
        "scope": "per product — one tier drawn per product, applied to every variant",
        "note": (
            "Set a struck-through compare-at so each product shows a sale. One random tier "
            "(30/40/50% off, weighted toward 30/40) is drawn per product and applied to ALL "
            "its variants uniformly. Toggle on/off in the listing plan. Both listing paths."
        ),
    },
    "per_variant": True,  # every rule is applied per variant/option, never product-wide
}

PRICING_RULES = [
    {
        "id": "undercut-competitor",
        "name": "Undercut competing Google listings",
        "rule": "Price just below the lowest competing Google listing we import.",
        "basis": "competitor",
        "priority": 1,
        "status": "setting",
        "note": (
            "Win the click on price first. Toggleable setting — basis is the competitor "
            "Google listing URL we import. Can be turned off to price on the floor alone."
        ),
    },
    {
        "id": "marketplace-markup-floor",
        "name": "Marketplace markup floor (per variant)",
        "rule": f"Never price below {MARKETPLACE_MARKUP:g}× the marketplace COGS basis.",
        "basis": "cogs",
        "multiple": MARKETPLACE_MARKUP,
        "priority": 2,
        "status": "draft",
        "note": (
            "Protects margin so undercutting can't run at a loss. Applied per variant/option "
            "— each variant's COGS sets its own floor (larger size = higher COGS = higher "
            "price). Multiplier still to be validated."
        ),
    },
    {
        "id": "charm-ending",
        "name": "Charm ending (X2.99 / X4.99 / X7.99 / X9.99)",
        "rule": "Every price snaps to a 2.99 / 4.99 / 7.99 / 9.99 ending.",
        "basis": "format",
        "endings": list(CHARM_ENDINGS),
        "priority": 3,
        "status": "locked",
    },
    {
        "id": "compare-at-discount",
        "name": "Compare-at sale (per product)",
        "rule": "Show a 30/40/50%-off compare-at — one tier per product, all variants the same.",
        "basis": "compare_at",
        "priority": 4,
        "status": "setting",
        "note": (
            "One random discount tier per product (weighted toward 30/40% off), applied "
            "uniformly to every variant. Toggleable on/off in the listing plan."
        ),
    },
]


# ----------------------------------------------------------- persisted operator overrides
# Settings is the single source of truth: these are the knobs the operator can edit + save
# from the Settings → Listing tab. Defaults below are the starting point; the saved override
# (one runlog key) is layered on top, and every price suggestion reads the effective values.
PRICING_KEY = "pricing_rules"

PRICING_DEFAULTS = {
    "undercut_enabled": True,      # undercut the lowest imported competitor Google listing
    "undercut_amount": 0.01,       # come in this much under, then snap to a .99 charm
    "marketplace_markup": MARKETPLACE_MARKUP,  # per-variant COGS markup floor (default 4×)
    "compare_at_enabled": True,    # show a struck-through "was" sale price
    "compare_at_tiers": [int(t * 100) for t in COMPARE_AT_TIERS],   # [30, 40, 50] % off
    "compare_at_weights": list(COMPARE_AT_WEIGHTS),                 # weighting per tier
}


def _clampf(v, lo, hi) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return lo


def _fresh_pricing() -> dict:
    cfg = dict(PRICING_DEFAULTS)
    cfg["compare_at_tiers"] = list(PRICING_DEFAULTS["compare_at_tiers"])
    cfg["compare_at_weights"] = list(PRICING_DEFAULTS["compare_at_weights"])
    return cfg


def _apply_pricing_overrides(cfg: dict, overrides: dict | None) -> dict:
    o = overrides or {}
    if o.get("undercut_enabled") is not None:
        cfg["undercut_enabled"] = bool(o["undercut_enabled"])
    if o.get("undercut_amount") is not None:
        cfg["undercut_amount"] = _clampf(o["undercut_amount"], 0.0, 50.0)
    if o.get("marketplace_markup") is not None:
        cfg["marketplace_markup"] = _clampf(o["marketplace_markup"], 1.0, 20.0)
    if o.get("compare_at_enabled") is not None:
        cfg["compare_at_enabled"] = bool(o["compare_at_enabled"])
    if isinstance(o.get("compare_at_tiers"), list) and o["compare_at_tiers"]:
        cfg["compare_at_tiers"] = [int(_clampf(t, 0, 90)) for t in o["compare_at_tiers"]]
    if isinstance(o.get("compare_at_weights"), list) and o["compare_at_weights"]:
        cfg["compare_at_weights"] = [_clampf(w, 0.0, 1.0) for w in o["compare_at_weights"]]
    return cfg


def saved_pricing() -> dict:
    """Effective pricing knobs = DEFAULTS with the persisted operator override layered on.
    This is what the Settings editor reads/writes and what every price suggestion uses."""
    cfg = _fresh_pricing()
    try:
        stored = runlog.setting_get(PRICING_KEY)
    except Exception:
        stored = None
    return _apply_pricing_overrides(cfg, stored)


def save_pricing(overrides: dict | None) -> dict:
    """Persist clamped pricing knobs as the new default; returns the saved (clamped) cfg."""
    cfg = _apply_pricing_overrides(_fresh_pricing(), overrides)
    runlog.setting_set(PRICING_KEY, cfg)
    return cfg


def reset_pricing() -> dict:
    """Drop the persisted override → back to the hard-coded defaults."""
    try:
        runlog.setting_delete(PRICING_KEY)
    except Exception:
        pass
    return _fresh_pricing()


def rules() -> dict:
    """The pricing rule set, reflecting the operator's SAVED knobs (Settings is the source
    of truth). The displayed rules + numbers track whatever is persisted."""
    p = saved_pricing()
    markup = p["marketplace_markup"]
    settings = {
        "undercut": {
            **PRICING_SETTINGS["undercut"],
            "enabled": p["undercut_enabled"],
            "amount": p["undercut_amount"],
        },
        "compare_at": {
            **PRICING_SETTINGS["compare_at"],
            "enabled": p["compare_at_enabled"],
            "tiers": p["compare_at_tiers"],
            "weights": p["compare_at_weights"],
        },
        "per_variant": True,
    }
    rules_list = []
    for r in PRICING_RULES:
        r2 = dict(r)
        if r2["id"] == "marketplace-markup-floor":
            r2["multiple"] = markup
            r2["rule"] = f"Never price below {markup:g}× the marketplace COGS basis."
        elif r2["id"] == "undercut-competitor":
            r2["status"] = "setting" if p["undercut_enabled"] else "off"
        elif r2["id"] == "compare-at-discount":
            r2["status"] = "setting" if p["compare_at_enabled"] else "off"
        rules_list.append(r2)
    return {
        "rules": rules_list,
        "editable": p,
        "defaults": _fresh_pricing(),
        "marketplace_markup": markup,
        "charm": "2.99 / 4.99 / 7.99 / 9.99",
        "charm_endings": list(CHARM_ENDINGS),
        "settings": settings,
        "per_variant": True,
        "note": (
            "Undercut the cheapest competing Google listing we import (a toggleable setting), "
            f"but never below the {markup:g}× marketplace-COGS floor. Applied per "
            "variant/option, not product-wide. Every price snaps to a 2.99/4.99/7.99/9.99 "
            "charm ending. These knobs are editable + saved in Settings → Listing."
        ),
    }


def _charm_candidates(x: float) -> list[float]:
    """Every allowed charm price (X2.99/X4.99/X7.99/X9.99) from 0 up past x, ascending.
    Bounded a little above x so a 2× compare-at lookup always finds a value ≥ its target."""
    top = math.floor((max(x, 0.0) * 2 + 20) / 10) * 10
    out: list[float] = []
    tens = 0
    while tens <= top:
        for e in CHARM_ENDINGS:
            out.append(round(tens + e, 2))
        tens += 10
    return out


def _charm_floor(x: float) -> float:
    """Largest allowed charm price ≤ x (falls back to the smallest ending for tiny x)."""
    cands = _charm_candidates(x)
    chosen = cands[0]
    for v in cands:
        if v <= x + 1e-9:
            chosen = v
    return chosen


def _charm_ceil(x: float) -> float:
    """Smallest allowed charm price ≥ x."""
    cands = _charm_candidates(x)
    for v in cands:
        if v >= x - 1e-9:
            return v
    return cands[-1]


def pick_discount_tier(seed: int | None = None) -> int:
    """Draw ONE compare-at discount tier for a whole product, from the operator's SAVED
    tiers/weights (Settings → Listing). Call once per product, then apply the same tier to
    every variant — never re-draw per variant. `seed` makes it reproducible per product."""
    p = saved_pricing()
    if not p["compare_at_enabled"]:
        return 0
    tiers = p["compare_at_tiers"] or [int(t * 100) for t in COMPARE_AT_TIERS]
    weights = p["compare_at_weights"] or list(COMPARE_AT_WEIGHTS)
    if len(weights) != len(tiers):
        weights = None  # mismatched → uniform
    rng = random.Random(seed) if seed is not None else random
    pct = rng.choices(tiers, weights=weights, k=1)[0]
    return int(round(pct))


def compare_at_for(price: float | None, discount_pct: int | None) -> float | None:
    """The struck-through 'was' price so that `price` reads as `discount_pct`% off,
    snapped up to a .99 charm. e.g. 24.99 at 30% off → was 35.99."""
    if price is None or price <= 0 or not discount_pct:
        return None
    frac = discount_pct / 100.0
    if frac >= 1:
        return None
    return _charm_ceil(price / (1 - frac))


def price_suggest(
    cogs: float | None = None,
    competitor_low: float | None = None,
    undercut_enabled: bool | None = None,
    compare_at_pct: int | None = None,
) -> dict:
    """Suggest a list price for ONE variant from the rules. Transparent: returns both
    candidates, which rule won, the margin, and whether the two rules conflict. Call once
    per variant/option — a larger size with a higher COGS just gets a higher floor.

    `undercut_enabled` reflects the undercut setting: when False the competitor price is
    ignored and the price rides purely on the per-variant markup floor.

    `compare_at_pct` is the product's drawn discount tier (30/40/50). When given, a
    struck-through compare-at price is computed off the recommended price so the variant
    shows that % off. Same tier is passed for every variant of the product."""
    p = saved_pricing()
    if undercut_enabled is None:
        undercut_enabled = p["undercut_enabled"]
    amount = p["undercut_amount"]
    markup = p["marketplace_markup"]
    undercut: float | None = None
    if undercut_enabled and competitor_low is not None and competitor_low > 0:
        # Strictly below the lowest competitor (imported Google listing), snap to a .99 charm.
        undercut = _charm_floor(competitor_low - amount)
        if undercut <= 0:
            undercut = None

    markup_floor: float | None = None
    if cogs is not None and cogs > 0:
        markup_floor = _charm_ceil(cogs * markup)

    conflict = False
    basis = None
    recommended: float | None = None
    if undercut is not None and markup_floor is not None:
        if undercut >= markup_floor:
            recommended, basis = undercut, "undercut-competitor"
        else:
            # Can't undercut without breaking the margin floor — floor wins, flag it.
            recommended, basis, conflict = markup_floor, "marketplace-markup-floor", True
    elif undercut is not None:
        recommended, basis = undercut, "undercut-competitor"
    elif markup_floor is not None:
        recommended, basis = markup_floor, "marketplace-markup-floor"

    margin_pct = None
    if recommended is not None and cogs:
        margin_pct = round((recommended - cogs) / recommended * 100, 1)

    compare_at = compare_at_for(recommended, compare_at_pct)

    return {
        "inputs": {"cogs": cogs, "competitor_low": competitor_low},
        "undercut_enabled": undercut_enabled,
        "undercut": undercut,
        "markup_floor": markup_floor,
        "marketplace_markup": markup,
        "recommended": recommended,
        "compare_at_pct": compare_at_pct,
        "compare_at": compare_at,
        "basis": basis,
        "conflict": conflict,
        "margin_pct": margin_pct,
        "note": (
            f"Undercut breaks the {markup:g}× margin floor — held at the floor instead; consider re-sourcing cheaper."
            if conflict else
            f"Undercuts the cheapest imported competitor while clearing the {markup:g}× margin floor."
            if basis == "undercut-competitor" and markup_floor is not None else
            "Undercuts the cheapest imported competitor." if basis == "undercut-competitor" else
            f"Undercut off — priced on the {markup:g}× marketplace-COGS floor alone."
            if basis == "marketplace-markup-floor" and not undercut_enabled else
            f"No competitor price given — held at the {markup:g}× marketplace-COGS floor."
            if basis == "marketplace-markup-floor" else
            "Provide a COGS basis and/or the lowest competitor price for a suggestion."
        ),
    }
