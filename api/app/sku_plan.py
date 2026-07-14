"""SKU-plan engine — turn the keyword-discovery backlog into a buildable, ranked plan.

The SKU Plan tab is the DRIVER of research: each gate-cleared head keyword fans out into
sourceable sub-keywords, and THIS module decides — deterministically, stdlib-only — which
of them to actually build, what ROLE each plays, how many products to FIND per source, and
what the DEDUP rules are. The output feeds the research/sourcing handoff.

Design (locked with the operator 2026-06-20):

  1. TWO-TIER SELECTION. The head keyword is the leader/anchor. `anchor_pct` of the
     product-research effort is spent on the head term itself (the anchor cluster); the
     rest spreads across the similar/related sub-keywords (Tier 2), which are selected by
     CAPTURED search demand until `coverage_pct` of the Tier-2 demand is covered. So a few
     big-volume similars get built before a long tail of tiny ones.

  2. ROLE CLASSIFICATION. Every sub-keyword is one of:
       standalone   — enough distinct demand to be its own SKU
       combine      — a feature/spec modifier that rides INTO another SKU's title
       supporting   — too little demand for a SKU; feeds an existing listing's copy
     (Heuristic baseline here; an LLM refinement seam is documented at `classify_role` —
     the app defers AI steps the same way the vision check is deferred. "It can be both"
     per the operator, so the LLM upgrade decides combine-vs-supporting per keyword.)

  3. SOURCE QUOTA. For each to-build keyword, the products to FIND are allocated across the
     discovery sources purely by weight, Google-competitor-LED (Meta weighted smallest):
       google ~55% · marketplace ~15% · amazon ~15% · meta ~15%
     The weights ARE the lever — Meta's share is just its weight, there's no separate cap.
     Min floors keep every weighted source contributing. When a source runs dry (no new
     products to find), its budget is redistributed onto the still-productive sources
     (`adapt_weights`) so the daily listing count is still met — e.g. Meta + Amazon dry →
     their share flows to Google competitor + Marketplace. The Google lane itself has two
     paths (our Best-Seller-Spy roster + direct non-branded Google Shopping) — surfaced as
     metadata so the handoff knows where to look.

  4. DEDUP. Sub-keywords already built for the store are skipped; the same physical product
     is capped at `dedup_cap` (2-3) DIFFERENT-STYLE listings, never ~10×. The pixel-level
     vision dedup runs at research time (pending seam); this module carries the rules + the
     already-in-catalog skips.

Pure-stdlib + read-only: it consumes `readers.keyword_discovery()` and returns a plan. It
never mutates state. Firing the research handoff is a separate, explicit POST that routes
through the existing jobs system.
"""
from __future__ import annotations

from . import readers, runlog

# Persisted operator-tuned weight split lives under this key in app_settings (db.py). The
# stored value layers ON TOP of DEFAULTS to become the effective base; per-request query
# overrides then layer on top of THAT (so a one-off plan can still nudge a number without
# changing the saved default).
SETTINGS_KEY = "sku_plan_weights"

# Persisted per-source SUPPLY state. The discovery sources don't always have NEW products to
# find: Meta ads, an Amazon/marketplace movers feed, or a per-keyword pocket can run dry for a
# while until fresh ones appear. When a source is flagged `dry`, this engine redistributes its
# find-budget onto the still-productive sources (proportionally, Meta cap still honoured) so the
# DAILY DESIRED LISTING COUNT is still met from what's actually available — then the dry source
# is periodically re-checked and, the moment new products pop up, flipped back to `live` so its
# weight returns. Default = every source live (a no-op, so behaviour is unchanged until a source
# is actually marked dry). Yield-driven auto-flagging is the documented seam: a research scan that
# returns no NEW candidates for a source marks it dry; one that finds new ones marks it live.
SUPPLY_KEY = "sku_plan_source_supply"

# ------------------------------------------------------------------ tunable defaults
# Every number here is an operator-tunable default (exposed in the API response + Settings).
DEFAULTS: dict = {
    "anchor_pct": 40,          # share of product-research effort anchored on the head keyword
    "coverage_pct": 60,        # cover this much of the Tier-2 (similar-query) search demand
    "products_per_build": 8,   # nominal candidate products to find per to-build keyword (SCALED by
                               # the head keyword's volume tier — see `products_by_tier`)
    "dedup_cap": 3,            # max DIFFERENT-STYLE listings of the same physical product
    "source_weights": {"google_shopping": 30, "competitor_catalog": 25,
                       "marketplace": 15, "amazon": 15, "meta": 15},
    "role": {
        "standalone_sv_floor": 1000,   # absolute SV that earns a standalone SKU
        "standalone_head_frac": 0.30,  # OR this fraction of the head keyword's SV
        "supporting_sv_ceiling": 300,  # at/below this → supporting-only (no SKU)
    },
    # VOLUME TIERS (project-wide, operator 2026-07-04). The 10k floor is the GATE, not the focus:
    # search volume is the winner-probability lever, so we bias the whole plan toward the biggest
    # keywords. A keyword's monthly SV maps to a tier, and that tier SCALES how many products we
    # build for it — a 200k+ "prime" keyword earns the most listings (take market share, high
    # winner odds), a floor-band "entry" keyword the fewest. Mid keywords are still taken, just
    # with fewer variants. Thresholds are the lower bound of each tier (monthly searches).
    "volume": {
        "prime": 200_000,   # ≥200k — primary focus, list the most products
        "strong": 100_000,  # 100k–200k — very good, list more
        "solid": 30_000,    # 30k–100k — good supplementary
        "entry": 10_000,    # 10k–30k — floor band (== the gate), take but not the focus
    },
    # Multiplier on products_per_build by the head keyword's volume tier (list MORE for big SV).
    "products_by_tier": {"prime": 2.0, "strong": 1.5, "solid": 1.0, "entry": 0.6},
}

# Tier order high→low, and the display labels the API/UI speak.
VOLUME_TIER_ORDER = ("prime", "strong", "solid", "entry", "below")
VOLUME_TIER_LABELS = {
    "prime": "Prime (≥200k)", "strong": "Strong (100–200k)", "solid": "Solid (30–100k)",
    "entry": "Entry (10–30k)", "below": "Below floor (<10k)",
}


def volume_tier(sv: int | float | None, tiers: dict) -> str:
    """Map a monthly search volume to its tier name (prime/strong/solid/entry/below). `below`
    means it's under the 10k gate floor — it should never have cleared the gate, but we classify
    it rather than crash so the tier is always defined."""
    if sv is None:
        return "below"
    v = float(sv)
    if v >= tiers.get("prime", 200_000):
        return "prime"
    if v >= tiers.get("strong", 100_000):
        return "strong"
    if v >= tiers.get("solid", 30_000):
        return "solid"
    if v >= tiers.get("entry", 10_000):
        return "entry"
    return "below"

# What the Google-competitor lane actually scans — both paths feed the same lane. Surfaced
# so the research handoff (and the UI) know the Google budget splits across these two.
GOOGLE_PATHS = [
    {
        "id": "spy_roster",
        "label": "Best-Seller Spy roster",
        "what": (
            "Scan the WHOLE catalog of the competitors we already track for this keyword "
            "(full products.json crawl — not capped at the top-30 best-sellers; top-30 is "
            "only the winner-read on the Spy dashboard)."
        ),
    },
    {
        "id": "google_shopping",
        "label": "Direct Google Shopping (non-branded)",
        "what": "Live PLA + category-row results for the keyword — non-branded dropshippable products only.",
    },
]

SOURCE_LABELS = {
    "google_shopping": "GoogleShopping Search",
    "competitor_catalog": "Google Competitor Catalog",
    "marketplace": "Marketplace (Temu/Ali)",
    "amazon": "Amazon Movers & Shakers",
    "meta": "Meta ads",
}

ROLES = ("anchor", "standalone", "combine", "supporting")


# ------------------------------------------------------------------ settings resolution
def _apply_overrides(cfg: dict, overrides: dict | None) -> dict:
    """Layer clamped overrides onto an already-built cfg (mutates + returns it). Used both
    for the persisted base (DEFAULTS ← saved) and per-request nudges (base ← query params)."""
    o = overrides or {}
    if o.get("anchor_pct") is not None:
        cfg["anchor_pct"] = _clamp(o["anchor_pct"], 10, 90)
    if o.get("coverage_pct") is not None:
        cfg["coverage_pct"] = _clamp(o["coverage_pct"], 10, 100)
    if o.get("products_per_build") is not None:
        # No hard upper cap — the operator decides how many to find; only floor at 1.
        try:
            cfg["products_per_build"] = max(1, int(o["products_per_build"]))
        except (TypeError, ValueError):
            pass
    if o.get("dedup_cap") is not None:
        cfg["dedup_cap"] = int(_clamp(o["dedup_cap"], 1, 10))
    if isinstance(o.get("source_weights"), dict):
        sw = dict(o["source_weights"])
        # Migrate a legacy single "google" weight → split across the two Google methods (60/40).
        if "google" in sw and "google_shopping" not in sw and "competitor_catalog" not in sw:
            g = max(0.0, float(sw.pop("google") or 0))
            sw["google_shopping"], sw["competitor_catalog"] = round(g * 0.6, 2), round(g * 0.4, 2)
        for k in _SOURCES:
            v = sw.get(k)
            if v is not None:
                cfg["source_weights"][k] = max(0.0, float(v))
    if isinstance(o.get("role"), dict):
        rc = o["role"]
        if rc.get("standalone_sv_floor") is not None:
            cfg["role"]["standalone_sv_floor"] = int(_clamp(rc["standalone_sv_floor"], 0, 1_000_000))
        if rc.get("standalone_head_frac") is not None:
            cfg["role"]["standalone_head_frac"] = _clamp(rc["standalone_head_frac"], 0.0, 1.0)
        if rc.get("supporting_sv_ceiling") is not None:
            cfg["role"]["supporting_sv_ceiling"] = int(_clamp(rc["supporting_sv_ceiling"], 0, 1_000_000))
    if isinstance(o.get("volume"), dict):
        cfg.setdefault("volume", dict(DEFAULTS["volume"]))
        for k in ("prime", "strong", "solid", "entry"):
            v = o["volume"].get(k)
            if v is not None:
                cfg["volume"][k] = int(_clamp(v, 0, 100_000_000))
    if isinstance(o.get("products_by_tier"), dict):
        cfg.setdefault("products_by_tier", dict(DEFAULTS["products_by_tier"]))
        for k in ("prime", "strong", "solid", "entry"):
            v = o["products_by_tier"].get(k)
            if v is not None:
                cfg["products_by_tier"][k] = _clamp(v, 0.1, 5.0)
    return cfg


def _fresh_defaults() -> dict:
    return {
        **DEFAULTS,
        "source_weights": dict(DEFAULTS["source_weights"]),
        "role": dict(DEFAULTS["role"]),
        "volume": dict(DEFAULTS["volume"]),
        "products_by_tier": dict(DEFAULTS["products_by_tier"]),
    }


def saved_settings() -> dict:
    """The effective BASE = DEFAULTS with the persisted operator override layered on. This is
    what the Settings editor reads/writes; `build()` starts here before per-request nudges."""
    cfg = _fresh_defaults()
    try:
        stored = runlog.setting_get(SETTINGS_KEY)
    except Exception:
        stored = None
    return _apply_overrides(cfg, stored)


def save_settings(overrides: dict | None) -> dict:
    """Persist a clamped weight split as the new default; returns the saved (clamped) cfg."""
    cfg = _apply_overrides(_fresh_defaults(), overrides)
    runlog.setting_set(SETTINGS_KEY, cfg)
    return cfg


def reset_settings() -> dict:
    """Drop the persisted override → back to hard-coded DEFAULTS."""
    try:
        runlog.setting_delete(SETTINGS_KEY)
    except Exception:
        pass
    return _fresh_defaults()


def _merge_settings(overrides: dict | None) -> dict:
    """Effective plan settings: persisted base (DEFAULTS ← saved) with per-request overrides
    on top, all clamped so a bad value can't produce a nonsense plan."""
    return _apply_overrides(saved_settings(), overrides)


def _clamp(v, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, f))


# ------------------------------------------------------------------ source supply (adaptive)
# The five finding METHODS the operator sets a count/weight per (operator model 2026-07-09). The
# old single "google" method is split into the two distinct Google paths: direct Google-Shopping
# dropshippers vs scanning our tracked competitors' catalogs.
_SOURCES = ("google_shopping", "competitor_catalog", "marketplace", "amazon", "meta")
_SUPPLY_STATES = ("live", "dry")


def source_supply() -> dict:
    """Per-source supply state, default every source `live`. Each entry carries its state plus
    `checked` (last time we tested for new products) and `last_new` (last time new ones appeared)
    so the operator can see how long a source has been dry and when it last produced."""
    out = {k: {"state": "live", "checked": None, "last_new": None} for k in _SOURCES}
    try:
        stored = runlog.setting_get(SUPPLY_KEY) or {}
    except Exception:
        stored = {}
    for k in _SOURCES:
        s = stored.get(k)
        if isinstance(s, dict):
            st = s.get("state")
            out[k]["state"] = st if st in _SUPPLY_STATES else "live"
            out[k]["checked"] = s.get("checked")
            out[k]["last_new"] = s.get("last_new")
    return out


def set_source_supply(source: str, state: str, found_new: bool | None = None) -> dict:
    """Flag a discovery source `live` or `dry` (the operator, or — later — the yield tracker after
    a scan). `checked` is stamped now; flipping to `live` or passing found_new=True stamps
    `last_new`. Returns the full supply map. This is the lever that makes the quota adaptive."""
    if source not in _SOURCES:
        raise KeyError(source)
    if state not in _SUPPLY_STATES:
        raise ValueError(f"state must be one of {_SUPPLY_STATES}")
    supply = source_supply()
    now = runlog._now()
    supply[source]["state"] = state
    supply[source]["checked"] = now
    if state == "live" or found_new:
        supply[source]["last_new"] = now
    runlog.setting_set(SUPPLY_KEY, supply)
    return supply


def _live_sources(supply: dict) -> set[str]:
    return {k for k in _SOURCES if (supply.get(k) or {}).get("state", "live") != "dry"}


def adapt_weights(weights: dict, supply: dict) -> dict:
    """Effective source weights after supply adaptation: a `dry` source contributes 0 and its
    weight is redistributed across the still-`live` sources in proportion to their own weights —
    so the same total find-budget is spent, just sourced from what's actually producing. If every
    source is dry (nothing to redistribute to), the configured weights are returned unchanged so
    the plan still yields a budget and the all-dry condition is surfaced rather than zeroing out."""
    w = {k: max(0.0, float(weights.get(k, 0))) for k in _SOURCES}
    live = _live_sources(supply)
    dry = set(_SOURCES) - live
    if not dry or not live:
        return w
    dry_weight = sum(w[k] for k in dry)
    live_weight = sum(w[k] for k in live) or 1.0
    return {
        k: (0.0 if k in dry else w[k] + dry_weight * (w[k] / live_weight))
        for k in _SOURCES
    }


def _supply_block(configured: dict, effective: dict, supply: dict) -> dict:
    """The build() `supply` payload: per-source state, configured vs effective weight, the dry
    list, whether adaptation actually fired, and a human note explaining the redistribution +
    the re-check posture so the UI can show WHY the budget shifted."""
    live = _live_sources(supply)
    dry = [k for k in _SOURCES if k not in live]
    adapted = bool(dry) and bool(live)
    if not dry:
        note = "All sources live — find-budget split as configured."
    elif not live:
        note = "Every source is dry — keeping the configured split until any source produces again."
    else:
        moved = sorted((SOURCE_LABELS.get(k, k) for k in dry))
        onto = sorted((SOURCE_LABELS.get(k, k) for k in live))
        note = (
            f"{', '.join(moved)} dry → its find-budget is redistributed onto {', '.join(onto)} "
            f"to still hit the daily listing count. Re-checking the dry source(s); weight returns "
            f"the moment new products appear."
        )
    return {
        "sources": {
            k: {
                **(supply.get(k) or {"state": "live", "checked": None, "last_new": None}),
                "configured_weight": round(float(configured.get(k, 0)), 2),
                "effective_weight": round(float(effective.get(k, 0)), 2),
            }
            for k in _SOURCES
        },
        "dry": dry,
        "adapted": adapted,
        "note": note,
    }


# ------------------------------------------------------------------ role classification
def classify_role(seg_sv: int | None, head_sv: int | None, role_cfg: dict) -> str:
    """Heuristic role for a sub-keyword. The operator's rule: "it can be both" — so this is
    the deterministic baseline, and the documented upgrade is an LLM pass that re-decides
    combine-vs-supporting (and which SKU a combine rides into) per keyword.

      standalone  enough distinct demand to be its own SKU
      combine     a feature/spec modifier (unknown or mid demand) → rides into a title
      supporting  too little demand → feeds an existing listing's copy, no SKU
    """
    if seg_sv is None:
        return "combine"  # unknown demand → fold as a title feature until measured
    if seg_sv >= role_cfg["standalone_sv_floor"] or (
        head_sv and seg_sv >= head_sv * role_cfg["standalone_head_frac"]
    ):
        return "standalone"
    if seg_sv <= role_cfg["supporting_sv_ceiling"]:
        return "supporting"
    return "combine"


# ------------------------------------------------------------------ source quota
def source_quota(n_products: int, weights: dict, floor: int = 1) -> dict:
    """Allocate `n_products` candidate-finds across the five methods (google_shopping /
    competitor_catalog / marketplace / amazon / meta) purely by `weights`, with a per-source floor
    so every weighted source contributes when n allows. The weights handed in are already the
    supply-adapted ones, so a dry source's budget has flowed onto the live ones before we get
    here. Largest-remainder integer split guarantees the parts sum to exactly `n_products`."""
    sources = _SOURCES
    if n_products <= 0:
        return {k: 0 for k in sources}
    w = {k: max(0.0, float(weights.get(k, 0))) for k in sources}
    tw = sum(w.values()) or 1.0
    target = {k: n_products * w[k] / tw for k in sources}

    # Floors: give every weighted source at least `floor` when there's room for all of them.
    if n_products >= sum(1 for k in sources if w[k] > 0) * floor:
        for k in sources:
            if w[k] > 0:
                target[k] = max(target[k], float(floor))

    base = {k: int(target[k]) for k in sources}
    rem = n_products - sum(base.values())

    # Hand out the remainder by largest fractional part.
    order = sorted(sources, key=lambda k: target[k] - int(target[k]), reverse=True)
    i = 0
    while rem > 0 and order:
        base[order[i % len(order)]] += 1
        rem -= 1
        i += 1

    # Floors may over-allocate when n is tiny — trim from the biggest bucket.
    while rem < 0:
        k = max(sources, key=lambda s: base[s])
        if base[k] <= 0:
            break
        base[k] -= 1
        rem += 1
    return base


def _distribute_weighted(total: int, weights: list[float]) -> list[int]:
    """Split `total` across len(weights) buckets proportional to weights, summing exactly."""
    n = len(weights)
    if n == 0 or total <= 0:
        return [0] * n
    tw = sum(weights) or float(n)
    target = [total * (w / tw if tw else 1.0 / n) for w in weights]
    base = [int(t) for t in target]
    rem = total - sum(base)
    order = sorted(range(n), key=lambda i: target[i] - int(target[i]), reverse=True)
    i = 0
    while rem > 0 and order:
        base[order[i % len(order)]] += 1
        rem -= 1
        i += 1
    return base


# ------------------------------------------------------------------ per-head plan
def _plan_for_candidate(c: dict, cfg: dict, weights: dict | None = None) -> dict:
    """Build the ranked, role-classified, budget-allocated plan for ONE head keyword. `weights`
    is the EFFECTIVE (supply-adapted) source split; defaults to the configured weights when no
    supply adaptation is in play."""
    head = (c.get("keyword") or "").strip()
    head_sv = c.get("sv")
    role_cfg = cfg["role"]
    weights = weights if weights is not None else cfg["source_weights"]

    rows: list[dict] = []
    for s in (c.get("segments") or []):
        rows.append({
            "term": s.get("term"),
            "sv": s.get("sv"),
            "source": s.get("source"),
            "price_band": s.get("price_band"),
            "in_catalog": bool(s.get("in_catalog")),
            "role": classify_role(s.get("sv"), head_sv, role_cfg),
        })

    # Tier 2 = standalone sub-keywords not already built, ranked by captured demand.
    standalones = [r for r in rows if r["role"] == "standalone" and not r["in_catalog"]]
    standalones.sort(key=lambda r: (r.get("sv") is None, -(r.get("sv") or 0)))
    tier2_demand = sum((r.get("sv") or 0) for r in standalones)
    coverage_target = tier2_demand * cfg["coverage_pct"] / 100.0

    build_terms: list[str] = []
    captured = 0
    for r in standalones:
        if captured >= coverage_target and build_terms:
            break
        build_terms.append(r["term"])
        captured += (r.get("sv") or 0)
    build_set = {t.lower() for t in build_terms}

    # Mark selection on every row.
    for r in rows:
        if r["in_catalog"]:
            r["selected"] = "built"
        elif r["role"] == "standalone" and r["term"].lower() in build_set:
            r["selected"] = "build"
        else:
            r["selected"] = "hold"

    combine_children = [r["term"] for r in rows if r["role"] == "combine" and not r["in_catalog"]]
    supporting = [r["term"] for r in rows if r["role"] == "supporting" and not r["in_catalog"]]

    # ---- Product-research budget: anchor_pct on the head, the rest across Tier-2 builds.
    # The 10k floor is the GATE, not the focus — search volume is the winner-probability lever, so
    # the head keyword's volume TIER scales how many products we build for it. A 200k+ "prime"
    # keyword earns the most listings (take market share, high winner odds); a floor-band "entry"
    # keyword the fewest. Mid keywords still build, just with fewer variants.
    head_tier = volume_tier(head_sv, cfg.get("volume") or DEFAULTS["volume"])
    tier_mult = (cfg.get("products_by_tier") or DEFAULTS["products_by_tier"]).get(head_tier, 1.0)
    base_ppb = cfg["products_per_build"]
    ppb = max(1, round(base_ppb * tier_mult))
    n_tier2 = len(build_terms)
    total_budget = ppb * (1 + n_tier2)
    head_budget = max(1, round(total_budget * cfg["anchor_pct"] / 100.0))
    tier2_total = max(0, total_budget - head_budget)
    tier2_weights = [
        (next((r["sv"] for r in standalones if r["term"] == t), None) or 1) for t in build_terms
    ]
    tier2_budgets = _distribute_weighted(tier2_total, [float(w) for w in tier2_weights])

    # The anchor row (the head keyword itself — always built; combine children ride its title).
    anchor = {
        "term": head,
        "sv": head_sv,
        "role": "anchor",
        "selected": "build",
        "budget": head_budget,
        "quota": source_quota(head_budget, weights),
        "combine_children": combine_children,
    }

    # Attach a find budget + source quota to each Tier-2 build row.
    for r in rows:
        if r["selected"] == "build":
            idx = build_terms.index(r["term"]) if r["term"] in build_terms else None
            b = tier2_budgets[idx] if idx is not None and idx < len(tier2_budgets) else 0
            r["budget"] = b
            r["quota"] = source_quota(b, weights)
        else:
            r["budget"] = 0
            r["quota"] = None

    research_budget = head_budget + sum(tier2_budgets)
    anchor_share = round(head_budget / research_budget * 100) if research_budget else 0

    return {
        "store": c.get("store"),
        "keyword": head,
        "sv": head_sv,
        "gate": c.get("gate"),
        "capture_bucket": c.get("capture_bucket"),
        "volume_tier": head_tier,
        "volume_tier_label": VOLUME_TIER_LABELS.get(head_tier, head_tier),
        "products_per_build": ppb,           # tier-scaled budget for THIS head (base × tier mult)
        "products_per_build_base": base_ppb,
        "anchor": anchor,
        "segments": rows,
        "counts": {
            "build": 1 + n_tier2,            # the anchor SKU + selected Tier-2 standalones
            "tier2_build": n_tier2,
            "combine": len(combine_children),
            "supporting": len(supporting),
            "built": sum(1 for r in rows if r["in_catalog"]),
            "hold": sum(1 for r in rows if r["selected"] == "hold"),
        },
        "demand": {
            "tier2_total": tier2_demand,
            "tier2_captured": captured,
            "coverage_pct_actual": round(captured / tier2_demand * 100) if tier2_demand else 0,
        },
        "research_budget": research_budget,
        "anchor_share_pct": anchor_share,
        "build_terms": [head] + build_terms,  # everything the research handoff should fire on
    }


def _vision_status() -> dict:
    """Live status of the Gemini photo-duplicate check for the plan banner. Lazy import keeps
    this module stdlib-light when photo_dedup's deps aren't available (e.g. unit contexts)."""
    try:
        from . import photo_dedup
        vs = photo_dedup.vision_status()
        return {"vision_status": vs["status"], "vision_hint": vs["hint"]}
    except Exception:
        return {"vision_status": "pending", "vision_hint": None}


# ------------------------------------------------------------------ public: whole plan
def build(overrides: dict | None = None) -> dict:
    """The store-wide SKU plan. Reads the keyword-discovery backlog, plans every head keyword
    that fanned out into ≥1 sub-keyword, and returns the enriched plan + the settings used."""
    cfg = _merge_settings(overrides)
    supply = source_supply()
    eff_weights = adapt_weights(cfg["source_weights"], supply)
    disc = readers.keyword_discovery()
    disc_cands = disc.get("candidates") or []
    by_key = {(c.get("store"), (c.get("keyword") or "").strip().lower()): c for c in disc_cands}

    # A PROMOTED keyword (committed to build → in the store's listing queue) is a head even
    # before it fans out into sub-keywords: the operator promoted it, so it belongs in the plan
    # with at least its anchor product, ready to set a count and start finding. Research fills
    # in the sub-products later. This is what makes "Promote → SKU Plan" actually land here.
    candidates = [c for c in disc_cands if (c.get("n_segments") or 0) > 0]
    seen = {(c.get("store"), (c.get("keyword") or "").strip().lower()) for c in candidates}
    for store in readers.list_stores():
        for slug, cat in ((readers.listing_queue(store) or {}).get("categories") or {}).items():
            kw = (cat.get("keyword") or slug or "").strip()
            key = (store, kw.strip().lower())
            # Even a PROMOTED head must clear the SAME central listable gate as every other surface —
            # a brand/non-product/bad-title ('bed jet', 'mattress', 'jet') must not resurface as a head.
            if not kw or key in seen or not readers.is_listable_keyword(kw):
                continue
            existing = by_key.get(key)
            if existing:
                candidates.append(existing)  # promoted candidate that has no segments yet
            else:
                # Promoted, but the backlog candidate is gone — synthesize a minimal head.
                candidates.append({
                    "store": store, "keyword": kw, "sv": cat.get("sv"),
                    "gate": "PASS", "capture_bucket": cat.get("capture"),
                    "segments": [], "n_segments": 0,
                })
            seen.add(key)

    heads = [_plan_for_candidate(c, cfg, eff_weights) for c in candidates]

    tier_counts = {t: sum(1 for h in heads if h.get("volume_tier") == t) for t in VOLUME_TIER_ORDER}
    totals = {
        "heads": len(heads),
        "build_skus": sum(h["counts"]["build"] for h in heads),
        "tier2_builds": sum(h["counts"]["tier2_build"] for h in heads),
        "combine": sum(h["counts"]["combine"] for h in heads),
        "supporting": sum(h["counts"]["supporting"] for h in heads),
        "built": sum(h["counts"]["built"] for h in heads),
        "research_budget": sum(h["research_budget"] for h in heads),
        "by_volume_tier": tier_counts,
    }

    return {
        "settings": {
            "anchor_pct": cfg["anchor_pct"],
            "coverage_pct": cfg["coverage_pct"],
            "products_per_build": cfg["products_per_build"],
            "dedup_cap": cfg["dedup_cap"],
            "source_weights": cfg["source_weights"],
            "role": cfg["role"],
            "volume": cfg.get("volume") or dict(DEFAULTS["volume"]),
            "products_by_tier": cfg.get("products_by_tier") or dict(DEFAULTS["products_by_tier"]),
        },
        "volume_tiers": {
            "order": list(VOLUME_TIER_ORDER),
            "labels": VOLUME_TIER_LABELS,
            "note": (
                "The 10k floor is the gate, not the focus. Search volume is the winner-probability "
                "lever, so the plan biases toward the biggest keywords: a keyword's volume tier "
                "scales how many products get built for it (prime ≥200k lists the most)."
            ),
        },
        "source_labels": SOURCE_LABELS,
        "google_paths": GOOGLE_PATHS,
        "supply": _supply_block(cfg["source_weights"], eff_weights, supply),
        "stores": disc.get("stores") or [],
        "segment_sources": disc.get("segment_sources") or [],
        "dedup": {
            "cap": cfg["dedup_cap"],
            "rule": (
                f"Skip anything already in your store's catalog or already found; cap the SAME "
                f"product at {cfg['dedup_cap']} DIFFERENT-STYLE listings (A/B), never ~10×. The "
                f"product-duplicate check matches by TITLE + PHOTO and scans your catalog, so the "
                f"same product never gets built twice."
            ),
            **_vision_status(),
        },
        "totals": totals,
        "heads": heads,
    }


def research_targets(store: str, keyword: str, overrides: dict | None = None) -> list[str]:
    """The build terms (head + selected Tier-2) for one head keyword — what the research
    handoff should fire discovery jobs on. Empty if the head isn't in the plan."""
    plan = build(overrides)
    for h in plan["heads"]:
        if h["store"] == store and (h.get("keyword") or "").strip().lower() == keyword.strip().lower():
            return h["build_terms"]
    return []
