"""Operation-cost model — the pipeline's running expenses, estimated honestly.

Nothing in this project is metered server-side yet (the heavy steps run as *manual*
jobs in the operator's own Claude Code, which holds the keys/MCP). So this is an
**estimate** built from two editable layers:

  1. UNIT COSTS  — the per-call price of each external driver (DataForSEO task, a
                   Bright Data unlock, one generated image, LLM tokens). Defaults are
                   grounded in June-2026 public pricing (see `source` on each entry)
                   and are operator-editable (overrides persist to data/cost_assumptions.json).
  2. SPEC RECIPES — for each pipeline job spec, roughly how many of each driver one run
                   consumes. Multiplying a recipe by the unit costs gives a per-run estimate;
                   multiplying by how many times that spec has actually run (from the jobs
                   table) gives estimated spend to date.

Fixed monthly infra (the always-on worker that would run the pipeline 24/7, plus the
Postgres it writes to) is listed separately — it is a flat burn that does not scale per
listing. Everything is labelled "estimate" in the UI; this is a planning instrument, not
an invoice.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config, runlog

_OVERRIDES_PATH = config.data_root() / "operator-app" / "api" / "data" / "cost_assumptions.json"


# ------------------------------------------------------------------ unit costs
# value is USD per `unit`. Grounded in public June-2026 pricing; editable by the operator.
DEFAULT_UNIT_COSTS: dict[str, dict] = {
    "dfs_task": {
        "value": 0.0015, "unit": "task", "label": "DataForSEO task",
        "source": "DFS pay-as-you-go ~$0.001–0.002/task (project .env, canonical provider)",
    },
    "brightdata_unlock": {
        "value": 0.004, "unit": "request", "label": "Bright Data Web Unlocker (success)",
        "source": "BD Web Unlocker ~$3–5 / 1K successful requests (Jun 2026)",
    },
    "brightdata_serp": {
        "value": 0.0015, "unit": "request", "label": "Bright Data SERP query",
        "source": "BD SERP API ~$1.50 / 1K Google searches (Jun 2026)",
    },
    "image_gen": {
        "value": 0.039, "unit": "image", "label": "Image gen (nano_banana / Gemini Flash Image)",
        "source": "Gemini 2.5 Flash Image (nano_banana) $0.039 / 1024² image (Jun 2026)",
    },
    "vision_scan": {
        "value": 0.0005, "unit": "image", "label": "Vision scan (Gemini Flash — read an image)",
        "source": "Gemini 2.5 Flash vision ~$0.30/Mtok in; ~1 image + prompt + verdict ≈ $0.0005/scan "
                  "(Jun 2026). Drives catalog-duplicate dedup + 1688/China gallery match.",
    },
    "llm_input_per_mtok": {
        "value": 1.00, "unit": "Mtok", "label": "Agent LLM — input tokens",
        "source": "Driven by the selected agent model (default Claude Haiku 4.5).",
    },
    "llm_output_per_mtok": {
        "value": 5.00, "unit": "Mtok", "label": "Agent LLM — output tokens",
        "source": "Driven by the selected agent model (default Claude Haiku 4.5).",
    },
}

# Selectable orchestrator/agent models for the Agent-SDK driving the pipeline. This is a
# repetitive workflow with clear, codified steps — it does NOT need a flagship model, so the
# DEFAULT is a cheap-but-capable tier. The operator can switch; the choice drives the two
# llm_* unit costs above. Prices = USD / 1M tokens (input, output), grounded Jun-2026.
LLM_MODELS: dict[str, dict] = {
    "claude-haiku-4-5": {"label": "Claude Haiku 4.5", "input": 1.00, "output": 5.00,
                         "source": "Anthropic Haiku 4.5 $1 / $5 per MTok (Jun 2026)"},
    "gpt-5-mini": {"label": "GPT-5 mini", "input": 0.13, "output": 1.00,
                   "source": "OpenAI GPT-5 mini $0.13 / $1.00 per MTok (Jun 2026)"},
    "claude-sonnet-4-6": {"label": "Claude Sonnet 4.6", "input": 3.00, "output": 15.00,
                          "source": "Anthropic Sonnet 4.6 $3 / $15 per MTok (Jun 2026)"},
    "gpt-5-4": {"label": "GPT-5.4", "input": 2.50, "output": 15.00,
                "source": "OpenAI GPT-5.4 $2.50 / $15 per MTok (Jun 2026)"},
    "gpt-5-5": {"label": "GPT-5.5", "input": 5.00, "output": 30.00,
                "source": "OpenAI GPT-5.5 $5 / $30 per MTok (Jun 2026)"},
    "claude-opus-4-8": {"label": "Claude Opus 4.8", "input": 5.00, "output": 25.00,
                        "source": "Anthropic Opus 4.8 $5 / $25 per MTok (Jun 2026)"},
}
DEFAULT_AGENT_MODEL = "claude-haiku-4-5"

# Fixed monthly infrastructure to run the pipeline 24/7. These do NOT scale per listing.
# Each option is tagged by `role`:
#   - "worker_api" = the always-on background process (runs the daily plan) + the FastAPI backend.
#                    This is the load-bearing host: it needs a PERSISTENT process + RAM, which is
#                    why a serverless frontend host (Vercel) structurally cannot do this job.
#   - "frontend"   = serves the Next.js operator UI only (stateless, request/response).
# `selected` marks the default recommendation PER ROLE; the operator can switch within a role.
FIXED_MONTHLY_OPTIONS: list[dict] = [
    # ---- worker + API hosts (always-on process; ordered cheapest→most managed) ----
    {"id": "railway_hobby", "label": "Railway Hobby — worker + API", "value": 5.00,
     "role": "worker_api", "selected": True, "resource": "~512MB RAM · shared vCPU",
     "note": "RECOMMENDED. $5/mo credit covers a small always-on container holding the background "
             "worker AND the FastAPI backend. Overage $10/GB-RAM-mo + $20/vCPU-mo. Cheapest for "
             "low/variable load, simplest deploy."},
    {"id": "fly_machine", "label": "Fly.io small Machine — worker + API", "value": 5.00,
     "role": "worker_api", "selected": False, "resource": "256–512MB RAM · shared-cpu-1x",
     "note": "~$5/mo, per-second billing, can scale-to-zero between runs. Slightly more setup than "
             "Railway; great if you want the worker idle-cheap and only billed while it works."},
    {"id": "koyeb_starter", "label": "Koyeb — worker + API", "value": 5.00,
     "role": "worker_api", "selected": False, "resource": "512MB RAM · shared (eco) instance",
     "note": "Eco/nano always-on instance ~$5/mo; has a small free tier to prototype. Global edge, "
             "auto-deploy from git. Comparable to Railway/Fly for a single small service."},
    {"id": "hetzner_vps", "label": "Hetzner Cloud CX22 (VPS, self-managed)", "value": 5.00,
     "role": "worker_api", "selected": False, "resource": "2 vCPU · 4GB RAM · 40GB SSD",
     "note": "Cheapest raw compute by far (~€4.5–5/mo) and far more RAM/CPU than the PaaS tiers — "
             "but you manage the box yourself (Docker, deploy, updates, no built-in cron UI). Best "
             "$/resource if you're comfortable running a Linux server."},
    {"id": "render_pro", "label": "Render — worker + cron", "value": 25.00,
     "role": "worker_api", "selected": False, "resource": "512MB RAM Starter instance",
     "note": "$25/mo flat; first-class background workers & cron, most predictable for steady "
             "always-on. Pricier but the most 'set-and-forget' managed option."},
    {"id": "northflank_dev", "label": "Northflank — worker + API", "value": 20.00,
     "role": "worker_api", "selected": False, "resource": "small combined service + DB",
     "note": "Usage-based; a small always-on service lands ~$15–25/mo. More platform (built-in "
             "Postgres, pipelines) than needed for v1, but scales cleanly if the project grows."},
    # ---- frontend hosts (stateless UI only; CANNOT run the always-on worker) ----
    {"id": "vercel_hobby", "label": "Vercel Hobby — Next.js frontend", "value": 0.00,
     "role": "frontend", "selected": True, "resource": "serverless / edge (no persistent process)",
     "note": "FREE for the operator UI (hobby/personal use). Hosts the Next.js app beautifully, "
             "BUT being serverless it canNOT hold the always-on background worker — that stays on "
             "the worker+API host above. Pair Vercel (frontend) + Railway (worker+API) + Neon (DB)."},
    {"id": "vercel_pro", "label": "Vercel Pro — Next.js frontend", "value": 20.00,
     "role": "frontend", "selected": False, "resource": "serverless / edge (team features)",
     "note": "$20/mo/seat — only needed for team seats, more bandwidth, or commercial use. For a "
             "single-operator tool the free Hobby tier is enough."},
]

# Postgres + other always-present services (flat, mostly free-tier for v1 scale).
FIXED_SERVICES: list[dict] = [
    {"id": "neon_postgres", "label": "Neon Postgres (run-log / state)", "value": 0.00,
     "note": "Free tier comfortably covers v1 run-log + job/gameplan tables."},
    {"id": "trendtrack", "label": "TrendTrack (spy / discovery MCP)", "value": 0.00,
     "note": "Operator subscription; marginal cost per discovery call ≈ $0."},
]

# Stored-data model. At scale the operator keeps run-logs/job rows/snapshots in Postgres and
# (optionally) generated image masters in object storage. Both grow with cumulative listings,
# but priced at grounded Jun-2026 rates they stay tiny next to per-listing variable spend.
STORAGE = {
    "db_kb_per_listing": 6.0,        # retained rows/listing: run-log + job + snapshot + gameplan (~KB)
    "neon_free_gb": 0.5,             # Neon free tier storage
    "neon_per_gb_mo": 0.35,          # Neon paid storage $0.35/GB-mo (Jun 2026)
    "image_master_mb_per_listing": 0.0,  # default 0 = DON'T retain (Shopify hosts live product imgs);
                                         # set >0 to cost keeping masters in object storage
    "object_free_gb": 10.0,          # Cloudflare R2 free tier
    "object_per_gb_mo": 0.015,       # R2 storage $0.015/GB-mo, zero egress (Jun 2026)
}


# Per-run consumption recipe for each job spec: {driver: quantity-in-its-unit}.
# These are deliberate ESTIMATES of a typical run; tune via the same overrides file
# (key "recipes") if real metering later disagrees. Auto/stdlib specs consume nothing external.
SPEC_RECIPES: dict[str, dict[str, float]] = {
    "candidate-score": {},                      # stdlib, on-disk
    "listing-snapshot": {},                     # stdlib, on-disk
    "keyword-discovery": {"dfs_task": 25, "llm_input_per_mtok": 0.02, "llm_output_per_mtok": 0.006},
    "shopping-scan": {"dfs_task": 6},           # AdsPower browser is fixed/operator infra
    "discover-general-stores": {"llm_input_per_mtok": 0.03, "llm_output_per_mtok": 0.008},
    "bestseller-spy": {"brightdata_unlock": 30},
    "discover-niches": {"dfs_task": 40, "llm_input_per_mtok": 0.03, "llm_output_per_mtok": 0.01},
    "discover-niches-pain-first": {"brightdata_unlock": 40, "llm_input_per_mtok": 0.04, "llm_output_per_mtok": 0.012},
    "trend-radar": {"dfs_task": 10, "llm_input_per_mtok": 0.01, "llm_output_per_mtok": 0.004},
    "build-listing": {"image_gen": 7, "llm_input_per_mtok": 0.025, "llm_output_per_mtok": 0.01},
    "source-import": {"brightdata_unlock": 2, "image_gen": 7, "llm_input_per_mtok": 0.025, "llm_output_per_mtok": 0.01},
    "general-import-url": {"brightdata_unlock": 3, "image_gen": 7, "llm_input_per_mtok": 0.03, "llm_output_per_mtok": 0.012},
    # Gemini-vision catalog DEDUP: read every live hero to flag a background-invariant duplicate.
    "catalog-scan": {"vision_scan": 30, "llm_input_per_mtok": 0.02, "llm_output_per_mtok": 0.006},
    # 1688 / China sourcing MATCH: vision-judge candidate galleries vs the supplier ref (match_china.py).
    "china-match": {"vision_scan": 30, "llm_input_per_mtok": 0.02, "llm_output_per_mtok": 0.008},
    # FREE local image-QA fixes — Pillow/cv2 only, no paid AI. Listed at $0 so the cost view shows
    # them as the cheap repair path (vs the paid regen).
    "image-autoclean": {},                      # remove a removable overlay (diffusion-inpaint)
    "image-upscale": {},                        # lift a clean-but-small image to 1024² (LANCZOS)
}

# Specs the cost view costs that are NOT runnable job specs in jobs.py (manual scripts the
# operator drives, e.g. the 1688 vision match) — give them a label so per_spec can show them.
_LOCAL_SPEC_META: dict[str, dict] = {
    "china-match": {"title": "1688 / China sourcing match (vision)", "mode": "manual", "surface": "sourcing"},
}

# Group every cost line into the categories the operator thinks in: operating (fixed infra),
# listing (per build method), research, trend research, vision scan (catalog dedup + 1688 match).
SPEC_CATEGORY: dict[str, str] = {
    "candidate-score": "maintenance",
    "listing-snapshot": "maintenance",
    "keyword-discovery": "research",
    "shopping-scan": "research",
    "discover-general-stores": "research",
    "bestseller-spy": "research",
    "discover-niches": "research",
    "discover-niches-pain-first": "research",
    "trend-radar": "trend",
    "build-listing": "listing",
    "source-import": "listing",
    "general-import-url": "listing",
    "catalog-scan": "vision",
    "china-match": "vision",
    "image-autoclean": "maintenance",
    "image-upscale": "maintenance",
}
CATEGORY_LABELS: dict[str, str] = {
    "listing": "Listing · per build method",
    "research": "Research · keyword / product / niche",
    "trend": "Trend research",
    "vision": "Vision scan · catalog dedup + 1688 match",
    "maintenance": "Maintenance · stdlib (free)",
}
CATEGORY_ORDER = ["listing", "research", "trend", "vision", "maintenance"]

# A full "list one product end to end" = research the keyword once, then build the listing.
# (Discovery is amortised across many listings, so per-listing leans on the build step.)
PER_LISTING_SPECS = ["build-listing"]

# A listing's cost depends on HOW it's built — image gen is NOT every listing. A plain
# supplier-photo import reuses the supplier's own photos (LLM copy only); a branded build
# generates a full AI gallery. Show both so the single per-listing number isn't misleading.
_LLM_COPY = {"llm_input_per_mtok": 0.025, "llm_output_per_mtok": 0.01}
PER_LISTING_TYPES: list[dict] = [
    {"id": "plain", "label": "Plain / supplier-photo listing", "images": 0,
     "recipe": dict(_LLM_COPY),
     "note": "Reuses the supplier's own photos — no AI image gen. Keyword title + template copy only. "
             "This is the cheap path (LLM copy ≈ a cent)."},
    {"id": "branded", "label": "Branded listing (AI gallery)", "images": 7,
     "recipe": {"image_gen": 7, **_LLM_COPY},
     "note": "Generates a full V10 gallery (1 hero + 6 tiles), supplier-grounded. Image gen is ~95% "
             "of the cost — the LLM copy is the same cheap cent."},
]


# ------------------------------------------------------------------ overrides
def _load_overrides() -> dict:
    try:
        return json.loads(_OVERRIDES_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_overrides(data: dict) -> None:
    _OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OVERRIDES_PATH.write_text(json.dumps(data, indent=2))


def agent_model() -> str:
    """The selected orchestrator/agent model id (defaults to the cheap-but-capable tier)."""
    m = _load_overrides().get("agent_model")
    return m if m in LLM_MODELS else DEFAULT_AGENT_MODEL


def unit_costs() -> dict[str, dict]:
    """Default unit costs, with the two llm_* costs driven by the selected agent model,
    then any explicit per-key operator override applied on top."""
    ov = _load_overrides().get("unit_costs", {})
    model_id = agent_model()
    model = LLM_MODELS[model_id]
    out: dict[str, dict] = {}
    for key, base in DEFAULT_UNIT_COSTS.items():
        merged = dict(base)
        # The agent model sets the LLM token prices unless the operator overrode them directly.
        if key == "llm_input_per_mtok":
            merged["value"] = model["input"]
            merged["label"] = f"Agent LLM input — {model['label']}"
            merged["source"] = model["source"]
        elif key == "llm_output_per_mtok":
            merged["value"] = model["output"]
            merged["label"] = f"Agent LLM output — {model['label']}"
            merged["source"] = model["source"]
        if key in ov:
            try:
                merged["value"] = float(ov[key])
                merged["edited"] = True
            except (TypeError, ValueError):
                pass
        out[key] = merged
    return out


def _default_value(key: str) -> float:
    """The default for a unit cost — for llm_* keys this is the *selected model's* price."""
    if key == "llm_input_per_mtok":
        return LLM_MODELS[agent_model()]["input"]
    if key == "llm_output_per_mtok":
        return LLM_MODELS[agent_model()]["output"]
    return DEFAULT_UNIT_COSTS[key]["value"]


def save_overrides(values: dict | None, model_id: str | None) -> dict:
    """Persist operator overrides: per-key unit-cost `value`s and/or the agent model.

    A unit-cost value equal to its current default is dropped so the file stays minimal.
    Switching the agent model is what cheaply re-prices the whole pipeline's LLM line."""
    cur = _load_overrides()
    if model_id is not None:
        if model_id in LLM_MODELS:
            cur["agent_model"] = model_id
        elif model_id == DEFAULT_AGENT_MODEL:
            cur.pop("agent_model", None)
    if model_id is not None and "unit_costs" not in cur:
        cur["unit_costs"] = cur.get("unit_costs", {})
    uc = dict(cur.get("unit_costs", {}))
    for key, raw in (values or {}).items():
        if key not in DEFAULT_UNIT_COSTS:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if abs(v - _default_value(key)) < 1e-12:
            uc.pop(key, None)
        else:
            uc[key] = v
    cur["unit_costs"] = uc
    _save_overrides(cur)
    return unit_costs()


# ------------------------------------------------------------------ compute
def _recipe_cost(recipe: dict[str, float], uc: dict[str, dict]) -> tuple[float, list[dict]]:
    """Cost one recipe against the current unit costs, returning total + line breakdown."""
    total = 0.0
    breakdown: list[dict] = []
    for driver, qty in recipe.items():
        unit = uc.get(driver)
        if not unit:
            continue
        line = round(unit["value"] * qty, 4)
        total += line
        breakdown.append({
            "driver": driver, "label": unit["label"], "qty": qty,
            "unit": unit["unit"], "unit_cost": unit["value"], "cost": line,
        })
    return round(total, 4), breakdown


def per_spec(uc: dict[str, dict] | None = None) -> list[dict]:
    """Estimated cost of ONE run of each job spec, with its driver breakdown."""
    uc = uc or unit_costs()
    from . import jobs  # lazy — jobs imports nothing from costs
    titles = {s["id"]: s for s in jobs.specs()}
    out: list[dict] = []
    for spec_id, recipe in SPEC_RECIPES.items():
        cost, breakdown = _recipe_cost(recipe, uc)
        # Prefer the live jobs.specs() metadata; fall back to _LOCAL_SPEC_META for
        # cost-only specs (manual scripts that aren't runnable job specs, e.g. china-match).
        meta = titles.get(spec_id) or _LOCAL_SPEC_META.get(spec_id, {})
        out.append({
            "spec": spec_id,
            "title": meta.get("title", spec_id),
            "mode": meta.get("mode", "?"),
            "surface": meta.get("surface", "?"),
            "category": SPEC_CATEGORY.get(spec_id, "maintenance"),
            "est_cost": cost,
            "breakdown": breakdown,
        })
    out.sort(key=lambda r: r["est_cost"], reverse=True)
    return out


def cost_groups(specs: list[dict]) -> list[dict]:
    """Bucket the per-spec rows into the operator's mental categories (listing / research /
    trend / vision / maintenance), each with a subtotal of one-run costs. Operating (fixed
    infra) is a separate flat layer and is NOT a per-spec category."""
    by_cat: dict[str, list[dict]] = {}
    for s in specs:
        by_cat.setdefault(s.get("category", "maintenance"), []).append(s)
    groups: list[dict] = []
    for cat in CATEGORY_ORDER:
        rows = by_cat.get(cat, [])
        if not rows:
            continue
        groups.append({
            "category": cat,
            "label": CATEGORY_LABELS.get(cat, cat),
            "specs": rows,
            "subtotal": round(sum(r["est_cost"] for r in rows), 4),
        })
    return groups


def _storage(total_listings_month: int, months_retained: int) -> dict:
    """Monthly cost of holding `months_retained` months of accumulated data at the current
    listing rate. DB rows (always) + optional retained image masters. Tiny at grounded rates."""
    cumulative = max(0, total_listings_month) * max(1, months_retained)
    db_gb = cumulative * STORAGE["db_kb_per_listing"] / 1_000_000.0
    img_gb = cumulative * STORAGE["image_master_mb_per_listing"] / 1024.0
    db_cost = round(max(0.0, db_gb - STORAGE["neon_free_gb"]) * STORAGE["neon_per_gb_mo"], 2)
    img_cost = round(max(0.0, img_gb - STORAGE["object_free_gb"]) * STORAGE["object_per_gb_mo"], 2)
    return {
        "months_retained": months_retained,
        "cumulative_listings": cumulative,
        "db_gb": round(db_gb, 3),
        "db_cost": db_cost,
        "image_gb": round(img_gb, 2),
        "image_cost": img_cost,
        "total": round(db_cost + img_cost, 2),
        "note": ("DB run-log/job/snapshot rows (Neon $0.35/GB-mo, 0.5GB free) + optional image "
                 "masters (R2 $0.015/GB-mo zero-egress, 10GB free; default OFF — Shopify hosts "
                 "live product images). Even years of data here stays a rounding error vs the "
                 "per-listing variable spend."),
    }


def scenarios(per_listing: float, fixed_total: float) -> list[dict]:
    """A small scale curve so the operator sees how cost behaves as the operation grows.
    The headline: past hobby scale, per-listing VARIABLE spend dominates — fixed infra +
    storage become a rounding error, so the real lever is the per-listing cost (cheap image
    model + cheap LLM), not the host."""
    presets = [
        ("Hobby", 1, 30),                 # 1 store, ~1 listing/day
        ("1 store · 30/day", 1, 900),     # steady single store
        ("5 stores · 30/day", 5, 900),    # 4,500 listings/mo
        ("5 stores · 150/day", 5, 4500),  # 22,500 listings/mo — heavy
    ]
    out: list[dict] = []
    for label, n_stores, per_store in presets:
        total = n_stores * per_store
        variable = round(per_listing * total, 2)
        storage = _storage(total, 12)["total"]
        monthly = round(variable + fixed_total + storage, 2)
        out.append({
            "label": label, "stores": n_stores, "per_store": per_store,
            "total_listings": total, "variable": variable, "storage": storage,
            "fixed": fixed_total, "monthly": monthly,
            "variable_share": round(variable / monthly * 100, 1) if monthly else 0.0,
        })
    return out


def overview(store: str | None = None, listings_per_month: int = 30,
             stores: int = 1, months_retained: int = 12) -> dict:
    """Full cost picture: unit costs, fixed infra, per-spec + per-listing estimates, spend to
    date (from real job counts), and a SCALE-AWARE monthly projection (stores × listings, a
    data-storage line, and a scenario curve from hobby to 5-store production)."""
    uc = unit_costs()
    specs = per_spec(uc)
    spec_cost = {s["spec"]: s["est_cost"] for s in specs}
    groups = cost_groups(specs)

    # per-listing by build TYPE: plain (supplier photos, no image gen) vs branded (AI gallery).
    # Image gen is NOT every listing — show both so the single number isn't misleading.
    listing_types: list[dict] = []
    for t in PER_LISTING_TYPES:
        cost, breakdown = _recipe_cost(t["recipe"], uc)
        listing_types.append({
            "id": t["id"], "label": t["label"], "images": t["images"],
            "est_cost": cost, "breakdown": breakdown, "note": t["note"],
        })
    branded = next((t for t in listing_types if t["id"] == "branded"), None)
    plain = next((t for t in listing_types if t["id"] == "plain"), None)

    # Headline per-listing = the BUILD spec (branded gallery) — kept for the projection /
    # scenarios which model the heavier branded path; plain is shown alongside as the cheap path.
    per_listing = round(sum(spec_cost.get(s, 0.0) for s in PER_LISTING_SPECS), 4)

    # fixed monthly infra: the selected host PER ROLE (worker+API, frontend) + flat services
    def _selected(role: str) -> dict:
        opts = [o for o in FIXED_MONTHLY_OPTIONS if o.get("role") == role]
        return next((o for o in opts if o.get("selected")), opts[0])

    worker = _selected("worker_api")
    frontend = _selected("frontend")
    fixed_total = round(
        worker["value"] + frontend["value"] + sum(s["value"] for s in FIXED_SERVICES), 2
    )

    # estimated spend to date = real job-spec counts × per-run cost
    counts = runlog.job_spec_counts(store)
    by_spec = []
    spend_to_date = 0.0
    for spec_id, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        unit = spec_cost.get(spec_id, 0.0)
        line = round(unit * n, 4)
        spend_to_date += line
        by_spec.append({"spec": spec_id, "runs": n, "unit_cost": unit, "cost": line})

    # scale-aware monthly projection: total = stores × per-store listings
    stores = max(1, stores)
    total_listings = max(0, listings_per_month) * stores
    variable_monthly = round(per_listing * total_listings, 2)
    storage = _storage(total_listings, months_retained)
    storage_monthly = storage["total"]
    projected_monthly = round(variable_monthly + fixed_total + storage_monthly, 2)
    variable_share = round(variable_monthly / projected_monthly * 100, 1) if projected_monthly else 0.0

    model_id = agent_model()
    return {
        "store": store,
        "unit_costs": uc,
        "agent": {
            "selected": model_id,
            "models": [
                {"id": mid, "label": m["label"], "input": m["input"],
                 "output": m["output"], "source": m["source"]}
                for mid, m in LLM_MODELS.items()
            ],
            "note": "Repetitive, clearly-stepped workflow — a cheap tier (default Haiku 4.5 / "
                    "GPT-5 mini) is enough; no flagship model needed. The choice re-prices "
                    "every step's LLM line.",
        },
        "fixed_monthly": {
            "options": FIXED_MONTHLY_OPTIONS,
            "worker_options": [o for o in FIXED_MONTHLY_OPTIONS if o.get("role") == "worker_api"],
            "frontend_options": [o for o in FIXED_MONTHLY_OPTIONS if o.get("role") == "frontend"],
            "services": FIXED_SERVICES,
            "selected": worker["id"],
            "selected_worker": worker["id"],
            "selected_frontend": frontend["id"],
            "total": fixed_total,
            "note": "Recommended split: Vercel (frontend, free) + Railway (worker+API, ~$5/mo) + "
                    "Neon (Postgres, free) ≈ $5/mo all-in. The worker+API host is load-bearing "
                    "(needs a persistent process); the frontend host cannot run it.",
        },
        "per_spec": specs,
        "cost_groups": groups,
        "per_listing": {
            "specs": PER_LISTING_SPECS,
            "est_cost": per_listing,
            "types": listing_types,
            "plain": plain,
            "branded": branded,
            "note": "A listing's cost depends on HOW it's built. Plain (supplier photos) is "
                    "basically just the LLM copy (~a cent); branded (AI gallery) is ~95% image "
                    "gen. Image gen is NOT every listing.",
        },
        "spend_to_date": {
            "by_spec": by_spec,
            "total": round(spend_to_date, 2),
            "total_jobs": sum(counts.values()),
        },
        "projection": {
            "listings_per_month": listings_per_month,
            "stores": stores,
            "total_listings": total_listings,
            "per_listing": per_listing,
            "variable_monthly": variable_monthly,
            "storage_monthly": storage_monthly,
            "fixed_monthly": fixed_total,
            "projected_monthly": projected_monthly,
            "variable_share": variable_share,
        },
        "storage": storage,
        "scenarios": scenarios(per_listing, fixed_total),
        "note": "All figures are ESTIMATES from editable unit-cost assumptions × typical "
                "per-run recipes — nothing is metered server-side yet (heavy steps run as "
                "manual jobs in the operator's own Claude Code). Tune the unit costs to refine.",
    }
